"""
newapi_checkin/github_provision.py
GitHub 账号自动填入编排：ReMail 定位邮箱取码 → 浏览器登录 → 状态过滤 → 写回平台池子。

把三块地基串成一条流水线，本模块自己不发明协议：
- 取码走 remail.Remail（搜订单定位邮箱、轮询取件）
- 登录判定与表单交互走 github_login 的纯函数（蜜罐、判定顺序那些坑都在那边守着）
- 写回走平台 POST /api/github-accounts/ops（契约见 docs/github-accounts-api.md §3）

为什么写回必须走 ops 而不是改本地 config.json：Python 侧的 Config 压根没有
github_accounts 这一节 —— 池子只存在平台库里，客户端是读取方。而整份 PUT /api/config
会用陈旧快照覆盖平台刚轮转的凭据（契约 §3 开头那句警告就是为此），所以只能用
「只描述这一次要做什么」的 ops。端点地址按 config_sync.url 同源推导，与 remote_sync 里
签发、读回核实同一套惯例。

状态过滤为什么不能省：被停用/封禁的 GitHub 账号照样能登录成功、照样下发 user_session，
入池后每轮签发都失败，白占一个名额还多打几次 GitHub。所以判定放在写回之前，
且判不出结论时**不入池** —— 丢掉一个待入池账号只是浪费一次登录，放进一个坏账号是
每轮都要付的账。

全程串行：三条外部链路（ReMail、GitHub、平台）都对固定出口有限流，并发只会更快撞墙。
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Callable, Optional
from urllib.parse import urlsplit, urlunsplit

from curl_cffi import requests as cffi

from . import logger as log
from . import remote_sync
from .config import (
    Account,
    Config,
    ConfigError,
    ConfigSyncConfig,
    GitHubProvisionAccount,
    GitHubProvisionConfig,
)
from .github_login import (
    LOGIN_FIELD,
    PASSWORD_FIELD,
    SUBMIT_BUTTON,
    CaptchaError,
    CredentialError,
    LoginError,
    attach_session_grabber,
    classify_account_page,
    fill_device_code,
    read_stage,
    type_like_human,
)
from .remail import EmailHit, Remail, RemailError

# GitHub 登录页。路径不是猜的：github_login.classify_login_stage 判凭据错误的依据就是
# 「回到 /login 且有 .flash-error」，判定函数与这个入口地址是一对
GITHUB_LOGIN_URL = "https://github.com/login"
# 账号状态探测页。与 Go 侧 server/github_status.go 的 githubProfileURL 同一个地址 ——
# 首页对未登录用户也返回 200，据此判不出登录态
GITHUB_PROFILE_URL = "https://github.com/settings/profile"

# 平台端点。前者写回池子，后者是本地判不出状态时的复核
POOL_OPS_PATH = "/api/github-accounts/ops"
POOL_STATUS_PATH = "/api/github-accounts/status"

# 账号状态取值。刻意与 Go 侧 github_status.go 的五态同名同义，
# 两边漂了就会出现「客户端说能用、平台说封了」这种最难查的不一致
STATUS_ACTIVE = "active"
STATUS_SUSPENDED = "suspended"
STATUS_BANNED = "banned"
STATUS_EXPIRED = "expired"
STATUS_UNKNOWN = "unknown"

# 登录态特征词，抄自 Go 侧 githubLoggedInMarkers。取并集是为了单个 DOM 改版
# 不至于让整条判定失效
_LOGGED_IN_MARKERS = ("logged-in", "user-session", "sign out", "退出登录",
                      "公共资料", "public profile", "settings/profile")
# 未登录跳转的落地特征：GitHub 会把没有会话的请求打回登录页
_SIGNED_OUT_URL_MARKERS = ("/login", "/session")
# 正文短于这个长度就当「什么都没看到」。空正文喂给 classify_account_page 会返回
# active（没有停用特征词嘛），那是最危险的假 active —— 页面根本没加载出来
_PROFILE_BODY_MIN = 200

# 一个账号最多填几次设备验证码。GitHub 不会自动补发新码，第二次多半会以
# 「取件 N 次仍未收到」收场 —— 那正是我们要的收口，比无限重填同一个码好
MAX_CODE_ATTEMPTS = 2

# 每轮看现场的间隔。太密只是白刷 DOM，太疏会让「已经跳转成功」晚发现
STAGE_POLL_SECONDS = 1.5

# 结果状态。写成常量是因为调用方（CLI/GUI/报告）要按它分流
RESULT_PROVISIONED = "provisioned"          # 已写回池子
RESULT_REJECTED = "rejected"                # 状态过滤拦下，没入池
RESULT_LOGIN_FAILED = "login_failed"        # 登录没拿到 user_session
RESULT_NO_MAILBOX = "no_mailbox"            # ReMail 里找不到这个账号的收件箱
RESULT_WRITEBACK_FAILED = "writeback_failed"  # 登录与状态都过了，平台没收下
RESULT_CONFIG_ERROR = "config_error"        # 缺配置，压根没开始


@dataclass
class ProvisionResult:
    """一个账号跑完这条流水线的结论。

    刻意不带 user_session：这个对象会进日志、进汇总报告，凭据只在函数内部流转，
    这里只留长度供排查「是不是拿到了个空串」。
    """

    username: str
    status: str
    account_status: str = ""
    detail: str = ""
    session_len: int = 0
    email: str = ""

    @property
    def ok(self) -> bool:
        return self.status == RESULT_PROVISIONED


@dataclass
class ProvisionReport:
    """整批的结论。"""

    results: list = field(default_factory=list)

    @property
    def provisioned(self) -> int:
        return sum(1 for r in self.results if r.ok)

    @property
    def failed(self) -> int:
        return sum(1 for r in self.results if not r.ok)

    @property
    def ok(self) -> bool:
        """一条都没成功才算整批失败；部分成功仍然是有产出的一轮。"""
        return bool(self.results) and self.provisioned > 0


# --------------------------------------------------------------------------- #
# 状态判定（纯函数，离线可测）
# --------------------------------------------------------------------------- #


def classify_profile_page(url: str, html: str) -> tuple[str, str]:
    """按 settings/profile 页的落地 URL 与正文判账号状态。

    分流顺序照抄 Go 侧 classifyGitHubProfileResponse，写反任何一步都会误判：
    1. 封禁 / 停用特征词优先 —— 这类账号可能仍返回 200，只是正文换成提示页
    2. 落到 /login 或 /session → session 没了（账号本身状态未知）
    3. 正文根本没读到（空 / 太短）→ unknown
    4. 正文有登录态特征 → active
    5. 其余 → unknown

    最后一条是刻意保守：GitHub 改版、页面没加载完、被限流都会走到这里，
    当成「账号有问题」会把好账号拦在池子外；而它也不会被当成可用 —— 入池的唯一
    依据是 active。

    第 3 条必须排在登录态判定之前：GitHub 的 <html> 标签本身就带 class="logged-in"，
    正文还没到时它已经渲染出来了。少了这道闸，一个半加载的页面就会被判成 active。
    """
    body = html or ""
    lowered_url = (url or "").lower()

    # 特征词判定复用 github_login 的词表，不在这里重抄一份 —— 两处词表漂了
    # 就会出现「登录时说停用、入池时说正常」
    marker_status = classify_account_page(body)
    if marker_status == STATUS_BANNED:
        return STATUS_BANNED, "GitHub 提示该账号已被终止/禁用"
    if marker_status == STATUS_SUSPENDED:
        return STATUS_SUSPENDED, "GitHub 提示该账号已被暂停"

    if any(marker in lowered_url for marker in _SIGNED_OUT_URL_MARKERS):
        return STATUS_EXPIRED, f"settings 页被打回登录页（{url}），user_session 不生效"
    if len(body.strip()) < _PROFILE_BODY_MIN:
        return STATUS_UNKNOWN, f"没读到 settings 页正文（长度 {len(body.strip())}）"

    low = body.lower()
    if any(marker in low for marker in _LOGGED_IN_MARKERS):
        return STATUS_ACTIVE, "settings 页显示已登录，账号可用"
    return STATUS_UNKNOWN, "settings 页正文看不出登录态（页面结构可能已变）"


# --------------------------------------------------------------------------- #
# 平台端点（写回池子 / 状态复核）
# --------------------------------------------------------------------------- #


def _platform_endpoint(sync: ConfigSyncConfig, path: str) -> str:
    """按 config_sync.url 同源推导平台端点；推不出来返回空串。

    与 remote_sync._issue_endpoint 同一套做法：不复用 writeback_url —— 那是
    「按账号回写凭据」的地址，可能被配成带 {name} 的模板或指向第三方网关，
    从它身上推不出池子端点。
    """
    if not sync.url:
        return ""
    parts = urlsplit(sync.url)
    if not parts.scheme or not parts.netloc:
        return ""
    return urlunsplit((parts.scheme, parts.netloc, path, "", ""))


def pool_upsert_op(username: str, session: str, client_id: str = "") -> dict:
    """构造一条 upsert 操作。

    字段名以 server/github_account_ops.go 的 GitHubAccountOp / GitHubAccount 为准：
    type / account.name / account.user_session / account.client_id。
    fingerprint 与 proxy_addr 是服务端运行状态，提交上去会被忽略，所以不带 ——
    带了反而会让人误以为客户端能决定出口绑定。
    """
    return {
        "type": "upsert",
        "account": {
            "name": (username or "").strip(),
            "user_session": (session or "").strip(),
            "client_id": (client_id or "").strip(),
        },
    }


def writeback_pool_account(sync: ConfigSyncConfig, username: str, session: str,
                           client_id: str = "") -> tuple[bool, str]:
    """把一条可用账号写进平台的 github_accounts[]。返回 (是否落库, 说明)。

    只认响应体里的 ok 字段，不认 HTTP 状态码：网关、鉴权代理、缓存层都可能替
    服务端回一个 200，而平台压根没收到这条凭据 —— 那样池子里少一个账号，
    却在日志上表现为成功（这条教训来自 remote_sync._writeback_once）。

    显式直连（proxies=None）：打的是自己的平台，不是被盾挡着的站点，套代理只是
    多一个失败点。与 remote_sync 里的回写、读回核实同一套口径。
    """
    if not str(session or "").strip():
        return False, "user_session 为空，拒绝写回（空值会让签发静默回落旧字段）"
    endpoint = _platform_endpoint(sync, POOL_OPS_PATH)
    if not endpoint:
        return False, "无法确定池子端点（config_sync.url 未配置或非法）"

    try:
        response = cffi.request(
            "POST", endpoint,
            headers={**remote_sync._headers(sync), "Content-Type": "application/json"},
            json={"ops": [pool_upsert_op(username, session, client_id)]},
            timeout=sync.timeout,
            proxies=None,
        )
    except Exception as exc:  # noqa: BLE001 - 网络库异常类型不固定
        return False, f"{type(exc).__name__}: {exc}"[:160]

    status = int(getattr(response, "status_code", 0) or 0)
    if status >= 400:
        text = str(getattr(response, "text", "") or "")[:160]
        return False, f"HTTP {status}: {text}"
    try:
        body = response.json()
    except Exception:  # noqa: BLE001 - 不是 JSON 就说明没到服务端
        text = str(getattr(response, "text", "") or "")[:120]
        return False, f"HTTP {status} 但响应不是 JSON（疑似网关代答）: {text!r}"
    if not isinstance(body, dict):
        return False, f"HTTP {status} 但响应不是 JSON 对象: {str(body)[:120]}"
    if body.get("ok") is not True:
        reason = str(body.get("error") or "").strip() or str(body)[:120]
        return False, f"平台未确认收下: {reason}"

    # skipped 是并发编辑下被跳过的操作。upsert 不会进 skipped（只有 delete 会），
    # 真出现了说明平台版本或语义变了，必须说出来而不是当成成功
    skipped = body.get("skipped")
    if isinstance(skipped, list) and skipped:
        return False, f"平台跳过了本次操作: {'；'.join(str(s) for s in skipped)[:160]}"
    return True, endpoint


def platform_account_status(sync: ConfigSyncConfig, session: str) -> tuple[str, str]:
    """问平台「这个 GitHub 账号本身还在不在」。返回 (status, 说明)。

    只在本地判不出结论时才调用：平台会带着这条刚登录出来的 session 从**它自己的
    IP** 去访问 GitHub，而会话换 IP 是风控高权重信号。拿不到结论一律归 unknown ——
    复核是加固手段，它失灵不该反过来把账号判成有问题。

    入参用 user_session 而不是 name：这条账号还没入池，按 name 查平台会 404
    （见 server/github_status.go 的 handleCheckGitHubStatus）。
    """
    endpoint = _platform_endpoint(sync, POOL_STATUS_PATH)
    if not endpoint:
        return STATUS_UNKNOWN, "无法确定状态端点（config_sync.url 未配置或非法）"
    try:
        response = cffi.request(
            "POST", endpoint,
            headers={**remote_sync._headers(sync), "Content-Type": "application/json"},
            json={"user_session": session},
            # 平台内部会真的去访问 GitHub，契约里写明可能数十秒，
            # 沿用 config_sync.timeout 会经常超时，这里单独放宽
            timeout=max(sync.timeout, 180),
            proxies=None,
        )
    except Exception as exc:  # noqa: BLE001 - 网络库异常类型不固定
        return STATUS_UNKNOWN, f"平台状态复核失败 {type(exc).__name__}: {exc}"[:160]
    if int(getattr(response, "status_code", 0) or 0) >= 400:
        text = str(getattr(response, "text", "") or "")[:120]
        return STATUS_UNKNOWN, f"平台状态复核 HTTP 错误: {text}"
    try:
        body = response.json()
    except Exception:  # noqa: BLE001
        return STATUS_UNKNOWN, "平台状态复核响应不是 JSON"
    result = body.get("result") if isinstance(body, dict) else None
    if not isinstance(result, dict):
        return STATUS_UNKNOWN, "平台状态复核响应里没有 result 对象"

    status = str(result.get("status") or "").strip().lower() or STATUS_UNKNOWN
    message = str(result.get("message") or "").strip()
    # usable 是平台侧「值得留在池子里」的唯一依据，只在 active 时为真。
    # 两者不一致说明平台口径变了 —— 宁可判不出，也不能拿一个语义不明的 active 入池
    if status == STATUS_ACTIVE and result.get("usable") is not True:
        return STATUS_UNKNOWN, "平台回了 active 但 usable 不为真，口径不一致"
    return status, message or f"平台判定 {status}"


# --------------------------------------------------------------------------- #
# 浏览器登录（现场信息交给 github_login 的判定函数）
# --------------------------------------------------------------------------- #


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _goto(page, url: str, timeout_ms: int = 60000) -> None:
    """导航并吞掉超时。

    GitHub 登录/跳转期间 goto 抛超时是常事，但页面往往已经在了 —— 判定统一交给
    read_stage 看现场，这里当成失败会把还有救的登录直接丢掉。
    """
    try:
        page.goto(url, wait_until="domcontentloaded", timeout=timeout_ms)
    except Exception as exc:  # noqa: BLE001 - 驱动异常类型不固定
        log.debug(f"[gh-provision] goto {url} 异常（继续看现场）: {type(exc).__name__}: {exc}")


def _submit_login(page) -> None:
    """点提交。点击触发导航，超时属正常现象，逐级退化到回车。"""
    try:
        page.click(SUBMIT_BUTTON, timeout=6000)
        return
    except Exception as exc:  # noqa: BLE001
        log.debug(f"[gh-provision] 点提交按钮失败，退化为回车: {type(exc).__name__}: {exc}")
    try:
        page.press(PASSWORD_FIELD, "Enter")
    except Exception as exc:  # noqa: BLE001 - 两条路都不通就交给主循环超时收口
        log.debug(f"[gh-provision] 回车提交也失败: {type(exc).__name__}: {exc}")


def login_for_session(page, username: str, password: str, *,
                      code_provider: Callable[[datetime], str],
                      timeout: float = 180.0,
                      login_url: str = GITHUB_LOGIN_URL,
                      poll: float = STAGE_POLL_SECONDS,
                      sleep: Callable[[float], None] = time.sleep,
                      clock: Callable[[], float] = time.monotonic,
                      utc_now: Callable[[], datetime] = _utc_now) -> str:
    """走完一次登录，返回 user_session。

    code_provider(since) 负责取设备验证码：只在 GitHub 真的要码时才调用，
    since 之前的邮件不算 —— 一个账号多次登录会攒下多封验证码邮件，填到旧码
    就是「验证码不对」，而且看日志完全看不出为什么。

    since 必须在**提交之前**取：提交后再取会把「提交后 1 秒就送达的那封」判成旧邮件，
    然后一直等一封永远不会来的新邮件。

    判成功的唯一依据是 user_session 下发（read_stage → classify_login_stage），
    不是 URL 变了 —— 设备验证码页同样是 github.com 域、URL 里也不含 /login。
    """
    holder: dict = {}
    attach_session_grabber(page, holder)

    _goto(page, login_url)
    type_like_human(page, LOGIN_FIELD, username)
    type_like_human(page, PASSWORD_FIELD, password)

    code_since = utc_now()
    _submit_login(page)

    deadline = clock() + max(1.0, float(timeout))
    code_attempts = 0
    while True:
        stage = read_stage(page, holder)
        if stage.kind == "success":
            return stage.session
        if stage.kind == "credential_error":
            raise CredentialError(stage.detail or "GitHub 拒绝了账号或密码")
        if stage.kind == "captcha":
            raise CaptchaError(stage.detail or "撞上人机验证")
        if stage.kind == "device_code":
            if code_attempts >= MAX_CODE_ATTEMPTS:
                raise LoginError(f"设备验证码填了 {code_attempts} 次仍未通过")
            code_attempts += 1
            code = str(code_provider(code_since) or "").strip()
            if not code:
                raise LoginError("没取到设备验证码（取件返回空值）")
            # 下一次取码不能再命中这封旧邮件，否则会一直填同一个错码
            code_since = utc_now()
            log.info(f"[gh-provision] 填入设备验证码（第 {code_attempts} 次）")
            fill_device_code(page, code)
            # 填码是一次明确进展，时间盒重新起算 —— 取码本身可能已经花掉几十秒，
            # 不重置会把刚填上码、马上就要成功的登录判死
            deadline = clock() + max(1.0, float(timeout))
            sleep(poll)
            continue
        if clock() >= deadline:
            raise LoginError(f"登录 {int(timeout)}s 内没拿到 user_session（最后状态 {stage.kind}）")
        sleep(poll)


def probe_status_with_browser(page, profile_url: str = GITHUB_PROFILE_URL) -> tuple[str, str]:
    """在已登录的浏览器里判一次账号状态。

    为什么用现成的浏览器而不是让平台去问：这条 session 刚在本机出口登录成功，
    平台复核会让它立刻从另一个 IP 出现，而会话换 IP 是风控高权重信号。
    """
    _goto(page, profile_url)
    try:
        url = page.url or ""
        html = page.content() or ""
    except Exception as exc:  # noqa: BLE001 - 读不到现场就是判不出，不能当成账号有问题
        return STATUS_UNKNOWN, f"读不到 settings 页现场: {type(exc).__name__}: {exc}"[:160]
    return classify_profile_page(url, html)


# --------------------------------------------------------------------------- #
# 编排：一个账号跑完四步
# --------------------------------------------------------------------------- #


def default_driver_factory(cfg: Config, account: Account):
    """按 browser.driver 选驱动。

    与 cf.solver._make_driver 同一套分流，但不复用它：那边的第三个参数是签到用的
    RunOptions，这条链路没有那个东西（驱动本身只是把 options 存起来，不使用）。
    """
    if cfg.browser.driver == "patchright":
        from .cf.driver_patchright import PatchrightDriver

        return PatchrightDriver(cfg, account, None)
    from .cf.driver_camoufox import CamoufoxDriver

    return CamoufoxDriver(cfg, account, None)


def _shell_account(username: str, proxy: Optional[str] = None) -> Account:
    """给浏览器驱动造一个壳账号：它只用到 profile_dir 与 proxy。

    名字必须按 username 稳定派生 —— profile 目录复用才能让 GitHub 认得这台设备。
    每次换目录等于每次都是新设备，设备验证码次次都得走一遍（还要多消耗一封邮件）。
    """
    name = (username or "").strip()
    return Account(name=f"gh-{name}", url="https://github.com", proxy=proxy)


def provision_one(cfg: Config, entry: GitHubProvisionAccount, *,
                  remail: Remail,
                  driver_factory: Optional[Callable] = None,
                  proxy: Optional[str] = None) -> ProvisionResult:
    """把一个账号跑完四步：取件箱 → 登录 → 判状态 → 写回。

    任何失败都翻译成一条 ProvisionResult，绝不抛异常 —— 上层是批量循环，
    一个账号的意外不该让剩下的都不跑。
    """
    gp: GitHubProvisionConfig = cfg.github_provision
    username = (entry.username or "").strip()
    if not username:
        # 用户名同时是收件箱定位依据和池子里的引用键，空的话两头都对不上：
        # 平台会用 400 拒绝，但那已经白跑了一次登录
        return ProvisionResult("", RESULT_CONFIG_ERROR,
                               detail="这条账号没配 username，定位不了收件箱也入不了池")
    if not entry.password:
        return ProvisionResult(username, RESULT_CONFIG_ERROR,
                               detail="这条账号没配 password，无法登录")

    # 先确认收件箱：GitHub 对陌生出口几乎总要设备验证码，取不到码的账号去登录只会
    # 白留一次异常登录记录、白发一封我们读不到的验证码邮件
    hit: Optional[EmailHit] = None
    try:
        hit = remail.find_email(entry.mailbox_name)
    except RemailError as exc:
        return ProvisionResult(username, RESULT_NO_MAILBOX,
                               detail=f"搜收件箱失败: {exc}"[:160])
    if hit is None:
        return ProvisionResult(
            username, RESULT_NO_MAILBOX,
            detail=f"ReMail 里找不到 {entry.mailbox_name} 的可用订单"
                   f"（邮箱前缀与用户名不同时请配 email_name）")

    account = _shell_account(username, proxy)
    factory = driver_factory or default_driver_factory
    session = ""
    status, detail = STATUS_UNKNOWN, ""
    try:
        driver = factory(cfg, account)
        with driver:
            session = login_for_session(
                driver.page, username, entry.password,
                code_provider=lambda since: remail.poll_for_code(
                    hit, since,
                    max_tries=gp.remail_max_tries,
                    fallback_poll_sec=gp.remail_poll_seconds,
                )[0],
                timeout=float(gp.login_timeout),
            )
            status, detail = probe_status_with_browser(driver.page)
    except (LoginError, RemailError) as exc:
        return ProvisionResult(username, RESULT_LOGIN_FAILED,
                               detail=f"{type(exc).__name__}: {exc}"[:200],
                               email=hit.email)
    except Exception as exc:  # noqa: BLE001 - 驱动缺失、页面异常都不该打断整批
        return ProvisionResult(username, RESULT_LOGIN_FAILED,
                               detail=f"登录链路异常 {type(exc).__name__}: {exc}"[:200],
                               email=hit.email)

    # 本地判不出时才问平台，且要说清是两段结论拼起来的
    if status == STATUS_UNKNOWN and gp.platform_status_recheck:
        remote_status, remote_detail = platform_account_status(cfg.config_sync, session)
        detail = f"{detail}；平台复核：{remote_detail}"
        status = remote_status

    if status != STATUS_ACTIVE:
        return ProvisionResult(username, RESULT_REJECTED, account_status=status,
                               detail=detail, session_len=len(session), email=hit.email)

    ok, wb_detail = writeback_pool_account(cfg.config_sync, username, session,
                                           entry.client_id)
    if not ok:
        return ProvisionResult(username, RESULT_WRITEBACK_FAILED, account_status=status,
                               detail=wb_detail, session_len=len(session), email=hit.email)
    return ProvisionResult(username, RESULT_PROVISIONED, account_status=status,
                           detail=detail, session_len=len(session), email=hit.email)


# --------------------------------------------------------------------------- #
# 编排：整批
# --------------------------------------------------------------------------- #


def _make_remail(gp: GitHubProvisionConfig, timeout: int) -> Remail:
    return Remail(gp.remail_base_url, list(gp.remail_api_keys), timeout=timeout)


def provision_accounts(cfg: Config, *, only: Optional[list] = None,
                       remail: Optional[Remail] = None,
                       driver_factory: Optional[Callable] = None,
                       proxy: Optional[str] = None) -> ProvisionReport:
    """按配置把待入池的 GitHub 账号逐个跑完流水线。

    串行执行，不并发：三条外部链路（ReMail 按 token 限流、GitHub 对同一出口的登录
    频率、平台的写库锁）都受不了并发，同时开几个只会更快撞限流。

    缺配置直接抛 ConfigError（main.py 已按它退 2 码）：这类问题必须在开跑前暴露，
    跑到一半才发现「没配收件服务」的代价是一堆半途失败的登录。
    """
    gp: GitHubProvisionConfig = cfg.github_provision
    sync: ConfigSyncConfig = cfg.config_sync

    # 写回是这条链路的唯一出口，配不齐就没有做下去的意义。与 remote_sync 的凭据回写
    # 同一套前置判断（那边也是 enabled 关着就直接判负），不另立一套语义
    if not sync.enabled:
        raise ConfigError("写回 GitHub 账号池需要 config_sync.enabled = true")
    if not _platform_endpoint(sync, POOL_OPS_PATH):
        raise ConfigError("写回 GitHub 账号池需要合法的 config_sync.url（推导池子端点用）")
    if remail is None:
        if not gp.remail_base_url:
            raise ConfigError("github_provision.remail_base_url 为空，取不到设备验证码")
        if not gp.remail_api_keys:
            raise ConfigError("github_provision.remail_api_keys 为空，取不到设备验证码")

    entries = gp.select(only)
    if not entries:
        raise ConfigError("github_provision.accounts 为空，没有要处理的账号")

    client = remail or _make_remail(gp, cfg.http.timeout)
    report = ProvisionReport()
    log.step(f"[gh-provision] 开始处理 {len(entries)} 个 GitHub 账号（串行）")
    for entry in entries:
        with log.context(entry.username):
            result = provision_one(cfg, entry, remail=client,
                                   driver_factory=driver_factory, proxy=proxy)
        report.results.append(result)
        text = f"{result.username}: {result.status}"
        if result.account_status:
            text += f"（{result.account_status}）"
        if result.detail:
            text += f" — {result.detail}"
        if result.ok:
            log.ok(f"[gh-provision] {text}")
        elif result.status == RESULT_REJECTED:
            # 状态过滤拦下来不是故障，是这条链路存在的理由
            log.warn(f"[gh-provision] {text}")
        else:
            log.err(f"[gh-provision] {text}")
    log.info(f"[gh-provision] 完成：入池 {report.provisioned}/{len(report.results)}")
    return report




