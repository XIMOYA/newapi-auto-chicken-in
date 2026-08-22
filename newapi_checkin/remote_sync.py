"""远程配置 API 获取、AES-GCM 解密和本地保存（保留本地加密状态）。"""

from __future__ import annotations

import copy
import json
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
    """把轮转出的 new_api_refresh 回写到管理平台。

    平台端拿旧代次去检测会直接被判失效，所以每轮转一次就同步一次。
    这里失败不抛异常：本地 sessions.json 已经存了新值，签到本身不受影响。
    """
    if not sync.enabled:
        return False, "未启用 config_sync"
    endpoint = _writeback_endpoint(sync, account_name)
    if not endpoint:
        return False, "无法确定回写地址（配置 config_sync.writeback_url）"
    if not str(cookie or "").strip():
        return False, "凭据为空"
    try:
        response = cffi.request(
            "POST", endpoint,
            headers={**_headers(sync), "Content-Type": "application/json"},
            json={"cookie": cookie},
            timeout=sync.timeout,
        )
    except Exception as exc:  # noqa: BLE001 - 网络库异常类型不固定
        return False, f"{type(exc).__name__}: {exc}"[:160]
    status = int(getattr(response, "status_code", 0) or 0)
    if status >= 400:
        text = str(getattr(response, "text", "") or "")[:160]
        return False, f"HTTP {status}: {text}"
    return True, endpoint


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
