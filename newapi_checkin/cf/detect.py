"""Cloudflare 拦截识别。

比关键词匹配可靠得多的多信号判定：状态码 + 响应头 + 正文特征三路交叉，
并区分「可过的质询」和「硬封禁」——后者重试无意义，必须明确报错。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping, Optional

# 挑战类型
MANAGED_CHALLENGE = "managed_challenge"
TURNSTILE = "turnstile"
JS_CHALLENGE = "js_challenge"
WAF_BLOCK = "waf_block"
LOGIN_REQUIRED = "login_required"

CHALLENGE_LABEL = {
    MANAGED_CHALLENGE: "Managed Challenge（托管质询）",
    TURNSTILE: "Turnstile 组件",
    JS_CHALLENGE: "JS 质询（Checking your browser）",
    WAF_BLOCK: "WAF 硬封禁 / 限流",
    LOGIN_REQUIRED: "浏览器未登录",
}

# 正文特征：质询类（可过）。
# 只收录「只可能出现在真正质询页」的特征——任何正常业务页面也会出现的东西
# 一律放到 _WEAK_CF_MARKERS，否则过盾成功后 DOM 仍被判为质询，流程会死等到超时。
_CHALLENGE_MARKERS = (
    "__cf_chl",
    "cf_chl_opt",
    "_cf_chl_opt",
    "cf-please-wait",
    "just a moment",
    "enable javascript and cookies to continue",
    "challenge-running",
    "challenge-stage",
    'id="challenge-form"',
    "id='challenge-form'",
)
# 弱信号：Cloudflare 开启 Bot Fight / JS 检测后，会把
# /cdn-cgi/challenge-platform/h/b/scripts/jsd/main.js 注入到**已经通过**的
# 正常业务页面里。单独命中它只说明 CF 在链路上，必须配合质询状态码才算被拦。
_WEAK_CF_MARKERS = ("/cdn-cgi/challenge-platform",)
_JS_MARKERS = (
    "checking your browser",
    "checking if the site connection is secure",
    "浏览器检查",
)
_TURNSTILE_MARKERS = (
    "challenges.cloudflare.com/turnstile",
    "cf-turnstile",
    "turnstile/v0/api.js",
)
# 正文特征：硬封禁类（过不去）
_WAF_MARKERS = (
    "attention required",
    "error 1020",
    "access denied",
    "you have been blocked",
    "sorry, you have been blocked",
    "error code: 1015",
    "rate limited",
)
# 只看 DOM（没有状态码佐证）时用的强封禁特征：
# "access denied" / "rate limited" / "attention required" 这类短语在业务站点的
# i18n 文案里很常见，不能凭它判定被封。
_STRONG_WAF_MARKERS = (
    "you have been blocked",
    "sorry, you have been blocked",
    "error 1020",
    "error code: 1015",
    "cf-error-details",
)
# 说明这是 Cloudflare 在响应
_CF_HINTS = ("cloudflare", "cf-ray", "cdn-cgi")

CHALLENGE_STATUSES = (403, 429, 503)


@dataclass
class Verdict:
    blocked: bool = False
    challenge: Optional[str] = None
    signals: list = field(default_factory=list)

    @property
    def recoverable(self) -> bool:
        """能否靠浏览器过盾解决。WAF 硬封禁不行。"""
        return self.blocked and self.challenge != WAF_BLOCK

    @property
    def label(self) -> str:
        if not self.blocked:
            return "未被拦截"
        return CHALLENGE_LABEL.get(self.challenge or "", self.challenge or "未知质询")

    def describe(self) -> str:
        if not self.signals:
            return self.label
        return f"{self.label} [信号: {', '.join(self.signals)}]"


def _norm_headers(headers: Any) -> dict:
    if not headers:
        return {}
    try:
        items = headers.items()
    except AttributeError:
        items = headers
    out = {}
    for key, value in items:
        try:
            out[str(key).lower()] = str(value)
        except Exception:  # noqa: BLE001 - headers 来源不可控，坏值直接跳过
            continue
    return out


def _to_text(body: Any, limit: int = 200_000) -> str:
    if body is None:
        return ""
    if isinstance(body, bytes):
        try:
            body = body.decode("utf-8", errors="ignore")
        except Exception:  # noqa: BLE001
            return ""
    if not isinstance(body, str):
        try:
            body = str(body)
        except Exception:  # noqa: BLE001
            return ""
    return body[:limit].lower()


def _hits(text: str, markers) -> list:
    return [m for m in markers if m in text]


def analyze(status: int, headers: Any = None, body: Any = None) -> Verdict:
    """三路交叉判定是否被 Cloudflare 拦截，以及属于哪种质询。"""
    head = _norm_headers(headers)
    text = _to_text(body)
    signals: list = []

    mitigated = head.get("cf-mitigated", "").strip().lower()
    server = head.get("server", "").lower()

    is_cf = False
    if mitigated:
        is_cf = True
        signals.append(f"cf-mitigated={mitigated}")
    if "cloudflare" in server:
        is_cf = True
        signals.append("server=cloudflare")
    if "cf-ray" in head:
        is_cf = True
        signals.append("cf-ray")

    challenge_hits = _hits(text, _CHALLENGE_MARKERS)
    weak_hits = _hits(text, _WEAK_CF_MARKERS)
    js_hits = _hits(text, _JS_MARKERS)
    ts_hits = _hits(text, _TURNSTILE_MARKERS)
    waf_hits = _hits(text, _WAF_MARKERS)

    for name, hits in (
        ("质询页特征", challenge_hits),
        ("CF脚本注入", weak_hits),
        ("JS质询特征", js_hits),
        ("Turnstile特征", ts_hits),
        ("封禁特征", waf_hits),
    ):
        if hits:
            is_cf = True
            signals.append(f"{name}({hits[0]})")

    if not is_cf:
        return Verdict(False, None, signals)

    signals.insert(0, f"HTTP {status}")
    body_challenge = bool(challenge_hits or js_hits)

    # 判定是否真的被拦：CF 在链路上 != 被拦
    if mitigated and mitigated != "none":
        blocked = True
    elif status in CHALLENGE_STATUSES and (
        body_challenge or weak_hits or waf_hits or not text.strip()
    ):
        blocked = True
    elif body_challenge:
        # CF 有时用 200 直接回质询页（但仅凭注入的 JS 检测脚本不算）
        blocked = True
    else:
        blocked = False

    if not blocked:
        return Verdict(False, None, signals)

    if waf_hits and not body_challenge:
        challenge = WAF_BLOCK
    elif status == 429 and not body_challenge:
        challenge = WAF_BLOCK
    elif ts_hits:
        challenge = TURNSTILE
    elif js_hits:
        challenge = JS_CHALLENGE
    else:
        challenge = MANAGED_CHALLENGE

    return Verdict(True, challenge, signals)


def analyze_response(resp: Any) -> Verdict:
    """直接吃 curl_cffi / requests 风格的响应对象。"""
    status = getattr(resp, "status_code", 0) or 0
    headers = getattr(resp, "headers", None)
    try:
        body = resp.text
    except Exception:  # noqa: BLE001 - 二进制响应等异常情况
        body = getattr(resp, "content", b"")
    return analyze(status, headers, body)


_TITLE_MARKERS = ("just a moment", "attention required", "checking your browser", "请稍候")
_LOGIN_PATH_MARKERS = ("/sign-in", "/signin", "/login", "/auth/login")
_LOGIN_TEXT_MARKERS = (
    "用户名或电子邮件",
    "输入密码",
    "forgot password",
    "sign in",
    "登录",
)


def page_challenge_type(html: Any, title: str = "", url: str = "") -> Optional[str]:
    """浏览器上下文用：看当前 DOM 停在哪种质询上，None 表示正常业务页面。

    这里没有状态码可以佐证，所以只认强特征：CF 注入到正常页面里的 JS 检测脚本
    和业务站点 i18n 里的 "access denied" 之类文案都不能算被拦。
    """
    text = _to_text(html)
    low_title = (title or "").strip().lower()
    low_url = (url or "").strip().lower()

    if any(marker in low_url for marker in _LOGIN_PATH_MARKERS):
        return LOGIN_REQUIRED
    has_password = "type=\"password\"" in text or "type='password'" in text
    has_login_text = any(marker in text for marker in _LOGIN_TEXT_MARKERS)
    if has_password and has_login_text:
        return LOGIN_REQUIRED

    if any(m in low_title for m in _TITLE_MARKERS):
        if _hits(text, _STRONG_WAF_MARKERS) and not _hits(text, _CHALLENGE_MARKERS):
            return WAF_BLOCK
        if _hits(text, _TURNSTILE_MARKERS):
            return TURNSTILE
        return MANAGED_CHALLENGE

    if _hits(text, _CHALLENGE_MARKERS):
        return TURNSTILE if _hits(text, _TURNSTILE_MARKERS) else MANAGED_CHALLENGE
    if _hits(text, _JS_MARKERS):
        return JS_CHALLENGE
    if _hits(text, _STRONG_WAF_MARKERS):
        return WAF_BLOCK
    return None


def looks_like_challenge_page(html: Any, title: str = "") -> bool:
    return page_challenge_type(html, title) is not None


# --------------------------------------------------------------------------- #
# 轻量探针：把「整页 HTML 拉回 Python 再扫」换成「在页面里扫，只回传命中结果」
# --------------------------------------------------------------------------- #


def probe_markers() -> dict:
    """交给浏览器端脚本使用的标记表。与 page_challenge_type 共用同一份常量。"""
    return {
        "challenge": list(_CHALLENGE_MARKERS),
        "js": list(_JS_MARKERS),
        "turnstile": list(_TURNSTILE_MARKERS),
        "waf": list(_STRONG_WAF_MARKERS),
        "login": list(_LOGIN_TEXT_MARKERS),
    }


def classify_probe(probe: Any) -> Optional[str]:
    """按浏览器端探针返回的命中结果判定质询类型，语义与 page_challenge_type 一致。"""
    if not isinstance(probe, Mapping):
        raise ValueError("probe 必须是字典")
    low_url = str(probe.get("url") or "").strip().lower()
    if any(marker in low_url for marker in _LOGIN_PATH_MARKERS):
        return LOGIN_REQUIRED
    if bool(probe.get("password")) and bool(probe.get("login")):
        return LOGIN_REQUIRED

    challenge_hit = bool(probe.get("challenge"))
    js_hit = bool(probe.get("js"))
    ts_hit = bool(probe.get("turnstile"))
    waf_hit = bool(probe.get("waf"))

    low_title = str(probe.get("title") or "").strip().lower()
    if any(marker in low_title for marker in _TITLE_MARKERS):
        if waf_hit and not challenge_hit:
            return WAF_BLOCK
        if ts_hit:
            return TURNSTILE
        return MANAGED_CHALLENGE

    if challenge_hit:
        return TURNSTILE if ts_hit else MANAGED_CHALLENGE
    if js_hit:
        return JS_CHALLENGE
    if waf_hit:
        return WAF_BLOCK
    return None
