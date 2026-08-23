"""tests/test_refresh_inflight.py
测试：TaBiAI 代次悬空预算（refresh 超时后的重试闸门）

职责：
- 守住 Runner._refresh_inflight_blocked 的判定：超预算停手、预算内放行、
  指纹不匹配不拦、普通 Cookie 账号一律放行
- 守住 _run_account_with_retries 的 NETWORK_ERROR 分支：被拦下时**不能**再换 IP
- 守住 _tabiai_api_call -> TabiAIClient(on_inflight/on_settled) 的接线：
  请求发出前记账、拿到响应销账、超时保留标记
- 守住 refresh() 用的是 REFRESH_TIMEOUT_SECONDS 而不是 http.timeout

背景：旧代重放的安全窗口实测只有 20~45 秒（2026-08 对 tabitoken.cc），超窗重放会
把整条会话撤销（AUTH_SESSION_REVOKED），所有账号都得重新签发。所以下面每条断言挡的
都是同一件事：一次网络超时被无限换 IP 重试放大成整条会话作废。

tabiai 账号的构造方式只该有一份，_cfg / _runner 直接复用 test_run_state_report。
"""

import json

from newapi_checkin import client as api
from newapi_checkin import logger as log
from newapi_checkin import runner as runner_mod
from newapi_checkin import tabiai
from newapi_checkin.cf import session_store
from test_run_state_report import _cfg, _runner

STALE = 999.0          # 远超预算的悬空时长，用于「早该被拦」的场景


# --------------------------------------------------------------------------- #
# 脚手架
# --------------------------------------------------------------------------- #


def _freeze_clock(monkeypatch, start=1_700_000_000.0):
    """冻结 session_store 用的时钟。

    悬空判定是拿 now() 减去落盘的时间戳，真实时钟下「刚好卡在预算边界」这种断言必然
    抖动。这里换成手推的时钟，测试想让这一代悬空多久就是多久。
    """
    clock = {"t": float(start)}
    monkeypatch.setattr(session_store, "now", lambda: clock["t"])
    return clock


def _arm_inflight(store, slug, cookie, clock, age):
    """按生产路径打上悬空标记，再把时钟往前推 age 秒。

    只用公开 API（mark_refresh_inflight），不手改记录字段：指纹怎么算、同代重复调用
    要不要重置计时，都交给被测代码自己决定，测试才有意义。
    """
    store.mark_refresh_inflight(slug, cookie)
    record = store.get(slug)
    assert record.refresh_inflight_at, "悬空标记没落上，后面的断言就都是空跑"
    clock["t"] += age
    return record


def _script_attempt(runner, monkeypatch, statuses, cap=6):
    """让 _attempt 按 statuses 顺序出结果，返回「每次尝试时用的代理」列表。

    cap 是保险丝：NETWORK_ERROR 分支换 IP 不计次数也不吃时间盒，闸门一旦失效就是死
    循环，这里让它以断言失败告终，而不是把整个测试挂住。
    """
    seen = []

    def fake_attempt(account, _record):
        idx = len(seen)
        assert idx < cap, f"_attempt 被调用超过 {cap} 次，重试循环没有收口"
        seen.append(account.proxy)
        status = statuses[min(idx, len(statuses) - 1)]
        return log.SummaryRow(account.name, status, "S1", f"第 {idx + 1} 次尝试")

    monkeypatch.setattr(runner, "_attempt", fake_attempt)
    return seen


def _script_swap(runner, monkeypatch, allow=0):
    """替掉换 IP：只允许成功 allow 次，之后返回 False 让循环自然收口。

    返回值是 reason 列表 —— 空列表就等于「压根没试着换 IP」，这是本轮修复的核心断言。
    """
    swaps = []

    def fake_swap(account, reason="net"):
        swaps.append(reason)
        if len(swaps) <= allow:
            account.proxy = f"p{len(swaps)}:80"
            return True
        return False

    monkeypatch.setattr(runner, "_swap_pooled_proxy", fake_swap)
    return swaps


def _retry(runner, account):
    """跑一遍重试主循环（record 取 store 里那份，和生产路径一致）。"""
    return runner._run_account_with_retries(account, runner.store.get(account.slug))


# --------------------------------------------------------------------------- #
# 常量本身就是安全约束
# --------------------------------------------------------------------------- #


class TestBudgetConstants:
    def test_constants_allow_exactly_one_retry_inside_the_proven_window(self):
        """8/15 这两个数不是随手填的，改动前先看这条断言。

        一次超时（8s）后还得允许再试一次，两次超时（16s）就必须停手，而 16s 仍落在
        实测已证实安全的 20 秒重放窗口里。三条同时成立才叫「留了一次重试的余量」。
        """
        timeout = tabiai.REFRESH_TIMEOUT_SECONDS
        budget = tabiai.INFLIGHT_BUDGET_SECONDS
        assert timeout <= budget, "一次超时就超预算等于没有重试机会"
        assert timeout * 2 > budget, "两次超时还不超预算，就会把安全窗口烧穿"
        assert timeout * 2 < 20, "累计悬空必须留在实测安全的 20 秒窗口内"

    def test_refresh_timeout_is_not_the_generic_http_timeout(self):
        """refresh 故意不复用 http.timeout：20 秒一次就把窗口烧光。"""
        cfg = _cfg()
        assert tabiai.REFRESH_TIMEOUT_SECONDS < cfg.http.timeout


# --------------------------------------------------------------------------- #
# 判定函数本身
# --------------------------------------------------------------------------- #


class TestRefreshInflightBlocked:
    def test_no_mark_at_all_is_allowed(self, tmp_path, monkeypatch):
        runner = _runner(tmp_path, monkeypatch)
        account = runner.cfg.accounts[0]
        assert runner._refresh_inflight_blocked(account) is None

    def test_age_exactly_at_budget_is_still_allowed(self, tmp_path, monkeypatch):
        """边界归「放行」：预算是「超过才停」，卡在点上还得给这一代最后一次机会。"""
        runner = _runner(tmp_path, monkeypatch)
        account = runner.cfg.accounts[0]
        clock = _freeze_clock(monkeypatch)
        _arm_inflight(runner.store, account.slug, runner._tabiai_cookie(account),
                      clock, tabiai.INFLIGHT_BUDGET_SECONDS)
        assert runner._refresh_inflight_blocked(account) is None

    def test_age_over_budget_is_blocked_with_a_readable_reason(self, tmp_path, monkeypatch):
        runner = _runner(tmp_path, monkeypatch)
        account = runner.cfg.accounts[0]
        clock = _freeze_clock(monkeypatch)
        _arm_inflight(runner.store, account.slug, runner._tabiai_cookie(account),
                      clock, tabiai.INFLIGHT_BUDGET_SECONDS + 0.5)
        reason = runner._refresh_inflight_blocked(account)
        assert reason is not None
        # 用户看到的必须是「为什么主动放弃」，而不是一句网络错误
        assert "悬空" in reason and "重放" in reason
        assert str(tabiai.INFLIGHT_BUDGET_SECONDS) in reason

    def test_mark_of_another_generation_does_not_block(self, tmp_path, monkeypatch):
        """平台回写 / 人工重签都会换代，上一代的悬空账跟着作废。"""
        runner = _runner(tmp_path, monkeypatch)
        account = runner.cfg.accounts[0]
        clock = _freeze_clock(monkeypatch)
        _arm_inflight(runner.store, account.slug, "new_api_refresh=sid.oldgen", clock, STALE)
        # 手上这一代（sid.secret）从没被送出去过，指纹不匹配就不该受连累
        assert runner._tabiai_cookie(account) == "new_api_refresh=sid.secret"
        assert runner._refresh_inflight_blocked(account) is None

    def test_plain_cookie_account_is_never_blocked(self, tmp_path, monkeypatch):
        """站点 Cookie 是静态凭据，没有代次概念，网络失败无限换 IP 才是对的。"""
        runner = _runner(tmp_path, monkeypatch, cfg=_cfg(tabiai_account=False))
        account = runner.cfg.accounts[0]
        clock = _freeze_clock(monkeypatch)
        # 故意用「假如它是 tabiai 账号会查到的那把 key」打标记：这样断言压的就是
        # login_method 那道分支，而不是碰巧指纹对不上
        _arm_inflight(runner.store, account.slug, runner._tabiai_cookie(account), clock, STALE)
        assert account.login_method != runner_mod.LOGIN_METHOD_TABIAI
        assert runner._refresh_inflight_blocked(account) is None

    def test_account_without_any_credential_is_not_blocked(self, tmp_path, monkeypatch):
        """凭据都没有的账号会在更早的地方被跳过，这里不该抢着报「悬空」。"""
        runner = _runner(tmp_path, monkeypatch)
        account = runner.cfg.accounts[0]
        account.cookie = ""
        assert runner._tabiai_cookie(account) == ""
        assert runner._refresh_inflight_blocked(account) is None


# --------------------------------------------------------------------------- #
# 重试主循环的 NETWORK_ERROR 分支
# --------------------------------------------------------------------------- #


class TestNetworkRetryGuard:
    """这一段就是本轮 bug 的现场：以前只要网络失败就无限换 IP，每次都拿同一代旧 cookie。"""

    def test_over_budget_stops_before_swapping_ip(self, tmp_path, monkeypatch):
        runner = _runner(tmp_path, monkeypatch)
        account = runner.cfg.accounts[0]
        clock = _freeze_clock(monkeypatch)
        _arm_inflight(runner.store, account.slug, runner._tabiai_cookie(account), clock, STALE)
        attempts = _script_attempt(runner, monkeypatch, [api.NETWORK_ERROR])
        swaps = _script_swap(runner, monkeypatch, allow=3)

        row = _retry(runner, account)

        assert row.status == "skipped"
        assert "悬空" in row.detail
        # 换 IP 必须在闸门之后：同一代从不同出口反复出现，正是重放攻击的特征
        assert swaps == [], "已经超预算还去换 IP，等于把同一代凭据重放一遍"
        assert len(attempts) == 1, "拦下之后不该再发起任何尝试"

    def test_inside_budget_still_swaps_ip(self, tmp_path, monkeypatch):
        """预算内不许改变原有行为：一次超时之后仍然值得换个出口再试。"""
        runner = _runner(tmp_path, monkeypatch)
        account = runner.cfg.accounts[0]
        clock = _freeze_clock(monkeypatch)
        _arm_inflight(runner.store, account.slug, runner._tabiai_cookie(account),
                      clock, tabiai.REFRESH_TIMEOUT_SECONDS)
        attempts = _script_attempt(runner, monkeypatch, [api.NETWORK_ERROR, api.SUCCESS])
        swaps = _script_swap(runner, monkeypatch, allow=1)

        row = _retry(runner, account)

        assert row.status == api.SUCCESS
        assert swaps == ["net"]
        assert attempts == [None, "p1:80"]

    def test_without_any_mark_it_keeps_swapping(self, tmp_path, monkeypatch):
        """没有悬空历史的账号（第一次网络就不通）行为完全照旧。"""
        runner = _runner(tmp_path, monkeypatch)
        account = runner.cfg.accounts[0]
        attempts = _script_attempt(runner, monkeypatch, [api.NETWORK_ERROR, api.SUCCESS])
        swaps = _script_swap(runner, monkeypatch, allow=1)

        row = _retry(runner, account)

        assert row.status == api.SUCCESS
        assert swaps == ["net"] and len(attempts) == 2
        assert runner.store.get(account.slug).refresh_inflight_at is None

    def test_plain_cookie_account_keeps_swapping_despite_a_stale_mark(self, tmp_path,
                                                                     monkeypatch):
        """站点 Cookie 账号不能被一起改掉：它无限换 IP 是正确行为。"""
        runner = _runner(tmp_path, monkeypatch, cfg=_cfg(tabiai_account=False))
        account = runner.cfg.accounts[0]
        clock = _freeze_clock(monkeypatch)
        _arm_inflight(runner.store, account.slug, runner._tabiai_cookie(account), clock, STALE)
        attempts = _script_attempt(runner, monkeypatch, [api.NETWORK_ERROR, api.SUCCESS])
        swaps = _script_swap(runner, monkeypatch, allow=1)

        row = _retry(runner, account)

        assert row.status == api.SUCCESS
        assert swaps == ["net"] and len(attempts) == 2

    def test_mark_from_another_generation_does_not_stop_the_retry(self, tmp_path, monkeypatch):
        """悬空账绑代次指纹：换了代就该重新拿到完整的重试权。"""
        runner = _runner(tmp_path, monkeypatch)
        account = runner.cfg.accounts[0]
        clock = _freeze_clock(monkeypatch)
        _arm_inflight(runner.store, account.slug, "new_api_refresh=sid.oldgen", clock, STALE)
        attempts = _script_attempt(runner, monkeypatch, [api.NETWORK_ERROR, api.SUCCESS])
        swaps = _script_swap(runner, monkeypatch, allow=1)

        row = _retry(runner, account)

        assert row.status == api.SUCCESS
        assert swaps == ["net"] and len(attempts) == 2

    def test_no_new_ip_still_reports_the_network_reason(self, tmp_path, monkeypatch):
        """预算内但池子空了：原来的「没有可用新 IP」结论不能被悬空文案顶掉。"""
        runner = _runner(tmp_path, monkeypatch)
        account = runner.cfg.accounts[0]
        _script_attempt(runner, monkeypatch, [api.NETWORK_ERROR])
        swaps = _script_swap(runner, monkeypatch, allow=0)

        row = _retry(runner, account)

        assert row.status == "skipped"
        assert "新 IP" in row.detail and "悬空" not in row.detail
        assert swaps == ["net"]


# --------------------------------------------------------------------------- #
# runner -> TabiAIClient 的回调接线
# --------------------------------------------------------------------------- #


def _fake_client(monkeypatch, behaviour):
    """把 tabiai.TabiAIClient 换成只跑 behaviour 的替身，返回被创建的实例列表。

    构造签名照抄真实的那份 —— runner 少传一个 kwarg 就该在这里炸出来，而不是等到
    线上才发现回调没接上。behaviour(client) 负责模拟「发出 / 超时 / 拿到响应」的时序。
    """
    made = []

    class FakeTabiAIClient:
        def __init__(self, account, http, cookie, cf=None, on_rotate=None,
                     on_inflight=None, on_settled=None):
            self.account = account
            self.cookie = cookie
            self.cf = cf
            self.on_rotate = on_rotate
            self.on_inflight = on_inflight
            self.on_settled = on_settled
            self.impersonate = "chrome"
            made.append(self)

        def __enter__(self):
            return self

        def __exit__(self, *_exc):
            return None

        def checkin(self, turnstile_provider=None, dry_run=False):
            return behaviour(self)

    monkeypatch.setattr(tabiai, "TabiAIClient", FakeTabiAIClient)
    return made


class TestCallbackWiring:
    def test_inflight_callback_marks_the_generation_and_persists_it(self, tmp_path, monkeypatch):
        """记账必须落到 store 并立刻落盘：Actions 被强杀是常态，纯内存计时挡不住下一轮。"""
        runner = _runner(tmp_path, monkeypatch)
        account = runner.cfg.accounts[0]
        cookie = runner._tabiai_cookie(account)

        def behaviour(client):
            assert client.on_inflight is not None, "runner 没把 on_inflight 传下去"
            client.on_inflight(client.cookie)      # refresh 请求发出
            return api.ApiResult(api.NETWORK_ERROR, message="ReadTimeout")  # 超时：不销账

        made = _fake_client(monkeypatch, behaviour)
        result = runner._tabiai_api_call(account, None)

        assert result.kind == api.NETWORK_ERROR
        assert made[0].cookie == cookie
        record = runner.store.get(account.slug)
        assert record.refresh_inflight_gen == session_store.generation_fingerprint(cookie)
        assert runner.store.refresh_inflight_age(account.slug, cookie) is not None
        saved = json.loads((tmp_path / "sessions.json").read_text(encoding="utf-8"))
        assert saved[account.slug]["refresh_inflight_gen"] == record.refresh_inflight_gen

    def test_settled_callback_clears_the_mark(self, tmp_path, monkeypatch):
        """拿到响应就说明站点侧状态已定（哪怕是 401），悬空账必须销掉。"""
        runner = _runner(tmp_path, monkeypatch)
        account = runner.cfg.accounts[0]
        cookie = runner._tabiai_cookie(account)

        def behaviour(client):
            client.on_inflight(client.cookie)
            client.on_settled()
            return api.ApiResult(api.AUTH_FAILED, message="凭据已失效")

        _fake_client(monkeypatch, behaviour)
        runner._tabiai_api_call(account, None)

        record = runner.store.get(account.slug)
        assert record.refresh_inflight_at is None and record.refresh_inflight_gen is None
        assert runner.store.refresh_inflight_age(account.slug, cookie) is None
        # 销账之后闸门当然不该再拦
        assert runner._refresh_inflight_blocked(account) is None

    def test_two_refresh_timeouts_burn_the_budget_and_skip_the_account(self, tmp_path,
                                                                      monkeypatch):
        """端到端复现本轮 bug：两次 8 秒超时就得停手，而不是继续换 IP 重放同一代。"""
        runner = _runner(tmp_path, monkeypatch)
        account = runner.cfg.accounts[0]
        cookie = runner._tabiai_cookie(account)
        clock = _freeze_clock(monkeypatch)
        sent = []

        def behaviour(client):
            client.on_inflight(client.cookie)
            sent.append(client.cookie)
            clock["t"] += tabiai.REFRESH_TIMEOUT_SECONDS   # 超时把这段时间烧掉
            return api.ApiResult(api.NETWORK_ERROR, message="ConnectTimeout")

        _fake_client(monkeypatch, behaviour)
        swaps = _script_swap(runner, monkeypatch, allow=3)

        row = _retry(runner, account)

        # 第一次超时（8s）仍在预算内 -> 换一次 IP 再试；第二次累计 16s 超预算 -> 停手
        assert sent == [cookie, cookie], "两次都拿同一代重发，这正是要被预算掐住的行为"
        assert swaps == ["net"]
        assert row.status == "skipped" and "悬空" in row.detail
        # 标记留着：本进程被杀后，下一轮捡起同一代时还得能被拦住
        assert runner.store.get(account.slug).refresh_inflight_gen == \
            session_store.generation_fingerprint(cookie)


# --------------------------------------------------------------------------- #
# refresh 自己的超时与记账时序
# --------------------------------------------------------------------------- #


class _FakeResponse:
    """够 detect.analyze_response + refresh() 用的最小响应体。"""

    def __init__(self, status=401, payload=None):
        self.status_code = status
        self._payload = payload
        self.text = json.dumps(payload) if payload is not None else ""
        self.cookies = {}
        self.headers = {"Content-Type": "application/json"}
        self.url = "https://t.example.com/api/user/auth/refresh"

    def json(self):
        if self._payload is None:
            raise ValueError("not json")
        return self._payload


def _fake_curl(monkeypatch, outcome):
    """替掉 curl_cffi 的 Session：记下构造参数与每次请求的 kwargs。

    outcome 是异常就抛（模拟超时），是响应就返回。真实客户端走完整 __init__，
    这样「会话默认超时」和「refresh 单发超时」的区别才是真的被测出来。
    """
    made = []

    class FakeCurlSession:
        def __init__(self, **kwargs):
            self.init_kwargs = dict(kwargs)
            self.headers = {}
            self.calls = []
            self.closed = False
            made.append(self)

        def request(self, method, url, headers=None, **kwargs):
            self.calls.append({"method": method, "url": url, "kwargs": dict(kwargs)})
            if isinstance(outcome, Exception):
                raise outcome
            return outcome

        def close(self):
            self.closed = True

    monkeypatch.setattr(tabiai.cffi, "Session", FakeCurlSession)
    return made


class TestRefreshTimeoutAndSettling:
    def test_refresh_overrides_the_session_timeout_with_its_own(self, monkeypatch):
        """会话默认仍是 http.timeout，唯独 refresh 这一发压到 REFRESH_TIMEOUT_SECONDS。"""
        cfg = _cfg()
        account = cfg.accounts[0]
        made = _fake_curl(monkeypatch, TimeoutError("connect timeout"))

        with tabiai.TabiAIClient(account, cfg.http, account.cookie) as client:
            step = client.refresh()

        session = made[0]
        assert step.result.kind == api.NETWORK_ERROR
        assert session.init_kwargs["timeout"] == cfg.http.timeout
        assert session.calls[0]["kwargs"]["timeout"] == tabiai.REFRESH_TIMEOUT_SECONDS
        assert session.calls[0]["kwargs"]["timeout"] < cfg.http.timeout

    def test_timeout_keeps_the_mark_by_not_settling(self, monkeypatch):
        """超时那一次恰恰最需要记账：标记要打上，且绝不许销账。"""
        cfg = _cfg()
        account = cfg.accounts[0]
        _fake_curl(monkeypatch, TimeoutError("read timeout"))
        marks, settled = [], []

        with tabiai.TabiAIClient(account, cfg.http, account.cookie,
                                 on_inflight=marks.append,
                                 on_settled=lambda: settled.append(True)) as client:
            client.refresh()

        assert marks == ["new_api_refresh=sid.secret"]
        assert settled == [], "超时销了账，下一轮就会拿旧代去撞重放检测"

    def test_response_even_a_401_settles_the_mark(self, monkeypatch):
        """站点回了 401 也算「状态已定」：会话已经作废，悬空账没有留着的理由。"""
        cfg = _cfg()
        account = cfg.accounts[0]
        _fake_curl(monkeypatch, _FakeResponse(
            401, {"success": False, "code": "AUTH_SESSION_REVOKED", "message": "revoked"}))
        marks, settled = [], []

        with tabiai.TabiAIClient(account, cfg.http, account.cookie,
                                 on_inflight=marks.append,
                                 on_settled=lambda: settled.append(True)) as client:
            step = client.refresh()

        assert step.result.kind == api.AUTH_FAILED
        assert marks == ["new_api_refresh=sid.secret"] and settled == [True]

    def test_inflight_is_recorded_before_the_request_goes_out(self, monkeypatch):
        """顺序不能反：先记账再发请求，否则超时的那一次就永远补不上账。"""
        cfg = _cfg()
        account = cfg.accounts[0]
        order = []
        made = _fake_curl(monkeypatch, TimeoutError("boom"))

        client = tabiai.TabiAIClient(
            account, cfg.http, account.cookie,
            on_inflight=lambda cookie: order.append("mark"),
            on_settled=lambda: order.append("settle"),
        )
        session = made[0]
        original = session.request

        def watched(method, url, headers=None, **kwargs):
            order.append("request")
            return original(method, url, headers=headers, **kwargs)

        session.request = watched
        with client:
            client.refresh()

        assert order == ["mark", "request"]




