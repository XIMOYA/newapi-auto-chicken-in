"""newapi_checkin/remote_sync.py
远程配置 API 获取、AES-GCM 解密和本地保存（保留本地加密状态）。

职责：
- 拉取远程配置并解密、按本地加密状态写回 config.json
- TaBiAI 凭据轮转后回写管理平台，并**读回核实**新代次确实落了库
- 签到运行状态上报（start / heartbeat / stop）与查锁
"""

from __future__ import annotations

import copy
import json
import time
from pathlib import Path
from typing import Any, Optional

from curl_cffi import requests as cffi

from . import logger as log
from .config import (
    ConfigError,
    ConfigSyncConfig,
    migrate_legacy,
    load_config,
)
from .config_store import load_document, save_document
from .secure_config import ConfigEncryptionError, config_key_from_environment, decrypt_json
from .utils import sanitize_header_value


class RemoteSyncError(ValueError):
    """远程配置同步失败。"""


# 远端永远不能覆盖的本地模块：同步设置和密钥必须由本地控制，
# 否则覆盖后可能再也无法同步/解密。
_LOCAL_CONTROLLED_KEYS = frozenset({"security", "config_sync", "tabiai"})
# 参与合并的业务模块（缺失告警用）。
# tabiai 不在其中：cdp_url 指向本机 Chrome 的调试端口，属于机器级设置，
# 远端平台不可能知道每台机器的端口，下发覆盖只会把能用的配置改坏。
_SYNC_MODULES = ("accounts", "ai", "browser", "http", "proxy_pool", "notify")

# 凭据回写的重试次数与退避。平台攥着旧代次的后果是它的保活/网页端检测下次 refresh
# 撞重放、整条会话被撤销，所以这条链路值得多试几次；它打的是自己的平台而不是目标
# 站点，重试没有加重风控的顾虑。
WRITEBACK_ATTEMPTS = 3
WRITEBACK_BACKOFF_SECONDS = (1.0, 2.0)



def _merge_payload(local_raw: dict, payload: dict) -> dict:
    """把远端 payload 合并到本地配置。

    - 远端没带的顶级模块保留本地（缺键不动）。
    - 远端显式给出的值（包括显式空数组）按远端意图覆盖。
    - security / config_sync 永远由本地控制。
    """
    merged = copy.deepcopy(local_raw)
    for key, value in payload.items():
        if key in _LOCAL_CONTROLLED_KEYS:
            continue
        merged[key] = copy.deepcopy(value)
    return merged


def _missing_modules(local_raw: dict, payload: dict) -> list:
    """本地存在但远端没提供的业务模块（保留本地并告警）。"""
    return [key for key in _SYNC_MODULES if key in local_raw and key not in payload]


def _json_value(value: Any) -> Any:
    if not isinstance(value, str):
        return value
    text = value.strip()
    if not text:
        return value
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return value


def _dig(value: Any, path: str) -> Any:
    current = value
    for part in (item.strip() for item in path.split(".") if item.strip()):
        if isinstance(current, dict) and part in current:
            current = current[part]
        else:
            raise RemoteSyncError(f"response_field 找不到字段: {path}")
    return current


def _is_envelope(value: Any) -> bool:
    return isinstance(value, dict) and value.get("algorithm") == "AES-256-GCM"


def _looks_like_config(value: Any) -> bool:
    return isinstance(value, dict) and any(
        key in value for key in ("accounts", "ai", "browser", "http", "security", "config_sync")
    )


def _select_payload(value: Any, response_field: str = "") -> Any:
    value = _json_value(value)
    if response_field:
        return _json_value(_dig(value, response_field))
    if _is_envelope(value) or _looks_like_config(value):
        return value
    if isinstance(value, dict):
        for key in ("data", "payload", "config", "result", "encrypted"):
            if key in value:
                candidate = _select_payload(value[key])
                if _is_envelope(candidate) or _looks_like_config(candidate) or isinstance(candidate, list):
                    return candidate
    return value


def _decode_payload(value: Any, sync: ConfigSyncConfig, key: str) -> tuple[dict, bool]:
    candidate = _select_payload(value, sync.response_field)
    encrypted = False
    if _is_envelope(candidate):
        if not key:
            raise RemoteSyncError("远程响应是密文，但 security.config_key 为空")
        try:
            candidate = decrypt_json(candidate, key)
        except ConfigEncryptionError as exc:
            raise RemoteSyncError(str(exc)) from exc
        encrypted = True
        candidate = _select_payload(candidate)
    candidate = _json_value(candidate)
    if isinstance(candidate, list):
        candidate = migrate_legacy(candidate)
    if not isinstance(candidate, dict):
        raise RemoteSyncError("远程响应未解析出 JSON 配置对象")
    return copy.deepcopy(candidate), encrypted


def _headers(sync: ConfigSyncConfig) -> dict[str, str]:
    headers = {"Accept": "application/json, text/plain, */*"}
    for key, value in sync.headers.items():
        key = sanitize_header_value(key)
        if key:
            headers[key] = sanitize_header_value(value)
    if sync.token:
        prefix = sync.token_prefix.strip()
        value = f"{prefix} {sync.token}".strip()
        token_header = sanitize_header_value(sync.token_header)
        if token_header:
            headers[token_header] = sanitize_header_value(value)
    return headers


def _request(sync: ConfigSyncConfig):
    if not sync.url:
        raise RemoteSyncError("未配置远程配置 API URL")
    kwargs: dict[str, Any] = {
        "headers": _headers(sync),
        "timeout": sync.timeout,
    }
    if sync.method == "POST":
        if sync.body is not None:
            kwargs["json"] = sync.body
    elif isinstance(sync.body, dict) and sync.body:
        kwargs["params"] = sync.body
    try:
        response = cffi.request(sync.method, sync.url, **kwargs)
    except Exception as exc:  # noqa: BLE001 - 网络库异常类型不固定
        raise RemoteSyncError(f"远程配置请求失败: {type(exc).__name__}: {exc}") from exc
    status = int(getattr(response, "status_code", 0) or 0)
    if status >= 400:
        text = str(getattr(response, "text", "") or "")[:200]
        raise RemoteSyncError(f"远程配置 API 返回 HTTP {status}: {text}")
    try:
        return response.json()
    except Exception as exc:  # noqa: BLE001
        text = str(getattr(response, "text", "") or "")[:500]
        try:
            return json.loads(text)
        except json.JSONDecodeError as json_exc:
            raise RemoteSyncError(f"远程配置 API 未返回合法 JSON: {text}") from json_exc


def sync_remote_config(
    path: Optional[Path] = None,
    *,
    force: bool = False,
    auto_only: bool = False,
) -> dict:
    """请求远程配置，解密后校验并按本地加密状态写回 config.json。"""
    try:
        document = load_document(path)
        sync = ConfigSyncConfig.from_raw(document.raw.get("config_sync"))
        if not force and (not sync.enabled or (auto_only and not sync.auto_before_checkin)):
            return {"ok": True, "operation": "sync_config", "skipped": True, "message": "远程配置自动同步未启用"}
        if not sync.url:
            return {"ok": False, "operation": "sync_config", "error": "未配置远程配置 API URL"}

        response_data = _request(sync)
        key = config_key_from_environment(document.security.config_key)
        payload, encrypted = _decode_payload(response_data, sync, key)

        merged = _merge_payload(document.raw, payload)
        missing = _missing_modules(document.raw, payload)
        if missing:
            log.warn(f"远程配置缺少本地模块: {', '.join(missing)}；保留本地设置")
        # 同步设置和密钥由本地控制，避免远端配置覆盖后无法再次同步。
        merged["config_sync"] = copy.deepcopy(document.raw.get("config_sync") or sync.to_dict())
        # 保留本地 security 加密状态：原配置加密时用原密钥继续加密保存，
        # 禁止同步后明文落盘或删除密文文件。
        saved = save_document(
            merged,
            document.path,
            encryption_enabled=document.security.encryption_enabled,
        )
        cfg = load_config(saved.path)
        return {
            "ok": True,
            "operation": "sync_config",
            "skipped": False,
            "encrypted_response": encrypted,
            "path": str(saved.path),
            "account_count": len(cfg.accounts),
            "message": "远程配置已获取并保存到本地",
        }
    except (ConfigError, OSError, ValueError) as exc:
        return {"ok": False, "operation": "sync_config", "error": str(exc)}


# --------------------------------------------------------------------------- #
# TaBiAI 凭据回写
# --------------------------------------------------------------------------- #


def _writeback_endpoint(sync: ConfigSyncConfig, account_name: str) -> str:
    """回写端点：显式配了 writeback_url 就用它，否则按拉取 URL 同源推导。"""
    from urllib.parse import quote, urlsplit, urlunsplit

    template = (sync.writeback_url or "").strip()
    if template:
        if "{name}" in template:
            return template.replace("{name}", quote(account_name, safe=""))
        return template.rstrip("/")
    if not sync.url:
        return ""
    parts = urlsplit(sync.url)
    if not parts.scheme or not parts.netloc:
        return ""
    path = f"/api/accounts/{quote(account_name, safe='')}/refresh-cookie"
    return urlunsplit((parts.scheme, parts.netloc, path, "", ""))


def writeback_refresh_cookie(sync: ConfigSyncConfig, account_name: str,
                             cookie: str) -> tuple[bool, str]:
    """把轮转出的 new_api_refresh 回写到管理平台，并确认平台真的收下了。

    平台端拿旧代次去检测或保活会直接撞重放检测、整条会话被撤销，所以这里不能只看
    HTTP 状态码：中间要是有网关或鉴权代理回了自己的 200，请求压根没到服务端，平台
    仍然攥着旧代次，而客户端会以为同步成功。必须读响应体里的 `ok` 字段。

    验收通过后**还要再拉一次读回核实**（GET /api/accounts/{name}/raw）：响应体说收下了
    也只是服务端的自述，库里到底存了什么只有读回来才知道。核实不一致说明「收了但没存」
    或「存成了别的值」，和回写失败同等严重，走同一条判负路径；核实本身没拿到结论
    （网络错、超时、老版本平台没这个端点）只记日志放过 —— 回写已经被明确确认过，
    加固手段失灵不该反过来把成功的轮次判成失败。

    失败会重试（见 WRITEBACK_ATTEMPTS）。这条链路打的是自己的配置管理平台，不是目标
    站点，多试几次没有「加重风控」的顾虑，而漏掉一次的代价是整条会话。

    **全程不走代理**：目标是自己的平台而不是被盾挡着的站点，没有伪装出口的需要；
    套上代理只会让这条关键链路多一个失败点。
    """
    if not sync.enabled:
        return False, "未启用 config_sync"
    endpoint = _writeback_endpoint(sync, account_name)
    if not endpoint:
        return False, "无法确定回写地址（配置 config_sync.writeback_url）"
    if not str(cookie or "").strip():
        return False, "凭据为空"

    last = ""
    for attempt in range(1, WRITEBACK_ATTEMPTS + 1):
        ok, detail = _writeback_once(sync, endpoint, cookie)
        if ok:
            if attempt > 1:
                log.debug(f"凭据回写第 {attempt} 次成功")
            matched, reason = _verify_writeback(sync, account_name, cookie)
            if matched is False:
                # 平台自述收下了，库里却不是这一代 —— 它接下来还会拿旧代去保活
                return False, f"平台确认收下但读回核实不一致：{reason}"
            if matched is None:
                log.debug(f"凭据回写读回核实未取得结论（{reason}）；回写已被平台确认，按成功处理")
            return True, endpoint
        last = detail
        if attempt < WRITEBACK_ATTEMPTS:
            wait = WRITEBACK_BACKOFF_SECONDS[
                min(attempt - 1, len(WRITEBACK_BACKOFF_SECONDS) - 1)
            ]
            log.debug(f"凭据回写第 {attempt} 次失败（{detail}），{wait:.0f}s 后重试")
            time.sleep(wait)
    return False, f"重试 {WRITEBACK_ATTEMPTS} 次仍失败：{last}"


def _writeback_once(sync: ConfigSyncConfig, endpoint: str,
                    cookie: str) -> tuple[bool, str]:
    """发一次回写并验收响应。返回 (平台是否确认收下, 失败原因)。"""
    try:
        response = cffi.request(
            "POST", endpoint,
            headers={**_headers(sync), "Content-Type": "application/json"},
            json={"cookie": cookie},
            timeout=sync.timeout,
            # 显式直连。写成参数而不是靠默认值，是为了让「这里不该套代理」变成
            # 看得见的约定 —— 免得以后有人顺手把它改成走带代理的 session
            proxies=None,
        )
    except Exception as exc:  # noqa: BLE001 - 网络库异常类型不固定
        return False, f"{type(exc).__name__}: {exc}"[:160]
    status = int(getattr(response, "status_code", 0) or 0)
    if status >= 400:
        text = str(getattr(response, "text", "") or "")[:160]
        return False, f"HTTP {status}: {text}"
    # HTTP 2xx 不等于平台收下了：网关、鉴权代理、缓存层都可能替它回 200。
    # 服务端只有在确认写库成功后才回 {"ok": true}，所以认这个字段而不是状态码。
    try:
        body = response.json()
    except Exception:  # noqa: BLE001 - 响应不是 JSON 就是没到服务端
        text = str(getattr(response, "text", "") or "")[:120]
        return False, f"HTTP {status} 但响应不是 JSON（疑似网关代答）: {text!r}"
    if not isinstance(body, dict):
        return False, f"HTTP {status} 但响应不是 JSON 对象: {str(body)[:120]}"
    if body.get("ok") is not True:
        return False, f"HTTP {status} 但平台未确认收下: {str(body)[:120]}"
    return True, ""


def issue_refresh_cookie_via_platform(sync: ConfigSyncConfig,
                                      account_name: str) -> tuple[str, str]:
    """请平台为该账号签发一条新的 new_api_refresh，返回 (cookie, error)。

    为什么让平台签发而不是本机自己走 OAuth：签发要打 GitHub 的 OAuth 端点，而
    Actions runner 的出口是随机的 Azure IP、代理池里又都是机房 IP —— 带着
    user_session 从这些地址反复出现，最容易触发 GitHub 的账号风控（最坏是把
    user_session 直接作废，那自救链路就彻底断了）。平台部署在固定 IP 上，
    GitHub 眼里它是「常用设备」，成功率和安全性都更好。

    带 for_running_checkin=true：签发端点默认被签到锁拦住（防人工手滑打断正在跑的
    那一轮），而这里的调用方**就是**那个正在跑的签到，必须放行。

    平台签发成功后会自己写库，所以拿到 cookie 只需落本地盘，**不必再回写平台**。

    显式直连（proxies=None）：打的是自己的平台，与回写、读回核实同一套口径。
    """
    endpoint = _issue_endpoint(sync)
    if not endpoint:
        return "", "无法确定签发地址（config_sync.url 未配置或非法）"
    try:
        response = cffi.request(
            "POST", endpoint,
            headers={**_headers(sync), "Content-Type": "application/json"},
            json={"account_name": account_name, "for_running_checkin": True},
            timeout=sync.timeout,
            proxies=None,
        )
    except Exception as exc:  # noqa: BLE001 - 网络库异常类型不固定
        return "", f"{type(exc).__name__}: {exc}"[:160]
    status = int(getattr(response, "status_code", 0) or 0)
    try:
        body = response.json()
    except Exception:  # noqa: BLE001 - 非 JSON 说明没到服务端（网关代答等）
        text = str(getattr(response, "text", "") or "")[:120]
        return "", f"平台签发响应不是 JSON（HTTP {status}）：{text!r}"
    if not isinstance(body, dict):
        return "", f"平台签发响应不是 JSON 对象（HTTP {status}）"
    if status >= 400 or not body.get("ok"):
        # 平台会把 OAuth 三步的具体原因放在 error 里，原样带出去好让人看懂
        reason = str(body.get("error") or "").strip() or f"HTTP {status}"
        return "", reason
    cookie = body.get("cookie")
    if not isinstance(cookie, str) or not cookie.strip():
        # 平台说签发成功但没回凭据：多半是用 JWT 而不是 API Key 调的（那种不下发明文）
        return "", "平台确认已签发但未返回凭据（检查 config_sync.token 是不是 API Key）"
    return cookie.strip(), ""


def _issue_endpoint(sync: ConfigSyncConfig) -> str:
    """签发端点：按拉取 URL 同源推导 /api/tabiai/issue-cookie。"""
    from urllib.parse import urlsplit, urlunsplit

    if not sync.url:
        return ""
    parts = urlsplit(sync.url)
    if not parts.scheme or not parts.netloc:
        return ""
    return urlunsplit((parts.scheme, parts.netloc, "/api/tabiai/issue-cookie", "", ""))


def _verify_endpoint(sync: ConfigSyncConfig, account_name: str) -> str:
    """读回核实端点：按拉取 URL 同源推导 /api/accounts/{name}/raw。

    有意不复用 writeback_url：那是「按账号回写」的地址，可能被配成带 {name} 的模板
    或指向第三方网关，从它身上推不出对应的读回地址。推不出来就不核实 —— 核实是加固，
    缺了它不能反过来把已被平台确认的回写判负。
    """
    from urllib.parse import quote, urlsplit, urlunsplit

    if not sync.url:
        return ""
    parts = urlsplit(sync.url)
    if not parts.scheme or not parts.netloc:
        return ""
    path = f"/api/accounts/{quote(account_name, safe='')}/raw"
    return urlunsplit((parts.scheme, parts.netloc, path, "", ""))


def _same_refresh_cookie(stored: str, written: str) -> bool:
    """两条凭据是不是同一代。

    先 strip 再比：中间哪一层多带了个换行都不该被判成「存错了」。
    仍不相等时再按平台的归一化规则兜一层 —— 平台落库前会给裸 sid.secret 补上
    new_api_refresh= 前缀（server/cookie_checker.go 的 normalizeTabiAIRefreshCookie），
    两种写法在平台眼里是同一个值，客户端不能因为表示形式不同就判负。
    """
    left, right = str(stored or "").strip(), str(written or "").strip()
    if left == right:
        return True
    from .tabiai import normalize_refresh_cookie

    return normalize_refresh_cookie(left) == normalize_refresh_cookie(right)


def _verify_writeback(sync: ConfigSyncConfig, account_name: str,
                      cookie: str) -> tuple[Optional[bool], str]:
    """回写被验收后再读回来比一遍。返回 (核实结论, 说明)。

    结论是三态，缺一不可：
      - True  库里就是刚写进去的那一代
      - False **确认存的是别的值**（收了没存 / 存成了别的），与回写失败同等严重
      - None  没拿到结论（推不出地址、网络错、非预期响应），只记日志放过

    把 None 和 False 分开是这一步的全部意义：回写已经被平台的 `ok` 明确确认过，
    核实只是加固；若把「核实拿不到结果」也当成失败，平台一有抖动就会把本来成功的
    账号判负，反而制造出一批假故障。

    同样显式直连（proxies=None）：读回打的还是自己的平台，套代理只是多一个失败点。
    超时沿用 config_sync.timeout，与回写请求同一个量级。
    """
    endpoint = _verify_endpoint(sync, account_name)
    if not endpoint:
        return None, "无法确定读回地址（config_sync.url 未配置或非法）"
    try:
        response = cffi.request(
            "GET", endpoint,
            headers=_headers(sync),
            timeout=sync.timeout,
            proxies=None,
        )
    except Exception as exc:  # noqa: BLE001 - 网络库异常类型不固定
        return None, f"{type(exc).__name__}: {exc}"[:160]
    status = int(getattr(response, "status_code", 0) or 0)
    if status >= 400:
        # 老版本平台没有这个端点会 404，同样只是「核实不了」而不是「写错了」
        text = str(getattr(response, "text", "") or "")[:120]
        return None, f"HTTP {status}: {text}"
    try:
        body = response.json()
    except Exception:  # noqa: BLE001 - 不是 JSON 就说明没到服务端，核实不了
        text = str(getattr(response, "text", "") or "")[:120]
        return None, f"HTTP {status} 但响应不是 JSON: {text!r}"
    if not isinstance(body, dict):
        return None, f"HTTP {status} 但响应不是 JSON 对象: {str(body)[:120]}"
    if not isinstance(body.get("cookie"), str):
        # 缺字段是响应形状不对（网关代答、端点被改），不能据此断言库里存错了
        return None, f"响应里没有 cookie 字段: {str(body)[:120]}"
    stored = str(body["cookie"])
    if _same_refresh_cookie(stored, cookie):
        return True, ""
    # 有 cookie 字段但值不是这一代（含被存成空串）：这是真故障，必须判负
    return False, f"库里存的是另一个值（长度 {len(stored.strip())}，非本次回写的凭据）"


# --------------------------------------------------------------------------- #
# 签到运行状态上报（让网页端在签到期间锁住高危凭据操作）
# --------------------------------------------------------------------------- #


def _run_state_endpoint(sync: ConfigSyncConfig, action: str) -> str:
    """按拉取 URL 同源推导 /api/run-state/<action>。

    不复用 writeback_url：那是「按账号回写凭据」的地址，可能被配成带 {name} 的模板
    或第三方网关，跟运行状态不是一回事。推不出来就直接不上报 —— 上报失败绝不能
    影响签到本身。
    """
    from urllib.parse import urlsplit, urlunsplit

    if not sync.url:
        return ""
    parts = urlsplit(sync.url)
    if not parts.scheme or not parts.netloc:
        return ""
    return urlunsplit((parts.scheme, parts.netloc, f"/api/run-state/{action}", "", ""))


def _post_run_state(sync: ConfigSyncConfig, action: str,
                    payload: Optional[dict] = None) -> tuple[bool, str, dict]:
    """给运行状态端点发一次 POST。失败只返回原因，不抛异常。"""
    if not sync.enabled:
        return False, "未启用 config_sync", {}
    endpoint = _run_state_endpoint(sync, action)
    if not endpoint:
        return False, "无法确定运行状态上报地址（config_sync.url 未配置或非法）", {}
    try:
        response = cffi.request(
            "POST", endpoint,
            headers={**_headers(sync), "Content-Type": "application/json"},
            json=payload or {},
            timeout=sync.timeout,
        )
    except Exception as exc:  # noqa: BLE001 - 网络库异常类型不固定
        return False, f"{type(exc).__name__}: {exc}"[:160], {}
    status = int(getattr(response, "status_code", 0) or 0)
    if status >= 400:
        text = str(getattr(response, "text", "") or "")[:160]
        return False, f"HTTP {status}: {text}", {}
    body: dict = {}
    try:
        parsed = response.json()
        if isinstance(parsed, dict):
            body = parsed
    except Exception:  # noqa: BLE001 - 响应不是 JSON 也不影响上报成败
        body = {}
    return True, endpoint, body


def report_run_start(sync: ConfigSyncConfig, source: str) -> tuple[bool, str, int]:
    """告诉平台「我开始签到了」，平台据此锁住 TaBiAI 检测与签发。

    第三个返回值是平台下发的建议心跳间隔（秒），取不到时为 0，
    调用方自己兜底 —— 客户端不必硬编码这个值，改平台常量就能全局生效。
    """
    ok, detail, body = _post_run_state(sync, "start", {"source": source})
    if not ok:
        return False, detail, 0
    gap = 0
    state = body.get("run_state")
    if isinstance(state, dict):
        raw = state.get("heartbeat_seconds")
        if isinstance(raw, (int, float)) and raw > 0:
            gap = int(raw)
    return True, detail, gap


def fetch_run_state(sync: ConfigSyncConfig) -> tuple[bool, dict]:
    """查平台当前的运行锁状态。返回 (查询是否成功, 状态字典)。

    用途是「签到开跑前看看凭据保活是不是正在跑」：保活也会真 refresh，两边同时动
    同一条 sid 会让旧代被判重放。查询失败一律当成没锁 —— 平台不可达时不该连签到
    都做不了。

    GET 而不是 POST：这个端点是只读的，用 POST 会被平台当成 start 上报。
    """
    if not sync.enabled:
        return False, {}
    # 端点是 /api/run-state（不带 action 后缀），复用同源推导后去掉尾部斜杠
    endpoint = _run_state_endpoint(sync, "").rstrip("/")
    if not endpoint:
        return False, {}
    try:
        response = cffi.request("GET", endpoint, headers=_headers(sync), timeout=sync.timeout)
    except Exception as exc:  # noqa: BLE001 - 网络库异常类型不固定
        log.debug(f"查运行锁失败: {type(exc).__name__}: {exc}")
        return False, {}
    if int(getattr(response, "status_code", 0) or 0) >= 400:
        return False, {}
    try:
        parsed = response.json()
    except Exception:  # noqa: BLE001
        return False, {}
    return (True, parsed) if isinstance(parsed, dict) else (False, {})


def report_run_heartbeat(sync: ConfigSyncConfig) -> tuple[bool, bool]:
    """续期。返回 (上报是否成功, 平台是否仍认为我在跑)。

    running=False 不是错误：说明管理员在界面上强制解锁了。调用方该把这件事
    说出来 —— 此刻网页端可能正在动同一条凭据。
    """
    ok, _, body = _post_run_state(sync, "heartbeat")
    if not ok:
        return False, False
    return True, bool(body.get("running"))


def report_run_stop(sync: ConfigSyncConfig) -> bool:
    """签到收尾，立刻解锁。平台侧是幂等的，重复调用无害。"""
    ok, _, _ = _post_run_state(sync, "stop")
    return ok
