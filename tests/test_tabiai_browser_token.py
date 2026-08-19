"""tests/test_tabiai_browser_token.py

TaBiAI 的 Turnstile token 回退链路：CDP 拿不到 token 时改由脚本浏览器现取。

这些断言存在的原因是一个具体的线上死局：Actions 云端没有真实 Chrome 可供 CDP
接管，_turnstile_provider 返回 None，TabiAIClient 直接给出 TURNSTILE_REQUIRED，
而 _attempt_tabiai 以前在这里就地收工——tabiai 账号在云端根本签不上。
现在这条路会转进过盾链，借 S4 那套取 token 的手段现取一个立刻用掉。

全程假 driver / 假 client，不发任何真实网络请求。
"""

from __future__ import annotations

import json

import pytest

from newapi_checkin import client as api
from newapi_checkin import config as cfgmod
from newapi_checkin import runner as runner_mod
from newapi_checkin import tabiai as tabiai_mod
from newapi_checkin.cf import solver as solver_mod
from newapi_checkin.cf.driver_base import PageState
from newapi_checkin.cf.session_store import CFSession
from newapi_checkin.config import TABIAI_CHECKIN_PATH
from newapi_checkin.utils import now

UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/151.0.0.0"
SITE_KEY_BODY = '{"data":{"turnstile_site_key":"0x4AAAA-test"}}'
BASE = "https://tabiai.example.com"


class StubDriver:
    """够 solver 用的假浏览器：只关心取 token 会碰到的那几个动作。

    有意不继承 BrowserDriver——那会把 Playwright/Camoufox 的启动路径拖进来，
    而这里要守的是 solver 的编排顺序（先读页面 token、再点、再挂 widget）。
    """

    name = "stub"

    def __init__(self, *, token="", token_after_click="", mounted_token="",
                 site_key_body=None):
        self._token = token
        self._token_after_click = token_after_click
        self._mounted_token = mounted_token
        self._site_key_body = site_key_body
        self.injected = []
        self.goto_calls = []
        self.fetch_calls = []
        self.clicks = 0
        self.mounts = []
    # ---- 进站阶段 ----
    def inject_cookies(self, cookie_header):
        self.injected.append(cookie_header)
        return 0

    def seed_auth_state(self, _user_id):
        return False

    def set_extra_http_headers(self, _headers=None):
        return False

    def goto(self, url, timeout=None):
        self.goto_calls.append(url)
        return PageState(url=url, title="TaBiAI", challenge=None)

    def state(self):
        return PageState(url=f"{BASE}/sign-in", title="TaBiAI", challenge=None)

    def wait_until_passed(self, timeout=None, poll=1.0):
        return self.state()

    # ---- 会话收割 ----
    def cookie_dict(self):
        return {"cf_clearance": "cf-value"}

    def cookies(self):
        return [{"name": "cf_clearance", "value": "cf-value", "expires": 4102444800.0}]

    def user_agent(self):
        return UA

    def accept_language(self):
        return "zh-CN,zh"

    # ---- 取 token ----
    def turnstile_token(self):
        return self._token

    def click_turnstile(self):
        self.clicks += 1
        if self._token_after_click:
            self._token = self._token_after_click
            return True
        return False

    def find_element_box(self, _selectors):
        return None
    def mount_turnstile(self, site_key):
        self.mounts.append(site_key)
        if not self._mounted_token:
            return False
        self._token = self._mounted_token
        return True

    def fetch_in_page(self, url, method="GET", headers=None, body=None):
        self.fetch_calls.append((method, url))
        if self._site_key_body and url.endswith("/api/status"):
            return {"ok": True, "status": 200, "headers": {}, "body": self._site_key_body}
        return {"ok": False, "status": 0, "headers": {}, "body": "no stub"}

    def screenshot(self, clip=None, full_page=False):
        return b""

    def viewport(self):
        return 1280, 800

    def dump_artifacts(self, _target_dir, _tag="fail"):
        return None

    # solve() 用 with 管理生命周期；特殊方法必须定义在类上才会被 with 找到
    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        return None


class StubOptions:
    manual = False


def _tabiai_cfg(**browser):
    raw = {
        "browser": {"driver": "camoufox", "headless": True, "humanize": False,
                    "timeout": 2, **browser},
        "accounts": [{
            "name": "TaBiAI",
            "url": BASE,
            "login_method": "tabiai",
            "cookie": "new_api_refresh=sid.gen1",
        }],
    }
    cfg = cfgmod.build_config(raw)
    return cfg, cfg.accounts[0]


@pytest.fixture()
def wired(tmp_path, monkeypatch):
    """把 profile / 现场证据目录挪到临时盘，避免污染真实数据目录。"""
    monkeypatch.setattr(cfgmod, "PROFILES_DIR", tmp_path / "profiles")
    monkeypatch.setattr(solver_mod, "SHOTS_DIR", tmp_path / "shots")
    return tmp_path
class TestSolverTabiaiBranch:
    """solver 的 TaBiAI 分支：只收割会话（+ 可选 token），绝不在页内签到。"""

    def test_page_token_is_carried_back(self, wired):
        """页面上已有 token 就直接带回，不该再去读 /api/status 挂 widget。"""
        cfg, account = _tabiai_cfg()
        driver = StubDriver(token="token-on-page")
        outcome = solver_mod._run(driver, cfg, account, None, StubOptions(), None,
                                  want_turnstile_token=True)
        assert outcome.ok is True
        assert outcome.turnstile_token == "token-on-page"
        assert outcome.api_result is None          # 页面里没有登录态，不能页内签到
        assert outcome.cf.cookies["cf_clearance"] == "cf-value"
        assert driver.fetch_calls == []
        assert driver.mounts == []
        # refresh 只接受 POST，浏览器只能走公开登录页过盾
        assert driver.goto_calls == [f"{BASE}/sign-in"]

    def test_widget_is_mounted_when_sign_in_page_has_no_token(self, wired):
        """登录页上本来没有 Turnstile，要复用 S4 的「读 site key + 挂官方 widget」。"""
        cfg, account = _tabiai_cfg()
        driver = StubDriver(mounted_token="token-from-mounted-widget",
                            site_key_body=SITE_KEY_BODY)
        outcome = solver_mod._run(driver, cfg, account, None, StubOptions(), None,
                                  want_turnstile_token=True)
        assert outcome.turnstile_token == "token-from-mounted-widget"
        assert driver.mounts == ["0x4AAAA-test"]
        assert ("GET", f"{BASE}/api/status") in driver.fetch_calls

    def test_checkbox_click_comes_before_mounting(self, wired):
        """先几何点一下复选框：能出 token 就省掉一次 /api/status 和挂载。"""
        cfg, account = _tabiai_cfg()
        driver = StubDriver(token_after_click="token-after-click",
                            site_key_body=SITE_KEY_BODY)
        outcome = solver_mod._run(driver, cfg, account, None, StubOptions(), None,
                                  want_turnstile_token=True)
        assert outcome.turnstile_token == "token-after-click"
        assert driver.clicks == 1
        assert driver.mounts == []
    def test_missing_token_still_hands_back_the_cf_session(self, wired):
        """取不到 token 不能把过盾判成失败：cf_clearance 本身仍然值钱。

        真失败了才该丢会话；这里丢了的话，下一轮又要从零过一次 Cloudflare。
        """
        cfg, account = _tabiai_cfg(timeout=0)
        driver = StubDriver()
        outcome = solver_mod._run(driver, cfg, account, None, StubOptions(), None,
                                  want_turnstile_token=True)
        assert outcome.ok is True
        assert outcome.turnstile_token == ""
        assert outcome.cf is not None
        assert "未取到 Turnstile token" in outcome.detail

    def test_token_is_not_touched_unless_upper_layer_asks(self, wired):
        """上层没要求时行为要和以前完全一样：不点、不挂、不等，直接交回会话。

        CDP 那条路能用的时候，这里多点一次 Turnstile 会白烧站点的频率配额。
        """
        cfg, account = _tabiai_cfg()
        driver = StubDriver(token="token-on-page")
        outcome = solver_mod._run(driver, cfg, account, None, StubOptions(), None)
        assert outcome.ok is True
        assert outcome.turnstile_token == ""
        assert driver.clicks == 0
        assert driver.mounts == []
        assert driver.fetch_calls == []
        assert outcome.detail == "站点过盾完成，交回 TaBiAI 签到链路"

    def test_solve_entry_passes_the_flag_down(self, wired, monkeypatch):
        """solve() 是唯一对外入口，want_turnstile_token 必须能穿到 _run。"""
        cfg, account = _tabiai_cfg()
        driver = StubDriver(token="token-on-page")
        monkeypatch.setattr(solver_mod, "_make_driver", lambda *_a, **_k: driver)
        outcome = solver_mod.solve(cfg=cfg, account=account, exit_ip=None,
                                   options=StubOptions(), ai=None,
                                   want_turnstile_token=True)
        assert outcome.turnstile_token == "token-on-page"


def _cf_session():
    return CFSession(cookies={"cf_clearance": "cf-value"}, user_agent=UA,
                     accept_language="zh-CN,zh", expires_at=now() + 3600, saved_at=now())


def _install_fake_client(monkeypatch, script=None):
    """把 TabiAIClient 换成假实现；返回按调用顺序记账的实例列表。

    script 给定时按顺序返回预置结果（用来模拟 CF 拦截这类跟 token 无关的失败）；
    不给时照 tabiai.py 的真实语义走 provider：没 provider 或拿不到 token 就
    TURNSTILE_REQUIRED。这样测的是「runner 怎么喂 provider」，不是假件的心情。
    """
    calls: list = []
    queue = list(script or [])

    class Fake:
        def __init__(self, account, http, cookie, cf=None, on_rotate=None):
            self.account = account
            self.cookie = cookie
            self.cf = cf
            self.on_rotate = on_rotate
            self.impersonate = "chrome131"
            self.provider = None
            self.token = None
            self.dry_run = None
            calls.append(self)

        def __enter__(self):
            return self

        def __exit__(self, *_exc):
            return None

        def checkin(self, turnstile_provider=None, dry_run=False):
            self.provider = turnstile_provider
            self.dry_run = dry_run
            if queue:
                return queue.pop(0)
            if turnstile_provider is None:
                return api.ApiResult(
                    api.TURNSTILE_REQUIRED,
                    message="需要 Turnstile token，但未启用 tabiai 浏览器取 token"
                            "（配置 tabiai.enabled）",
                    path=TABIAI_CHECKIN_PATH, user_id=42)
            self.token, error = turnstile_provider()
            if not self.token:
                return api.ApiResult(api.TURNSTILE_REQUIRED,
                                     message=error or "未取得 Turnstile token",
                                     path=TABIAI_CHECKIN_PATH, user_id=42)
            return api.ApiResult(api.SUCCESS, message="签到成功", quota=1000,
                                 path=TABIAI_CHECKIN_PATH, user_id=42)

    monkeypatch.setattr(tabiai_mod, "TabiAIClient", Fake)
    return calls
def _install_fake_solve(monkeypatch, outcome):
    """替掉真实过盾（不开浏览器），记录 solve 收到的每一组入参。"""
    seen: list = []

    def fake_solve(**kwargs):
        seen.append(kwargs)
        return outcome

    monkeypatch.setattr(solver_mod, "solve", fake_solve)
    return seen


def _make_runner(tmp_path, monkeypatch, *, use_browser=True, tabiai_enabled=False,
                 login_method=cfgmod.LOGIN_METHOD_TABIAI, **opts):
    cfg = cfgmod.build_config({
        "defaults": {"interval_seconds": [0, 0]},
        "tabiai": {"enabled": tabiai_enabled},
        "accounts": [{
            "name": "TaBiAI",
            "url": BASE,
            "login_method": login_method,
            "cookie": ("new_api_refresh=sid.gen1"
                       if login_method == cfgmod.LOGIN_METHOD_TABIAI
                       else "session=abc; user=7"),
        }],
    })
    monkeypatch.setattr(runner_mod, "SESSIONS_FILE", tmp_path / "sessions.json")
    monkeypatch.setattr(runner_mod, "probe_exit_ip", lambda proxy=None, timeout=5: None)
    runner = runner_mod.Runner(
        cfg, runner_mod.RunOptions(use_ai=False, use_browser=use_browser, **opts))
    return runner, cfg.accounts[0]


def _attempt(runner, account):
    """直接跑一轮尝试。

    刻意不走 _run_account：盾类结果在默认配置下会无限重试（时间盒是关着的），
    单元测试里那等于死循环。这里只关心「一轮之内的决策」。
    """
    return runner._attempt(account, runner.store.get(account.slug))
class TestRunnerFallsBackToBrowserToken:
    """CDP 拿不到 token 时必须进过盾链，而不是就地把账号判死。"""

    def test_turnstile_required_enters_the_solve_chain(self, wired, tmp_path, monkeypatch):
        """这条守的就是那个线上死局：以前 TURNSTILE_REQUIRED 在 S1 就 return 了。"""
        runner, account = _make_runner(tmp_path, monkeypatch)
        _install_fake_client(monkeypatch)
        seen = _install_fake_solve(monkeypatch, solver_mod.SolveOutcome(
            True, "S2", cf=_cf_session(), turnstile_token="tok-from-browser"))

        row = _attempt(runner, account)

        assert len(seen) == 1
        assert seen[0]["want_turnstile_token"] is True   # 明确要求这一轮带 token 回来
        assert row.status == api.SUCCESS

    def test_browser_token_is_spent_in_the_same_round(self, wired, tmp_path, monkeypatch):
        """token 短时一次性且绑当前浏览器上下文，必须当轮就交给 TabiAIClient 用掉。"""
        runner, account = _make_runner(tmp_path, monkeypatch)
        clients = _install_fake_client(monkeypatch)
        _install_fake_solve(monkeypatch, solver_mod.SolveOutcome(
            True, "S2", cf=_cf_session(), turnstile_token="tok-from-browser"))

        row = _attempt(runner, account)

        assert row.status == api.SUCCESS
        assert len(clients) == 2
        # 第一轮没 provider（tabiai.enabled=false），第二轮拿到一次性 provider
        assert clients[0].provider is None
        assert clients[1].token == "tok-from-browser"
        # 过盾拿到的 CF 会话也要一起交回去，别让第二次请求裸奔
        assert clients[1].cf is not None
        assert clients[1].cf.cookies["cf_clearance"] == "cf-value"

    def test_browser_token_is_never_persisted(self, wired, tmp_path, monkeypatch):
        """token 落盘毫无意义还危险：换一轮浏览器上下文它就废了。"""
        runner, account = _make_runner(tmp_path, monkeypatch)
        _install_fake_client(monkeypatch)
        _install_fake_solve(monkeypatch, solver_mod.SolveOutcome(
            True, "S2", cf=_cf_session(), turnstile_token="tok-from-browser"))

        _attempt(runner, account)
        runner.store.flush()
        saved = (tmp_path / "sessions.json").read_text(encoding="utf-8")

        assert "tok-from-browser" not in saved
        assert "cf-value" in saved            # 该存的 CF 会话仍然要存
        assert json.loads(saved)[account.slug]["cf"]["cookies"]["cf_clearance"] == "cf-value"
    def test_no_token_from_browser_keeps_turnstile_required(self, wired, tmp_path, monkeypatch):
        """两条取 token 的路都空手时，结论仍是 TURNSTILE_REQUIRED，交给主循环换 IP 重试。

        顺带守住「不重复发 refresh」：refresh 会轮转一代凭据，白转一次没好处，
        CDP 那条路还得先干等一个 token_interval 间隔才失败第二次。
        """
        runner, account = _make_runner(tmp_path, monkeypatch)
        clients = _install_fake_client(monkeypatch)
        _install_fake_solve(monkeypatch, solver_mod.SolveOutcome(
            True, "S2", cf=_cf_session(), turnstile_token=""))

        row = _attempt(runner, account)

        assert row.status == api.TURNSTILE_REQUIRED
        assert "仍未取到 Turnstile token" in row.detail
        assert len(clients) == 1

    def test_failed_solve_does_not_crash(self, wired, tmp_path, monkeypatch):
        """过盾本身失败（连浏览器都没起来）时按盾类失败收口，不能抛出去。"""
        runner, account = _make_runner(tmp_path, monkeypatch)
        clients = _install_fake_client(monkeypatch)
        _install_fake_solve(monkeypatch, solver_mod.SolveOutcome(
            False, "S2", detail="Camoufox 浏览器未下载"))

        row = _attempt(runner, account)

        assert row.status == api.CF_BLOCKED
        assert "Camoufox" in row.detail
        assert len(clients) == 1

    def test_disabled_browser_still_fails_in_place(self, wired, tmp_path, monkeypatch):
        """--no-browser 时没有任何取 token 的手段，白开过盾链只是浪费时间。"""
        runner, account = _make_runner(tmp_path, monkeypatch, use_browser=False)
        clients = _install_fake_client(monkeypatch)
        seen = _install_fake_solve(monkeypatch, solver_mod.SolveOutcome(
            True, "S2", cf=_cf_session(), turnstile_token="tok-from-browser"))

        row = _attempt(runner, account)

        assert row.status == api.TURNSTILE_REQUIRED
        assert seen == []
        assert len(clients) == 1

    def test_disabled_browser_run_is_summarised_as_skipped(self, wired, tmp_path, monkeypatch):
        """整轮语义不变：盾类问题 + 没有浏览器 = 跳过，不空转重试。"""
        runner, _account = _make_runner(tmp_path, monkeypatch, use_browser=False)
        _install_fake_client(monkeypatch)
        assert runner.run() == 0
        assert runner.summary.rows[0].status == "skipped"
    def test_cdp_token_path_is_unchanged(self, wired, tmp_path, monkeypatch):
        """CDP 仍是首选：能接管真实 Chrome 拿到 token 时压根不该进浏览器过盾链。"""
        from newapi_checkin.cf import driver_cdp

        runner, account = _make_runner(tmp_path, monkeypatch, tabiai_enabled=True)
        clients = _install_fake_client(monkeypatch)
        seen = _install_fake_solve(monkeypatch, solver_mod.SolveOutcome(
            True, "S2", cf=_cf_session(), turnstile_token="tok-from-browser"))
        monkeypatch.setattr(driver_cdp, "fetch_turnstile_token",
                            lambda _cfg, _account: ("cdp-token", ""))

        row = _attempt(runner, account)

        assert row.status == api.SUCCESS
        assert clients[0].token == "cdp-token"
        assert seen == []

    def test_cf_block_path_does_not_ask_for_a_token(self, wired, tmp_path, monkeypatch):
        """CF 盾拦住是另一回事：走原来的过盾链，不顺带点 Turnstile 白烧频率配额。"""
        runner, account = _make_runner(tmp_path, monkeypatch)
        _install_fake_client(monkeypatch, script=[
            api.ApiResult(api.CF_BLOCKED, message="命中 Cloudflare 质询"),
        ])
        seen = _install_fake_solve(monkeypatch, solver_mod.SolveOutcome(
            True, "S2", cf=_cf_session()))

        _attempt(runner, account)

        assert len(seen) == 1
        assert seen[0]["want_turnstile_token"] is False


class TestOneShotProvider:
    """_turnstile_provider 的两种形态：现成 token 用完即弃，否则照旧看 CDP 配置。"""

    def test_ready_token_does_not_consume_the_rate_limit_slot(self, wired, tmp_path,
                                                              monkeypatch):
        """token 已经拿在手里了，再走一次频率间隔就是白等 20 多分钟。"""
        runner, account = _make_runner(tmp_path, monkeypatch, tabiai_enabled=True)

        def boom():
            raise AssertionError("一次性 token 不该再去等 Turnstile 频率间隔")

        monkeypatch.setattr(runner, "_wait_turnstile_slot", boom)
        provider = runner._turnstile_provider(account, "ready-token")

        assert provider() == ("ready-token", "")

    def test_without_ready_token_the_cdp_decision_is_kept(self, wired, tmp_path, monkeypatch):
        """没有现成 token 时行为完全照旧：未启用 tabiai 就没有 provider。"""
        off, account_off = _make_runner(tmp_path, monkeypatch, tabiai_enabled=False)
        on, account_on = _make_runner(tmp_path, monkeypatch, tabiai_enabled=True)

        assert off._turnstile_provider(account_off) is None
        assert on._turnstile_provider(account_on) is not None
class TestNewapiCookieRegression:
    """_solve 是两种登录方式共用的，站点 Cookie 那条链路一个字都不能被带歪。"""

    def test_cookie_account_goes_back_to_the_http_fast_path(self, wired, tmp_path,
                                                            monkeypatch):
        """过盾拿到 CF 会话后仍由 _api_call 重发，且不该向 solver 要 token。"""
        runner, account = _make_runner(tmp_path, monkeypatch,
                                       login_method=cfgmod.LOGIN_METHOD_NEWAPI_COOKIE)
        clients = _install_fake_client(monkeypatch)
        seen = _install_fake_solve(monkeypatch, solver_mod.SolveOutcome(
            True, "S2", cf=_cf_session(), turnstile_token="不该被用到的 token"))
        scripted = [api.ApiResult(api.CF_BLOCKED, message="命中 Cloudflare 质询"),
                    api.ApiResult(api.SUCCESS, message="签到成功", quota=100,
                                  path="/api/user/check_in")]
        seen_cf: list = []

        def fake_api_call(_account, cf):
            seen_cf.append(cf)
            return scripted[min(len(seen_cf) - 1, len(scripted) - 1)]

        monkeypatch.setattr(runner, "_api_call", fake_api_call)
        row = _attempt(runner, account)

        assert row.status == api.SUCCESS
        assert seen[0]["want_turnstile_token"] is False
        assert len(seen_cf) == 2 and seen_cf[0] is None and seen_cf[1] is not None
        assert clients == []          # 站点 Cookie 账号不该碰 TabiAIClient

    def test_cookie_account_s4_result_wins(self, wired, tmp_path, monkeypatch):
        """S4 已经在浏览器里签完了，就用它的结果，不再多发一次 HTTP 请求。"""
        runner, account = _make_runner(tmp_path, monkeypatch,
                                       login_method=cfgmod.LOGIN_METHOD_NEWAPI_COOKIE)
        _install_fake_solve(monkeypatch, solver_mod.SolveOutcome(
            True, "S4", cf=_cf_session(),
            api_result=api.ApiResult(api.SUCCESS, message="签到成功", quota=1000,
                                     path="/api/user/check_in", user_id=7)))
        seen_cf: list = []

        def fake_api_call(_account, cf):
            seen_cf.append(cf)
            return api.ApiResult(api.CF_BLOCKED, message="命中 Cloudflare 质询")

        monkeypatch.setattr(runner, "_api_call", fake_api_call)
        row = _attempt(runner, account)

        assert row.status == api.SUCCESS
        assert row.quota == 1000
        assert len(seen_cf) == 1       # 只有 S1 那次，S4 之后不再重发
