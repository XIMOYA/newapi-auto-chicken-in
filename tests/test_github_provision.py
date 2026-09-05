"""
tests/test_github_provision.py
GitHub 账号自动填入编排的离线测试：不联网，浏览器与三条外部链路全部打桩。

守的是这条流水线上最容易静默错的地方：
- settings 页被打回登录页时不能判成 active（正文里确实没有停用特征词）
- 正文没读到（空/太短）同样不能判成 active
- 写回只认响应体里的 ok，HTTP 200 不等于平台收下了
- 状态没过就绝不能发写回请求 —— 那正是这条链路存在的理由
- 第二次取验证码必须换新的 since，否则会一直填同一个旧码
"""

from datetime import datetime, timedelta, timezone

import pytest

from newapi_checkin import github_provision as gp
from newapi_checkin.config import (
    Config,
    ConfigError,
    ConfigSyncConfig,
    GitHubProvisionAccount,
    GitHubProvisionConfig,
)
from newapi_checkin.github_login import CaptchaError, CredentialError, LoginError, OTP_FIELD
from newapi_checkin.remail import EmailHit, RemailError

NOW = datetime(2026, 8, 31, 12, 0, 0, tzinfo=timezone.utc)
PANEL = "https://panel.example.com/api/config/raw"
# 够长的正文，避免撞上「正文太短判 unknown」那道闸
LONG_BODY = "x" * 400


class FakeResponse:
    def __init__(self, payload, status_code=200, text=None):
        self.payload = payload
        self.status_code = status_code
        self.text = text if text is not None else str(payload)

    def json(self):
        if isinstance(self.payload, Exception):
            raise self.payload
        return self.payload


class FakeLocator:
    def __init__(self, page, selector):
        self.page = page
        self.selector = selector

    @property
    def first(self):
        return self

    def count(self):
        step = self.page.current
        if self.selector == ".flash-error":
            return 1 if step.get("flash") else 0
        if self.selector in ("iframe[title*='challenge' i]", ".octospider"):
            return 1 if step.get("captcha") else 0
        if self.selector == OTP_FIELD:
            return 1 if step.get("otp") else 0
        return 1  # 提交按钮之类：存在即可

    def click(self, **_kw):
        self.page.clicked.append(self.selector)


class FakePage:
    """按脚本回放的假页面。

    steps 每项描述一次「看现场」时的样子：url / session / flash / captcha / otp。
    advance() 前进一步，并在新一步带 session 时通过 response 回调把它推给
    attach_session_grabber 挂的 holder —— 走真实通路，不直接塞 holder。
    """

    def __init__(self, steps, content=LONG_BODY):
        self.steps = list(steps) or [{}]
        self.index = 0
        self.typed = []          # (selector, text)
        self.filled = []         # (selector, value)
        self.clicked = []
        self.pressed = []
        self.goto_urls = []
        self.listeners = []
        self._content = content
        self.content_error = None
        # 让测试能模拟「点击因跳转超时」：真实链路里这是常态，不是失败
        self.click_error = None
        self.goto_error = None

    # ---------------------------------------------------------------- 脚本推进
    @property
    def current(self) -> dict:
        return self.steps[min(self.index, len(self.steps) - 1)]

    def advance(self, *_a):
        self.index = min(self.index + 1, len(self.steps) - 1)
        self._emit_session()

    def _emit_session(self):
        value = self.current.get("session")
        if not value:
            return
        for cb in self.listeners:
            cb(_FakeResponseEvent(f"user_session={value}; Path=/"))

    # ---------------------------------------------------------------- page API
    @property
    def url(self):
        return self.current.get("url", "https://github.com/login")

    def on(self, event, cb):
        if event == "response":
            self.listeners.append(cb)
            self._emit_session()

    def locator(self, selector):
        return FakeLocator(self, selector)

    def goto(self, url, **_kw):
        self.goto_urls.append(url)
        if self.goto_error:
            raise self.goto_error

    def click(self, selector, **_kw):
        self.clicked.append(selector)
        if self.click_error:
            raise self.click_error

    def focus(self, selector):
        pass

    def type(self, selector, text, **_kw):
        self.typed.append((selector, text))

    def fill(self, selector, value):
        self.filled.append((selector, value))

    def press(self, selector, key):
        self.pressed.append((selector, key))

    def content(self):
        if self.content_error:
            raise self.content_error
        return self._content


class _FakeResponseEvent:
    """response 事件对象：attach_session_grabber 只用 url 与 header_values。"""

    def __init__(self, set_cookie):
        self.url = "https://github.com/session"
        self._set_cookie = set_cookie

    def header_values(self, name):
        return [self._set_cookie] if name == "set-cookie" else []


class FakeDriver:
    """假驱动：只需要交出 page，并让 with 语句能进出。"""

    def __init__(self, page):
        self.page = page
        self.entered = False
        self.exited = False

    def __enter__(self):
        self.entered = True
        return self

    def __exit__(self, *_exc):
        self.exited = True


class FakeRemail:
    def __init__(self, hit=None, code="123456", find_error=None, poll_error=None):
        self.hit = hit
        self.code = code
        self.find_error = find_error
        self.poll_error = poll_error
        self.searched = []
        self.poll_calls = []

    def find_email(self, name):
        self.searched.append(name)
        if self.find_error:
            raise self.find_error
        return self.hit

    def poll_for_code(self, hit, since, max_tries=10, fallback_poll_sec=8):
        self.poll_calls.append({"hit": hit, "since": since,
                                "max_tries": max_tries, "poll": fallback_poll_sec})
        if self.poll_error:
            raise self.poll_error
        return self.code, "mail#1.body"


def _sync(enabled=True, url=PANEL, token="k" * 20):
    return ConfigSyncConfig.from_raw({"enabled": enabled, "url": url, "token": token})


def _cfg(*, sync=None, accounts=None, **provision):
    provision.setdefault("enabled", True)
    provision.setdefault("remail_base_url", "https://remail.example.com")
    provision.setdefault("remail_api_keys", ["rk-1"])
    entries = accounts if accounts is not None else [
        GitHubProvisionAccount(username="Steven", password="pw"),
    ]
    return Config(
        config_sync=sync if sync is not None else _sync(),
        github_provision=GitHubProvisionConfig(accounts=entries, **provision),
    )


class TestClassifyProfilePage:
    """账号状态判定：分支顺序照 Go 侧 classifyGitHubProfileResponse。"""

    def test_logged_in_body_is_active(self):
        status, _ = gp.classify_profile_page(
            "https://github.com/settings/profile", LONG_BODY + "Sign out")
        assert status == gp.STATUS_ACTIVE

    def test_bounced_to_login_is_expired_not_active(self):
        """登录页正文里没有停用特征词，按词表判会得出 active —— 那是最坏的假 active。"""
        status, detail = gp.classify_profile_page(
            "https://github.com/login?return_to=%2Fsettings%2Fprofile", LONG_BODY)
        assert status == gp.STATUS_EXPIRED
        assert "登录页" in detail

    def test_session_url_also_expired(self):
        status, _ = gp.classify_profile_page("https://github.com/session", LONG_BODY)
        assert status == gp.STATUS_EXPIRED

    def test_empty_body_is_unknown_not_active(self):
        """页面没加载出来就是判不出，不能当成账号可用。"""
        for body in ("", "   ", "<html></html>"):
            status, _ = gp.classify_profile_page(
                "https://github.com/settings/profile", body)
            assert status == gp.STATUS_UNKNOWN

    def test_half_loaded_page_with_login_marker_is_still_unknown(self):
        """只渲染出 <html class="logged-in"> 就判 active 是真会发生的假 active：
        GitHub 的 html 标签上本来就带这个 class，正文还没到时它已经在了。"""
        status, detail = gp.classify_profile_page(
            "https://github.com/settings/profile", '<html class="logged-in">')
        assert status == gp.STATUS_UNKNOWN
        assert "没读到" in detail

    def test_body_without_login_markers_is_unknown(self):
        status, detail = gp.classify_profile_page(
            "https://github.com/settings/profile", LONG_BODY)
        assert status == gp.STATUS_UNKNOWN
        assert "看不出登录态" in detail

    def test_suspended_wins_over_login_markers(self):
        """停用页可能仍是 200 且带着导航栏特征，特征词必须排在登录态判定之前。"""
        body = LONG_BODY + "Sign out" + "Your account has been suspended"
        status, _ = gp.classify_profile_page(
            "https://github.com/settings/profile", body)
        assert status == gp.STATUS_SUSPENDED

    def test_banned_wins_over_suspended_and_url(self):
        body = "account has been terminated / account has been suspended"
        status, _ = gp.classify_profile_page("https://github.com/login", body)
        assert status == gp.STATUS_BANNED

    def test_banned_beats_short_body_guard(self):
        """特征词判定在正文长度闸之前：停用提示页往往就是很短的一页。"""
        status, _ = gp.classify_profile_page(
            "https://github.com/settings/profile", "account was disabled")
        assert status == gp.STATUS_BANNED


class TestPlatformEndpoint:
    def test_derives_from_config_sync_url(self):
        assert gp._platform_endpoint(_sync(), gp.POOL_OPS_PATH) == \
            "https://panel.example.com/api/github-accounts/ops"
        assert gp._platform_endpoint(_sync(url="http://10.0.0.5:8080/api/config"),
                                     gp.POOL_STATUS_PATH) == \
            "http://10.0.0.5:8080/api/github-accounts/status"

    @pytest.mark.parametrize("bad", ["", "不是地址", "/api/config"])
    def test_unusable_url_gives_empty(self, bad):
        assert gp._platform_endpoint(_sync(url=bad), gp.POOL_OPS_PATH) == ""


class TestPoolUpsertOp:
    def test_field_names_match_server_contract(self):
        op = gp.pool_upsert_op(" Steven ", " sess ", " Ov23li ")
        assert op == {"type": "upsert", "account": {
            "name": "Steven", "user_session": "sess", "client_id": "Ov23li"}}

    def test_never_sends_server_owned_fields(self):
        """fingerprint / proxy_addr 是服务端运行状态，提交上去只会误导人。"""
        op = gp.pool_upsert_op("Steven", "sess")
        assert "fingerprint" not in op["account"]
        assert "proxy_addr" not in op["account"]


class TestWritebackPoolAccount:
    def _capture(self, monkeypatch, response):
        seen = {}

        def fake_request(method, url, **kwargs):
            seen.update(method=method, url=url, **kwargs)
            if isinstance(response, Exception):
                raise response
            return response

        monkeypatch.setattr(gp.cffi, "request", fake_request)
        return seen

    def test_success_path_sends_expected_request(self, monkeypatch):
        seen = self._capture(monkeypatch, FakeResponse({"ok": True, "revision": 42}))
        ok, detail = gp.writeback_pool_account(_sync(), "Steven", "sess-value", "cid")
        assert ok is True
        assert detail == "https://panel.example.com/api/github-accounts/ops"
        assert seen["method"] == "POST"
        assert seen["json"] == {"ops": [
            {"type": "upsert",
             "account": {"name": "Steven", "user_session": "sess-value", "client_id": "cid"}}]}
        # 打自己的平台：显式直连，套代理只是多一个失败点
        assert seen["proxies"] is None
        assert seen["headers"]["Content-Type"] == "application/json"

    def test_carries_the_api_key_header(self, monkeypatch):
        """客户端只有 API Key，认证头漏了平台会 401，池子永远空着。"""
        seen = self._capture(monkeypatch, FakeResponse({"ok": True}))
        gp.writeback_pool_account(_sync(), "Steven", "sess")
        assert any("k" * 20 in str(v) for v in seen["headers"].values())

    def test_http_200_without_ok_is_failure(self, monkeypatch):
        """网关/鉴权代理都可能替平台回 200，而凭据压根没到服务端。"""
        self._capture(monkeypatch, FakeResponse({"revision": 42}))
        ok, detail = gp.writeback_pool_account(_sync(), "Steven", "sess")
        assert ok is False
        assert "未确认收下" in detail

    def test_http_error_is_failure(self, monkeypatch):
        self._capture(monkeypatch, FakeResponse({"error": "user_session 不能为空"},
                                                status_code=400))
        ok, detail = gp.writeback_pool_account(_sync(), "Steven", "sess")
        assert ok is False and "HTTP 400" in detail

    def test_non_json_is_failure(self, monkeypatch):
        self._capture(monkeypatch, FakeResponse(ValueError("no json"), text="<html>"))
        ok, detail = gp.writeback_pool_account(_sync(), "Steven", "sess")
        assert ok is False and "不是 JSON" in detail

    def test_skipped_ops_are_not_success(self, monkeypatch):
        """upsert 不该出现在 skipped 里，出现了说明平台语义变了。"""
        self._capture(monkeypatch, FakeResponse({"ok": True, "skipped": ["upsert 被跳过"]}))
        ok, detail = gp.writeback_pool_account(_sync(), "Steven", "sess")
        assert ok is False and "跳过" in detail

    def test_empty_session_refuses_before_network(self, monkeypatch):
        """空 session 入池会让签发静默回落旧字段，必须在发请求之前拦住。"""
        called = []
        monkeypatch.setattr(gp.cffi, "request",
                            lambda *a, **kw: called.append(1))
        ok, detail = gp.writeback_pool_account(_sync(), "Steven", "   ")
        assert ok is False and "为空" in detail
        assert called == []

    def test_network_error_is_failure(self, monkeypatch):
        self._capture(monkeypatch, RuntimeError("connection reset"))
        ok, detail = gp.writeback_pool_account(_sync(), "Steven", "sess")
        assert ok is False and "RuntimeError" in detail

    def test_unusable_url_reports_instead_of_raising(self, monkeypatch):
        monkeypatch.setattr(gp.cffi, "request",
                            lambda *a, **kw: FakeResponse({"ok": True}))
        ok, detail = gp.writeback_pool_account(_sync(url=""), "Steven", "sess")
        assert ok is False and "config_sync.url" in detail


class TestPlatformAccountStatus:
    """平台复核：拿不到结论一律 unknown，绝不反过来把账号判成有问题。"""

    def _capture(self, monkeypatch, response):
        seen = {}

        def fake_request(method, url, **kwargs):
            seen.update(method=method, url=url, **kwargs)
            if isinstance(response, Exception):
                raise response
            return response

        monkeypatch.setattr(gp.cffi, "request", fake_request)
        return seen

    def test_active_with_usable(self, monkeypatch):
        seen = self._capture(monkeypatch, FakeResponse(
            {"ok": True, "result": {"status": "active", "message": "已登录，账号可用",
                                    "usable": True}}))
        status, detail = gp.platform_account_status(_sync(), "sess-value")
        assert status == gp.STATUS_ACTIVE and "可用" in detail
        # 尚未入池的凭据只能按 user_session 问，按 name 查平台会 404
        assert seen["json"] == {"user_session": "sess-value"}
        assert seen["url"].endswith("/api/github-accounts/status")
        # 平台内部要真的去访问 GitHub，契约写明可能数十秒
        assert seen["timeout"] >= 180

    def test_active_without_usable_is_unknown(self, monkeypatch):
        """usable 是平台侧「值得留在池子里」的唯一依据，对不上就别拿它入池。"""
        self._capture(monkeypatch, FakeResponse(
            {"ok": True, "result": {"status": "active", "usable": False}}))
        status, detail = gp.platform_account_status(_sync(), "sess")
        assert status == gp.STATUS_UNKNOWN and "usable" in detail

    @pytest.mark.parametrize("status_text", ["suspended", "banned", "expired"])
    def test_passes_through_negative_verdicts(self, monkeypatch, status_text):
        self._capture(monkeypatch, FakeResponse(
            {"ok": True, "result": {"status": status_text, "message": "m"}}))
        status, _ = gp.platform_account_status(_sync(), "sess")
        assert status == status_text

    def test_network_error_is_unknown(self, monkeypatch):
        self._capture(monkeypatch, RuntimeError("boom"))
        status, detail = gp.platform_account_status(_sync(), "sess")
        assert status == gp.STATUS_UNKNOWN and "RuntimeError" in detail

    def test_http_error_and_missing_result_are_unknown(self, monkeypatch):
        self._capture(monkeypatch, FakeResponse({"error": "x"}, status_code=500))
        assert gp.platform_account_status(_sync(), "sess")[0] == gp.STATUS_UNKNOWN
        self._capture(monkeypatch, FakeResponse({"ok": True}))
        assert gp.platform_account_status(_sync(), "sess")[0] == gp.STATUS_UNKNOWN
        self._capture(monkeypatch, FakeResponse(ValueError("no json")))
        assert gp.platform_account_status(_sync(), "sess")[0] == gp.STATUS_UNKNOWN

    def test_unusable_url_is_unknown(self, monkeypatch):
        called = []
        monkeypatch.setattr(gp.cffi, "request", lambda *a, **kw: called.append(1))
        status, detail = gp.platform_account_status(_sync(url=""), "sess")
        assert status == gp.STATUS_UNKNOWN and "config_sync.url" in detail
        assert called == []


class Clock:
    """可控时钟：每次读都往前走一点，需要时测试可以直接把 t 推远。"""

    def __init__(self, step=0.1):
        self.t = 0.0
        self.step = step

    def __call__(self):
        self.t += self.step
        return self.t


def _login(page, *, provider=None, timeout=1000.0, clock=None):
    """跑一次登录：sleep 换成推进页面脚本，时钟与 UTC 时刻都可注入。

    utc_now 在「提交」这个事件前后返回不同的值（之后每次再跳 5 分钟），
    专门用来验证取验证码的 since 是**提交之前**那一刻 —— 取晚了会把提交后
    一秒就送达的那封邮件判成旧邮件，然后死等一封不会来的新邮件。
    """
    marks = {"submitted": False, "n": 0}
    original_click = page.click

    def click(selector, **kw):
        if selector == gp.SUBMIT_BUTTON:
            marks["submitted"] = True
        return original_click(selector, **kw)

    page.click = click

    def utc_now():
        if not marks["submitted"]:
            return NOW
        marks["n"] += 1
        return NOW + timedelta(minutes=5 * marks["n"])

    return gp.login_for_session(
        page, "steven", "pw",
        code_provider=provider or (lambda since: "000000"),
        timeout=timeout,
        sleep=lambda _s: page.advance(),
        clock=clock or Clock(),
        utc_now=utc_now,
    )


class TestLoginForSession:
    def test_returns_session_and_fills_the_form(self):
        page = FakePage([{"url": "https://github.com/login"},
                         {"url": "https://github.com/", "session": "abc1234567890"}])
        assert _login(page) == "abc1234567890"
        assert page.goto_urls[0] == gp.GITHUB_LOGIN_URL
        typed = {sel: "".join(t for s, t in page.typed if s == sel)
                 for sel, _ in page.typed}
        assert typed[gp.LOGIN_FIELD] == "steven"
        assert typed[gp.PASSWORD_FIELD] == "pw"
        assert gp.SUBMIT_BUTTON in page.clicked

    def test_never_touches_honeypot_fields(self):
        """required_field_* 是蜜罐，填了等于自证机器人。"""
        page = FakePage([{"session": "abc1234567890"}])
        _login(page)
        touched = [sel for sel, _ in page.typed] + [sel for sel, _ in page.filled]
        assert not any("required_field" in sel for sel in touched)

    def test_session_wins_over_device_code_page(self):
        """会话已下发就不该再去取码 —— 那会白消耗一封验证码邮件。"""
        calls = []
        page = FakePage([{"otp": True, "session": "abc1234567890"}])
        assert _login(page, provider=lambda since: calls.append(since) or "1") \
            == "abc1234567890"
        assert calls == []

    def test_device_code_is_fetched_and_filled(self):
        page = FakePage([
            {"otp": True, "url": "https://github.com/sessions/verified-device"},
            {"url": "https://github.com/", "session": "sess-abcdefghij"},
        ])
        seen = []

        def provider(since):
            seen.append(since)
            return "654321"

        assert _login(page, provider=provider) == "sess-abcdefghij"
        assert (OTP_FIELD, "654321") in page.filled
        # since 必须是**提交之前**的时刻：取晚了会把「提交后一秒就送达的那封」
        # 判成旧邮件，然后一直等一封永远不会来的新邮件
        assert seen == [NOW]

    def test_second_attempt_uses_a_fresh_since(self):
        """填错码后再取，since 不更新就会一直命中同一封旧邮件、填同一个错码。"""
        page = FakePage([{"otp": True, "url": "https://github.com/sessions/two-factor"}])
        seen = []

        def provider(since):
            seen.append(since)
            return "111111"

        with pytest.raises(LoginError) as exc:
            _login(page, provider=provider)
        assert f"{gp.MAX_CODE_ATTEMPTS} 次" in str(exc.value)
        assert len(seen) == gp.MAX_CODE_ATTEMPTS
        assert seen[1] > seen[0]

    def test_empty_code_raises_instead_of_filling_blank(self):
        page = FakePage([{"otp": True}])
        with pytest.raises(LoginError) as exc:
            _login(page, provider=lambda since: "")
        assert "没取到" in str(exc.value)
        assert page.filled == []

    def test_credential_error_is_terminal(self):
        page = FakePage([{"url": "https://github.com/login", "flash": True}])
        with pytest.raises(CredentialError):
            _login(page)

    def test_captcha_is_terminal(self):
        page = FakePage([{"url": "https://github.com/login", "captcha": True}])
        with pytest.raises(CaptchaError):
            _login(page)

    def test_timeout_raises_with_last_stage(self):
        page = FakePage([{"url": "https://github.com/somewhere"}])
        with pytest.raises(LoginError) as exc:
            _login(page, timeout=1.0, clock=Clock(step=1.0))
        assert "pending" in str(exc.value)

    def test_deadline_restarts_after_filling_the_code(self):
        """取码本身可能花掉几十秒，不重置时间盒会把马上要成功的登录判死。"""
        clock = Clock(step=0.1)

        def provider(since):
            clock.t += 100.0  # 轮询等验证码邮件等了 100 秒
            return "654321"

        page = FakePage([
            {"otp": True},
            {"url": "https://github.com/"},                       # 还在跳转
            {"url": "https://github.com/", "session": "sess-abcdefghij"},
        ])
        assert _login(page, provider=provider, timeout=10.0, clock=clock) \
            == "sess-abcdefghij"

    def test_submit_falls_back_to_enter(self):
        """点不上提交按钮不能放弃整次登录：慢链路下 click 的命中测试会死等。"""
        page = FakePage([{"session": "abc1234567890"}])
        page.click_error = RuntimeError("timeout")
        _login(page)
        assert (gp.PASSWORD_FIELD, "Enter") in page.pressed


class TestProbeStatusWithBrowser:
    def test_reads_the_profile_page(self):
        page = FakePage([{"url": "https://github.com/settings/profile"}],
                        content=LONG_BODY + "Sign out")
        status, _ = gp.probe_status_with_browser(page)
        assert page.goto_urls == [gp.GITHUB_PROFILE_URL]
        assert status == gp.STATUS_ACTIVE

    def test_goto_timeout_still_judges_the_scene(self):
        """goto 抛超时时页面往往已经在了，直接判负会白丢一条刚登录好的会话。"""
        page = FakePage([{"url": "https://github.com/settings/profile"}],
                        content=LONG_BODY + "public profile")
        page.goto_error = RuntimeError("timeout 30000ms exceeded")
        assert gp.probe_status_with_browser(page)[0] == gp.STATUS_ACTIVE

    def test_unreadable_scene_is_unknown(self):
        page = FakePage([{"url": "https://github.com/settings/profile"}])
        page.content_error = RuntimeError("target closed")
        status, detail = gp.probe_status_with_browser(page)
        assert status == gp.STATUS_UNKNOWN and "读不到" in detail


def _patch_flow(monkeypatch, *, session="sess-abcdefghij", status=gp.STATUS_ACTIVE,
                detail="已登录", login_error=None):
    """把浏览器那两步换成打桩，只留下分支与写回逻辑给测试盯。"""
    seen = {}

    def fake_login(page, username, password, **kwargs):
        seen.update(page=page, username=username, password=password, **kwargs)
        if login_error:
            raise login_error
        return session

    def fake_probe(page, profile_url=gp.GITHUB_PROFILE_URL):
        seen["probed"] = profile_url
        return status, detail

    monkeypatch.setattr(gp, "login_for_session", fake_login)
    monkeypatch.setattr(gp, "probe_status_with_browser", fake_probe)
    return seen


def _patch_writeback(monkeypatch, ok=True, detail="endpoint"):
    calls = []

    def fake_writeback(sync, username, session, client_id=""):
        calls.append({"username": username, "session": session, "client_id": client_id})
        return ok, detail

    monkeypatch.setattr(gp, "writeback_pool_account", fake_writeback)
    return calls


def _hit(email="steven@mail.com"):
    return EmailHit(key_index=1, email=email, service_token="st_x", order_no="o1")


class TestProvisionOne:
    def _run(self, cfg, entry, remail, monkeypatch_driver=True):
        page = FakePage([{}])
        driver = FakeDriver(page)
        made = []

        def factory(cfg_arg, account):
            made.append(account)
            return driver

        result = gp.provision_one(cfg, entry, remail=remail, driver_factory=factory)
        return result, driver, made

    def test_missing_password_stops_before_any_link(self, monkeypatch):
        remail = FakeRemail(hit=_hit())
        made = []
        result = gp.provision_one(
            _cfg(), GitHubProvisionAccount(username="Steven", password=""),
            remail=remail, driver_factory=lambda c, a: made.append(a))
        assert result.status == gp.RESULT_CONFIG_ERROR
        assert remail.searched == [] and made == []

    def test_missing_username_stops_before_any_link(self):
        """用户名既是收件箱定位依据也是池子引用键，空的话两头都对不上。"""
        remail = FakeRemail(hit=_hit())
        made = []
        result = gp.provision_one(
            _cfg(), GitHubProvisionAccount(username="  ", password="pw"),
            remail=remail, driver_factory=lambda c, a: made.append(a))
        assert result.status == gp.RESULT_CONFIG_ERROR
        assert "username" in result.detail
        assert remail.searched == [] and made == []

    def test_missing_mailbox_never_opens_the_browser(self, monkeypatch):
        """取不到码的账号去登录只会白留一次异常登录、白发一封读不到的邮件。"""
        remail = FakeRemail(hit=None)
        made = []
        result = gp.provision_one(
            _cfg(), GitHubProvisionAccount(username="Steven", password="pw"),
            remail=remail, driver_factory=lambda c, a: made.append(a))
        assert result.status == gp.RESULT_NO_MAILBOX
        assert "email_name" in result.detail
        assert made == []

    def test_email_name_overrides_username_for_search(self, monkeypatch):
        _patch_flow(monkeypatch)
        _patch_writeback(monkeypatch)
        remail = FakeRemail(hit=_hit())
        entry = GitHubProvisionAccount(username="Steven", password="pw",
                                       email_name="shiao1974")
        self._run(_cfg(), entry, remail)
        assert remail.searched == ["shiao1974"]

    def test_search_failure_is_reported(self, monkeypatch):
        remail = FakeRemail(find_error=RemailError("key#1 搜订单认证失败（HTTP 401）"))
        result, _, made = self._run(
            _cfg(), GitHubProvisionAccount(username="Steven", password="pw"), remail)
        assert result.status == gp.RESULT_NO_MAILBOX
        assert "401" in result.detail and made == []

    def test_active_account_is_written_back(self, monkeypatch):
        seen = _patch_flow(monkeypatch)
        calls = _patch_writeback(monkeypatch)
        entry = GitHubProvisionAccount(username="Steven", password="pw", client_id="Ov23li")
        result, driver, made = self._run(_cfg(login_timeout=240), entry, FakeRemail(hit=_hit()))

        assert result.status == gp.RESULT_PROVISIONED
        assert result.account_status == gp.STATUS_ACTIVE
        assert calls == [{"username": "Steven", "session": "sess-abcdefghij",
                          "client_id": "Ov23li"}]
        # 登录拿到的是账号名与密码，不是收件箱名
        assert seen["username"] == "Steven" and seen["password"] == "pw"
        assert seen["timeout"] == 240.0
        # profile 目录按账号名稳定派生，换目录等于每次都是新设备
        assert made[0].name == "gh-Steven"
        assert driver.entered and driver.exited

    def test_code_provider_unwraps_the_code_and_carries_poll_settings(self, monkeypatch):
        """poll_for_code 返回 (code, source)，忘了取 [0] 会把一个元组填进表单。"""
        seen = _patch_flow(monkeypatch)
        _patch_writeback(monkeypatch)
        remail = FakeRemail(hit=_hit(), code="654321")
        cfg = _cfg(remail_max_tries=3, remail_poll_seconds=2)
        self._run(cfg, GitHubProvisionAccount(username="Steven", password="pw"), remail)

        code = seen["code_provider"](NOW)
        assert code == "654321"
        assert remail.poll_calls[0]["max_tries"] == 3
        assert remail.poll_calls[0]["poll"] == 2
        assert remail.poll_calls[0]["since"] == NOW

    @pytest.mark.parametrize("status", [gp.STATUS_SUSPENDED, gp.STATUS_BANNED,
                                        gp.STATUS_EXPIRED, gp.STATUS_UNKNOWN])
    def test_non_active_never_reaches_the_pool(self, monkeypatch, status):
        """状态没过就绝不写回 —— 这条链路存在的全部理由。"""
        _patch_flow(monkeypatch, status=status, detail="不可用")
        calls = _patch_writeback(monkeypatch)
        result, _, _ = self._run(
            _cfg(), GitHubProvisionAccount(username="Steven", password="pw"),
            FakeRemail(hit=_hit()))
        assert result.status == gp.RESULT_REJECTED
        assert result.account_status == status
        assert calls == []

    def test_unknown_does_not_ask_platform_by_default(self, monkeypatch):
        """默认不复核：平台会让这条会话立刻从另一个 IP 出现，那是风控高权重信号。"""
        _patch_flow(monkeypatch, status=gp.STATUS_UNKNOWN)
        _patch_writeback(monkeypatch)
        asked = []
        monkeypatch.setattr(gp, "platform_account_status",
                            lambda sync, session: asked.append(session) or ("active", "m"))
        result, _, _ = self._run(
            _cfg(), GitHubProvisionAccount(username="Steven", password="pw"),
            FakeRemail(hit=_hit()))
        assert result.status == gp.RESULT_REJECTED
        assert asked == []

    def test_unknown_with_recheck_uses_the_platform_verdict(self, monkeypatch):
        _patch_flow(monkeypatch, status=gp.STATUS_UNKNOWN, detail="看不出登录态")
        calls = _patch_writeback(monkeypatch)
        monkeypatch.setattr(gp, "platform_account_status",
                            lambda sync, session: (gp.STATUS_ACTIVE, "平台说可用"))
        result, _, _ = self._run(
            _cfg(platform_status_recheck=True),
            GitHubProvisionAccount(username="Steven", password="pw"),
            FakeRemail(hit=_hit()))
        assert result.status == gp.RESULT_PROVISIONED
        # 两段结论都要留在 detail 里，否则排查时看不出是谁下的判断
        assert "看不出登录态" in result.detail and "平台说可用" in result.detail
        assert len(calls) == 1

    def test_recheck_can_also_reject(self, monkeypatch):
        _patch_flow(monkeypatch, status=gp.STATUS_UNKNOWN)
        calls = _patch_writeback(monkeypatch)
        monkeypatch.setattr(gp, "platform_account_status",
                            lambda sync, session: (gp.STATUS_BANNED, "已封禁"))
        result, _, _ = self._run(
            _cfg(platform_status_recheck=True),
            GitHubProvisionAccount(username="Steven", password="pw"),
            FakeRemail(hit=_hit()))
        assert result.status == gp.RESULT_REJECTED
        assert result.account_status == gp.STATUS_BANNED and calls == []

    def test_writeback_failure_is_surfaced(self, monkeypatch):
        _patch_flow(monkeypatch)
        _patch_writeback(monkeypatch, ok=False, detail="平台未确认收下")
        result, _, _ = self._run(
            _cfg(), GitHubProvisionAccount(username="Steven", password="pw"),
            FakeRemail(hit=_hit()))
        assert result.status == gp.RESULT_WRITEBACK_FAILED
        assert "未确认收下" in result.detail

    @pytest.mark.parametrize("error", [
        CredentialError("GitHub 拒绝了账号或密码"),
        CaptchaError("撞上人机验证"),
        LoginError("登录 180s 内没拿到 user_session"),
        RemailError("取件 10 次仍未收到 GitHub 验证码邮件"),
        RuntimeError("browser crashed"),
    ])
    def test_login_failure_never_writes_back_and_closes_driver(self, monkeypatch, error):
        _patch_flow(monkeypatch, login_error=error)
        calls = _patch_writeback(monkeypatch)
        result, driver, _ = self._run(
            _cfg(), GitHubProvisionAccount(username="Steven", password="pw"),
            FakeRemail(hit=_hit()))
        assert result.status == gp.RESULT_LOGIN_FAILED
        assert calls == []
        assert driver.exited is True

    def test_result_never_carries_the_plaintext_session(self, monkeypatch):
        """结果对象会进日志和汇总报告，凭据只能留长度。"""
        _patch_flow(monkeypatch, session="s3cr3t-user-session-value")
        _patch_writeback(monkeypatch)
        result, _, _ = self._run(
            _cfg(), GitHubProvisionAccount(username="Steven", password="pw"),
            FakeRemail(hit=_hit()))
        assert result.session_len == len("s3cr3t-user-session-value")
        assert "s3cr3t" not in repr(result)


class TestProvisionAccounts:
    def _factory(self, made=None):
        def factory(cfg, account):
            if made is not None:
                made.append(account.name)
            return FakeDriver(FakePage([{}]))
        return factory

    def _boom_factory(self):
        """前置校验没拦住就会走到这里 —— 起浏览器本身就是失败。

        用爆炸工厂而不是正常工厂：校验漏掉时会掉进真实登录循环里干等超时，
        测试表现为「卡住」而不是「红」，那种红看不出是哪条断言没守住。
        """
        def factory(cfg, account):
            raise AssertionError("前置校验没拦住，不该起浏览器")
        return factory

    def test_requires_config_sync_enabled(self):
        """写回是这条链路的唯一出口，与 remote_sync 的凭据回写同一套前置判断。"""
        with pytest.raises(ConfigError) as exc:
            gp.provision_accounts(_cfg(sync=_sync(enabled=False)),
                                  remail=FakeRemail(hit=_hit()),
                                  driver_factory=self._boom_factory())
        assert "config_sync.enabled" in str(exc.value)

    def test_requires_usable_platform_url(self):
        with pytest.raises(ConfigError) as exc:
            gp.provision_accounts(_cfg(sync=_sync(url="不是地址")),
                                  remail=FakeRemail(hit=_hit()),
                                  driver_factory=self._boom_factory())
        assert "config_sync.url" in str(exc.value)

    @pytest.mark.parametrize("missing", ["remail_base_url", "remail_api_keys"])
    def test_requires_remail_config_when_client_not_injected(self, missing):
        """缺收件服务就别开跑：跑到设备验证码那步才失败，邮件已经发出去了。"""
        overrides = {missing: "" if missing == "remail_base_url" else []}
        with pytest.raises(ConfigError) as exc:
            gp.provision_accounts(_cfg(**overrides), driver_factory=self._boom_factory())
        assert missing in str(exc.value)

    def test_injected_client_skips_remail_config_check(self, monkeypatch):
        """调用方自带客户端时不该再要求配置 —— 那是构造客户端才需要的东西。"""
        _patch_flow(monkeypatch)
        _patch_writeback(monkeypatch)
        report = gp.provision_accounts(_cfg(remail_base_url="", remail_api_keys=[]),
                                       remail=FakeRemail(hit=_hit()),
                                       driver_factory=self._factory())
        assert report.provisioned == 1

    def test_empty_accounts_is_a_config_error(self):
        with pytest.raises(ConfigError):
            gp.provision_accounts(_cfg(accounts=[]), remail=FakeRemail(hit=_hit()),
                                  driver_factory=self._boom_factory())

    def test_only_filters_and_unknown_name_raises(self, monkeypatch):
        _patch_flow(monkeypatch)
        _patch_writeback(monkeypatch)
        entries = [GitHubProvisionAccount(username="A", password="p"),
                   GitHubProvisionAccount(username="B", password="p")]
        made = []
        report = gp.provision_accounts(_cfg(accounts=entries), only=["B"],
                                       remail=FakeRemail(hit=_hit()),
                                       driver_factory=self._factory(made))
        assert [r.username for r in report.results] == ["B"]
        assert made == ["gh-B"]

        with pytest.raises(ConfigError) as exc:
            gp.provision_accounts(_cfg(accounts=entries), only=["Nope"],
                                  remail=FakeRemail(hit=_hit()),
                                  driver_factory=self._factory())
        assert "Nope" in str(exc.value)

    def test_one_bad_account_does_not_stop_the_batch(self, monkeypatch):
        """一个账号的意外不该让剩下的都不跑 —— 整批白跑的代价太大。"""
        _patch_writeback(monkeypatch)
        calls = {"n": 0}

        def fake_login(page, username, password, **kwargs):
            calls["n"] += 1
            if username == "Boom":
                raise RuntimeError("browser crashed")
            return "sess-abcdefghij"

        monkeypatch.setattr(gp, "login_for_session", fake_login)
        monkeypatch.setattr(gp, "probe_status_with_browser",
                            lambda page, profile_url=gp.GITHUB_PROFILE_URL:
                            (gp.STATUS_ACTIVE, "ok"))
        entries = [GitHubProvisionAccount(username="Boom", password="p"),
                   GitHubProvisionAccount(username="Fine", password="p")]
        report = gp.provision_accounts(_cfg(accounts=entries),
                                       remail=FakeRemail(hit=_hit()),
                                       driver_factory=self._factory())
        assert calls["n"] == 2
        assert [(r.username, r.status) for r in report.results] == [
            ("Boom", gp.RESULT_LOGIN_FAILED), ("Fine", gp.RESULT_PROVISIONED)]
        assert (report.provisioned, report.failed) == (1, 1)
        assert report.ok is True

    def test_all_failed_report_is_not_ok(self, monkeypatch):
        _patch_flow(monkeypatch, status=gp.STATUS_BANNED)
        _patch_writeback(monkeypatch)
        report = gp.provision_accounts(_cfg(), remail=FakeRemail(hit=_hit()),
                                       driver_factory=self._factory())
        assert report.ok is False and report.failed == 1


class TestGitHubProvisionConfig:
    def test_from_raw_parses_accounts(self):
        cfg = GitHubProvisionConfig.from_raw({
            "enabled": True,
            "remail_base_url": " https://remail.example.com/ ",
            "remail_api_keys": ["rk-1", "  ", "rk-2 "],
            "accounts": [
                {"username": " Steven ", "password": "pw", "client_id": " cid "},
                {"username": "", "password": "pw"},          # 没名字，定位不了也入不了池
                "不是对象",
                {"username": "Alt", "password": "pw2", "email_name": "shiao1974"},
            ],
        })
        # 只去空白：尾斜杠由 Remail.__init__ 自己 rstrip（remail.py），
        # 两处都做等于同一件事写两遍
        assert cfg.remail_base_url == "https://remail.example.com/"
        assert cfg.remail_api_keys == ["rk-1", "rk-2"]
        assert [a.username for a in cfg.accounts] == ["Steven", "Alt"]
        assert cfg.accounts[0].client_id == "cid"
        # 邮箱前缀留空就按用户名找；填了就以它为准
        assert cfg.accounts[0].mailbox_name == "Steven"
        assert cfg.accounts[1].mailbox_name == "shiao1974"

    def test_from_raw_defaults_and_floors(self):
        cfg = GitHubProvisionConfig.from_raw({"remail_max_tries": 0,
                                             "remail_poll_seconds": 0,
                                             "login_timeout": 1})
        assert cfg.enabled is False
        assert (cfg.remail_max_tries, cfg.remail_poll_seconds) == (1, 1)
        # 时间盒太短会把「还在等验证码邮件」的登录判死
        assert cfg.login_timeout == 30
        assert cfg.platform_status_recheck is False

    def test_from_raw_tolerates_garbage(self):
        for raw in (None, [], "x", {}):
            cfg = GitHubProvisionConfig.from_raw(raw)
            assert cfg.accounts == [] and cfg.enabled is False

    def test_to_dict_roundtrip(self):
        raw = GitHubProvisionConfig.from_raw({
            "enabled": True, "remail_base_url": "https://r.example.com",
            "remail_api_keys": ["rk-1"], "platform_status_recheck": True,
            "accounts": [{"username": "Steven", "password": "pw",
                          "email_name": "e", "client_id": "c"}],
        }).to_dict()
        assert GitHubProvisionConfig.from_raw(raw).accounts[0].email_name == "e"
        assert raw["platform_status_recheck"] is True

    def test_select(self):
        entries = [GitHubProvisionAccount(username="A", password="p"),
                   GitHubProvisionAccount(username="B", password="p")]
        cfg = GitHubProvisionConfig(accounts=entries)
        assert [a.username for a in cfg.select()] == ["A", "B"]
        assert [a.username for a in cfg.select(["B"])] == ["B"]
        with pytest.raises(ConfigError):
            cfg.select(["C"])


class TestBuildConfigWiring:
    @staticmethod
    def _raw(**provision):
        return {
            "accounts": [{"name": "A", "url": "https://a.example.com", "cookie": "c"}],
            "github_provision": provision,
        }

    def test_enabled_without_remail_config_fails_fast(self):
        from newapi_checkin import config as cfgmod

        with pytest.raises(ConfigError) as exc:
            cfgmod.build_config(self._raw(enabled=True))
        text = str(exc.value)
        assert "remail_base_url" in text and "remail_api_keys" in text
        assert "accounts 为空" in text

    def test_disabled_section_is_not_validated(self):
        from newapi_checkin import config as cfgmod

        cfg = cfgmod.build_config(self._raw(enabled=False))
        assert cfg.github_provision.enabled is False

    def test_full_section_loads(self):
        from newapi_checkin import config as cfgmod

        cfg = cfgmod.build_config(self._raw(
            enabled=True, remail_base_url="https://r.example.com",
            remail_api_keys=["rk-1"],
            accounts=[{"username": "Steven", "password": "pw"}]))
        assert cfg.github_provision.accounts[0].username == "Steven"

    def test_missing_section_defaults_to_disabled(self):
        from newapi_checkin import config as cfgmod

        cfg = cfgmod.build_config(
            {"accounts": [{"name": "A", "url": "https://a.example.com", "cookie": "c"}]})
        assert cfg.github_provision.enabled is False
        assert cfg.github_provision.accounts == []


def test_remote_config_cannot_wipe_local_github_provision():
    """GitHub 密码是本机物料：平台下发同名节不能把它覆盖掉。"""
    from newapi_checkin.remote_sync import _merge_payload

    local = {"github_provision": {"enabled": True, "accounts": [
        {"username": "Steven", "password": "pw"}]}}
    merged = _merge_payload(local, {"github_provision": {"enabled": False,
                                                         "accounts": []},
                                    "accounts": [{"name": "X"}]})
    assert merged["github_provision"]["accounts"][0]["password"] == "pw"
    assert merged["accounts"] == [{"name": "X"}]








