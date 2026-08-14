"""HTTP 快路径：curl_cffi 伪装 TLS/JA3 指纹直发请求（策略 S0 / S1）。

New API 的两个硬要求（沿用原 JS 脚本已验证的结论）：
  1. 先 GET /api/user/self 拿到用户 id
  2. 之后所有请求都要带 New-Api-User: <id> 头

签到路径在不同 fork 里不一致，这里按候选列表探测，命中后由上层写回缓存。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Optional

from curl_cffi import requests as cffi

from .config import SELF_PATH, Account, HttpConfig
from .utils import parse_proxy, sanitize_header_value  # noqa: F401  (parse_proxy 供上层复用)
from .cf import detect
from .cf.session_store import CFSession

# 结果分类
SUCCESS = "success"
ALREADY_DONE = "already_done"
FAILED = "failed"
AUTH_FAILED = "auth_failed"
LOGIN_REQUIRED = "login_required"
CF_BLOCKED = "cf_blocked"
WAF_BLOCKED = "waf_block"
TURNSTILE_REQUIRED = "turnstile_required"
NETWORK_ERROR = "network_error"
UNKNOWN = "unknown"

_ALREADY_MARKERS = (
    "已签到", "已经签到", "重复签到", "今日已", "今天已", "已完成签到", "签到过",
    "already", "duplicate", "signed in today", "checked in",
)
_AUTH_MARKERS = (
    "未登录", "无权限", "登录已过期", "无效的凭证", "请先登录", "身份验证",
    "unauthorized", "invalid credential", "not logged in", "forbidden",
)
_TURNSTILE_MARKERS = (
    "turnstile token 为空",
    "turnstile token",
    "turnstile 校验失败",
    "turnstile verification failed",
)
_WRONG_PATH_STATUSES = (404, 405, 501)

DEFAULT_ACCEPT_LANGUAGE = "zh-CN,zh;q=0.9,en;q=0.8"


def pick_impersonate(configured: str, user_agent: str = "") -> str:
    """让 TLS 指纹和 UA 保持同一浏览器族。

    Camoufox 是 Firefox 系，它拿到的 cf_clearance 配上 Chrome 的 JA3 会立刻失效，
    所以按缓存里的真实 UA 反推该用哪个 impersonate 目标。
    """
    cfg = (configured or "chrome").strip() or "chrome"
    ua = (user_agent or "").lower()
    if not ua:
        return cfg
    if "firefox" in ua and not cfg.startswith("firefox"):
        return "firefox"
    if "chrome" in ua and "firefox" not in ua and not cfg.startswith(("chrome", "edge")):
        return "chrome"
    return cfg


def parse_cookie_header(raw: str) -> dict:
    out: dict = {}
    for chunk in (raw or "").split(";"):
        chunk = chunk.strip()
        if not chunk or "=" not in chunk:
            continue
        name, _, value = chunk.partition("=")
        name = name.strip()
        if name:
            out[name] = value.strip()
    return out


def build_cookie_header(cookies: dict) -> str:
    """拼 Cookie 头。每个名/值都过 latin-1 清洗，防止坏 cookie 让 curl_cffi
    在 headers.update 时抛 UnicodeEncodeError（koqj 事故根因）。"""
    parts = []
    for k, v in cookies.items():
        key = sanitize_header_value(k)
        if not key:
            continue
        parts.append(f"{key}={sanitize_header_value(v)}")
    return "; ".join(parts)


def merge_cookies(account_cookie: str, cf_cookies: Optional[dict]) -> dict:
    """账号 cookie 为底，过盾得到的 cookie 覆盖同名项（cf_clearance/__cf_bm 以新的为准）。"""
    merged = parse_cookie_header(account_cookie)
    for key, value in (cf_cookies or {}).items():
        if value:
            merged[key] = value
    return merged


def _text_of(resp: Any, limit: int = 400) -> str:
    try:
        return (resp.text or "")[:limit]
    except Exception:  # noqa: BLE001
        return ""


def _json_of(resp: Any) -> Optional[dict]:
    try:
        data = resp.json()
    except Exception:  # noqa: BLE001 - HTML 质询页 / 空响应都会走到这里
        return None
    return data if isinstance(data, dict) else None


def _extract_quota(data: dict) -> Any:
    payload = data.get("data")
    if isinstance(payload, dict):
        for key in ("quota_awarded", "quota", "award", "reward", "amount"):
            if payload.get(key) not in (None, ""):
                return payload[key]
    for key in ("quota_awarded", "quota"):
        if data.get(key) not in (None, ""):
            return data[key]
    return None


def _hit(message: str, markers) -> bool:
    low = (message or "").lower()
    return any(m in low for m in markers)


def classify_self(status: int, data: Optional[dict], text: str = "") -> "ApiResult":
    """把 /api/user/self 的响应归类。独立成函数，S4 浏览器内直发也复用。"""
    if data is None:
        return ApiResult(UNKNOWN, message=f"HTTP {status} 非 JSON 响应: {text}",
                         status=status, path=SELF_PATH)
    message = str(data.get("message") or "")
    if status in (401, 403) or not data.get("success"):
        kind = AUTH_FAILED if status in (401, 403) or _hit(message, _AUTH_MARKERS) else FAILED
        return ApiResult(kind, message=message or f"HTTP {status}", status=status, path=SELF_PATH)
    payload = data.get("data") if isinstance(data.get("data"), dict) else {}
    try:
        user_id = int(payload.get("id"))
    except (TypeError, ValueError):
        return ApiResult(FAILED, message="响应中缺少 data.id，无法构造 New-Api-User 头",
                         status=status, path=SELF_PATH)
    username = str(payload.get("username") or payload.get("display_name") or "")
    return ApiResult(SUCCESS, message=username, status=status, path=SELF_PATH,
                     user_id=user_id, quota=payload.get("quota"))


def classify_checkin(status: int, data: Optional[dict], text: str = "",
                     path: Optional[str] = None) -> "ApiResult":
    """把签到接口的响应归类。"""
    if data is None:
        return ApiResult(UNKNOWN, message=f"HTTP {status} 非 JSON 响应: {text}",
                         status=status, path=path)
    message = str(data.get("message") or "")
    if data.get("success"):
        return ApiResult(SUCCESS, message=message or "签到成功", quota=_extract_quota(data),
                         status=status, path=path)
    if _hit(message, _TURNSTILE_MARKERS):
        return ApiResult(TURNSTILE_REQUIRED, message=message or "需要 Turnstile token",
                         status=status, path=path)
    if _hit(message, _ALREADY_MARKERS):
        return ApiResult(ALREADY_DONE, message=message or "今日已签到", status=status, path=path)
    if status in (401, 403) or _hit(message, _AUTH_MARKERS):
        return ApiResult(AUTH_FAILED, message=message or f"HTTP {status}", status=status, path=path)
    return ApiResult(FAILED, message=message or f"HTTP {status}", status=status, path=path)


@dataclass
class ApiResult:
    kind: str
    message: str = ""
    quota: Any = None
    status: int = 0
    path: Optional[str] = None
    user_id: Optional[int] = None
    verdict: Optional[detect.Verdict] = None
    signals: list = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return self.kind in (SUCCESS, ALREADY_DONE)


class ApiClient:
    """一个账号一个实例；带上过盾缓存后即为策略 S0，不带即为 S1。"""

    def __init__(self, account: Account, http: HttpConfig, cf: Optional[CFSession] = None):
        self.account = account
        self.http = http
        self.cf = cf
        self.user_id = account.user_id
        self.impersonate = pick_impersonate(http.impersonate, cf.user_agent if cf else "")
        self._session = self._build_session()

    def _build_session(self):
        proxy = self.account.proxy
        session = cffi.Session(
            impersonate=self.impersonate,
            verify=self.http.verify,
            timeout=self.http.timeout,
            proxies={"http": proxy, "https": proxy} if proxy else None,
        )
        base = self.account.base_url
        headers = {
            "Accept": "application/json, text/plain, */*",
            "Accept-Language": sanitize_header_value(
                self.cf.accept_language if self.cf and self.cf.accept_language
                else DEFAULT_ACCEPT_LANGUAGE
            ),
            "Referer": sanitize_header_value(base + "/"),
            "Origin": sanitize_header_value(base),
        }
        # cf_clearance 与 UA 绑定：缓存里的 UA 必须原样带回（清洗后仍可能为空，忽略即可）
        if self.cf and self.cf.user_agent:
            ua = sanitize_header_value(self.cf.user_agent)
            if ua:
                headers["User-Agent"] = ua
        cookies = merge_cookies(self.account.cookie, self.cf.cookies if self.cf else None)
        if cookies:
            cookie_header = build_cookie_header(cookies)
            if cookie_header:
                headers["Cookie"] = cookie_header
        session.headers.update(headers)
        if self.user_id:
            session.headers["New-Api-User"] = str(self.user_id)
        return session

    def set_user_id(self, user_id: int) -> None:
        self.user_id = int(user_id)
        self._session.headers["New-Api-User"] = str(self.user_id)

    def close(self) -> None:
        try:
            self._session.close()
        except Exception:  # noqa: BLE001
            pass

    def __enter__(self) -> "ApiClient":
        return self

    def __exit__(self, *_exc) -> None:
        self.close()

    def _request(self, method: str, path: str, **kwargs):
        """返回 (response, blocked_result)。blocked_result 非空表示已被判定拦截/异常。"""
        try:
            resp = self._session.request(method, self.account.api(path), **kwargs)
        except Exception as exc:  # noqa: BLE001 - curl 层异常类型很杂，统一归为网络错误
            return None, ApiResult(NETWORK_ERROR, message=f"{type(exc).__name__}: {exc}"[:200],
                                   path=path)
        verdict = detect.analyze_response(resp)
        if verdict.blocked:
            return resp, ApiResult(CF_BLOCKED, message=verdict.describe(),
                                   status=resp.status_code, path=path,
                                   verdict=verdict, signals=list(verdict.signals))
        return resp, None

    # ----------------------------------------------------------------- #

    def fetch_self(self) -> ApiResult:
        """GET /api/user/self，成功后自动设置 New-Api-User 头。"""
        resp, blocked = self._request("GET", SELF_PATH)
        if blocked is not None:
            return blocked
        result = classify_self(resp.status_code, _json_of(resp), _text_of(resp, 160))
        if result.user_id:
            self.set_user_id(result.user_id)
        return result

    def checkin(self) -> ApiResult:
        """POST 签到接口，路径按候选列表探测（404/405 视为路径不对，继续试下一个）。"""
        last: Optional[ApiResult] = None
        for path in self.account.checkin_candidates:
            resp, blocked = self._request("POST", path)
            if blocked is not None:
                return blocked
            status = resp.status_code
            if status in _WRONG_PATH_STATUSES:
                last = ApiResult(FAILED, message=f"HTTP {status}（路径不存在）",
                                 status=status, path=path)
                continue

            result = classify_checkin(status, _json_of(resp), _text_of(resp, 160), path)
            if result.kind == UNKNOWN and status >= 400:
                last = result
                continue
            return result

        return last or ApiResult(FAILED, message="所有候选签到路径都不可用")
