"""GitHub Cookie 登录与 OAuth 回调签到。

该模块只负责 GitHub OAuth 相关 HTTP 请求，不把 OAuth 语义混入 NewAPI
Cookie 的 ApiClient；Cloudflare 站点响应仍转换成现有 ApiResult，交由 runner
决定是否启动浏览器/AI 过盾和换出口 IP。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Optional
from urllib.parse import parse_qs, urlparse

from curl_cffi import requests as cffi

from . import client as api
from .cf import detect
from .cf.session_store import CFSession
from .config import (
    GITHUB_PROTOCOL_TABI,
    Account,
    HttpConfig,
)
from .utils import sanitize_header_value

GITHUB_AUTHORIZE_URL = "https://github.com/login/oauth/authorize"
GITHUB_AUTHORIZE_PATH = "/login/oauth/authorize"
OAUTH_STATE_PATH = "/api/oauth/state?mode=login"
TABI_OAUTH_STATE_PATH = "/api/oauth/state"
STATUS_PATH = "/api/status"
OAUTH_CALLBACK_PATH = "/api/oauth/github"

BROWSER_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/151.0.0.0 Safari/537.36 Edg/151.0.0.0"
)


@dataclass
class OAuthStep:
    """内部步骤结果，保留响应/数据供后续步骤使用。"""

    result: api.ApiResult
    data: Optional[dict] = None
    response: Any = None


class GithubOAuthClient:
    """一个账号一次 GitHub OAuth 流程的短生命周期客户端。"""

    def __init__(self, account: Account, http: HttpConfig,
                 cf: Optional[CFSession] = None):
        self.account = account
        self.http = http
        self.cf = cf
        self.user_id = account.user_id
        self.impersonate = api.pick_impersonate(http.impersonate, cf.user_agent if cf else "")
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
            "Accept-Language": api.sanitize_header_value(
                self.cf.accept_language if self.cf and self.cf.accept_language
                else api.DEFAULT_ACCEPT_LANGUAGE
            ),
            "Referer": sanitize_header_value(base + "/"),
            "Origin": sanitize_header_value(base),
            "User-Agent": sanitize_header_value(self.cf.user_agent if self.cf else BROWSER_UA),
        }
        if self.cf and self.cf.user_agent:
            headers["User-Agent"] = sanitize_header_value(self.cf.user_agent)
        site_cookies = api.merge_cookies(self.account.cookie, self.cf.cookies if self.cf else None)
        if site_cookies:
            cookie_header = api.build_cookie_header(site_cookies)
            if cookie_header:
                headers["Cookie"] = cookie_header
        session.headers.update({k: v for k, v in headers.items() if v})
        if self.user_id:
            session.headers["New-Api-User"] = str(self.user_id)
        return session

    def close(self) -> None:
        try:
            self._session.close()
        except Exception:  # noqa: BLE001
            pass

    def __enter__(self) -> "GithubOAuthClient":
        return self

    def __exit__(self, *_exc) -> None:
        self.close()

    def _site_request(self, method: str, path: str, **kwargs) -> OAuthStep:
        try:
            resp = self._session.request(method, self.account.api(path), **kwargs)
        except Exception as exc:  # noqa: BLE001
            return OAuthStep(api.ApiResult(
                api.NETWORK_ERROR,
                message=f"{type(exc).__name__}: {exc}"[:240],
                path=path,
            ))
        verdict = detect.analyze_response(resp)
        if verdict.blocked:
            kind = api.WAF_BLOCKED if verdict.challenge == detect.WAF_BLOCK else api.CF_BLOCKED
            return OAuthStep(api.ApiResult(
                kind,
                message=verdict.describe(),
                status=resp.status_code,
                path=path,
                verdict=verdict,
                signals=list(verdict.signals),
            ), response=resp)
        return OAuthStep(api.ApiResult(api.UNKNOWN, status=resp.status_code, path=path), response=resp)

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

    def _decode_json(self, step: OAuthStep, label: str) -> OAuthStep:
        if step.result.kind != api.UNKNOWN:
            return step
        data = self._json(step.response)
        if data is None:
            step.result = api.ApiResult(
                api.UNKNOWN,
                message=(
                    f"{label}返回非 JSON（HTTP {step.response.status_code}）："
                    f"{self._text(step.response)}"
                ),
                status=step.response.status_code,
                path=step.result.path,
            )
            return step
        step.data = data
        step.result = api.ApiResult(
            api.SUCCESS if data.get("success") else api.FAILED,
            message=str(data.get("message") or ""),
            status=step.response.status_code,
            path=step.result.path,
        )
        return step

    def _finish_state_step(self, step: OAuthStep, label: str) -> OAuthStep:
        step = self._decode_json(step, label)
        if step.result.kind != api.SUCCESS:
            if step.result.kind == api.FAILED:
                message = step.result.message or "取 OAuth state 失败"
                if step.result.status in (401, 403) or any(
                    marker in message.lower() for marker in api._AUTH_MARKERS
                ):
                    step.result.kind = api.AUTH_FAILED
                step.result.message = message
            return step
        state = str((step.data or {}).get("data") or "").strip()
        if not state:
            step.result = api.ApiResult(
                api.FAILED,
                message="取 OAuth state 成功但返回为空",
                status=step.result.status,
                path=step.result.path,
            )
            return step
        step.data = {"state": state}
        return step

    def fetch_state(self) -> OAuthStep:
        if self.account.github_protocol == GITHUB_PROTOCOL_TABI:
            step = self._site_request(
                "POST",
                TABI_OAUTH_STATE_PATH,
                json={"provider": "github", "intent": "login"},
                headers={"Content-Type": "application/json"},
            )
            return self._finish_state_step(step, "TaBi OAuth state 接口")
        step = self._site_request("GET", OAUTH_STATE_PATH)
        return self._finish_state_step(step, "OAuth state 接口")

    def fetch_github_client_id(self) -> OAuthStep:
        """TaBi 协议优先使用账号配置，否则从站点状态读取 OAuth Client ID。"""
        configured = str(self.account.github_client_id or "").strip()
        if configured:
            return OAuthStep(
                api.ApiResult(api.SUCCESS, message="使用账号配置的 GitHub Client ID"),
                data={"client_id": configured},
            )
        step = self._site_request("GET", STATUS_PATH)
        step = self._decode_json(step, "站点状态接口")
        if step.result.kind != api.SUCCESS:
            return step
        payload = step.data.get("data") if isinstance(step.data, dict) else None
        client_id = str(payload.get("github_client_id") or "").strip() if isinstance(payload, dict) else ""
        if not client_id:
            step.result = api.ApiResult(
                api.FAILED,
                message="站点状态未返回 github_client_id",
                status=step.result.status,
                path=STATUS_PATH,
            )
            return step
        step.data = {"client_id": client_id}
        return step

    def _github_cookie_header(self) -> str:
        value = sanitize_header_value(self.account.github_user_session)
        return api.build_cookie_header({
            "user_session": value,
            "__Host-user_session_same_site": value,
            "logged_in": "yes",
        })

    def fetch_github_code(self, state: str, client_id: Optional[str] = None) -> OAuthStep:
        if self.account.github_protocol == GITHUB_PROTOCOL_TABI and not client_id:
            client_step = self.fetch_github_client_id()
            if not client_step.result.ok:
                return client_step
            client_id = str((client_step.data or {}).get("client_id") or "").strip()
        client_id = client_id or self.account.effective_github_client_id
        try:
            response = self._session.get(
                GITHUB_AUTHORIZE_URL,
                params={
                    "client_id": client_id,
                    "scope": "user:email",
                    "state": state,
                },
                headers={
                    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                    "Cookie": self._github_cookie_header(),
                    "Referer": sanitize_header_value(self.account.base_url + "/login"),
                },
                allow_redirects=False,
                timeout=self.http.timeout,
            )
        except Exception as exc:  # noqa: BLE001
            return OAuthStep(api.ApiResult(
                api.NETWORK_ERROR,
                message=f"GitHub 请求异常: {type(exc).__name__}: {exc}"[:240],
                path=GITHUB_AUTHORIZE_PATH,
            ))

        location = response.headers.get("Location", "")
        if response.status_code not in (301, 302, 303, 307, 308) or not location:
            message = (
                f"GitHub 未返回授权重定向（HTTP {response.status_code}），"
                "user_session 可能已失效或应用未授权"
            )
            if response.status_code in (401, 403) or "/login" in urlparse(location).path:
                kind = api.AUTH_FAILED
            else:
                kind = api.FAILED
            return OAuthStep(api.ApiResult(kind, message=message,
                                            status=response.status_code,
                                            path=GITHUB_AUTHORIZE_PATH), response=response)

        parsed = urlparse(location)
        if parsed.path.rstrip("/") == "/login" or "/login" in parsed.path:
            return OAuthStep(api.ApiResult(
                api.AUTH_FAILED,
                message="GitHub 要求重新登录，user_session 已失效",
                status=response.status_code,
                path=GITHUB_AUTHORIZE_PATH,
            ), response=response)
        query = parse_qs(parsed.query)
        code = (query.get("code") or [""])[0]
        if not code:
            error = (query.get("error_description") or query.get("error") or ["未知原因"])[0]
            return OAuthStep(api.ApiResult(
                api.AUTH_FAILED,
                message=f"GitHub 未返回 code: {error}",
                status=response.status_code,
                path=GITHUB_AUTHORIZE_PATH,
            ), response=response)
        return OAuthStep(api.ApiResult(
            api.SUCCESS,
            message="已取得 GitHub OAuth code",
            status=response.status_code,
            path=GITHUB_AUTHORIZE_PATH,
        ), data={"code": code}, response=response)

    def oauth_callback(self, code: str, state: str) -> OAuthStep:
        step = self._site_request(
            "GET",
            OAUTH_CALLBACK_PATH,
            params={"code": code, "state": state, "mode": "login"},
            headers={"Referer": sanitize_header_value(self.account.base_url + "/oauth/github")},
        )
        step = self._decode_json(step, "OAuth 回调接口")
        if step.result.kind != api.SUCCESS:
            if step.result.kind == api.FAILED:
                message = step.result.message or "OAuth 回调失败"
                if step.result.status in (401, 403) or any(
                    marker in message.lower() for marker in api._AUTH_MARKERS
                ):
                    step.result.kind = api.AUTH_FAILED
                step.result.message = message
            return step
        payload = step.data or {}
        step.data = payload.get("data") if isinstance(payload.get("data"), dict) else {}
        return step

    def fetch_self(self, user_id: Optional[int] = None) -> api.ApiResult:
        headers = {"Referer": sanitize_header_value(self.account.base_url + "/console/personal")}
        if user_id:
            headers["New-Api-User"] = str(user_id)
        step = self._site_request("GET", api.SELF_PATH, headers=headers)
        if step.result.kind != api.UNKNOWN:
            return step.result
        result = api.classify_self(
            step.response.status_code,
            self._json(step.response),
            self._text(step.response, 160),
        )
        if result.user_id:
            self.user_id = result.user_id
        return result

    def test_cookie(self) -> api.ApiResult:
        """只验证 GitHub Cookie 能否取得 OAuth code，不触发站点回调签到。"""
        state_step = self.fetch_state()
        if not state_step.result.ok:
            return state_step.result
        code_step = self.fetch_github_code((state_step.data or {}).get("state", ""))
        if not code_step.result.ok:
            return code_step.result
        return api.ApiResult(
            api.SUCCESS,
            message="GitHub Cookie 可用（已取得 OAuth code，未执行签到）",
            status=code_step.result.status,
            path=GITHUB_AUTHORIZE_PATH,
        )

    def checkin(self, dry_run: bool = False) -> api.ApiResult:
        if dry_run:
            return self.test_cookie()

        state_step = self.fetch_state()
        if not state_step.result.ok:
            return state_step.result
        state = (state_step.data or {}).get("state", "")

        code_step = self.fetch_github_code(state)
        if not code_step.result.ok:
            return code_step.result
        code = (code_step.data or {}).get("code", "")

        callback = self.oauth_callback(code, state)
        if not callback.result.ok:
            return callback.result
        data = callback.data if isinstance(callback.data, dict) else {}
        user_id = data.get("id") if isinstance(data.get("id"), int) else None
        username = str(data.get("display_name") or data.get("username") or "")
        message = str(data.get("message") or "")
        checked_in = data.get("checked_in") is True
        kind = api.SUCCESS if checked_in else api.ALREADY_DONE
        if not message:
            message = "签到成功，额度已到账" if checked_in else "登录成功，今日已签到"

        latest = self.fetch_self(user_id)
        quota = latest.quota if latest.kind == api.SUCCESS else None
        if latest.kind == api.SUCCESS:
            user_id = latest.user_id or user_id
            username = latest.message or username
        result = api.ApiResult(
            kind,
            message=username + "：" + message if username else message,
            status=callback.result.status,
            path=OAUTH_CALLBACK_PATH,
            user_id=user_id,
            quota=quota,
        )
        return result
