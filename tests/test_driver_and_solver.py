"""浏览器层与 S3/S4 逻辑：用假 Playwright 对象覆盖，不需要真浏览器。"""

import pytest

from newapi_checkin import client as api
from newapi_checkin import config as cfgmod
from newapi_checkin.ai import prompts
from newapi_checkin.ai.vision import PageVerdict
from newapi_checkin.cf import detect, solver
from newapi_checkin.cf import driver_camoufox as camoufox_mod
from newapi_checkin.cf import driver_patchright as patchright_mod
from newapi_checkin.cf.driver_base import BrowserDriver
from newapi_checkin.cf.driver_camoufox import CamoufoxDriver
from newapi_checkin.cf.driver_patchright import PatchrightDriver

CHALLENGE = ("Just a moment...",
             "<html><script src='/cdn-cgi/challenge-platform/x'></script></html>")
TURNSTILE = ("Just a moment...",
             "<html><div class='cf-turnstile'></div>"
             "<script src='https://challenges.cloudflare.com/turnstile/v0/api.js'></script>"
             "<script src='/cdn-cgi/challenge-platform/x'></script></html>")
NORMAL = ("控制台 - New API", "<html><body>今日额度</body></html>")

UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:133.0) Gecko/20100101 Firefox/133.0"


class FakeLocator:
    def __init__(self, box=None):
        self._box = box
        self.typed = []
        self.clicked = 0

    @property
    def first(self):
        return self

    def count(self):
        return 1 if self._box is not None else 0

    def bounding_box(self):
        return self._box

    def click(self):
        self.clicked += 1

    def fill(self, _value):
        pass

    def type(self, value, delay=None):
        self.typed.append(value)


class FakeMouse:
    def __init__(self):
        self.moves = []
        self.clicks = []

    def move(self, x, y):
        self.moves.append((x, y))

    def click(self, x, y):
        self.clicks.append((x, y))


class FakeKeyboard:
    def __init__(self):
        self.keys = []

    def press(self, key):
        self.keys.append(key)


class FakePage:
    viewport_size = None

    def __init__(self, frames=None, boxes=None, fetches=None, turnstile_token="",
                 redirect_url=""):
        self._frames = list(frames or [NORMAL])
        self._current = None
        self._boxes = boxes or {}
        self._fetches = fetches or {}
        self._turnstile_token = turnstile_token
        self._redirect_url = redirect_url
        self.mouse = FakeMouse()
        self.keyboard = FakeKeyboard()
        self.url = "https://site.example.com/"
        self.goto_calls = []
        self.fetch_calls = []
        self.init_scripts = []
        self.locators = {}

    def _advance(self):
        if len(self._frames) > 1:
            self._current = self._frames.pop(0)
        else:
            self._current = self._frames[0]
        return self._current

    def goto(self, url, **_kw):
        self.goto_calls.append(url)
        self.url = self._redirect_url or url

    def add_init_script(self, script):
        self.init_scripts.append(script)

    def title(self):
        return self._advance()[0]

    def content(self):
        return (self._current or self._frames[0])[1]

    def locator(self, selector):
        loc = self.locators.get(selector)
        if loc is None:
            loc = FakeLocator(self._boxes.get(selector))
            self.locators[selector] = loc
        return loc

    def screenshot(self, **_kw):
        return b"\x89PNG-fake"

    def evaluate(self, script, arg=None):
        if "navigator.userAgent" in script:
            return UA
        if "navigator.languages" in script:
            return "zh-CN,zh,en-US"
        if "window.innerWidth" in script:
            return [1280, 800]
        if "turnstile.render" in script:
            self._turnstile_token = "token-from-mounted-widget"
            return True
        if "cf-turnstile-response" in script:
            return self._turnstile_token
        if "fetch(" in script:
            self.fetch_calls.append((arg["method"], arg["url"], arg["headers"]))
            return self._fetches.get(arg["url"], {"ok": False, "status": 0,
                                                  "body": "no stub", "headers": {}})
        raise AssertionError(f"未预期的 evaluate: {script[:60]}")


class FakeContext:
    def __init__(self, page, cookies=None):
        self.pages = [page]
        self._cookies = cookies or []
        self.added = []
        self.extra_headers = {}
        self.timeout = None
        self.closed = False

    def set_default_timeout(self, ms):
        self.timeout = ms

    def set_extra_http_headers(self, headers):
        self.extra_headers.update(headers)

    def cookies(self):
        return list(self._cookies)

    def add_cookies(self, items):
        self.added.extend(items)
        self._cookies.extend(items)

    def new_page(self):
        raise AssertionError("不该走到 new_page")

    def close(self):
        self.closed = True


class FakeDriver(BrowserDriver):
    name = "fake"

    def __init__(self, cfg, account, context):
        super().__init__(cfg, account, None)
        self._ctx = context

    def _launch(self):
        self._closers.append(self._ctx.close)
        return self._ctx


def make_cfg(**browser):
    raw = {
        "browser": {"driver": "camoufox", "headless": True, "humanize": False,
                    "timeout": 2, **browser},
        "accounts": [{"name": "站点A", "url": "https://site.example.com",
                      "cookie": "session=abc; user=7"}],
    }
    return cfgmod.build_config(raw)


@pytest.fixture()
def wired(tmp_path, monkeypatch):
    monkeypatch.setattr(cfgmod, "PROFILES_DIR", tmp_path / "profiles")
    monkeypatch.setattr(solver, "SHOTS_DIR", tmp_path / "shots")
    return tmp_path


def build(page, cookies=None, **browser):
    cfg = make_cfg(**browser)
    account = cfg.accounts[0]
    driver = FakeDriver(cfg, account, FakeContext(page, cookies))
    return cfg, account, driver


class TestPatchrightExecutable:
    def test_resolves_explicit_executable_path(self, wired, tmp_path):
        executable = tmp_path / "browser" / "chromium"
        executable.parent.mkdir(parents=True)
        executable.write_bytes(b"armhf-placeholder")
        cfg, account, _ = build(FakePage(), driver="patchright", executable_path=str(executable))
        driver = PatchrightDriver(cfg, account, None)
        assert driver._resolve_executable_path() == executable

    def test_resolves_packaged_browser_default(self, wired, tmp_path, monkeypatch):
        executable = tmp_path / "browser" / "chromium"
        executable.parent.mkdir(parents=True)
        executable.write_bytes(b"armhf-placeholder")
        monkeypatch.setattr(patchright_mod, "ROOT", tmp_path)
        cfg, account, _ = build(FakePage(), driver="patchright")
        driver = PatchrightDriver(cfg, account, None)
        assert driver._resolve_executable_path() == executable


class TestCamoufoxExecutable:
    def test_resolves_explicit_executable_path(self, wired, tmp_path):
        executable = tmp_path / "browser" / "camoufox.exe"
        executable.parent.mkdir(parents=True)
        executable.write_bytes(b"windows-x86-placeholder")
        cfg, account, _ = build(FakePage(), executable_path=str(executable))
        driver = CamoufoxDriver(cfg, account, None)
        assert driver._resolve_executable_path() == executable

    def test_base_options_use_packaged_executable_and_version(self, wired, tmp_path, monkeypatch):
        browser_root = tmp_path / "browser" / "camoufox-x86"
        browser_root.mkdir(parents=True)
        executable = browser_root / "camoufox.exe"
        executable.write_bytes(b"windows-x86-placeholder")
        (browser_root / "version.json").write_text(
            '{"version":"152.0.4","build":"beta.28"}', encoding="utf-8"
        )
        monkeypatch.setattr(camoufox_mod, "ROOT", tmp_path)
        cfg, account, _ = build(
            FakePage(), executable_path="browser/camoufox-x86/camoufox.exe"
        )
        driver = CamoufoxDriver(cfg, account, None)
        options = driver._base_options()
        assert options["executable_path"] == str(executable)
        assert options["ff_version"] == 152

    def test_headed_config_falls_back_without_linux_display(self, wired, monkeypatch):
        monkeypatch.setattr(camoufox_mod.sys, "platform", "linux")
        monkeypatch.delenv("DISPLAY", raising=False)
        monkeypatch.delenv("WAYLAND_DISPLAY", raising=False)
        cfg, account, _ = build(FakePage(), headless=False)
        driver = CamoufoxDriver(cfg, account, None)
        assert driver._headless() is True


class TestPageState:
    def test_normal_page(self, wired):
        _, _, driver = build(FakePage([NORMAL]))
        with driver:
            state = driver.state()
        assert state.passed is True
        assert state.challenge is None

    def test_challenge_page(self, wired):
        _, _, driver = build(FakePage([CHALLENGE]))
        with driver:
            assert driver.state().challenge == detect.MANAGED_CHALLENGE

    def test_wait_until_passed_polls_until_resolved(self, wired):
        page = FakePage([CHALLENGE, CHALLENGE, NORMAL])
        _, _, driver = build(page)
        with driver:
            state = driver.wait_until_passed(timeout=3, poll=0.01)
        assert state.passed is True

    def test_wait_gives_up_after_timeout(self, wired):
        _, _, driver = build(FakePage([CHALLENGE]))
        with driver:
            state = driver.wait_until_passed(timeout=0.05, poll=0.01)
        assert state.passed is False

    def test_waf_short_circuits_wait(self, wired):
        waf = ("Attention Required! | Cloudflare", "<html>you have been blocked</html>")
        _, _, driver = build(FakePage([waf]))
        with driver:
            state = driver.wait_until_passed(timeout=5, poll=0.01)
        assert state.challenge == detect.WAF_BLOCK

    def test_context_gets_timeout_and_is_closed(self, wired):
        _, _, driver = build(FakePage())
        with driver:
            ctx = driver.context
            assert ctx.timeout == 2000
        assert ctx.closed is True


class ProbePage(FakePage):
    """支持轻量探针的假页面：轮询时不该再拉整页 HTML。"""

    def __init__(self, frames=None, **kwargs):
        super().__init__(frames=frames, **kwargs)
        self.content_calls = 0
        self.probe_calls = 0

    def content(self):
        self.content_calls += 1
        return super().content()

    def evaluate(self, script, arg=None):
        if "markers.challenge" in script:
            self.probe_calls += 1
            title, html = self._advance()
            low = html.lower()
            return {
                "url": self.url,
                "title": title,
                "challenge": any(m in low for m in arg["challenge"]),
                "js": any(m in low for m in arg["js"]),
                "turnstile": any(m in low for m in arg["turnstile"]),
                "waf": any(m in low for m in arg["waf"]),
                "login": any(m in low for m in arg["login"]),
                "password": 'type="password"' in low,
            }
        return super().evaluate(script, arg)


class TestLightweightProbe:
    def test_quick_state_avoids_pulling_full_html(self, wired):
        page = ProbePage([CHALLENGE])
        _, _, driver = build(page)
        with driver:
            state = driver.quick_state()
        assert state.challenge == detect.MANAGED_CHALLENGE
        assert page.probe_calls == 1
        assert page.content_calls == 0

    def test_wait_until_passed_uses_probe(self, wired):
        page = ProbePage([CHALLENGE, CHALLENGE, NORMAL])
        _, _, driver = build(page)
        with driver:
            state = driver.wait_until_passed(timeout=3, poll=0.01)
        assert state.passed is True
        assert page.content_calls == 0

    def test_falls_back_to_full_html_when_probe_unsupported(self, wired):
        page = FakePage([CHALLENGE])   # evaluate 对探针脚本会抛错
        _, _, driver = build(page)
        with driver:
            assert driver.quick_state().challenge == detect.MANAGED_CHALLENGE

    def test_probe_backoff_reduces_poll_count(self, wired):
        """长时间不变化时轮询要降频，而不是死磕每秒一次。"""
        page = ProbePage([CHALLENGE])
        _, _, driver = build(page)
        with driver:
            driver.wait_until_passed(timeout=3, poll=0.2)
        # 固定 0.2s 间隔会探测 ~15 次；1.5 倍退避应该明显更少
        assert page.probe_calls <= 9


class TestSessionHarvest:
    def test_cookie_dict_filters_foreign_domains(self, wired):
        cookies = [
            {"name": "cf_clearance", "value": "cf", "domain": "site.example.com"},
            {"name": "session", "value": "s", "domain": ".example.com"},
            {"name": "junk", "value": "j", "domain": "other.com"},
        ]
        _, _, driver = build(FakePage(), cookies)
        with driver:
            got = driver.cookie_dict()
        assert got == {"cf_clearance": "cf", "session": "s"}

    def test_accept_language_gets_q_values(self, wired):
        _, _, driver = build(FakePage())
        with driver:
            assert driver.accept_language() == "zh-CN,zh;q=0.9,en-US;q=0.8"

    def test_user_agent_and_viewport(self, wired):
        _, _, driver = build(FakePage())
        with driver:
            assert driver.user_agent() == UA
            assert driver.viewport() == (1280, 800)

    def test_turnstile_token_reads_page_value_without_logging_contents(self, wired):
        _, _, driver = build(FakePage(turnstile_token="token-from-widget"))
        with driver:
            assert driver.turnstile_token() == "token-from-widget"
            assert driver.wait_for_turnstile_token(timeout=0.01) == "token-from-widget"

    def test_missing_turnstile_token_times_out(self, wired):
        _, _, driver = build(FakePage())
        with driver:
            assert driver.wait_for_turnstile_token(timeout=0.01, poll=0.01) == ""

    def test_harvest_binds_ip_ua_and_proxy(self, wired):
        cookies = [{"name": "cf_clearance", "value": "cf", "domain": "site.example.com",
                    "expires": 4102444800.0}]
        _, account, driver = build(FakePage(), cookies)
        with driver:
            session = solver._harvest(driver, account, "8.8.8.8")
        assert session.cookies["cf_clearance"] == "cf"
        assert session.user_agent == UA
        assert session.exit_ip == "8.8.8.8"
        assert session.expires_at == 4102444800.0
        assert session.check("8.8.8.8", None)[0] is True
        assert session.check("1.1.1.1", None)[0] is False


class TestCookieInjection:
    def test_injects_config_cookies(self, wired):
        """浏览器 profile 本身没有登录态，S4 依赖这一步。"""
        _, account, driver = build(FakePage())
        with driver:
            count = driver.inject_cookies(account.cookie)
            added = driver.context.added
        assert count == 2
        assert {item["name"] for item in added} == {"session", "user"}
        assert all(item["domain"] == "site.example.com" for item in added)
        assert all(item["secure"] is True for item in added)

    def test_empty_cookie_is_noop(self, wired):
        _, _, driver = build(FakePage())
        with driver:
            assert driver.inject_cookies("") == 0

    def test_sets_site_api_headers_on_browser_context(self, wired):
        _, _, driver = build(FakePage())
        with driver:
            assert driver.set_extra_http_headers({"New-Api-User": "42"}) is True
            assert driver.context.extra_headers == {"New-Api-User": "42"}

    def test_seeds_gorouter_local_storage_auth_state(self, wired):
        page = FakePage()
        _, _, driver = build(page)
        with driver:
            assert driver.seed_auth_state(42) is True
        assert len(page.init_scripts) == 1
        assert "const id = 42;" in page.init_scripts[0]
        assert "localStorage.setItem('uid'" in page.init_scripts[0]
        assert "localStorage.setItem('user'" in page.init_scripts[0]


class TestTurnstileClick:
    BOX = {"x": 100.0, "y": 200.0, "width": 300.0, "height": 65.0}

    def test_geometry_click_hits_left_side_center(self, wired):
        page = FakePage([TURNSTILE],
                        boxes={"iframe[src*='challenges.cloudflare.com']": self.BOX})
        _, _, driver = build(page)
        with driver:
            assert driver.click_turnstile() is True
        # 复选框在组件左侧约 30px、垂直居中
        assert page.mouse.clicks == [(130.0, 232.5)]

    def test_returns_false_without_widget(self, wired):
        page = FakePage([CHALLENGE])
        _, _, driver = build(page)
        with driver:
            assert driver.click_turnstile() is False
        assert page.mouse.clicks == []

    def test_ai_locate_converts_local_coords_to_page_coords(self, wired):
        page = FakePage([TURNSTILE],
                        boxes={"iframe[src*='challenges.cloudflare.com']": self.BOX})
        _, _, driver = build(page)

        class AI:
            def locate(self, _png, width, height, _target):
                assert (width, height) == (320, 85)   # box 上下左右各扩 10px
                return 0.5, 0.5

        with driver:
            assert solver._click_turnstile_with_ai(driver, AI()) is True
        # clip 原点 (90,190) + 0.5 * (320,85)
        assert page.mouse.clicks == [(250.0, 232.5)]

    def test_ai_failure_falls_back_to_geometry(self, wired):
        page = FakePage([TURNSTILE],
                        boxes={"iframe[src*='challenges.cloudflare.com']": self.BOX})
        _, _, driver = build(page)

        class AI:
            def locate(self, *_a, **_k):
                return None

        with driver:
            assert solver._click_turnstile_with_ai(driver, AI()) is True
        assert page.mouse.clicks == [(130.0, 232.5)]

    def test_ai_full_page_locate_when_iframe_missing(self, wired):
        page = FakePage([CHALLENGE])
        _, _, driver = build(page)

        class AI:
            def locate(self, _png, width, height, _target):
                assert (width, height) == (1280, 800)
                return 0.25, 0.5

        with driver:
            assert solver._click_turnstile_with_ai(driver, AI()) is True
        assert page.mouse.clicks == [(320.0, 400.0)]


class TestImageCaptcha:
    def test_ocr_fill_and_submit(self, wired):
        page = FakePage([CHALLENGE], boxes={
            "img[src*='captcha']": {"x": 10.0, "y": 20.0, "width": 120.0, "height": 40.0},
            "input[name*='captcha']": {"x": 10.0, "y": 70.0, "width": 120.0, "height": 30.0},
        })
        _, _, driver = build(page)

        class AI:
            def ocr(self, _png):
                return "A7bK"

        with driver:
            assert solver._solve_image_captcha(driver, AI()) is True
        assert page.locators["input[name*='captcha']"].typed == ["A7bK"]
        assert page.keyboard.keys == ["Enter"]

    def test_no_captcha_image(self, wired):
        _, _, driver = build(FakePage([CHALLENGE]))

        class AI:
            def ocr(self, _png):
                raise AssertionError("不该调用 OCR")

        with driver:
            assert solver._solve_image_captcha(driver, AI()) is False

    def test_ocr_returns_nothing(self, wired):
        page = FakePage([CHALLENGE], boxes={
            "img[src*='captcha']": {"x": 0.0, "y": 0.0, "width": 100.0, "height": 40.0},
        })
        _, _, driver = build(page)

        class AI:
            def ocr(self, _png):
                return None

        with driver:
            assert solver._solve_image_captcha(driver, AI()) is False


BASE = "https://site.example.com"
SELF_OK = {"ok": True, "status": 200, "headers": {},
           "body": '{"success":true,"data":{"id":42,"username":"kiro"}}'}
CHECKIN_404 = {"ok": True, "status": 404, "headers": {},
               "body": '{"success":false,"message":"not found"}'}
CHECKIN_OK = {"ok": True, "status": 200, "headers": {},
              "body": '{"success":true,"message":"签到成功","data":{"quota_awarded":1000}}'}
CF_IN_PAGE = {"ok": True, "status": 403,
              "headers": {"server": "cloudflare", "cf-mitigated": "challenge"},
              "body": "<html><title>Just a moment...</title>__cf_chl</html>"}

ALL_FETCHES = {
    f"{BASE}/api/user/self": SELF_OK,
    f"{BASE}/api/user/checkin": CHECKIN_404,
    f"{BASE}/api/user/check_in": CHECKIN_OK,
}
STATUS_WITH_TURNSTILE = {
    "ok": True, "status": 200, "headers": {},
    "body": '{"data":{"turnstile_site_key":"0x4AAAA-test"}}',
}


class TestCheckinInPage:
    """S4：不迁移 cookie，直接在页面上下文里完成签到。"""

    def test_turnstile_query_parameter_is_encoded(self):
        assert solver._with_turnstile(
            "/api/user/checkin?foo=1", "abc+/=?"
        ) == "/api/user/checkin?foo=1&turnstile=abc%2B%2F%3D%3F"

    def test_reads_turnstile_site_key_from_status(self, wired):
        page = FakePage([NORMAL], fetches={f"{BASE}/api/status": STATUS_WITH_TURNSTILE})
        _, account, driver = build(page)
        with driver:
            assert solver._turnstile_site_key(driver, account) == "0x4AAAA-test"

    def test_mounts_official_turnstile_widget(self, wired):
        page = FakePage([NORMAL])
        _, _, driver = build(page)
        with driver:
            assert driver.mount_turnstile("0x4AAAA-test") is True
            assert driver.turnstile_token() == "token-from-mounted-widget"

    def test_full_flow_with_path_probing(self, wired):
        page = FakePage([NORMAL], fetches=ALL_FETCHES)
        _, account, driver = build(page)
        with driver:
            result = solver._checkin_in_page(driver, account)
        assert result.kind == api.SUCCESS
        assert result.quota == 1000
        assert result.path == "/api/user/check_in"
        assert result.user_id == 42
        methods = [(m, u) for m, u, _ in page.fetch_calls]
        assert methods[0] == ("GET", f"{BASE}/api/user/self")
        # 拿到 id 之后每个 POST 都带 New-Api-User
        assert all(h.get("New-Api-User") == "42"
                   for m, _u, h in page.fetch_calls if m == "POST")

    def test_turnstile_required_then_token_retry(self, wired):
        token = "token-from-official-widget"
        fetches = {
            f"{BASE}/api/user/self": SELF_OK,
            f"{BASE}/api/user/checkin": {
                "ok": True, "status": 200, "headers": {},
                "body": '{"success":false,"message":"Turnstile token 为空"}',
            },
            f"{BASE}/api/user/checkin?turnstile=token-from-official-widget": CHECKIN_OK,
        }
        page = FakePage([NORMAL], fetches=fetches, turnstile_token=token)
        _, account, driver = build(page)
        account.checkin_path = "/api/user/checkin"
        with driver:
            result = solver._checkin_in_page(driver, account)
            assert result.kind == api.TURNSTILE_REQUIRED
            result = solver._checkin_in_page(driver, account, token)
        assert result.kind == api.SUCCESS
        assert page.fetch_calls[-1][1].endswith("?turnstile=token-from-official-widget")

    def test_known_user_id_skips_self(self, wired):
        page = FakePage([NORMAL], fetches=ALL_FETCHES)
        _, account, driver = build(page)
        account.user_id = 42
        with driver:
            result = solver._checkin_in_page(driver, account)
        assert result.kind == api.SUCCESS
        assert f"{BASE}/api/user/self" not in [u for _, u, _ in page.fetch_calls]

    def test_challenge_inside_page_is_reported(self, wired):
        page = FakePage([NORMAL], fetches={f"{BASE}/api/user/self": CF_IN_PAGE})
        _, account, driver = build(page)
        with driver:
            result = solver._checkin_in_page(driver, account)
        assert result.kind == api.CF_BLOCKED

    def test_fetch_error_becomes_network_error(self, wired):
        page = FakePage([NORMAL], fetches={})
        _, account, driver = build(page)
        with driver:
            result = solver._checkin_in_page(driver, account)
        assert result.kind == api.NETWORK_ERROR

    def test_already_done(self, wired):
        fetches = dict(ALL_FETCHES)
        fetches[f"{BASE}/api/user/check_in"] = {
            "ok": True, "status": 200, "headers": {},
            "body": '{"success":false,"message":"今日已签到"}',
        }
        page = FakePage([NORMAL], fetches=fetches)
        _, account, driver = build(page)
        with driver:
            result = solver._checkin_in_page(driver, account)
        assert result.kind == api.ALREADY_DONE


class ScriptedAI:
    def __init__(self, verdicts, point=(0.5, 0.5)):
        self.verdicts = list(verdicts)
        self.point = point
        self.calls = 0

    def classify_page(self, _png):
        self.calls += 1
        return self.verdicts.pop(0) if self.verdicts else PageVerdict()

    def locate(self, *_a, **_k):
        return self.point


class TestAiAssist:
    def test_turnstile_click_then_pass(self, wired):
        page = FakePage([TURNSTILE, NORMAL, NORMAL], boxes={
            "iframe[src*='challenges.cloudflare.com']":
                {"x": 100.0, "y": 200.0, "width": 300.0, "height": 65.0},
        })
        cfg, _, driver = build(page)
        ai = ScriptedAI([PageVerdict(prompts.TURNSTILE_CHECKBOX, 0.95, "有复选框")])
        with driver:
            state = driver.state()
            assert state.challenge == detect.TURNSTILE
            state = solver._ai_assist(driver, cfg, ai, state)
        assert state.passed is True
        assert ai.calls == 1
        assert page.mouse.clicks

    def test_dom_wins_over_ai_optimism(self, wired):
        """AI 说过了但 DOM 还是质询页，以 DOM 为准。"""
        page = FakePage([CHALLENGE])
        cfg, _, driver = build(page, timeout=1)
        ai = ScriptedAI([PageVerdict(prompts.PASSED, 0.9, "看起来正常")] * 3)
        with driver:
            state = solver._ai_assist(driver, cfg, ai, driver.state())
        assert state.passed is False
        assert ai.calls == 3

    def test_rate_limited_stops_immediately(self, wired):
        page = FakePage([CHALLENGE])
        cfg, _, driver = build(page)
        ai = ScriptedAI([PageVerdict(prompts.RATE_LIMITED, 0.9, "被限流")])
        with driver:
            solver._ai_assist(driver, cfg, ai, driver.state())
        assert ai.calls == 1


class FakeOptions:
    manual = False


class TestSolverRun:
    def test_redirect_to_login_is_terminal_and_does_not_call_ai(self, wired):
        login_html = '<form><label>用户名或电子邮件</label><input type="password"></form>'
        page = FakePage(
            [("GoRouter", login_html)],
            redirect_url="https://site.example.com/sign-in",
        )
        cfg, account, driver = build(page)

        class AI:
            def classify_page(self, _png):
                raise AssertionError("登录页不应该进入 AI 过盾")

        with driver:
            outcome = solver._run(driver, cfg, account, None, FakeOptions(), AI())
        assert outcome.ok is False
        assert outcome.terminal is True
        assert outcome.result_kind == api.LOGIN_REQUIRED
        assert "登录页" in outcome.detail
        assert "完整登录 cookie" in outcome.detail

    def test_known_user_id_is_sent_before_dashboard_navigation(self, wired):
        page = FakePage([NORMAL], fetches=ALL_FETCHES)
        cfg, account, driver = build(page)
        account.user_id = 42
        with driver:
            context = driver.context
            outcome = solver._run(driver, cfg, account, None, FakeOptions(), None)
        assert outcome.ok is True
        assert context.extra_headers == {"New-Api-User": "42"}
        assert len(page.init_scripts) == 1
        assert "const id = 42;" in page.init_scripts[0]

    def test_turnstile_required_is_retried_with_current_page_token(self, wired):
        token = "token-from-official-widget"
        fetches = {
            f"{BASE}/api/user/self": SELF_OK,
            f"{BASE}/api/user/checkin": {
                "ok": True, "status": 200, "headers": {},
                "body": '{"success":false,"message":"Turnstile token 为空"}',
            },
            f"{BASE}/api/user/checkin?turnstile=token-from-official-widget": CHECKIN_OK,
        }
        page = FakePage([NORMAL], fetches=fetches, turnstile_token=token)
        cfg, account, driver = build(page)
        account.checkin_path = "/api/user/checkin"
        with driver:
            outcome = solver._run(driver, cfg, account, None, FakeOptions(), None)
        assert outcome.ok is True
        assert outcome.api_result.kind == api.SUCCESS
        assert outcome.strategy == "S4"

    def test_turnstile_widget_is_mounted_when_dashboard_has_none(self, wired):
        fetches = {
            f"{BASE}/api/user/self": SELF_OK,
            f"{BASE}/api/status": STATUS_WITH_TURNSTILE,
            f"{BASE}/api/user/checkin": {
                "ok": True, "status": 200, "headers": {},
                "body": '{"success":false,"message":"Turnstile token 为空"}',
            },
            f"{BASE}/api/user/checkin?turnstile=token-from-mounted-widget": CHECKIN_OK,
        }
        page = FakePage([NORMAL], fetches=fetches)
        cfg, account, driver = build(page)
        account.checkin_path = "/api/user/checkin"
        with driver:
            outcome = solver._run(driver, cfg, account, None, FakeOptions(), None)
        assert outcome.ok is True
        assert outcome.api_result.kind == api.SUCCESS
        assert outcome.strategy == "S4"
        assert page.fetch_calls[-1][1].endswith("?turnstile=token-from-mounted-widget")

    def test_missing_page_token_is_reported_as_failure(self, wired):
        fetches = {
            f"{BASE}/api/user/self": SELF_OK,
            f"{BASE}/api/user/checkin": {
                "ok": True, "status": 200, "headers": {},
                "body": '{"success":false,"message":"Turnstile token 为空"}',
            },
        }
        page = FakePage([NORMAL], fetches=fetches)
        cfg, account, driver = build(page, timeout=0)
        account.checkin_path = "/api/user/checkin"
        with driver:
            outcome = solver._run(driver, cfg, account, None, FakeOptions(), None)
        assert outcome.ok is False
        assert outcome.api_result.kind == api.TURNSTILE_REQUIRED
        assert "没有生成有效 token" in outcome.detail

    def test_challenge_resolves_then_checkin_in_page(self, wired):
        page = FakePage([CHALLENGE, NORMAL, NORMAL], fetches=ALL_FETCHES)
        cookies = [{"name": "cf_clearance", "value": "cf", "domain": "site.example.com",
                    "expires": 4102444800.0}]
        cfg, account, driver = build(page, cookies)
        with driver:
            injected = driver.context
            outcome = solver._run(driver, cfg, account, "8.8.8.8", FakeOptions(), None)
        assert outcome.ok is True
        assert outcome.strategy == "S4"
        assert outcome.api_result.kind == api.SUCCESS
        assert outcome.cf.cookies["cf_clearance"] == "cf"
        assert outcome.cf.exit_ip == "8.8.8.8"
        assert page.goto_calls == ["https://site.example.com/dashboard"]
        # 进站前必须注入登录 cookie，否则页内 fetch 会 401
        assert {item["name"] for item in injected.added} >= {"session", "user"}

    def test_waf_block_is_terminal(self, wired):
        waf = ("Attention Required! | Cloudflare",
               "<html>sorry, you have been blocked</html>")
        page = FakePage([waf], fetches=ALL_FETCHES)
        cfg, account, driver = build(page)
        with driver:
            outcome = solver._run(driver, cfg, account, None, FakeOptions(), None)
        assert outcome.ok is False
        assert outcome.terminal is True
        assert "WAF" in outcome.detail

    def test_unresolved_challenge_keeps_artifacts(self, wired):
        # 真正过不去时，页内 API 也一定是被拦的；两者一致才是自洽的现场
        page = FakePage([CHALLENGE], fetches={f"{BASE}/api/user/self": CF_IN_PAGE})
        cfg, account, driver = build(page, timeout=1)
        with driver:
            outcome = solver._run(driver, cfg, account, None, FakeOptions(), None)
        assert outcome.ok is False
        assert outcome.terminal is False
        assert "现场证据" in outcome.detail
        shots = list((wired / "shots").glob("*/cf-fail.*"))
        assert {p.suffix for p in shots} == {".png", ".html"}

    def test_false_positive_challenge_is_rescued_by_page_api(self, wired):
        """CF 把 JS 检测脚本注入到已过盾的正常页面时，DOM 会误判成质询页。

        这时页内 API 是通的，必须以实测为准继续签到，而不是死等到超时。
        """
        injected = ("控制台 - New API",
                    "<html><body>今日额度"
                    "<script src='/cdn-cgi/challenge-platform/h/b/scripts/jsd/main.js'>"
                    "</script></body></html>")
        page = FakePage([injected], fetches=ALL_FETCHES)
        cfg, account, driver = build(page, timeout=1)
        with driver:
            # 注入的 JS 检测脚本不该被当成质询页
            assert driver.state().passed is True
            outcome = solver._run(driver, cfg, account, None, FakeOptions(), None)
        assert outcome.ok is True
        assert outcome.api_result.kind == api.SUCCESS

    def test_ai_and_dom_disagreement_is_settled_by_page_api(self, wired):
        """AI 说过了、DOM 说没过：用站点 API 裁决，不再无限期相信 DOM。"""
        page = FakePage([CHALLENGE], fetches=ALL_FETCHES)
        cfg, account, driver = build(page, timeout=1)
        ai = ScriptedAI([PageVerdict(prompts.PASSED, 0.99, "正常业务页面")] * 3)
        with driver:
            state = solver._ai_assist(driver, cfg, ai, driver.state(), account)
        assert state.passed is True
        assert ai.calls == 1          # 一轮就定论，不再空转 3 轮

    def test_disagreement_without_account_still_trusts_dom(self, wired):
        """拿不到账号上下文时无法实测，仍以 DOM 为准（保持保守行为）。"""
        page = FakePage([CHALLENGE])
        cfg, _, driver = build(page, timeout=1)
        ai = ScriptedAI([PageVerdict(prompts.PASSED, 0.99, "看起来正常")] * 3)
        with driver:
            state = solver._ai_assist(driver, cfg, ai, driver.state())
        assert state.passed is False
        assert ai.calls == 3

    def test_falls_back_to_fast_path_when_page_fetch_unavailable(self, wired):
        page = FakePage([CHALLENGE, NORMAL, NORMAL], fetches={})
        cookies = [{"name": "cf_clearance", "value": "cf", "domain": "site.example.com"}]
        cfg, account, driver = build(page, cookies)
        with driver:
            outcome = solver._run(driver, cfg, account, None, FakeOptions(), None)
        assert outcome.ok is True
        assert outcome.api_result is None
        assert outcome.strategy == "S2"
        assert outcome.cf.cookies["cf_clearance"] == "cf"

    def test_driver_unavailable_becomes_actionable_detail(self, wired, monkeypatch):
        from newapi_checkin.cf.driver_base import DriverUnavailable

        def boom(*_a, **_k):
            raise DriverUnavailable("Camoufox 浏览器未下载 -> 执行: python -m camoufox fetch")

        monkeypatch.setattr(solver, "_make_driver", boom)
        cfg = make_cfg()
        outcome = solver.solve(cfg=cfg, account=cfg.accounts[0], exit_ip=None,
                               options=FakeOptions(), ai=None)
        assert outcome.ok is False
        assert "camoufox fetch" in outcome.detail
