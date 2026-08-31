"""
newapi_checkin/github_login.py
浏览器 GitHub 登录：拿 user_session，供写回平台的 GitHub 账号池。

为什么必须用浏览器：GitHub 登录表单带 timestamp/timestamp_secret 反自动化检测，
还有 required_field_* 蜜罐字段（一填就被判为机器人）；再加上设备验证码与 CAPTCHA
两道分支。纯 HTTP 走不通这条路，所以复用项目里既有的 camoufox/patchright 驱动。

流程与判定分开：所有「看到什么算什么」的判定都是本模块的纯函数（可离线单测），
浏览器交互只负责取现场信息喂给它们。踩过的坑都在判定里：
- 蜜罐字段绝对不碰
- 判成功的唯一信号是 user_session 下发，不是「URL 变了」——
  设备验证码页也不含 /login，按 URL 判会误判成功
- 停用/封禁的账号能登录成功却不该入池，所以登录后要再判一次账号状态
"""

from __future__ import annotations

import random
import re
from dataclasses import dataclass

# 登录表单选择器。required_field_* 是蜜罐，这里刻意不列 —— 填了等于自证机器人
LOGIN_FIELD = "#login_field"
PASSWORD_FIELD = "#password"
SUBMIT_BUTTON = ".js-sign-in-button"
OTP_FIELD = "#otp"

# Set-Cookie 里抓 user_session
_USER_SESSION_RE = re.compile(r"user_session=([^;]+)")

# 登录后的落地分流关键词
_DEVICE_VERIFY_MARKERS = ("/sessions/verified-device", "/sessions/two-factor")
_CAPTCHA_SELECTORS = ("iframe[title*='challenge' i]", ".octospider")

# 账号状态特征（与 Go 侧 github_status.go 的口径保持一致）
_SUSPENDED_MARKERS = ("account has been suspended", "account is suspended",
                      "this account has been suspended", "账号已被暂停")
_BANNED_MARKERS = ("account has been terminated", "account was disabled",
                   "account has been disabled", "permanently suspended")


class LoginError(Exception):
    """登录失败的基类。"""


class CredentialError(LoginError):
    """账号或密码错误 —— 终态，换 IP 重试无用。"""


class CaptchaError(LoginError):
    """撞上 CAPTCHA 且无法处理（无头模式或人工超时）—— 终态。"""


class AccountUnusableError(LoginError):
    """账号被停用/封禁 —— 登录本身可能成功，但不该入池。"""


class NetworkError(LoginError):
    """网络/代理类失败 —— 值得换出口重试。"""


@dataclass
class LoginStage:
    """一次「看现场」的判定结果。

    kind 取值：success / device_code / captcha / credential_error / pending
    pending 表示还看不出结论，调用方继续等。
    """

    kind: str
    session: str = ""
    detail: str = ""


def extract_user_session(set_cookie_values: list[str]) -> str:
    """从若干 Set-Cookie 头里取 user_session 的值；没有返回空串。

    要过滤登出占位值：GitHub 登出时会下发 user_session=deleted 之类，
    当成成功会写一条永远用不了的凭据进池子。
    """
    for raw in set_cookie_values or []:
        match = _USER_SESSION_RE.search(raw or "")
        if not match:
            continue
        value = match.group(1).strip()
        if value and value.lower() != "deleted" and len(value) > 10:
            return value
    return ""


def classify_account_page(body: str) -> str:
    """按页面正文判断账号状态：active / suspended / banned。

    与 Go 侧 github_status.go 同一套特征词 —— 两边判定漂了会出现
    「客户端说能用、平台说封了」这种最难查的不一致。
    """
    lower = (body or "").lower()
    for marker in _BANNED_MARKERS:
        if marker in lower:
            return "banned"
    for marker in _SUSPENDED_MARKERS:
        if marker in lower:
            return "suspended"
    return "active"


def classify_login_stage(*, url: str, session: str, has_flash_error: bool,
                         has_captcha: bool, has_otp_field: bool) -> LoginStage:
    """把登录后的现场判成一个阶段。

    顺序是关键，写反了就会误判：
    1. session 已下发 → 成功。这是唯一可靠的成功信号
    2. 设备验证码页 → 必须排在「凭据错误」之前判：verified-device 页也是
       github.com 域、URL 里不含 /login，但它有自己的表单
    3. 回到 /login 且有错误横幅 → 凭据错误（终态）
    4. CAPTCHA
    5. 其余 → pending，继续等
    """
    if session:
        return LoginStage("success", session=session)
    lowered = (url or "").lower()
    if has_otp_field or any(marker in lowered for marker in _DEVICE_VERIFY_MARKERS):
        return LoginStage("device_code", detail="需要设备验证码")
    if "/login" in lowered and has_flash_error:
        return LoginStage("credential_error", detail="GitHub 拒绝了账号或密码")
    if has_captcha:
        return LoginStage("captcha", detail="撞上人机验证")
    return LoginStage("pending")


def human_delays(text: str, low: int = 60, high: int = 180) -> list[int]:
    """给逐字符输入生成随机停顿（毫秒）。

    GitHub 表单带 timestamp 检测，fill() 瞬间填完是明显的机器特征。
    抽成函数只为让调用方能注入固定序列做可重复测试。
    """
    return [random.randint(low, high) for _ in range(len(text or ""))]


def type_like_human(page, selector: str, text: str,
                    delays: list[int] | None = None) -> None:
    """逐字符输入。聚焦失败时逐级退化，不因为点不上就放弃整次登录。

    慢链路下 click 的 actionability 命中测试会死等到超时，所以给短超时后
    退化到 focus()（纯 DOM 聚焦，不做命中测试）。
    """
    plan = delays if delays is not None else human_delays(text)
    try:
        page.click(selector, timeout=6000)
    except Exception:
        try:
            page.focus(selector)
        except Exception:
            pass  # page.type 自带可编辑等待，聚焦不上也继续
    for char, delay in zip(text, plan):
        page.type(selector, char, delay=delay)


def read_stage(page, session_holder: dict) -> LoginStage:
    """从当前页面读现场并判定阶段。异常一律当 pending，交给外层超时收口。"""
    try:
        url = page.url or ""
    except Exception:
        return LoginStage("pending")

    def _count(selector: str) -> int:
        try:
            return page.locator(selector).count()
        except Exception:
            return 0

    return classify_login_stage(
        url=url,
        session=session_holder.get("value", ""),
        has_flash_error=_count(".flash-error") > 0,
        has_captcha=any(_count(sel) > 0 for sel in _CAPTCHA_SELECTORS),
        has_otp_field=_count(OTP_FIELD) > 0,
    )


def attach_session_grabber(page, holder: dict):
    """挂上 response 监听，抓 github.com 下发的 user_session。

    事件驱动比轮询 cookie jar 快，也不受页面是否加载完影响。
    返回注销用的回调。
    """

    def _on_response(resp):
        try:
            if "github.com" not in (getattr(resp, "url", "") or ""):
                return
            values = resp.header_values("set-cookie") or []
            value = extract_user_session(list(values))
            if value:
                holder["value"] = value
        except Exception:
            pass

    page.on("response", _on_response)
    return _on_response


def fill_device_code(page, code: str) -> None:
    """填设备验证码并提交。

    点击提交会触发导航，click 因跳转超时属于正常现象，绝不能当失败 ——
    误判会把已经登录成功的会话丢掉。成功与否交回主循环按 session 判定。
    """
    page.fill(OTP_FIELD, code)
    for selector in ("button[type='submit']", "form button.btn-primary", "form button"):
        try:
            locator = page.locator(selector)
            if locator.count() > 0:
                try:
                    locator.first.click(timeout=4000)
                except Exception:
                    pass  # 跳转导致的超时
                return
        except Exception:
            continue
    try:
        page.press(OTP_FIELD, "Enter")
    except Exception:
        pass
