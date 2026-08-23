"""TaBiAI 页内签到（S4）测试。

这条链路和站点 Cookie 的 S4 对齐：注入凭据 → 页内 refresh 换 Bearer token →
查是否已签 → 取 Turnstile token → 页内 POST 签到。

最要命的断言是「页内 refresh 轮转出的新代次必须回写」：new_api_refresh 有代次轮转 +
重放检测，浏览器里换了代次却没落盘，下一轮用旧代就会被站点判重放、整条会话被撤销，
只能重新签发。所以这里对回写路径压得最紧，包括 refresh 判定失败时也必须回写。
"""

import json

import pytest

from newapi_checkin import client as api
from newapi_checkin import config as cfgmod
from newapi_checkin.cf import solver as solver_mod
from newapi_checkin.cf.driver_base import PageState
from newapi_checkin.cf.session_store import CFSession

BASE = "https://tabi.example.com"
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/124 Safari/537.36"


def _refresh_ok(access_token="jwt-token", uid=42):
    return json.dumps({
        "success": True,
        "data": {"access_token": access_token, "user": {"id": uid, "username": "kiq"}},
    })


def _checkin_status(checked):
    return json.dumps({"success": True, "data": {"stats": {"checked_in_today": checked}}})


def _checkin_ok(message="签到成功，获得 100 额度"):
    return json.dumps({"success": True, "message": message, "data": {"quota": 100}})


class PageDriver:
    """可编程的假浏览器：按 (method, path) 给出页内 fetch 响应。

    cookie jar 用 dict 模拟，并支持「某次 fetch 之后 new_api_refresh 变成新代次」——
    真实站点就是通过 Set-Cookie 换代次的，而页内 JS 读不到 Set-Cookie，
    solver 只能从 jar 里取，所以这里必须照这个机制来模拟。
    """

    name = "page-stub"

    def __init__(self, *, routes=None, cookies=None, token="", rotate_on=None,
                 rotate_to="sid.gen2"):
        self._routes = routes or {}
        self._cookies = dict(cookies or {"cf_clearance": "cf", "new_api_refresh": "sid.gen1"})
        self._token = token
        self._rotate_on = rotate_on
        self._rotate_to = rotate_to
        self.injected = []
        self.fetch_calls = []
        self.clicks = 0
        self.mounts = []

    # ---- 进站 ----
    def inject_cookies(self, cookie_header):
        self.injected.append(cookie_header)
        return 1 if cookie_header else 0

    def seed_auth_state(self, _uid):
        return False

    def set_extra_http_headers(self, _headers=None):
        return False

    def goto(self, url, timeout=None):
        return PageState(url=url, title="TaBiAI", challenge=None)

    def state(self):
        return PageState(url=f"{BASE}/sign-in", title="TaBiAI", challenge=None)

    def wait_until_passed(self, timeout=None, poll=1.0):
        return self.state()

    # ---- 会话收割 ----
    def cookie_dict(self):
        return dict(self._cookies)

    def cookies(self):
        return [{"name": k, "value": v, "expires": 4102444800.0} for k, v in self._cookies.items()]

    def user_agent(self):
        return UA

    def accept_language(self):
        return "zh-CN,zh"

    # ---- 取 token ----
    def turnstile_token(self):
        return self._token

    def click_turnstile(self):
        self.clicks += 1
        return False

    def find_element_box(self, _selectors):
        return None

    def mount_turnstile(self, site_key):
        self.mounts.append(site_key)
        return False

    # ---- 页内请求 ----
    def fetch_in_page(self, url, method="GET", headers=None, body=None):
        path = url[len(BASE):] if url.startswith(BASE) else url
        self.fetch_calls.append((method, path, dict(headers or {})))
        # 轮转发生在请求之后：浏览器收到 Set-Cookie 才更新 jar
        key = (method, path.split("?")[0])
        response = self._routes.get(key)
        if self._rotate_on and key == self._rotate_on:
            self._cookies["new_api_refresh"] = self._rotate_to
        if response is None:
            return {"ok": False, "status": 0, "headers": {}, "body": "no route"}
        return response

    def screenshot(self, clip=None, full_page=False):
        return b""

    def viewport(self):
        return 1280, 800

    def dump_artifacts(self, _target_dir, _tag="fail"):
        return None

    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        return None


class StubOptions:
    manual = False


def _ok(body, status=200):
    return {"ok": True, "status": status, "headers": {}, "body": body}


def _account(cookie="new_api_refresh=sid.gen1"):
    cfg = cfgmod.build_config({
        "browser": {"driver": "camoufox", "headless": True, "humanize": False, "timeout": 2},
        "accounts": [{"name": "T", "url": BASE, "login_method": "tabiai", "cookie": cookie}],
    })
    return cfg, cfg.accounts[0]


@pytest.fixture()
def wired(tmp_path, monkeypatch):
    monkeypatch.setattr(cfgmod, "PROFILES_DIR", tmp_path / "profiles")
    monkeypatch.setattr(solver_mod, "SHOTS_DIR", tmp_path / "shots")
    return tmp_path


def _run(driver, cfg, account, *, on_rotate=None, ai=None, want_token=False,
         on_inflight=None, on_settled=None):
    return solver_mod._run(driver, cfg, account, None, StubOptions(), ai,
                           want_turnstile_token=want_token, on_rotate=on_rotate,
                           on_inflight=on_inflight, on_settled=on_settled)


def _cf():
    return CFSession(cookies={"cf_clearance": "cf"}, user_agent=UA,
                     accept_language="zh-CN", exit_ip=None, proxy=None,
                     expires_at=None, saved_at="2026-01-01T00:00:00+08:00")


class TestCredentialRotation:
    """轮转回写是这条链路的生命线，压得最紧的一组。"""

    def test_rotated_cookie_is_reported_upward(self, wired):
        """页内 refresh 换了代次，必须立刻交回上层落盘。"""
        cfg, account = _account()
        driver = PageDriver(
            routes={
                ("POST", "/api/user/auth/refresh"): _ok(_refresh_ok()),
                ("GET", "/api/user/checkin"): _ok(_checkin_status(True)),
            },
            rotate_on=("POST", "/api/user/auth/refresh"),
            rotate_to="sid.gen2",
        )
        rotated = []
        _run(driver, cfg, account, on_rotate=rotated.append)
        assert rotated == ["new_api_refresh=sid.gen2"]

    def test_rotation_is_reported_even_when_refresh_is_rejected(self, wired):
        """站点判 401 之前也可能已经换过代次；不回写就等于把凭据丢了。"""
        cfg, account = _account()
        driver = PageDriver(
            routes={("POST", "/api/user/auth/refresh"): _ok(
                json.dumps({"success": False, "code": "AUTH_UNAUTHORIZED",
                            "message": "unauthorized"}), status=401)},
            rotate_on=("POST", "/api/user/auth/refresh"),
            rotate_to="sid.gen9",
        )
        rotated = []
        outcome = _run(driver, cfg, account, on_rotate=rotated.append)
        assert rotated == ["new_api_refresh=sid.gen9"]
        assert outcome.api_result.kind == api.AUTH_FAILED

    def test_no_rotation_means_no_callback(self, wired):
        """代次没变就不要瞎回写：多余的落盘与回写只会制造噪音。"""
        cfg, account = _account()
        driver = PageDriver(
            routes={
                ("POST", "/api/user/auth/refresh"): _ok(_refresh_ok()),
                ("GET", "/api/user/checkin"): _ok(_checkin_status(True)),
            },
        )
        rotated = []
        _run(driver, cfg, account, on_rotate=rotated.append)
        assert rotated == []

    def test_without_callback_the_page_route_is_skipped(self, wired):
        """没有回写通道就不许走页内：宁可少一步，也不能把新代次丢在浏览器里。"""
        cfg, account = _account()
        driver = PageDriver(routes={("POST", "/api/user/auth/refresh"): _ok(_refresh_ok())})
        outcome = _run(driver, cfg, account, on_rotate=None)
        assert outcome.ok is True
        assert outcome.api_result is None, "不该在没有回写通道时做页内签到"
        assert outcome.cf is not None, "CF 会话仍要交回上层"
        # 压根不该发出 refresh 请求
        assert not any(p == "/api/user/auth/refresh" for _m, p, _h in driver.fetch_calls)


class TestPageCheckinFlow:
    """编排顺序：注入凭据 → refresh → 查已签 → 取 token → 签到。"""

    def test_credential_is_injected_into_the_browser(self, wired):
        """以前 TaBiAI 有意不注入（签到走 HTTP），页内 refresh 需要它才不会 401。"""
        cfg, account = _account()
        driver = PageDriver(routes={
            ("POST", "/api/user/auth/refresh"): _ok(_refresh_ok()),
            ("GET", "/api/user/checkin"): _ok(_checkin_status(True)),
        })
        _run(driver, cfg, account, on_rotate=lambda _v: None)
        assert driver.injected == ["new_api_refresh=sid.gen1"]

    def test_already_checked_short_circuits_before_turnstile(self, wired):
        """已签就不该去取 token：Turnstile 有 20 分钟级频率限制，能省必须省。"""
        cfg, account = _account()
        driver = PageDriver(routes={
            ("POST", "/api/user/auth/refresh"): _ok(_refresh_ok()),
            ("GET", "/api/user/checkin"): _ok(_checkin_status(True)),
        })
        outcome = _run(driver, cfg, account, on_rotate=lambda _v: None)
        assert outcome.ok is True
        assert outcome.strategy == "S4"
        assert outcome.api_result.kind == api.ALREADY_DONE
        assert driver.clicks == 0 and driver.mounts == [], "已签还去点 Turnstile 是浪费"

    def test_full_flow_completes_checkin_in_page(self, wired):
        cfg, account = _account()
        driver = PageDriver(
            routes={
                ("POST", "/api/user/auth/refresh"): _ok(_refresh_ok()),
                ("GET", "/api/user/checkin"): _ok(_checkin_status(False)),
                ("POST", "/api/user/checkin"): _ok(_checkin_ok()),
            },
            token="ts-token",
        )
        outcome = _run(driver, cfg, account, on_rotate=lambda _v: None)
        assert outcome.ok is True
        assert outcome.strategy == "S4"
        assert outcome.api_result.kind == api.SUCCESS
        # 签到必须带上 Bearer 与 turnstile
        posts = [(p, h) for m, p, h in driver.fetch_calls
                 if m == "POST" and p.startswith("/api/user/checkin")]
        assert posts, "没有发出页内签到请求"
        path, headers = posts[-1]
        assert "turnstile=ts-token" in path
        assert headers.get("Authorization") == "Bearer jwt-token"

    def test_user_id_from_refresh_is_remembered(self, wired):
        cfg, account = _account()
        driver = PageDriver(routes={
            ("POST", "/api/user/auth/refresh"): _ok(_refresh_ok(uid=777)),
            ("GET", "/api/user/checkin"): _ok(_checkin_status(True)),
        })
        outcome = _run(driver, cfg, account, on_rotate=lambda _v: None)
        assert account.user_id == 777
        assert outcome.api_result.user_id == 777

    def test_unknown_check_status_still_tries_to_check_in(self, wired):
        """查不出来是否已签时不能就此收工，照样往下签。"""
        cfg, account = _account()
        driver = PageDriver(
            routes={
                ("POST", "/api/user/auth/refresh"): _ok(_refresh_ok()),
                ("GET", "/api/user/checkin"): _ok("not json"),
                ("POST", "/api/user/checkin"): _ok(_checkin_ok()),
            },
            token="ts-token",
        )
        outcome = _run(driver, cfg, account, on_rotate=lambda _v: None)
        assert outcome.api_result.kind == api.SUCCESS


class TestFailureRouting:
    """失败要分流：定论直接带出去，可恢复的退回 HTTP 链路再试。"""

    def test_no_turnstile_token_does_not_fire_a_doomed_request(self, wired):
        """站点强校验 token，硬发只是白扔一次机会；交回上层换 IP 重来才有戏。"""
        cfg, account = _account()
        driver = PageDriver(routes={
            ("POST", "/api/user/auth/refresh"): _ok(_refresh_ok()),
            ("GET", "/api/user/checkin"): _ok(_checkin_status(False)),
            ("POST", "/api/user/checkin"): _ok(_checkin_ok()),
        })  # token 为空
        outcome = _run(driver, cfg, account, on_rotate=lambda _v: None)
        assert outcome.ok is False
        assert outcome.result_kind == api.TURNSTILE_REQUIRED
        assert not any(m == "POST" and p.startswith("/api/user/checkin")
                       for m, p, _h in driver.fetch_calls), "没 token 还发了签到请求"

    def test_auth_failure_is_terminal_and_does_not_fall_back(self, wired):
        """认证失败是定论。退回 HTTP 链路只会再 refresh 一次、白轮转一代凭据。"""
        cfg, account = _account()
        driver = PageDriver(routes={("POST", "/api/user/auth/refresh"): _ok(
            json.dumps({"success": False, "code": "AUTH_SESSION_REVOKED",
                        "message": "Unauthorized"}), status=401)})
        outcome = _run(driver, cfg, account, on_rotate=lambda _v: None)
        assert outcome.ok is False
        assert outcome.api_result is not None
        assert outcome.api_result.kind == api.AUTH_FAILED
        assert "撤销" in outcome.api_result.message

    def test_refresh_network_error_falls_back_to_http(self, wired):
        """页内网络抖动不是定论，把 CF 会话交回去让 HTTP 链路再试。"""
        cfg, account = _account()
        driver = PageDriver(routes={})  # refresh 无路由 -> ok=False
        outcome = _run(driver, cfg, account, on_rotate=lambda _v: None)
        assert outcome.ok is True
        assert outcome.api_result is None, "网络异常时应退回 HTTP 链路而不是给结论"
        assert outcome.cf is not None

    def test_checkin_network_error_falls_back_to_http(self, wired):
        cfg, account = _account()
        driver = PageDriver(
            routes={
                ("POST", "/api/user/auth/refresh"): _ok(_refresh_ok()),
                ("GET", "/api/user/checkin"): _ok(_checkin_status(False)),
                # POST 签到无路由 -> ok=False
            },
            token="ts-token",
        )
        outcome = _run(driver, cfg, account, on_rotate=lambda _v: None)
        assert outcome.ok is True
        assert outcome.api_result is None

    def test_business_failure_is_reported_as_is(self, wired):
        """源站明确说失败就如实带出去，别退回去重复消耗凭据。"""
        cfg, account = _account()
        driver = PageDriver(
            routes={
                ("POST", "/api/user/auth/refresh"): _ok(_refresh_ok()),
                ("GET", "/api/user/checkin"): _ok(_checkin_status(False)),
                ("POST", "/api/user/checkin"): _ok(
                    json.dumps({"success": False, "message": "签到功能已关闭"})),
            },
            token="ts-token",
        )
        outcome = _run(driver, cfg, account, on_rotate=lambda _v: None)
        assert outcome.ok is False
        assert outcome.api_result is not None
        assert outcome.api_result.kind != api.SUCCESS
        assert "签到功能已关闭" in (outcome.api_result.message or "")

    def test_fallback_still_carries_the_token_when_asked(self, wired):
        """退回 HTTP 链路时，若上层要过 token 就还得把它带上，别白取一次。"""
        cfg, account = _account()
        driver = PageDriver(routes={}, token="ts-token")  # refresh 失败 -> 退回
        outcome = _run(driver, cfg, account, on_rotate=lambda _v: None, want_token=True)
        assert outcome.api_result is None
        assert outcome.turnstile_token == "ts-token"


class TestRunnerWiring:
    """runner 侧：什么时候给 on_rotate、页内结果怎么被采用。"""

    @staticmethod
    def _runner(tmp_path, monkeypatch, **opts):
        from newapi_checkin import runner as runner_mod

        cfg, _ = _account()
        monkeypatch.setattr(runner_mod, "SESSIONS_FILE", tmp_path / "sessions.json")
        monkeypatch.setattr(runner_mod, "probe_exit_ip", lambda proxy=None, timeout=5: None)
        options = runner_mod.RunOptions(use_ai=False, use_browser=True, **opts)
        return runner_mod.Runner(cfg, options)

    def _captured_solve(self, monkeypatch, runner, outcome):
        """替掉 solve，记录 runner 到底传了什么参数。"""
        captured = {}

        def fake_solve(**kwargs):
            captured.update(kwargs)
            return outcome

        import newapi_checkin.cf.solver as sol

        monkeypatch.setattr(sol, "solve", fake_solve)
        return captured

    def test_rotate_callback_is_passed_for_tabiai(self, tmp_path, monkeypatch):
        runner = self._runner(tmp_path, monkeypatch)
        account = runner.cfg.accounts[0]
        outcome = solver_mod.SolveOutcome(True, "S4", cf=_cf(), api_result=api.ApiResult(
            api.SUCCESS, message="ok", path="/api/user/checkin", user_id=42))
        captured = self._captured_solve(monkeypatch, runner, outcome)
        row = runner._solve(account, runner.store.get(account.slug), None,
                            api.ApiResult(api.TURNSTILE_REQUIRED))
        assert callable(captured.get("on_rotate")), "TaBiAI 必须拿到轮转回调"
        assert row.status == api.SUCCESS

    def test_rotate_callback_actually_persists(self, tmp_path, monkeypatch):
        """回调不能是空壳：要落本地盘、更新内存、并尝试回写平台。"""
        runner = self._runner(tmp_path, monkeypatch)
        account = runner.cfg.accounts[0]
        writebacks = []
        monkeypatch.setattr(runner, "_writeback_refresh_cookie",
                            lambda acct, cookie: writebacks.append(cookie))
        runner._tabiai_rotate_callback(account)("new_api_refresh=sid.gen5")
        assert account.cookie.endswith("sid.gen5")
        assert runner.store.get(account.slug).refresh_cookie.endswith("sid.gen5")
        assert writebacks == ["new_api_refresh=sid.gen5"]

    def test_dry_run_does_not_check_in_from_the_page(self, tmp_path, monkeypatch):
        """--dry-run / cookie 检测只验证连通性，不该真的签到。"""
        runner = self._runner(tmp_path, monkeypatch, dry_run=True)
        account = runner.cfg.accounts[0]
        outcome = solver_mod.SolveOutcome(True, "S2", cf=_cf())
        captured = self._captured_solve(monkeypatch, runner, outcome)
        monkeypatch.setattr(runner, "_tabiai_api_call",
                            lambda acct, cf, token="": api.ApiResult(api.SUCCESS, message="dry"))
        runner._solve(account, runner.store.get(account.slug), None,
                      api.ApiResult(api.CF_BLOCKED))
        assert captured.get("on_rotate") is None, "dry-run 不该走页内签到"

    def test_newapi_cookie_gets_no_rotate_callback(self, tmp_path, monkeypatch):
        """站点 Cookie 不存在代次轮转，传回调只会造成误解。"""
        from newapi_checkin import runner as runner_mod

        cfg = cfgmod.build_config({
            "browser": {"driver": "camoufox", "headless": True, "timeout": 2},
            "accounts": [{"name": "N", "url": BASE, "cookie": "session=abc"}],
        })
        monkeypatch.setattr(runner_mod, "SESSIONS_FILE", tmp_path / "sessions.json")
        monkeypatch.setattr(runner_mod, "probe_exit_ip", lambda proxy=None, timeout=5: None)
        runner = runner_mod.Runner(cfg, runner_mod.RunOptions(use_ai=False, use_browser=True))
        account = runner.cfg.accounts[0]
        outcome = solver_mod.SolveOutcome(True, "S4", cf=_cf(), api_result=api.ApiResult(
            api.SUCCESS, message="ok", path="/api/user/checkin", user_id=1))
        captured = self._captured_solve(monkeypatch, runner, outcome)
        runner._solve(account, runner.store.get(account.slug), None,
                      api.ApiResult(api.CF_BLOCKED))
        assert captured.get("on_rotate") is None


class TestPageRefreshInflightAccounting:
    """页内 refresh 的代次悬空记账。

    HTTP 链路那条已经有 8 秒短超时压着悬空时长，页内这一发的超时却由浏览器/页面决定，
    烧掉的重放窗口可能更长，所以这条路同样得记账 —— 漏了的话超时后回到重试循环，闸门
    查不到标记，照样会拿旧代换 IP 重放。

    好在浏览器有个 HTTP 链路没有的优势：Set-Cookie 只要进了 cookie jar，代次就能被
    抢救回来。那种情况即使 fetch 判失败也该销账，不能白扣一代。
    """

    def _routes(self, refresh_body=None):
        routes = {("GET", "/api/user/checkin"): _ok(_checkin_status(True))}
        if refresh_body is not None:
            routes[("POST", "/api/user/auth/refresh")] = refresh_body
        return routes

    def test_mark_happens_before_the_page_fetch(self, wired):
        """记账必须早于请求发出，理由和 HTTP 链路一样。"""
        cfg, account = _account()
        order = []
        driver = PageDriver(routes=self._routes(_ok(_refresh_ok())))
        original = driver.fetch_in_page

        def traced(url, method="GET", headers=None, body=None):
            order.append(("fetch", method))
            return original(url, method, headers, body)

        driver.fetch_in_page = traced
        _run(driver, cfg, account, on_rotate=lambda _v: None,
             on_inflight=lambda value: order.append(("mark", value)),
             on_settled=lambda: order.append(("settle", "")))
        assert order[0] == ("mark", "new_api_refresh=sid.gen1")
        assert order[1][0] == "fetch"

    def test_successful_refresh_settles_the_account(self, wired):
        cfg, account = _account()
        settled = []
        driver = PageDriver(routes=self._routes(_ok(_refresh_ok())))
        _run(driver, cfg, account, on_rotate=lambda _v: None,
             on_settled=lambda: settled.append(True))
        assert settled == [True]

    def test_page_fetch_failure_keeps_the_mark_when_nothing_was_rescued(self, wired):
        """fetch 挂了且 jar 里还是旧代：站点侧状态不明，标记必须留着。"""
        cfg, account = _account()
        settled = []
        # 不给 refresh 路由 -> fetch_in_page 返回 ok=False，且不触发轮转
        driver = PageDriver(routes=self._routes(None))
        marked = []
        _run(driver, cfg, account, on_rotate=lambda _v: None,
             on_inflight=lambda value: marked.append(value),
             on_settled=lambda: settled.append(True))
        assert marked == ["new_api_refresh=sid.gen1"]
        assert settled == []                     # 没销账，闸门据此拦住下一轮

    def test_rescued_generation_settles_even_though_fetch_failed(self, wired):
        """fetch 判失败但 jar 里已经有新代次 —— 代次安全交班了，不该白扣一代。"""
        cfg, account = _account()
        settled, rotated = [], []
        driver = PageDriver(routes=self._routes(None),
                            rotate_on=("POST", "/api/user/auth/refresh"))
        _run(driver, cfg, account, on_rotate=rotated.append,
             on_settled=lambda: settled.append(True))
        assert rotated == ["new_api_refresh=sid.gen2"]     # 抢救成功
        assert settled == [True]                           # 因此销账

    def test_missing_hooks_do_not_break_the_page_route(self, wired):
        """回调是可选的：不接线时页内链路照跑，不能炸。"""
        cfg, account = _account()
        outcome = _run(driver=PageDriver(routes=self._routes(_ok(_refresh_ok()))),
                       cfg=cfg, account=account, on_rotate=lambda _v: None)
        assert outcome is not None

    def test_empty_jar_skips_marking(self, wired):
        """jar 里压根没有凭据时没什么可记账的，也不该拿空串去打标记。"""
        cfg, account = _account()
        marked = []
        driver = PageDriver(routes=self._routes(_ok(_refresh_ok())),
                            cookies={"cf_clearance": "cf"})
        _run(driver, cfg, account, on_rotate=lambda _v: None,
             on_inflight=marked.append)
        assert marked == []
