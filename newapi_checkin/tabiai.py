"""TaBiAI（New API 分支）签到链路。

协议依据 docs/签到原理.md（2026-08-17 实测），要点：

1. 凭据是 `new_api_refresh=<sid>.<secret>` cookie，Path=/api/user/auth，HttpOnly
2. 业务接口只认 `Authorization: Bearer <JWT>`，JWT 由 POST /api/user/auth/refresh 换取，约 300 秒
3. **refresh 有轮转 + 重放检测**：每次成功都会下发下一代 secret。旧代超出宽限窗口后再用，
   会先 401 AUTH_UNAUTHORIZED，被判重放则整条会话被撤销（401 AUTH_SESSION_REVOKED）。
   所以每次 refresh 成功后必须立刻把新值落盘并回写平台，绝不能"更新一半又用回旧的"
4. 签到 `POST /api/user/checkin?turnstile=<token>` 需要真实 Turnstile token；
   业务失败也返回 HTTP 200，判定必须读 body 的 `success`
5. 先查 `GET /api/user/checkin?month=` 的 `checked_in_today`，已签就不浪费 Turnstile 配额
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any, Optional

from curl_cffi import requests as cffi

from . import client as api
from . import logger as log
from .cf import detect
from .cf.session_store import CFSession
from .config import (
    TABIAI_CHECKIN_PATH,
    TABIAI_REFRESH_COOKIE_NAME,
    TABIAI_REFRESH_PATH,
    Account,
    HttpConfig,
)
from .utils import sanitize_header_value

BROWSER_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/151.0.0.0 Safari/537.36 Edg/151.0.0.0"
)


@dataclass
class RefreshResult:
    """一次 refresh 的结果；rotated 为站点下发的新一代 cookie（必须落盘）。"""

    result: api.ApiResult
    access_token: str = ""
    user_id: Optional[int] = None
    username: str = ""
    rotated: str = ""


def normalize_refresh_cookie(raw: str) -> str:
    """允许只填裸 sid.secret，也允许填完整 new_api_refresh=...。"""
    value = str(raw or "").strip()
    if not value:
        return ""
    if f"{TABIAI_REFRESH_COOKIE_NAME}=" in value:
        return value
    return f"{TABIAI_REFRESH_COOKIE_NAME}={value}"


def refresh_cookie_value(raw: str) -> str:
    """取出 sid.secret 部分，便于比较两代是否相同。"""
    value = normalize_refresh_cookie(raw)
    if not value:
        return ""
    for part in value.split(";"):
        name, _, val = part.strip().partition("=")
        if name.strip() == TABIAI_REFRESH_COOKIE_NAME:
            return val.strip()
    return ""


class TabiAIClient:
    """一个账号一轮 TaBiAI 签到的短生命周期客户端。

    凭据轮转由 on_rotate 回调交给上层落盘（store + 回写平台），
    本类只保证「拿到新值就立刻回调」，不自己决定持久化策略。
    """

    def __init__(self, account: Account, http: HttpConfig, cookie: str,
                 cf: Optional[CFSession] = None, on_rotate=None):
        self.account = account
        self.http = http
        self.cf = cf
        self.on_rotate = on_rotate
        self._cookie = normalize_refresh_cookie(cookie)
        self.user_id = account.user_id
        self.impersonate = api.pick_impersonate(http.impersonate, cf.user_agent if cf else "")
        self._session = self._build_session()

    # ---- 生命周期 ----

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
            "Accept-Language": api.sanitize_header_value(
                self.cf.accept_language if self.cf and self.cf.accept_language
                else api.DEFAULT_ACCEPT_LANGUAGE
            ),
            "Referer": sanitize_header_value(base + "/profile"),
            "Origin": sanitize_header_value(base),
            "User-Agent": sanitize_header_value(self.cf.user_agent if self.cf else BROWSER_UA),
        }
        session.headers.update({k: v for k, v in headers.items() if v})
        return session

    def close(self) -> None:
        try:
            self._session.close()
        except Exception:  # noqa: BLE001
            pass

    def __enter__(self) -> "TabiAIClient":
        return self

    def __exit__(self, *_exc) -> None:
        self.close()

    # ---- 底层请求 ----

    def _request(self, method: str, path: str, *, cookie: str = "",
                 token: str = "", **kwargs):
        """发一次站点请求；返回 (ApiResult, response)。

        cookie 只在 refresh 时带（Path=/api/user/auth，业务接口不需要）；
        业务接口带 Bearer token。CF 拦截转成现有 ApiResult 交给 runner 过盾。
        """
        headers = dict(kwargs.pop("headers", {}) or {})
        merged_cookie = api.merge_cookies(cookie, self.cf.cookies if self.cf else None)
        if merged_cookie:
            header_value = api.build_cookie_header(merged_cookie)
            if header_value:
                headers["Cookie"] = header_value
        if token:
            headers["Authorization"] = sanitize_header_value(f"Bearer {token}")
        try:
            resp = self._session.request(method, self.account.api(path), headers=headers, **kwargs)
        except Exception as exc:  # noqa: BLE001
            return api.ApiResult(
                api.NETWORK_ERROR,
                message=f"{type(exc).__name__}: {exc}"[:240],
                path=path,
            ), None
        verdict = detect.analyze_response(resp)
        if verdict.blocked:
            kind = api.WAF_BLOCKED if verdict.challenge == detect.WAF_BLOCK else api.CF_BLOCKED
            return api.ApiResult(
                kind,
                message=verdict.describe(),
                status=resp.status_code,
                path=path,
                verdict=verdict,
                signals=list(verdict.signals),
            ), resp
        return api.ApiResult(api.UNKNOWN, status=resp.status_code, path=path), resp

    @staticmethod
    def _json(response: Any) -> Optional[dict]:
        try:
            data = response.json()
        except Exception:  # noqa: BLE001
            return None
        return data if isinstance(data, dict) else None

    @staticmethod
    def _text(response: Any, limit: int = 240) -> str:
        try:
            return " ".join((response.text or "")[:limit].split())
        except Exception:  # noqa: BLE001
            return ""

    # ---- 第一步：refresh 换 access token（并轮转凭据）----

    def refresh(self) -> RefreshResult:
        result, resp = self._request("POST", TABIAI_REFRESH_PATH, cookie=self._cookie)
        if result.kind != api.UNKNOWN:
            return RefreshResult(result)

        rotated = self._extract_rotated(resp)
        if rotated and refresh_cookie_value(rotated) != refresh_cookie_value(self._cookie):
            # 拿到新代次就立刻更新自身与外部存储：晚一步就可能用旧代重放
            self._cookie = rotated
            if self.on_rotate is not None:
                self.on_rotate(rotated)

        data = self._json(resp)
        if data is None:
            return RefreshResult(api.ApiResult(
                api.UNKNOWN,
                message=f"refresh 返回非 JSON（HTTP {resp.status_code}）：{self._text(resp)}",
                status=resp.status_code,
                path=TABIAI_REFRESH_PATH,
            ), rotated=rotated)

        code = str(data.get("code") or "").upper()
        message = str(data.get("message") or "")
        if resp.status_code in (401, 403):
            if code == "AUTH_SESSION_REVOKED":
                message = "会话已被撤销（旧代次重放或在别处登出了会话），需要重新签发 new_api_refresh"
            elif code == "AUTH_UNAUTHORIZED":
                message = "凭据已失效：可能已过期，或被更新后的代次取代"
            return RefreshResult(api.ApiResult(
                api.AUTH_FAILED,
                message=message or f"refresh HTTP {resp.status_code}",
                status=resp.status_code,
                path=TABIAI_REFRESH_PATH,
            ), rotated=rotated)

        if not data.get("success"):
            kind = api.AUTH_FAILED if api._hit(message, api._AUTH_MARKERS) else api.FAILED
            return RefreshResult(api.ApiResult(
                kind,
                message=message or "refresh 失败",
                status=resp.status_code,
                path=TABIAI_REFRESH_PATH,
            ), rotated=rotated)

        payload = data.get("data") if isinstance(data.get("data"), dict) else {}
        token = str(payload.get("access_token") or "").strip()
        if not token:
            return RefreshResult(api.ApiResult(
                api.FAILED,
                message="refresh 成功但未返回 access_token",
                status=resp.status_code,
                path=TABIAI_REFRESH_PATH,
            ), rotated=rotated)

        user = payload.get("user") if isinstance(payload.get("user"), dict) else {}
        uid = user.get("id")
        uid = int(uid) if isinstance(uid, (int, float)) and int(uid) > 0 else None
        username = str(user.get("username") or user.get("display_name") or "")
        if uid:
            self.user_id = uid
        return RefreshResult(
            api.ApiResult(api.SUCCESS, status=resp.status_code, path=TABIAI_REFRESH_PATH, user_id=uid),
            access_token=token,
            user_id=uid,
            username=username,
            rotated=rotated,
        )

    @staticmethod
    def _extract_rotated(resp: Any) -> str:
        """从响应里取新一代 new_api_refresh。curl_cffi 的 cookies 与 Set-Cookie 都看一遍。"""
        try:
            value = resp.cookies.get(TABIAI_REFRESH_COOKIE_NAME)
            if value:
                return f"{TABIAI_REFRESH_COOKIE_NAME}={value}"
        except Exception:  # noqa: BLE001
            pass
        try:
            raw_headers = resp.headers.get_list("Set-Cookie")  # type: ignore[attr-defined]
        except Exception:  # noqa: BLE001
            single = ""
            try:
                single = resp.headers.get("Set-Cookie") or ""
            except Exception:  # noqa: BLE001
                single = ""
            raw_headers = [single] if single else []
        for raw in raw_headers:
            head = str(raw).split(";", 1)[0].strip()
            name, _, value = head.partition("=")
            if name.strip() == TABIAI_REFRESH_COOKIE_NAME and value.strip():
                return f"{TABIAI_REFRESH_COOKIE_NAME}={value.strip()}"
        return ""

    # ---- 第二步：查当月签到状态（零 Turnstile 消耗）----

    def query_checkin(self, token: str) -> tuple[api.ApiResult, Optional[bool]]:
        """返回 (结果, 今日是否已签)。已签时上层直接收工，不去取 Turnstile token。"""
        month = datetime.now().strftime("%Y-%m")
        result, resp = self._request(
            "GET", f"{TABIAI_CHECKIN_PATH}?month={month}", token=token)
        if result.kind != api.UNKNOWN:
            return result, None
        data = self._json(resp)
        if data is None:
            return api.ApiResult(
                api.UNKNOWN,
                message=f"查询签到状态返回非 JSON（HTTP {resp.status_code}）：{self._text(resp)}",
                status=resp.status_code,
                path=TABIAI_CHECKIN_PATH,
            ), None
        if not data.get("success"):
            message = str(data.get("message") or "")
            kind = api.AUTH_FAILED if resp.status_code in (401, 403) else api.FAILED
            return api.ApiResult(kind, message=message or f"HTTP {resp.status_code}",
                                 status=resp.status_code, path=TABIAI_CHECKIN_PATH), None
        payload = data.get("data") if isinstance(data.get("data"), dict) else {}
        stats = payload.get("stats") if isinstance(payload.get("stats"), dict) else {}
        checked = stats.get("checked_in_today")
        return api.ApiResult(api.SUCCESS, status=resp.status_code, path=TABIAI_CHECKIN_PATH), \
            bool(checked) if checked is not None else None

    # ---- 第三步：执行签到 ----

    def do_checkin(self, token: str, turnstile: str) -> api.ApiResult:
        path = TABIAI_CHECKIN_PATH
        if turnstile:
            from urllib.parse import quote

            path = f"{TABIAI_CHECKIN_PATH}?turnstile={quote(turnstile, safe='')}"
        result, resp = self._request("POST", path, token=token)
        if result.kind != api.UNKNOWN:
            return result
        # 业务失败也返回 HTTP 200，必须读 body 的 success
        return api.classify_checkin(
            resp.status_code, self._json(resp), self._text(resp, 160), path=TABIAI_CHECKIN_PATH)

    # ---- 只验证凭据：--cookie-test tabiai 用，零浏览器零 AI ----

    def test_cookie(self) -> api.ApiResult:
        step = self.refresh()
        if not step.result.ok:
            return step.result
        who = step.username or (f"id={step.user_id}" if step.user_id else "")
        suffix = f"（{who}）" if who else ""
        return api.ApiResult(
            api.SUCCESS,
            message=f"TaBiAI 凭据有效{suffix}，未执行签到",
            status=step.result.status,
            path=TABIAI_REFRESH_PATH,
            user_id=step.user_id,
        )

    # ---- 完整签到 ----

    def checkin(self, turnstile_provider=None, dry_run: bool = False) -> api.ApiResult:
        """先 refresh 换 token，再查是否已签，必要时才取 Turnstile token 去签。

        turnstile_provider 是一个 () -> (token, error) 的可调用对象，由上层注入，
        这样本模块不依赖浏览器实现，测试也能直接塞假 token。
        """
        if dry_run:
            return self.test_cookie()

        step = self.refresh()
        if not step.result.ok:
            return step.result
        token = step.access_token

        status_result, checked = self.query_checkin(token)
        if not status_result.ok:
            return status_result
        if checked:
            return api.ApiResult(
                api.ALREADY_DONE,
                message="今日已签到",
                status=status_result.status,
                path=TABIAI_CHECKIN_PATH,
                user_id=step.user_id,
            )

        if turnstile_provider is None:
            return api.ApiResult(
                api.TURNSTILE_REQUIRED,
                message="需要 Turnstile token，但未启用 tabiai 浏览器取 token（配置 tabiai.enabled）",
                path=TABIAI_CHECKIN_PATH,
                user_id=step.user_id,
            )
        turnstile, token_error = turnstile_provider()
        if not turnstile:
            return api.ApiResult(
                api.TURNSTILE_REQUIRED,
                message=token_error or "未取得 Turnstile token",
                path=TABIAI_CHECKIN_PATH,
                user_id=step.user_id,
            )

        result = self.do_checkin(token, turnstile)
        if result.user_id is None:
            result.user_id = step.user_id
        if result.kind == api.SUCCESS and step.username:
            result.message = f"{step.username}: {result.message}"
        return result



