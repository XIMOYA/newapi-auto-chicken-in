"""TaBiAI（New API 分支）签到链路。

协议依据 docs/签到原理.md（2026-08-17 实测），要点：

1. 凭据是 `new_api_refresh=<sid>.<secret>` cookie，Path=/api/user/auth，HttpOnly
2. 业务接口只认 `Authorization: Bearer <JWT>`，JWT 由 POST /api/user/auth/refresh 换取，约 300 秒
3. **refresh 有轮转 + 重放检测**：每次成功都会下发下一代 secret。旧代超出宽限窗口后再用
   会被判重放，整条会话直接撤销（401 AUTH_SESSION_REVOKED）。实测（2026-08 对 tabitoken.cc）
   窗口只有 20~45 秒：放 20 秒重放仍幂等成功，放 45 秒直接 REVOKED，**没有 AUTH_UNAUTHORIZED
   这个温和中间态**。所以每次成功后必须立刻把新值落盘并回写平台，而超时后的重试必须受
   悬空预算约束 —— 见 REFRESH_TIMEOUT_SECONDS / INFLIGHT_BUDGET_SECONDS

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
    SELF_PATH,
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

# refresh 专用超时，故意不复用 http.timeout（默认 20 秒）。
#
# 实测旧代重放的安全窗口下界就是 20 秒，一次 20 秒的超时正好把窗口烧光，之后无论怎么重试
# 都是拿整条会话赌运气。8 秒的取舍：一次超时 + 一次重试累计 16 秒，仍落在已证实安全的
# 20 秒内。代价是慢站点更容易超时，但那属于「这一轮没刷成」，比撤销会话轻得多。
REFRESH_TIMEOUT_SECONDS = 8
# 连接阶段单独给一个更短的上限。死代理占了代理池相当一部分，让它们尽快暴露，把 8 秒的
# 整体预算留给真正在等站点回答的那段 —— 连不上的代理干等 8 秒纯属浪费悬空预算。
REFRESH_CONNECT_TIMEOUT_SECONDS = 4
# 代次悬空预算：这一代从第一次送进 refresh 起，超过这个时长就绝不再用。
# 窗口下界 20 秒，留 5 秒安全边际给发起请求本身的开销。
INFLIGHT_BUDGET_SECONDS = 15

# 「请求从未发出」的 curl 错误码。这些全都发生在把请求字节写出去之前，站点侧代次绝无
# 可能推进，所以悬空账要当场销掉、也允许照常换 IP 重试。
#
# 这个区分不是锦上添花：第一版一刀切把所有网络错误都当成「可能已发出」，上线后代理池里
# 的死地址把签到打瘫了 —— 8 秒连接超时 × 换两次 IP 就累计超过 15 秒预算，账号连站点都
# 没碰到就被闸门拦死。判据必须落在「字节有没有可能已经出去」上。
_NEVER_SENT_CURL_CODES = frozenset({
    5,   # COULDNT_RESOLVE_PROXY：代理域名都没解析出来
    6,   # COULDNT_RESOLVE_HOST：目标域名没解析出来
    7,   # COULDNT_CONNECT：TCP 握手就失败（连接被拒/不可达）
    35,  # SSL_CONNECT_ERROR：TLS 握手阶段失败，请求还没发
    97,  # PROXY：代理隧道没建起来
})
# curl 28 两种含义都用同一个码，只能靠消息区分：
#   "Connection timed out after N milliseconds"           -> 连接阶段，安全
#   "Operation timed out after N ms with M bytes received" -> 请求已发出，危险
_CURL_OPERATION_TIMEDOUT = 28
_CONNECT_TIMEOUT_MARKER = "connection timed out"


def refresh_never_sent(exc: Optional[BaseException]) -> bool:
    """判断这个网络异常是否发生在「请求字节写出之前」。

    返回 True 表示 refresh 从未到达站点、代次一定没动，可以安全销账并重试。

    判断从严：拿不准一律当成「可能已发出」。猜错成安全的代价是整条会话被撤销、所有
    账号重新签发；猜错成危险只是这一轮少刷一次。
    """
    if exc is None:
        return False
    try:
        code = int(getattr(exc, "code", 0) or 0)
    except (TypeError, ValueError):
        code = 0
    if code in _NEVER_SENT_CURL_CODES:
        return True
    if code == _CURL_OPERATION_TIMEDOUT:
        return _CONNECT_TIMEOUT_MARKER in str(exc).lower()
    return False


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

    on_inflight / on_settled 是代次悬空记账的两端：refresh 请求发出前调 on_inflight，
    拿到响应（哪怕是 4xx）后调 on_settled。超时时 on_settled 不会被调用，悬空标记就留在
    库里 —— 上层据此拒绝在预算外重试。同样只做回调，不碰持久化。
    """

    def __init__(self, account: Account, http: HttpConfig, cookie: str,
                 cf: Optional[CFSession] = None, on_rotate=None,
                 on_inflight=None, on_settled=None):
        self.account = account
        self.http = http
        self.cf = cf
        self.on_rotate = on_rotate
        self.on_inflight = on_inflight
        self.on_settled = on_settled
        self._cookie = normalize_refresh_cookie(cookie)
        self.user_id = account.user_id
        self.impersonate = api.pick_impersonate(http.impersonate, cf.user_agent if cf else "")
        # 最近一次请求的底层异常。_request 把异常吞成字符串了，但 refresh 要靠 curl
        # 错误码判断「请求到底有没有发出去」，所以原始异常必须留一份
        self._last_error: Optional[BaseException] = None
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
        self._last_error = None
        try:
            resp = self._session.request(method, self.account.api(path), headers=headers, **kwargs)
        except Exception as exc:  # noqa: BLE001
            self._last_error = exc      # 阶段判断要用 curl 错误码，字符串留不住它
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
        # 发出前记账：这一代即刻进入「站点可能已推进代次、我们还不知道」的悬空态。
        # 必须写在请求之前 —— 超时的那一次恰恰是最需要记账的，事后补写来不及。
        if self.on_inflight is not None:
            self.on_inflight(self._cookie)
        # 短超时压住悬空时长，见 REFRESH_TIMEOUT_SECONDS 的说明。
        # 元组是 (连接上限, 整体上限)：死代理 4 秒暴露，剩下的预算留给等站点回答
        result, resp = self._request(
            "POST", TABIAI_REFRESH_PATH, cookie=self._cookie,
            timeout=(REFRESH_CONNECT_TIMEOUT_SECONDS, REFRESH_TIMEOUT_SECONDS))
        if self.on_settled is not None:
            # 两种情况都该销账，但理由不同：
            #   拿到响应   -> 站点侧状态已确定（哪怕是 401），悬空态自然结束
            #   从未发出   -> 连接都没建起来，代次一定没动，这笔账压根不该存在
            # 只有「发出去了但不知道结果」才保留标记，让上层拒绝预算外的重试。
            if resp is not None or refresh_never_sent(self._last_error):
                self.on_settled()
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

    # ---- 查账户信息：只为拿剩余额度 ----

    def fetch_self(self, token: str) -> api.ApiResult:
        """GET /api/user/self，用它的 data.quota 当账户剩余额度。

        和签到走同一个 session、同一个 Bearer，凭据和盾都已经就绪，所以这一发很轻。
        """
        result, resp = self._request("GET", SELF_PATH, token=token)
        if result.kind != api.UNKNOWN:
            return result
        return api.classify_self(resp.status_code, self._json(resp), self._text(resp, 160))

    def fetch_quota_per_unit(self) -> Optional[int]:
        """GET /api/status 取额度换算率。公开接口，不带 token 也能读。

        探不到返回 None，由上层退回默认值。只影响金额怎么显示，不影响签到。
        """
        result, resp = self._request("GET", api.STATUS_PATH)
        if result.kind != api.UNKNOWN or resp is None:
            return None
        return api.extract_quota_per_unit(self._json(resp))

    def attach_balance(self, result: api.ApiResult, token: str) -> None:
        """给已经有结论的签到结果补上账户余额。

        任何失败都只 debug 一行就算了：余额是邮件里多一列信息，绝不能让它改变
        签到结论，更不能因为它把一个已经签成功的账号弄成失败。
        """
        try:
            me = self.fetch_self(token)
        except Exception as exc:  # noqa: BLE001 - 补充信息失败不许影响签到
            log.debug(f"查余额异常，跳过: {type(exc).__name__}: {exc}")
            return
        if me.balance is None:
            log.debug(f"未能取到剩余额度（{me.kind}: {me.message}）")
            return
        result.balance = me.balance

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
            already = api.ApiResult(
                api.ALREADY_DONE,
                message="今日已签到",
                status=status_result.status,
                path=TABIAI_CHECKIN_PATH,
                user_id=step.user_id,
            )
            # 已签到也要报余额：这正是老版本额度列空着的主因
            self.attach_balance(already, token)
            return already

        if turnstile_provider is None:
            return api.ApiResult(
                api.TURNSTILE_REQUIRED,
                message="需要 Turnstile token，未配置 CDP 接管真实 Chrome（tabiai.enabled），"
                        "将改由脚本浏览器过盾链代取",
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
        if result.kind in (api.SUCCESS, api.ALREADY_DONE):
            self.attach_balance(result, token)
        return result



