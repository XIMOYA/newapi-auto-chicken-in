"""
newapi_checkin/github_oauth.py
模块：TaBiAI 凭据签发（GitHub OAuth 三步换 new_api_refresh）
职责：
- 用账号里保存的 GitHub user_session 走三步 OAuth，换回一条全新的 new_api_refresh
- 三步共用一个 session（state 与站点会话绑定），全程走 account.proxy，
  保证签发与签到落在同一个出口 IP
- 任何失败都翻译成一句能直接写进汇总邮件的中文原因，绝不抛异常打断签到主循环
移植来源（协议细节的唯一权威，改动前先回去看 Go）：
- server/tabiai.go: issueTabiAIRefreshCookie / resolveGithubClientID /
  extractGithubAuthorizeCode / extractTabiAIFlowToken
- server/cookie_checker.go: extractTabiAIRefreshCookie / setCookieTestCommonHeaders
请求地址：
- POST {base}/api/oauth/state                     取与本会话绑定的 flow_token
- GET  {base}/api/status                          账号未配 client_id 时读 data.github_client_id
- GET  https://github.com/login/oauth/authorize   带 user_session 换授权 code（读 302 的 Location）
- GET  {base}/api/oauth/github?code=&state=       回调换 Set-Cookie: new_api_refresh
"""

from __future__ import annotations

import json
from typing import Any, Optional
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from curl_cffi import requests as cffi

from . import client as api
from . import logger as log
from .cf.session_store import CFSession
from .config import STATUS_PATH, TABIAI_REFRESH_COOKIE_NAME, Account, HttpConfig
from .tabiai import BROWSER_UA
from .utils import sanitize_header_value

OAUTH_STATE_PATH = "/api/oauth/state"
OAUTH_CALLBACK_PATH = "/api/oauth/github"
GITHUB_AUTHORIZE_URL = "https://github.com/login/oauth/authorize"
# 站点自己的 OAuth 应用申请的 scope，照抄 Go
GITHUB_OAUTH_SCOPE = "user:email"

_REDIRECT_STATUSES = frozenset({301, 302, 303, 307, 308})
# 错误文案里嵌远端文本的长度上限，和 Go 的 sanitizeCookieTestMessage 一致
_MESSAGE_LIMIT = 160


def issue_refresh_cookie(account: Account, http: HttpConfig, cf: Optional[CFSession] = None,
                         *, authorize_url: str = GITHUB_AUTHORIZE_URL) -> tuple[str, str]:
    """用账号的 GitHub user_session 签发一条新的 new_api_refresh。

    返回 (cookie, error)：成功时 cookie 非空、error 为空串；失败时 cookie 为空串、
    error 是一句能直接写进汇总邮件的中文原因。绝不抛异常 —— 调用方在签到主循环里，
    异常会打断整轮。

    cookie 形态与 Go 的 extractTabiAIRefreshCookie 一致（`new_api_refresh=<sid>.<secret>`），
    可以直接喂给 tabiai.normalize_refresh_cookie。

    authorize_url 供测试与自建 GHE 注入，对应 Go 同名参数；生产默认走 github.com。
    """
    base = _base_url(account.url)
    if not base:
        return "", "站点 URL 无效：必须是有效的 http(s) 地址"
    user_session = str(getattr(account, "github_user_session", "") or "").strip()
    if not user_session:
        return "", ("该账号未填写 GitHub user_session，无法自动签发；"
                    "请填写后重试，或直接从浏览器复制 new_api_refresh")

    proxy, proxy_error = _resolve_proxy(account)
    if proxy_error:
        return "", proxy_error
    # 代理配错就直接失败：签发换出来的会话必须和签到同一个出口 IP，
    # 悄悄退直连比签发失败更糟 —— 站点会把两个 IP 的会话当成异常
    proxies = {"http": proxy, "https": proxy} if proxy else None

    try:
        session = _build_session(http, cf, proxies)
    except Exception as exc:  # noqa: BLE001 - 构造失败也要变成一句人话
        return "", f"HTTP 客户端配置失败：{_short_error(exc)}"

    log.debug(f"[oauth] 开始签发：出口={'代理' if proxy else '直连'}，"
              f"user_session 长度={len(user_session)}")
    try:
        return _run_flow(session, base, account, cf, user_session, proxies, authorize_url)
    except Exception as exc:  # noqa: BLE001 - 兜底，漏网异常会打断整轮签到
        return "", f"签发流程内部异常：{type(exc).__name__}"
    finally:
        try:
            session.close()
        except Exception:  # noqa: BLE001
            pass


def _run_flow(session: Any, base: str, account: Account, cf: Optional[CFSession],
              user_session: str, proxies: Optional[dict], authorize_url: str) -> tuple[str, str]:
    """三步流程本体。任何一步给出 error 就当场收工，不做重试。"""
    ua = _pick_user_agent(cf)
    site_headers = _site_headers(base, ua, _pick_accept_language(cf))

    state, error = _fetch_flow_token(session, base, site_headers, proxies)
    if error:
        return "", error

    client_id, error = _resolve_client_id(session, base, account, site_headers, proxies)
    if error:
        return "", error

    code, error = _fetch_authorize_code(
        session, authorize_url, client_id, state, user_session, ua, proxies)
    if error:
        return "", error

    cookie, error = _fetch_refresh_cookie(session, base, code, state, site_headers, proxies)
    if error:
        return "", error
    log.debug(f"[oauth] 已签发新的 {TABIAI_REFRESH_COOKIE_NAME}（长度={len(cookie)}）")
    return cookie, ""


# ---- 第 1 步：POST /api/oauth/state 取 flow_token ----


def _fetch_flow_token(session: Any, base: str, headers: dict,
                      proxies: Optional[dict]) -> tuple[str, str]:
    """flow_token 就是后面两步要带的 state，它与本 session 绑定，换 session 即失效。"""
    payload = json.dumps({"provider": "github", "intent": "login"}, separators=(",", ":"))
    step_headers = dict(headers)
    step_headers["Content-Type"] = "application/json"
    step_headers["Cache-Control"] = "no-store"
    try:
        resp = _send(session, "POST", base + OAUTH_STATE_PATH, headers=step_headers,
                     proxies=proxies, data=payload.encode("utf-8"))
    except Exception as exc:  # noqa: BLE001
        return "", f"OAuth state 网络错误：{_short_error(exc)}"

    status = _status(resp)
    data = _json_map(resp)
    if data is None:
        # 非 JSON 基本都是被 CF/WAF 拦在门口，直接把这条线索给出去
        return "", f"OAuth state HTTP {status} 非 JSON 响应（站点可能拦截了当前出口）"
    if not data.get("success"):
        return "", f"取 OAuth state 失败：{_message_or(_message(data), f'HTTP {status}')}"
    state = extract_flow_token(data.get("data"))
    if not state:
        return "", "OAuth state 成功但未返回 flow_token"
    return state, ""


# ---- client_id：账号显式配置优先，其次读 /api/status ----


def _resolve_client_id(session: Any, base: str, account: Account, headers: dict,
                       proxies: Optional[dict]) -> tuple[str, str]:
    """站点自己的 GitHub OAuth 应用 ID。

    绝不内置默认值 —— 用错 client_id 会把授权发给别人的应用，拿回来的 code 站点根本换不动。
    """
    configured = str(getattr(account, "github_client_id", "") or "").strip()
    if configured:
        return configured, ""

    try:
        resp = _send(session, "GET", base + STATUS_PATH, headers=headers, proxies=proxies)
    except Exception as exc:  # noqa: BLE001
        return "", f"站点状态网络错误：{_short_error(exc)}"

    status = _status(resp)
    data = _json_map(resp)
    if data is None or status >= 400:
        return "", f"站点状态 HTTP {status} 非法响应"
    payload = data.get("data")
    client_id = _as_text(payload.get("github_client_id")) if isinstance(payload, dict) else ""
    if not client_id:
        return "", "站点状态未返回 github_client_id，请在账号里手动填写"
    return client_id, ""


# ---- 第 2 步：GET github.com/login/oauth/authorize 换授权 code ----


def _fetch_authorize_code(session: Any, authorize_url: str, client_id: str, state: str,
                          user_session: str, ua: str,
                          proxies: Optional[dict]) -> tuple[str, str]:
    """这一步不跟随重定向：code 就在 302 的 Location 里，跟过去就丢了。"""
    url = _with_query(authorize_url, {
        "client_id": client_id,
        "scope": GITHUB_OAUTH_SCOPE,
        "state": state,
    })
    if not url:
        return "", "GitHub authorize 地址无效"
    try:
        resp = _send(session, "GET", url, headers=_github_headers(user_session, ua),
                     proxies=proxies)
    except Exception as exc:  # noqa: BLE001
        return "", f"GitHub authorize 网络错误：{_short_error(exc)}"
    return extract_authorize_code(_status(resp), _header(resp, "Location"))


# ---- 第 3 步：GET /api/oauth/github 回调换 new_api_refresh ----


def _fetch_refresh_cookie(session: Any, base: str, code: str, state: str, headers: dict,
                          proxies: Optional[dict]) -> tuple[str, str]:
    """必须与第 1 步同一个 session：站点要拿会话里的 state 校验这条回调。"""
    url = base + OAUTH_CALLBACK_PATH + "?" + urlencode({"code": code, "state": state})
    step_headers = dict(headers)
    # Referer 覆盖成 OAuth 回调页：Go 在公共头之后专门又设了一次，照抄
    step_headers["Referer"] = sanitize_header_value(base + "/oauth/github")
    try:
        resp = _send(session, "GET", url, headers=step_headers, proxies=proxies)
    except Exception as exc:  # noqa: BLE001
        return "", f"OAuth 回调网络错误：{_short_error(exc)}"

    status = _status(resp)
    cookie = extract_refresh_cookie(_set_cookie_values(resp))
    if not cookie:
        cookie = _cookie_from_jar(resp, session)
    if cookie:
        return cookie, ""
    # 先看有没有明确的业务失败原因，再退回「站点没下发」这句笼统结论（顺序同 Go）
    data = _json_map(resp)
    if data is not None and not data.get("success"):
        return "", f"OAuth 回调失败：{_message_or(_message(data), f'HTTP {status}')}"
    return "", f"OAuth 回调成功但站点未下发 {TABIAI_REFRESH_COOKIE_NAME}（HTTP {status}）"


# ---- 纯解析：与 Go 同名函数一一对应，便于对着改 ----


def extract_flow_token(value: Any) -> str:
    """站点把 state 放在 data.flow_token；旧结构直接给字符串时也接受。"""
    if isinstance(value, dict):
        for key in ("flow_token", "state"):
            token = _as_text(value.get(key))
            if token:
                return token
        return ""
    return _as_text(value)


def extract_refresh_cookie(set_cookies) -> str:
    """从 Set-Cookie 里取出新一代 new_api_refresh，返回 `name=value` 形态。"""
    for raw in set_cookies or ():
        name, sep, rest = str(raw).partition("=")
        if not sep:
            continue
        name = name.strip()
        value = rest.split(";", 1)[0].strip()
        if name == TABIAI_REFRESH_COOKIE_NAME and value:
            return f"{name}={value}"
    return ""


def extract_authorize_code(status: int, location: str) -> tuple[str, str]:
    """从 authorize 的 302 里取 code，并把常见失败翻译成人话。

    判定顺序完全照 Go 的 extractGithubAuthorizeCode，别调换：
    「Location 指向 /login」这条压在最前面，哪怕状态码不是 3xx 也先认它 —— GitHub 在
    user_session 失效时的响应形态不止一种，但都会把你往登录页赶，而这条恰恰是调用方
    最需要区分出来的（要提示用户重新粘贴 user_session）。
    """
    location = str(location or "")
    if location:
        parsed = _parse_url(location)
        if parsed is not None and "/login" in parsed.path.lower():
            return "", "GitHub 要求重新登录，user_session 已失效"

    if int(status or 0) not in _REDIRECT_STATUSES:
        if int(status or 0) in (403, 429):
            return "", f"GitHub authorize HTTP {status}，当前出口被 GitHub 限制，稍后再试"
        return "", (f"GitHub 未返回授权重定向（HTTP {status}），"
                    "可能需要先在 GitHub 授权该 OAuth 应用")

    parsed = _parse_url(location)
    if parsed is None:
        # urlsplit 极少失败（畸形 IPv6 端口之类），留着这条是为了和 Go 的分支对齐
        return "", "GitHub 返回的跳转地址无法解析"
    query = dict(parse_qsl(parsed.query, keep_blank_values=True))
    code = query.get("code") or ""
    if code:
        return code, ""
    reason = query.get("error_description") or query.get("error") or ""
    if reason:
        return "", f"GitHub 未返回 code：{_sanitize_message(reason)}"
    return "", f"GitHub 未返回授权 code（HTTP {status}）"


# ---- 会话与请求 ----


def _build_session(http: HttpConfig, cf: Optional[CFSession], proxies: Optional[dict]):
    """带 cookiejar 的会话：三步的 state 与站点会话绑定，必须同一个 session 跑完。

    构造方式照 tabiai.TabiAIClient._build_session，唯一区别是这里不预置站点公共头 ——
    第 2 步打的是 github.com，把站点的 Referer / Origin 带过去毫无必要。公共头改成
    逐发显式给（对应 Go 每个请求单独 setCookieTestCommonHeaders 的写法）。
    """
    return cffi.Session(
        impersonate=api.pick_impersonate(http.impersonate, cf.user_agent if cf else ""),
        verify=http.verify,
        timeout=http.timeout,
        proxies=proxies,
    )


def _send(session: Any, method: str, url: str, *, headers: dict,
          proxies: Optional[dict], **kwargs):
    """三步共用的唯一出口：把「走代理」和「不跟随重定向」钉死在这一处。

    代理逐发显式带上，而不是只靠 session 级配置：三步里有一步打的是 github.com，
    任何一步悄悄退直连都会让签发出来的会话和签到不在同一个出口 IP 上。
    allow_redirects=False 同样是硬约束 —— 第 2 步要读 Location 取 code，跟过去就没了。
    """
    return session.request(
        method,
        url,
        headers={key: value for key, value in headers.items() if value},
        proxies=proxies,
        allow_redirects=False,
        **kwargs,
    )


def _site_headers(base: str, ua: str, accept_language: str) -> dict:
    """站点请求公共头，对应 Go 的 setCookieTestCommonHeaders（不含 Cookie，靠 cookiejar）。"""
    return {
        "Accept": "application/json, text/plain, */*",
        "Accept-Language": sanitize_header_value(accept_language),
        "Referer": sanitize_header_value(base + "/"),
        "Origin": sanitize_header_value(base),
        "User-Agent": sanitize_header_value(ua),
    }


def _github_headers(user_session: str, ua: str) -> dict:
    """打 github.com 的头。Cookie 三个键照抄 Go，别精简。

    Go 里 user_session 和 __Host-user_session_same_site 填的是同一个值，再补一个
    logged_in=yes。这看着冗余，但它是生产验证过的写法 —— 存疑记录：真实浏览器里
    __Host- 那个键的值确实与 user_session 相同，GitHub 只校验存在性与一致性；
    少给会被当成未登录会话直接打回 /login。
    """
    session_value = sanitize_header_value(user_session)
    return {
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "User-Agent": sanitize_header_value(ua),
        "Cookie": (f"user_session={session_value}; "
                   f"__Host-user_session_same_site={session_value}; logged_in=yes"),
    }


# ---- 杂项工具 ----


def _pick_user_agent(cf: Optional[CFSession]) -> str:
    """整个流程只用一个 UA。

    Go 三步都用同一个内置 UA；这里允许 cf 缓存里的真实 UA 顶上（同 tabiai 的做法），
    但绝不让站点两步一个 UA、GitHub 那步另一个 —— 同一 session 换 UA 更像机器人。
    """
    return sanitize_header_value((cf.user_agent if cf else "") or BROWSER_UA)


def _pick_accept_language(cf: Optional[CFSession]) -> str:
    return sanitize_header_value(
        (cf.accept_language if cf else "") or api.DEFAULT_ACCEPT_LANGUAGE)


def _base_url(raw: str) -> str:
    """对应 Go 的 cookieTestBaseURL：必须是 http(s) 且有 host，返回去掉尾斜杠的形态。"""
    parsed = _parse_url(str(raw or "").strip())
    if parsed is None or not parsed.netloc or parsed.scheme not in ("http", "https"):
        return ""
    return urlunsplit(parsed).rstrip("/")


def _resolve_proxy(account: Account) -> tuple[str, str]:
    """校验账号代理。写法对齐 Go 的 newCookieTestHTTPClient：缺 scheme 或 host 就算无效。

    没配代理返回空串（正常直连）；配了但写错必须失败，不能退直连。
    """
    raw = str(getattr(account, "proxy", "") or "").strip()
    if not raw:
        return "", ""
    parsed = _parse_url(raw)
    if parsed is None or not parsed.scheme or not parsed.hostname:
        return "", "代理地址无效"
    return raw, ""


def _with_query(raw: str, params: dict) -> str:
    """往 URL 上并入查询参数，保留原有参数（Go 也是 u.Query() 上改）。"""
    parsed = _parse_url(str(raw or "").strip())
    if parsed is None or not parsed.netloc:
        return ""
    query = dict(parse_qsl(parsed.query, keep_blank_values=True))
    query.update(params)
    return urlunsplit((parsed.scheme, parsed.netloc, parsed.path,
                       urlencode(query), parsed.fragment))


def _parse_url(raw: str):
    try:
        return urlsplit(str(raw or ""))
    except ValueError:
        return None


def _status(resp: Any) -> int:
    try:
        return int(getattr(resp, "status_code", 0) or 0)
    except (TypeError, ValueError):
        return 0


def _header(resp: Any, name: str) -> str:
    """取单个响应头。curl_cffi 的 Headers 大小写不敏感，假响应可能是普通 dict，两头都兼容。"""
    try:
        headers = resp.headers
    except Exception:  # noqa: BLE001
        return ""
    for key in (name, name.lower(), name.upper()):
        try:
            value = headers.get(key)
        except Exception:  # noqa: BLE001
            value = None
        if value:
            return str(value)
    try:
        for key, value in headers.items():
            if str(key).lower() == name.lower() and value:
                return str(value)
    except Exception:  # noqa: BLE001
        pass
    return ""


def _set_cookie_values(resp: Any) -> list:
    """把所有 Set-Cookie 头取成列表。多值优先 get_list，退化到单值（同 tabiai 的做法）。"""
    try:
        return [str(item) for item in resp.headers.get_list("Set-Cookie")]
    except Exception:  # noqa: BLE001
        pass
    single = _header(resp, "Set-Cookie")
    return [single] if single else []


def _cookie_from_jar(resp: Any, session: Any) -> str:
    """Set-Cookie 头没取到时的兜底：再去 cookiejar 里看一眼。

    Go 只读响应头，因为 net/http 的 Header.Values 一定拿得到全部。curl_cffi 在个别
    版本里不把多条 Set-Cookie 原样暴露出来，tabiai._extract_rotated 早就为此两头都查，
    这里沿用同一套兜底，避免「签发成功却报没下发」。
    """
    for holder in (resp, session):
        try:
            value = holder.cookies.get(TABIAI_REFRESH_COOKIE_NAME)
        except Exception:  # noqa: BLE001
            value = None
        if value:
            return f"{TABIAI_REFRESH_COOKIE_NAME}={value}"
    return ""


def _json_map(resp: Any) -> Optional[dict]:
    """对应 Go 的 cookieTestJSONMap：只认 JSON 对象，其他一律当「不是 JSON」。"""
    try:
        data = resp.json()
    except Exception:  # noqa: BLE001
        data = None
    if isinstance(data, dict):
        return data
    try:
        data = json.loads(getattr(resp, "text", "") or "")
    except Exception:  # noqa: BLE001
        return None
    return data if isinstance(data, dict) else None


def _message(data: dict) -> str:
    return _message_or(_as_text(data.get("message")), _as_text(data.get("error")))


def _message_or(message: str, fallback: str) -> str:
    return _sanitize_message(message) if str(message).strip() else _sanitize_message(fallback)


def _as_text(value: Any) -> str:
    """对应 Go 的 cookieTestString：字符串去空白，数字转紧凑形态，其他给空串。"""
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, bool):
        return ""
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        return str(int(value)) if value == int(value) else repr(value)
    return ""


def _sanitize_message(value: Any) -> str:
    """清洗要嵌进错误文案的远端文本：压掉换行/控制字符，再按上限截断。

    故意不用 sanitize_header_value —— 那个是给 HTTP 头准备的，会把 ord>255 的字符
    （包括中文）整个剔掉，拿它洗站点返回的中文提示会洗成一串残句。头值仍然只走
    sanitize_header_value。
    """
    text = " ".join(str(value or "").split())
    return text[:_MESSAGE_LIMIT] + "…" if len(text) > _MESSAGE_LIMIT else text


def _short_error(exc: Optional[BaseException]) -> str:
    """网络异常压成一句短话。只放异常类型和 curl 自己的描述，不碰任何凭据。"""
    if exc is None:
        return "未知错误"
    detail = str(exc).strip()
    text = _sanitize_message(f"{type(exc).__name__}: {detail}" if detail else type(exc).__name__)
    return text or "未知错误"
