"""代理池单元测试：解析、配置、分配、拉黑、降级直连。"""

from newapi_checkin import proxy_pool as proxy_pool_mod
from newapi_checkin import runner as runner_mod
from newapi_checkin.config import build_config
from newapi_checkin.proxy_pool import (
    DEFAULT_SOURCES,
    ProxyPool,
    ProxyPoolConfig,
    parse_proxy_lines,
)


# --------------------------------------------------------------------------- #
# 解析
# --------------------------------------------------------------------------- #


class TestParseProxyLines:
    def test_plain_text_lines(self):
        text = "1.2.3.4:80\n5.6.7.8:8080\n"
        assert parse_proxy_lines(text) == ["1.2.3.4:80", "5.6.7.8:8080"]

    def test_html_89ip_format(self):
        # 89ip.cn 返回的是嵌在广告脚本里的 IP:PORT<br>
        text = (
            '<script>$(function(){...</script>\n'
            "103.174.81.10:80<br>112.78.131.94:8080<br>"
            "更好用的代理ip请访问：www.qiyunip.com"
        )
        assert parse_proxy_lines(text) == ["103.174.81.10:80", "112.78.131.94:8080"]

    def test_filters_invalid_ip(self):
        text = "999.1.1.1:80\n256.0.0.1:80\n1.2.3.4:80\n"
        assert parse_proxy_lines(text) == ["1.2.3.4:80"]

    def test_filters_invalid_port(self):
        text = "1.2.3.4:0\n1.2.3.4:70000\n1.2.3.4:8080\n"
        assert parse_proxy_lines(text) == ["1.2.3.4:8080"]

    def test_dedup_via_set(self):
        text = "1.2.3.4:80\n1.2.3.4:80\n"
        assert len(set(parse_proxy_lines(text))) == 1

    def test_empty_input(self):
        assert parse_proxy_lines("") == []
        assert parse_proxy_lines(None) == []


# --------------------------------------------------------------------------- #
# 配置
# --------------------------------------------------------------------------- #


class TestProxyPoolConfig:
    def test_defaults(self):
        cfg = ProxyPoolConfig.from_raw(None)
        assert cfg.enabled is False
        assert cfg.test_url == "https://agentrouter.org/"
        assert cfg.timeout == 8
        assert cfg.max_workers == 25
        assert cfg.max_proxies == 250
        assert cfg.ip_swap_limit == 10
        assert cfg.sources == []  # 空 = 用内置默认源

    def test_from_raw_overrides(self):
        cfg = ProxyPoolConfig.from_raw(
            {"enabled": True, "timeout": 3, "max_workers": 4, "sources": ["http://a:1"]}
        )
        assert cfg.enabled is True
        assert cfg.timeout == 3
        assert cfg.max_workers == 4
        assert cfg.sources == ["http://a:1"]

    def test_clamps(self):
        cfg = ProxyPoolConfig.from_raw({"timeout": 9999, "max_workers": 0, "max_proxies": -5})
        assert cfg.timeout == 60
        assert cfg.max_workers == 1
        assert cfg.max_proxies == 1

    def test_enabled_strict_boolean_parsing(self):
        """enabled 必须严格布尔解析：字符串 "false" 不能因为非空被 bool() 误判成 True。"""
        assert ProxyPoolConfig.from_raw({"enabled": "false"}).enabled is False
        assert ProxyPoolConfig.from_raw({"enabled": "true"}).enabled is True
        assert ProxyPoolConfig.from_raw({"enabled": "0"}).enabled is False
        assert ProxyPoolConfig.from_raw({"enabled": "1"}).enabled is True
        assert ProxyPoolConfig.from_raw({"enabled": "no"}).enabled is False
        assert ProxyPoolConfig.from_raw({"enabled": "yes"}).enabled is True
        assert ProxyPoolConfig.from_raw({"enabled": "banana"}).enabled is False  # 非法值回退默认
        assert ProxyPoolConfig.from_raw({"enabled": 0}).enabled is False
        assert ProxyPoolConfig.from_raw({"enabled": 1}).enabled is True

    def test_default_sources_are_configured(self):
        assert len(DEFAULT_SOURCES) >= 5
        assert any("89ip.cn" in s for s in DEFAULT_SOURCES)


# --------------------------------------------------------------------------- #
# 分配 / 拉黑 / 降级
# --------------------------------------------------------------------------- #


class TestProxyPoolAllocation:
    """核心约定：共用未到上限时挑负载最轻的 IP；池子用尽时超额共用，绝不返回「直连」。"""

    def test_acquire_prefers_exclusive_ips(self):
        pool = ProxyPool(ProxyPoolConfig())
        pool._available = ["a:80", "b:80", "c:80"]
        picked = {pool.acquire() for _ in range(3)}
        assert picked == {"a:80", "b:80", "c:80"}

    def test_exhausted_pool_shares_instead_of_returning_none(self):
        pool = ProxyPool(ProxyPoolConfig())
        pool._available = ["a:80", "b:80"]
        first, second = pool.acquire(), pool.acquire()
        third = pool.acquire()
        assert third is not None                 # 宁可共用也不直连
        assert third in {first, second}

    def test_sharing_is_spread_across_ips(self):
        """共用时挑当前账号数最少的 IP，别把所有账号堆到同一个出口上。"""
        pool = ProxyPool(ProxyPoolConfig())
        pool._available = ["a:80", "b:80"]
        pool.acquire()
        pool.acquire()
        extra = [pool.acquire() for _ in range(4)]
        assert sorted(extra) == ["a:80", "a:80", "b:80", "b:80"]
        assert pool._share_count == {"a:80": 3, "b:80": 3}

    def test_returns_none_only_when_everything_is_blacklisted(self):
        pool = ProxyPool(ProxyPoolConfig())
        pool._available = ["a:80"]
        pool.mark_bad("a:80")
        assert pool.acquire() is None

    def test_mark_bad_excludes_proxy(self):
        pool = ProxyPool(ProxyPoolConfig())
        pool._available = ["a:80", "b:80"]
        pool.mark_bad("a:80")
        assert pool.acquire() == "b:80"
        assert pool.acquire() == "b:80"           # 只剩 b -> 共用 b
        pool.mark_bad("b:80")
        assert pool.acquire() is None

    def test_mark_bad_after_acquire(self):
        pool = ProxyPool(ProxyPoolConfig())
        pool._available = ["a:80", "b:80"]
        first = pool.acquire()
        pool.mark_bad(first)
        assert pool.acquire() != first

    def test_has_available_vs_has_exclusive(self):
        pool = ProxyPool(ProxyPoolConfig())
        pool._available = ["a:80"]
        assert pool.has_exclusive() is True
        pool.acquire()
        assert pool.has_exclusive() is False      # 没有空闲的了
        assert pool.has_available() is True       # 但还能共用
        pool.mark_bad("a:80")
        assert pool.has_available() is False


# --------------------------------------------------------------------------- #
# Runner 集成
# --------------------------------------------------------------------------- #


class FakePool:
    """不联网的假代理池，行为与真池一致。"""

    def __init__(self, proxies):
        self._queue = list(proxies)
        self._used = set()
        self._bad = set()
        self.ok: list = []

    def acquire(self):
        for proxy in self._queue:
            if proxy not in self._used and proxy not in self._bad:
                self._used.add(proxy)
                return proxy
        return None

    def mark_bad(self, proxy, reason="net"):
        if proxy:
            self._bad.add(proxy)

    def mark_ok(self, proxy):
        if proxy:
            self.ok.append(proxy)


def _make_runner(monkeypatch, tmp_path, pool_proxies=None, pool_enabled=True):
    cfg = build_config(
        {
            "proxy_pool": {
                "enabled": pool_enabled,
                "ip_swap_limit": 2,
            },
            "defaults": {"retry": 0, "interval_seconds": [0, 0]},
            "accounts": [
                {"name": "A", "url": "https://a.example.com", "cookie": "c"},
                {"name": "B", "url": "https://b.example.com", "cookie": "c"},
            ],
        }
    )
    monkeypatch.setattr(runner_mod, "SESSIONS_FILE", tmp_path / "sessions.json")
    # run() 会触发 init_proxy_pool 真联网抓取，测试里禁用，直接用 FakePool
    monkeypatch.setattr(runner_mod.Runner, "init_proxy_pool", lambda self, **kw: None)
    runner = runner_mod.Runner(cfg, runner_mod.RunOptions(use_ai=False, use_browser=False))
    if pool_proxies is not None:
        runner._pool = FakePool(pool_proxies)
    return runner


def test_pool_assigns_distinct_proxy_to_each_account(monkeypatch, tmp_path):
    runner = _make_runner(monkeypatch, tmp_path, pool_proxies=["p1:80", "p2:80", "p3:80"])
    seen_proxies = []
    seen_names = []

    def fake_attempt(account, record):
        seen_names.append(account.name)
        seen_proxies.append(account.proxy)
        return runner_mod.log.SummaryRow(account.name, "success", "fake", "ok")

    monkeypatch.setattr(runner, "_attempt", fake_attempt)

    runner.run()
    assert set(seen_names) == {"A", "B"}
    assert set(seen_proxies) == {"p1:80", "p2:80"}  # 每账号一个，互不重复


def test_network_error_swaps_proxy_and_retries(monkeypatch, tmp_path):
    runner = _make_runner(monkeypatch, tmp_path, pool_proxies=["p1:80", "p2:80"])
    calls = []

    def fake_attempt(account, record):
        calls.append(account.proxy)
        if len(calls) == 1:
            return runner_mod.log.SummaryRow(account.name, "network_error", "S1", "连不上")
        return runner_mod.log.SummaryRow(account.name, "success", "S1", "ok")

    monkeypatch.setattr(runner, "_attempt", fake_attempt)

    account = runner.cfg.accounts[0]
    runner._assign_proxy(account)
    row = runner._run_account(account)
    assert row.status == "success"
    assert calls == ["p1:80", "p2:80"]  # 第一次 p1 失败 -> 换 p2 成功
    assert "p1:80" in runner._pool._bad  # 失败代理被拉黑


def test_pool_exhausted_skips_account_instead_of_going_direct(monkeypatch, tmp_path):
    """启用代理池 = 必须走代理：一个代理都拿不到时跳过账号，不暴露真实 IP。"""
    runner = _make_runner(monkeypatch, tmp_path, pool_proxies=[])
    account = runner.cfg.accounts[0]

    def fake_attempt(_acct, _record):
        raise AssertionError("没有代理时不该发起签到")

    monkeypatch.setattr(runner, "_attempt", fake_attempt)
    row = runner._run_account(account)
    assert row.status == "skipped"
    assert "不降级直连" in row.detail
    assert account.proxy is None


def test_direct_still_works_when_pool_disabled(monkeypatch, tmp_path):
    """没启用代理池就是用户自己选择直连，不能被「必须走代理」挡住。"""
    runner = _make_runner(monkeypatch, tmp_path, pool_proxies=None, pool_enabled=False)
    account = runner.cfg.accounts[0]

    def fake_attempt(acct, _record):
        assert acct.proxy is None
        return runner_mod.log.SummaryRow(acct.name, "success", "S1", "ok")

    monkeypatch.setattr(runner, "_attempt", fake_attempt)
    assert runner._run_account(account).status == "success"


def test_manual_proxy_not_overwritten(monkeypatch, tmp_path):
    runner = _make_runner(monkeypatch, tmp_path, pool_proxies=["p1:80"])
    account = runner.cfg.accounts[0]
    account.proxy = "manual:8080"
    runner._assign_proxy(account)
    assert account.proxy == "manual:8080"  # 手动配置优先
    assert account.name not in runner._pooled_proxies  # 不会被池记录，也不会被换


def test_network_swaps_until_pool_is_empty_then_skips(monkeypatch, tmp_path):
    runner = _make_runner(monkeypatch, tmp_path, pool_proxies=["p1:80", "p2:80", "p3:80"])
    calls = []

    def fake_attempt(account, record):
        calls.append(account.proxy)
        return runner_mod.log.SummaryRow(account.name, "network_error", "S1", "连不上")

    monkeypatch.setattr(runner, "_attempt", fake_attempt)
    account = runner.cfg.accounts[0]
    runner._assign_proxy(account)
    row = runner._run_account(account)
    assert row.status == "skipped"
    # 不看 ip_swap_limit：p1 -> p2 -> p3，池空后直接跳过，不在 p3 上继续 retry
    assert calls == ["p1:80", "p2:80", "p3:80"]


class TestAIProxyBlacklisting:
    """AI 放弃某个 IP 时要不要拉黑：只拉黑没有账号在用的那些。"""

    def test_unused_ip_is_blacklisted(self, monkeypatch, tmp_path):
        """AI 自己取来的 IP 失败后必须拉黑，否则它会被当共用候选发给别的账号。"""
        runner = _make_runner(monkeypatch, tmp_path, pool_proxies=["p1:80", "p2:80"])
        proxy = runner._acquire_proxy_for_ai()
        runner._ai_proxy_failed(proxy)
        assert proxy in runner._pool._bad
        assert runner._pool.acquire() != proxy      # 拉黑后不再分出去

    def test_ip_in_use_by_account_is_kept(self, monkeypatch, tmp_path):
        """账号正拿它签到 = 它连目标站点是通的，拉黑等于误伤签到主流程。"""
        runner = _make_runner(monkeypatch, tmp_path, pool_proxies=["p1:80", "p2:80"])
        account = runner.cfg.accounts[0]
        runner._assign_proxy(account)
        runner._ai_proxy_failed(account.proxy)
        assert runner._pool._bad == set()
        assert account.proxy in runner._pooled_proxies.values()

    def test_missing_pool_or_proxy_is_a_noop(self, monkeypatch, tmp_path):
        runner = _make_runner(monkeypatch, tmp_path, pool_proxies=["p1:80"])
        runner._ai_proxy_failed(None)
        runner._ai_proxy_failed("")
        assert runner._pool._bad == set()
        runner._pool = None
        runner._ai_proxy_failed("p1:80")            # 没有池子也不能抛


# --------------------------------------------------------------------------- #
# 抓源 / 测通的耗时控制
# --------------------------------------------------------------------------- #


class TestSourceFetching:
    def test_sources_are_fetched_concurrently(self, monkeypatch):
        """串行抓源时每个源超时都要白等一次，这里要求并发。"""
        import threading
        import time

        cfg = ProxyPoolConfig(sources=[f"http://src{i}" for i in range(4)])
        pool = ProxyPool(cfg)
        state = {"active": 0, "maximum": 0}
        lock = threading.Lock()

        def fake_fetch(source):
            with lock:
                state["active"] += 1
                state["maximum"] = max(state["maximum"], state["active"])
            time.sleep(0.05)
            with lock:
                state["active"] -= 1
            index = source[-1]
            return [f"1.1.1.{index}:80"]

        monkeypatch.setattr(pool, "_fetch_source", fake_fetch)
        per_source = pool._fetch_all_sources()
        assert state["maximum"] > 1
        # 结果顺序按配置顺序还原，保证 _round_robin 的优先级稳定
        assert per_source == [["1.1.1.0:80"], ["1.1.1.1:80"], ["1.1.1.2:80"], ["1.1.1.3:80"]]

    def test_failing_source_is_skipped(self, monkeypatch):
        pool = ProxyPool(ProxyPoolConfig(sources=["http://bad", "http://good"]))

        def fake_fetch(source):
            if "bad" in source:
                raise RuntimeError("connect timeout")
            return ["9.9.9.9:80"]

        monkeypatch.setattr(pool, "_fetch_source", fake_fetch)
        assert pool._fetch_all_sources() == [["9.9.9.9:80"]]


class TestProbeEarlyStop:
    def test_stops_probing_once_target_reached(self, monkeypatch):
        """凑够 need 条就收手，不等剩余候选各自超时。"""
        pool = ProxyPool(ProxyPoolConfig(max_workers=2, timeout=2))
        tested = []

        def fake_test(proxy):
            tested.append(proxy)
            return 0.05   # 全部秒回，延迟都一样

        monkeypatch.setattr(pool, "_test_one", fake_test)
        alive = pool._test_many([f"1.1.1.{i}:80" for i in range(20)], need=3)
        assert len(alive) == 3
        # 不应该把 20 条全测完（并发 2，允许少量已在飞行中的多测几条）
        assert len(tested) < 20

    def test_without_need_tests_everything(self, monkeypatch):
        pool = ProxyPool(ProxyPoolConfig(max_workers=4))
        monkeypatch.setattr(pool, "_test_one",
                            lambda proxy: 0.5 if proxy.endswith(":80") else None)
        alive = pool._test_many(["1.1.1.1:80", "2.2.2.2:8080", "3.3.3.3:80"])
        assert sorted(alive) == ["1.1.1.1:80", "3.3.3.3:80"]

    def test_results_are_sorted_by_latency(self, monkeypatch):
        """可用代理按延迟升序返回：快的排前面，acquire() 顺序取优拿到最快出口。"""
        pool = ProxyPool(ProxyPoolConfig(max_workers=4))
        latency = {"slow:80": 8.0, "fast:80": 0.2, "mid:80": 3.0, "dead:80": None}

        monkeypatch.setattr(pool, "_test_one", lambda proxy: latency[proxy])
        alive = pool._test_many(["slow:80", "fast:80", "mid:80", "dead:80"])
        assert alive == ["fast:80", "mid:80", "slow:80"]

    def test_controlled_dispatch_window_is_bounded(self, monkeypatch):
        """受控派发：不同时把全部候选塞进线程池，在飞窗口不超过并发数。"""
        import threading
        import time

        pool = ProxyPool(ProxyPoolConfig(max_workers=3, timeout=2))
        state = {"active": 0, "max": 0}
        lock = threading.Lock()

        def fake_test(proxy):
            with lock:
                state["active"] += 1
                state["max"] = max(state["max"], state["active"])
            time.sleep(0.03)
            with lock:
                state["active"] -= 1
            return 0.1

        monkeypatch.setattr(pool, "_test_one", fake_test)
        alive = pool._test_many([f"1.1.1.{i}:80" for i in range(50)])
        assert len(alive) == 50
        # 在飞窗口被钳在并发数上：50 条候选也不会瞬间同时跑 50 个
        assert state["max"] == 3

    def test_deadline_stops_slow_batch(self, monkeypatch):
        """时间盒到了就带着已有结果返回，不让慢代理拖住签到开始。"""
        import time

        pool = ProxyPool(ProxyPoolConfig(max_workers=2, timeout=30))

        def slow_test(_proxy):
            time.sleep(5)
            return 0.1

        monkeypatch.setattr(pool, "_test_one", slow_test)
        started = time.monotonic()
        alive = pool._test_many([f"1.1.1.{i}:80" for i in range(10)],
                                deadline=time.monotonic() + 0.3)
        assert time.monotonic() - started < 2.0
        assert alive == []


class TestFullSweep:
    """测通不设数量上限：抓到多少条就全测多少条。"""

    def test_tests_every_fetched_candidate(self, monkeypatch):
        found = [f"10.0.{i // 250}.{i % 250}:8080" for i in range(4000)]
        pool = ProxyPool(ProxyPoolConfig(max_proxies=100, max_workers=8))
        monkeypatch.setattr(pool, "_fetch_all_sources", lambda: [found])
        seen = {}

        def fake_test_many(candidates, need=None, deadline=None):
            seen["count"] = len(candidates)
            seen["need"] = need
            return list(candidates[:37])

        monkeypatch.setattr(pool, "_test_many", fake_test_many)
        count = pool.refresh(desired=70)
        assert count == 37
        assert seen["count"] == 4000        # 全量，不再按目标数取样
        assert seen["need"] is None        # 也不再「凑够就收手」

    def test_max_proxies_no_longer_caps_the_sweep(self, monkeypatch):
        found = [f"10.0.0.{i}:8080" for i in range(200)]
        pool = ProxyPool(ProxyPoolConfig(max_proxies=5, max_workers=4))
        monkeypatch.setattr(pool, "_fetch_all_sources", lambda: [found])
        sizes = []
        monkeypatch.setattr(
            pool, "_test_many",
            lambda candidates, need=None, deadline=None: (sizes.append(len(candidates)),
                                                          list(candidates))[1],
        )
        assert pool.refresh(desired=10) == 200
        assert sizes == [200]              # 一次测完，不再分批

    def test_cross_source_dedup(self, monkeypatch):
        pool = ProxyPool(ProxyPoolConfig(max_workers=4))
        monkeypatch.setattr(
            pool, "_fetch_all_sources",
            lambda: [["1.1.1.1:80", "2.2.2.2:80"], ["2.2.2.2:80", "3.3.3.3:80"]],
        )
        monkeypatch.setattr(
            pool, "_test_many",
            lambda candidates, need=None, deadline=None: list(candidates),
        )
        assert pool.refresh() == 3

    def test_deadline_is_derived_from_workload(self, monkeypatch):
        """时间盒按「波数 × 单条超时」推导，只兜底卡死，不再充当数量上限。"""
        import time

        found = [f"10.0.0.{i}:8080" for i in range(20)]
        pool = ProxyPool(ProxyPoolConfig(max_workers=5, timeout=3))
        monkeypatch.setattr(pool, "_fetch_all_sources", lambda: [found])
        captured = {}

        def fake_test_many(candidates, need=None, deadline=None):
            captured["budget"] = deadline - time.monotonic()
            return list(candidates)

        monkeypatch.setattr(pool, "_test_many", fake_test_many)
        pool.refresh()
        # 20 条 / 5 并发 = 4 波，4 × 3s + 余量
        expected = 4 * 3 + proxy_pool_mod.REFRESH_SLACK_SECONDS
        assert abs(captured["budget"] - expected) < 1.0

    def test_empty_sources_report_error(self, monkeypatch):
        pool = ProxyPool(ProxyPoolConfig())
        monkeypatch.setattr(pool, "_fetch_all_sources", lambda: [])
        assert pool.refresh() == 0
        assert pool.last_error


class TestRemotePrefetchIsUncapped:
    """服务器预取：上游返回多少就全部收下，不受 desired / max_proxies 影响。"""

    @staticmethod
    def _pool(addrs):
        pool = ProxyPool(ProxyPoolConfig(
            remote_url="https://cfg.example.com/api/proxies/available",
            max_proxies=100,          # 已废弃字段，不该再截断任何东西
            preflight_check=False,    # 这组只看「收下多少条」，自筛会真去探测网络
        ))
        pool._fetch_remote = lambda: list(addrs)   # 绕开真实 HTTP
        return pool

    def test_keeps_every_remote_entry(self):
        addrs = [f"10.0.{i // 250}.{i % 250}:8080" for i in range(600)]
        pool = self._pool(addrs)
        # desired 远小于上游数量，也不能拿它去截断
        assert pool.refresh(desired=36) == 600
        assert len(pool._available) == 600

    def test_local_sweep_is_skipped_when_remote_succeeds(self, monkeypatch):
        called = []
        pool = self._pool(["1.1.1.1:80", "2.2.2.2:80"])
        monkeypatch.setattr(pool, "_refresh_local",
                            lambda desired=None: called.append(desired) or 0)
        assert pool.refresh(desired=5) == 2
        assert called == []          # 远程成功就不该再本地抓源测通

    def test_falls_back_to_local_when_remote_empty(self, monkeypatch):
        pool = self._pool([])
        pool._fetch_remote = lambda: None
        monkeypatch.setattr(pool, "_refresh_local", lambda desired=None: 7)
        assert pool.refresh(desired=5) == 7


class TestOneProxyPerAccount:
    def test_every_account_gets_a_distinct_ip(self, monkeypatch, tmp_path):
        """核心不变量：同一次运行内任何两个账号不会拿到同一个代理。"""
        cfg = build_config(
            {
                "proxy_pool": {"enabled": True},
                "defaults": {"retry": 0, "interval_seconds": [0, 0]},
                "accounts": [
                    {"name": f"A{i}", "url": f"https://a{i}.example.com", "cookie": "c"}
                    for i in range(12)
                ],
            }
        )
        monkeypatch.setattr(runner_mod, "SESSIONS_FILE", tmp_path / "sessions.json")
        monkeypatch.setattr(runner_mod.Runner, "init_proxy_pool", lambda self, **kw: None)
        runner = runner_mod.Runner(
            cfg, runner_mod.RunOptions(use_ai=False, use_browser=False, parallelism=6)
        )
        pool = ProxyPool(ProxyPoolConfig(enabled=True))
        pool._available = [f"10.0.0.{i}:8080" for i in range(20)]
        runner._pool = pool

        assigned = []
        lock = __import__("threading").Lock()

        def fake_attempt(account, record):
            with lock:
                assigned.append(account.proxy)
            return runner_mod.log.SummaryRow(account.name, "success", "S1", "ok")

        monkeypatch.setattr(runner, "_attempt", fake_attempt)
        assert runner.run() == 0
        assert len(assigned) == 12
        assert len(set(assigned)) == 12          # 12 个账号 12 个不同 IP
        assert None not in assigned

    def test_manual_proxy_is_never_replaced_by_pool(self, monkeypatch, tmp_path):
        runner = _make_runner(monkeypatch, tmp_path, pool_proxies=["p1:80", "p2:80"])
        first, second = runner.cfg.accounts
        first.proxy = "http://fixed.example.com:8080"
        runner._assign_proxy(first)
        runner._assign_proxy(second)
        assert first.proxy == "http://fixed.example.com:8080"
        assert second.proxy == "p1:80"

    def test_shortage_says_accounts_will_share_not_go_direct(self, monkeypatch, tmp_path):
        """池子不够时提前说清楚：多出来的账号共用 IP，而不是降级直连。"""
        runner = _make_runner(monkeypatch, tmp_path, pool_proxies=[])
        warnings = []
        monkeypatch.setattr(runner_mod.log, "warn", warnings.append)
        runner._report_proxy_capacity(3, 10)
        assert warnings
        assert "只有 3 个" in warnings[0]
        assert "7 个会与它们共用 IP" in warnings[0]
        assert "不会降级直连" in warnings[0]

    def test_exhausted_pool_shares_across_accounts(self, monkeypatch, tmp_path):
        """代理比账号少时，多出来的账号共用已分配的 IP，不会拿到 None。"""
        runner = _make_runner(monkeypatch, tmp_path, pool_proxies=None)
        pool = ProxyPool(ProxyPoolConfig(enabled=True))
        pool._available = ["10.0.0.1:8080"]
        runner._pool = pool
        first, second = runner.cfg.accounts
        runner._assign_proxy(first)
        runner._assign_proxy(second)
        assert first.proxy == "10.0.0.1:8080"
        assert second.proxy == "10.0.0.1:8080"      # 共用同一个出口 IP
        assert pool._share_count["10.0.0.1:8080"] == 2

    def test_swap_keeps_old_proxy_when_pool_is_empty(self, monkeypatch, tmp_path):
        """换不到新代理时保留原代理继续重试，绝不清空成直连。"""
        runner = _make_runner(monkeypatch, tmp_path, pool_proxies=None)
        pool = ProxyPool(ProxyPoolConfig(enabled=True))
        pool._available = ["10.0.0.1:8080"]
        runner._pool = pool
        account = runner.cfg.accounts[0]
        runner._assign_proxy(account)
        assert runner._swap_proxy(account) is None
        assert account.proxy == "10.0.0.1:8080"     # 没有被清空

    def test_sufficient_pool_reports_headroom(self, monkeypatch, tmp_path):
        runner = _make_runner(monkeypatch, tmp_path, pool_proxies=[])
        infos = []
        monkeypatch.setattr(runner_mod.log, "info", infos.append)
        runner._report_proxy_capacity(15, 10)
        assert infos and "一账号一 IP" in infos[0] and "5 个" in infos[0]
        # 备用池是所有账号共享的，且网络异常时会持续换到池耗尽
        assert "共享" in infos[0]
        assert "持续换 IP" in infos[0]
        assert "配额" not in infos[0]

    def test_shortage_points_at_the_server_side_limit(self, monkeypatch, tmp_path):
        """代理不够时要指明数量由服务端 save_limit 决定，否则没人知道去哪调。"""
        runner = _make_runner(monkeypatch, tmp_path, pool_proxies=[])
        warnings = []
        monkeypatch.setattr(runner_mod.log, "warn", warnings.append)
        runner._report_proxy_capacity(3, 10)
        assert warnings and "save_limit" in warnings[0]

    def test_swapped_proxy_is_never_reassigned(self, monkeypatch, tmp_path):
        runner = _make_runner(monkeypatch, tmp_path, pool_proxies=["p1:80", "p2:80", "p3:80"])
        account = runner.cfg.accounts[0]
        runner._assign_proxy(account)
        first = account.proxy
        runner._swap_proxy(account)
        second = account.proxy
        runner._swap_proxy(account)
        third = account.proxy
        assert len({first, second, third}) == 3
        assert first in runner._pool._bad and second in runner._pool._bad


# --------------------------------------------------------------------------- #
# 成功 IP 复用（换满阈值后，优先复用本轮签到成功的出口）
# --------------------------------------------------------------------------- #


class TestProvenProxyReuse:
    """账号换 IP 超过 PROVEN_REUSE_THRESHOLD 个后，池里没有新 IP 时复用
    本轮 mark_ok 过的代理。旧 IP 仍走 mark_bad，绝不复用已拉黑的出口。"""

    def _runner_with_proven(self, monkeypatch, tmp_path, proven):
        runner = _make_runner(monkeypatch, tmp_path, pool_proxies=[])
        # 池已耗尽：acquire 拿不到任何新 IP，只能靠成功 IP 复用
        runner._proven_proxies = set(proven)
        return runner

    def test_below_threshold_never_reuses(self, monkeypatch, tmp_path):
        runner = self._runner_with_proven(monkeypatch, tmp_path, ["ok:80"])
        account = runner.cfg.accounts[0]
        runner._swap_total[account.name] = runner_mod.PROVEN_REUSE_THRESHOLD - 1
        assert runner._reuse_proven_proxy(account) is None
        assert account.proxy != "ok:80"

    def test_at_threshold_reuses_proven(self, monkeypatch, tmp_path):
        runner = self._runner_with_proven(monkeypatch, tmp_path, ["ok:80"])
        account = runner.cfg.accounts[0]
        runner._swap_total[account.name] = runner_mod.PROVEN_REUSE_THRESHOLD
        assert runner._reuse_proven_proxy(account) == "ok:80"

    def test_swap_prefers_fresh_then_falls_back_to_proven(self, monkeypatch, tmp_path):
        """有没用过的新 IP 时先换新的；池空了才复用成功 IP。"""
        runner = _make_runner(monkeypatch, tmp_path, pool_proxies=["fresh:80"])
        runner._proven_proxies = {"ok:80"}
        account = runner.cfg.accounts[0]
        runner._swap_total[account.name] = runner_mod.PROVEN_REUSE_THRESHOLD
        runner._assign_proxy(account)
        assert account.proxy == "fresh:80"
        runner._swap_proxy(account)
        assert account.proxy == "ok:80"
        assert "fresh:80" in runner._pool._bad

    def test_reuse_is_blocked_for_mark_bad_proxy(self, monkeypatch, tmp_path):
        """成功 IP 一旦被拉黑就退出复用候选，不会反复踩同一个坑。"""
        runner = _make_runner(monkeypatch, tmp_path, pool_proxies=[])
        runner._proven_proxies = {"ok:80"}
        runner._pool.mark_bad("ok:80")
        account = runner.cfg.accounts[0]
        runner._swap_total[account.name] = runner_mod.PROVEN_REUSE_THRESHOLD
        assert runner._reuse_proven_proxy(account) is None

    def test_reused_proxy_failure_kills_it_permanently(self, monkeypatch, tmp_path):
        """复用成功 IP 后又失败 -> 被 mark_bad，彻底退出候选，不会无限震荡。"""
        runner = _make_runner(monkeypatch, tmp_path, pool_proxies=["fresh:80"])
        runner._proven_proxies = {"ok:80"}
        account = runner.cfg.accounts[0]
        runner._swap_total[account.name] = runner_mod.PROVEN_REUSE_THRESHOLD
        runner._assign_proxy(account)
        runner._swap_proxy(account)          # fresh 失败 -> 复用 ok
        assert account.proxy == "ok:80"
        runner._swap_proxy(account)          # ok 也失败 -> 拉黑，且无任何替代可用
        assert "ok:80" in runner._pool._bad
        # 拿不到替代品时保留原代理继续重试（绝不降级直连）—— 这是既定行为
        assert account.proxy == "ok:80"
        # 但 ok 已拉黑，复用候选里不可能再出现它
        assert runner._reuse_proven_proxy(account) is None

    def test_proven_success_tracks_ok_and_reuse_updates_swap_total(self, monkeypatch, tmp_path):
        """mark_ok 记录进 _proven_proxies；每次换 IP 都累计 _swap_total。"""
        runner = _make_runner(monkeypatch, tmp_path, pool_proxies=["fresh:80"])
        account = runner.cfg.accounts[0]
        runner._swap_total[account.name] = runner_mod.PROVEN_REUSE_THRESHOLD
        runner._assign_proxy(account)
        # 模拟该账号在 fresh 上签到成功：此时 fresh 尚未进 proven
        assert "fresh:80" not in runner._proven_proxies
        runner._record_proxy_success(account, runner_mod.log.SummaryRow(account.name, "success", "S0", ""))
        assert "fresh:80" in runner._proven_proxies


# --------------------------------------------------------------------------- #
# 单 IP 多账号共用
# --------------------------------------------------------------------------- #


class TestSharedIPCapacity:
    """同一出口 IP 最多服务 max_accounts_per_ip 个账号（默认 4）。"""

    def test_default_limit_is_four(self):
        assert ProxyPoolConfig().max_accounts_per_ip == 4
        assert ProxyPoolConfig.from_raw({}).max_accounts_per_ip == 4
        assert ProxyPoolConfig.from_raw({"max_accounts_per_ip": None}).max_accounts_per_ip == 4

    def test_limit_is_clamped_into_range(self):
        assert ProxyPoolConfig.from_raw({"max_accounts_per_ip": 999}).max_accounts_per_ip == 64
        assert ProxyPoolConfig.from_raw({"max_accounts_per_ip": -5}).max_accounts_per_ip == 0
        assert ProxyPoolConfig.from_raw({"max_accounts_per_ip": "8"}).max_accounts_per_ip == 8

    def test_limit_survives_a_config_round_trip(self):
        """to_dict 必须带上它，否则网页端保存一次配置就把这项抹回默认值。"""
        cfg = ProxyPoolConfig.from_raw({"max_accounts_per_ip": 6})
        assert cfg.to_dict()["max_accounts_per_ip"] == 6
        assert ProxyPoolConfig.from_raw(cfg.to_dict()).max_accounts_per_ip == 6

    def test_allocation_is_flattened_not_packed(self):
        """3 个代理 / 上限 4：12 次分配摊成 a,b,c,a,b,c…，每个 IP 正好 4 个。

        摊平而不是装箱（先把 a 填满 4 个再用 b）—— 账号散开后单个 IP 被盯上时
        受影响的账号更少。
        """
        pool = ProxyPool(ProxyPoolConfig(max_accounts_per_ip=4))
        pool._available = ["a:80", "b:80", "c:80"]
        assert [pool.acquire() for _ in range(12)] == ["a:80", "b:80", "c:80"] * 4
        assert pool._share_count == {"a:80": 4, "b:80": 4, "c:80": 4}

    def test_overflow_warns_but_still_shares(self, monkeypatch):
        """全部到上限后继续超额共用并告警，绝不降级直连。"""
        pool = ProxyPool(ProxyPoolConfig(max_accounts_per_ip=4))
        pool._available = ["a:80", "b:80", "c:80"]
        for _ in range(12):
            pool.acquire()
        warnings = []
        monkeypatch.setattr(proxy_pool_mod.log, "warn", warnings.append)
        extra = pool.acquire()
        assert extra is not None
        assert pool._share_count[extra] == 5
        assert warnings and "共用上限" in warnings[0]

    def test_limit_one_is_the_old_one_ip_per_account(self):
        """上限 1 退化成旧行为：先各占一个，占满了才超额。"""
        pool = ProxyPool(ProxyPoolConfig(max_accounts_per_ip=1))
        pool._available = ["a:80", "b:80"]
        assert {pool.acquire(), pool.acquire()} == {"a:80", "b:80"}
        assert pool.acquire() is not None

    def test_limit_zero_means_unlimited_but_still_spread(self):
        pool = ProxyPool(ProxyPoolConfig(max_accounts_per_ip=0))
        pool._available = ["a:80", "b:80"]
        for _ in range(6):
            pool.acquire()
        assert pool._share_count == {"a:80": 3, "b:80": 3}

    def test_blacklisted_proxy_takes_no_share(self):
        """拉黑的代理不参与摊平，哪怕它的共用数是 0（最轻）。"""
        pool = ProxyPool(ProxyPoolConfig(max_accounts_per_ip=4))
        pool._available = ["a:80", "b:80"]
        pool.mark_bad("a:80")
        assert [pool.acquire() for _ in range(4)] == ["b:80"] * 4
        assert "a:80" not in pool._share_count


class TestDesiredProxyCount:
    """预取量按共用上限折算，再加固定的换 IP 余量。"""

    def _runner(self, monkeypatch, tmp_path, limit):
        cfg = build_config({
            "proxy_pool": {"enabled": True, "max_accounts_per_ip": limit},
            "accounts": [{"name": "A", "url": "https://a.example.com", "cookie": "c"}],
        })
        monkeypatch.setattr(runner_mod, "SESSIONS_FILE", tmp_path / "sessions.json")
        monkeypatch.setattr(runner_mod.Runner, "init_proxy_pool", lambda self, **kw: None)
        return runner_mod.Runner(cfg, runner_mod.RunOptions(use_ai=False, use_browser=False))

    def test_spare_count_is_fifty(self):
        assert runner_mod.PROXY_SPARE_COUNT == 50

    def test_folds_account_count_by_the_limit(self, monkeypatch, tmp_path):
        """64 账号 / 上限 4 → 16 个 IP + 50 余量；240 账号 → 60 + 50。"""
        runner = self._runner(monkeypatch, tmp_path, 4)
        assert runner._desired_proxies(64) == 66
        assert runner._desired_proxies(240) == 110

    def test_rounds_up_partial_batches(self, monkeypatch, tmp_path):
        """65 账号要 17 个 IP，不能因为除不尽就少给一个。"""
        runner = self._runner(monkeypatch, tmp_path, 4)
        assert runner._desired_proxies(65) == 67
        assert runner._desired_proxies(1) == 51

    def test_unlimited_sharing_keeps_full_headroom(self, monkeypatch, tmp_path):
        """上限 0 不代表只要一个 IP —— 换 IP 的余量照留。"""
        runner = self._runner(monkeypatch, tmp_path, 0)
        assert runner._desired_proxies(64) == 114

    def test_never_asks_for_zero(self, monkeypatch, tmp_path):
        runner = self._runner(monkeypatch, tmp_path, 4)
        assert runner._desired_proxies(0) == 51


class TestProxySweep:
    """代理全量体检：从本机（Actions）视角把平台上的存活代理测一遍再回传。

    存在的理由是出口不同：平台自己的 refresh/speedtest 走服务器出口，而签到跑在
    Actions 上，代理商封机房 IP 段是常事。所以「服务器那边最优」不等于「Actions 能用」。

    这组断言里最要紧的一条是「时间盒到了，没轮到的代理不记账」——把没测的记成失败会
    诬陷好代理，反而把平台的优选排序搞坏，比不体检更糟。
    """

    @staticmethod
    def _pool(addrs, alive, **cfg_kw):
        kw = {"enabled": True, "max_workers": 4, "timeout": 1,
              "remote_url": "https://panel.example.com/api/proxies/available"}
        kw.update(cfg_kw)
        pool = ProxyPool(ProxyPoolConfig(**kw))
        pool._fetch_remote = lambda: list(addrs)
        pool._test_one = lambda p: 0.05 if p in alive else None
        return pool

    def test_every_proxy_gets_a_verdict(self):
        pool = self._pool([f"p{i}:80" for i in range(6)], {"p0:80", "p3:80"})
        stats = pool.sweep_remote(minutes=1)
        assert stats["total"] == 6 and stats["tested"] == 6
        assert stats["ok"] == 2 and stats["fail"] == 4
        assert len(pool.feedback_snapshot()) == 6      # 六条都有账

    def test_alive_counts_ok_dead_counts_net_fail(self):
        """记的是 report_feedback 要导出的那三个计数，平台按它们分档排序。"""
        pool = self._pool(["good:80", "bad:80"], {"good:80"})
        pool.sweep_remote(minutes=1)
        snap = {i["addr"]: i for i in pool.feedback_snapshot()}
        assert (snap["good:80"]["ok"], snap["good:80"]["net_fail"]) == (1, 0)
        assert (snap["bad:80"]["ok"], snap["bad:80"]["net_fail"]) == (0, 1)
        # 体检测的是连通性，不该往「被站点拦」那一栏记
        assert all(i["block_fail"] == 0 for i in snap.values())

    def test_empty_remote_list_reports_a_reason(self):
        """平台一条都没给时要说清原因，不能静默返回成功。"""
        pool = self._pool([], set())
        pool._fetch_remote = lambda: None
        pool.last_error = "远程代理预取 HTTP 500"
        stats = pool.sweep_remote(minutes=1)
        assert stats["total"] == 0
        assert "500" in stats["reason"]

    def test_sweep_does_not_stop_early_like_refresh(self):
        """refresh 凑够量就收手，体检必须每条都测 —— 否则回传的样本是偏的。"""
        pool = self._pool([f"p{i}:80" for i in range(20)], {f"p{i}:80" for i in range(20)})
        stats = pool.sweep_remote(minutes=1)
        assert stats["tested"] == 20 and stats["ok"] == 20


class TestTestManyResultCallback:
    """_test_many 的逐条回调：体检靠它记账，所以「哪条测了」必须精确。"""

    def test_callback_fires_for_alive_and_dead_alike(self):
        pool = ProxyPool(ProxyPoolConfig(enabled=True, max_workers=2, timeout=1))
        pool._test_one = lambda p: 0.01 if p.startswith("ok") else None
        seen = {}
        pool._test_many(["ok1:80", "bad1:80", "ok2:80"], on_result=lambda p, l: seen.update({p: l}))
        assert set(seen) == {"ok1:80", "bad1:80", "ok2:80"}
        assert seen["bad1:80"] is None and seen["ok1:80"] is not None

    def test_callback_fires_even_when_need_is_reached(self):
        """凑够 need 会 break，但那一条的结论已经出来了，不能漏记。"""
        pool = ProxyPool(ProxyPoolConfig(enabled=True, max_workers=1, timeout=1))
        pool._test_one = lambda p: 0.01
        seen = []
        alive = pool._test_many(["a:80", "b:80", "c:80"], need=1,
                                on_result=lambda p, l: seen.append(p))
        assert len(alive) == 1
        assert len(seen) >= 1                       # 至少把命中的那条记上了
        assert set(seen) <= {"a:80", "b:80", "c:80"}

    def test_untested_proxies_get_no_callback_when_deadline_hits(self):
        """时间盒到了就收工，没轮到的一条都不该回调 —— 记成失败等于诬陷。"""
        import time as _t

        pool = ProxyPool(ProxyPoolConfig(enabled=True, max_workers=1, timeout=1))

        def slow(proxy):
            _t.sleep(0.05)
            return 0.01

        pool._test_one = slow
        seen = []
        candidates = [f"p{i}:80" for i in range(40)]
        pool._test_many(candidates, deadline=_t.monotonic() + 0.12,
                        on_result=lambda p, l: seen.append(p))
        assert len(seen) < len(candidates)          # 确实没测完
        assert len(set(seen)) == len(seen)          # 没有重复记账


class TestPresetProxyList:
    """前置体检清单接管代理来源。

    签到 workflow 的 sweep job 先测一遍、把测通的写成 artifact，各分片下载后直接当池子用。
    这么绕一趟是因为平台的 proxies 表每 refresh_minutes（默认 30）就整表重建一次，
    指望它替我们记住「Actions 视角谁可用」几十分钟后就被冲没了 —— 同一个 run 内用文件
    传递才稳。

    附带解决了分片的老问题：各片共用同一份清单，「本片领到的不够」从根上不存在。代价是
    单 IP 上限只在单进程内计数，跨 job 的实际共用数可能高于配置值（已知取舍）。
    """

    def test_preset_skips_the_platform_entirely(self):
        """给了清单就不该再拉平台 —— 那批几分钟前刚在同一网络环境测通。"""
        pool = ProxyPool(ProxyPoolConfig(enabled=True, remote_url="https://panel.example.com/x"),
                         preset=["a:80", "b:80"])
        pool._fetch_remote = lambda: pytest.fail("给了 preset 还去拉平台")
        assert pool.refresh() == 2

    def test_preset_skips_preflight(self):
        """也不该再自筛：刚测通的东西再测一遍纯属重复消耗时间盒。"""
        pool = ProxyPool(ProxyPoolConfig(enabled=True), preset=["a:80"])
        pool.preflight = lambda: pytest.fail("给了 preset 还去自筛")
        assert pool.refresh() == 1

    def test_preset_preserves_order(self):
        """清单按延迟升序写的，顺序必须原样保留 —— acquire 顺序取优才有意义。"""
        pool = ProxyPool(ProxyPoolConfig(enabled=True, max_accounts_per_ip=1),
                         preset=["fast:80", "mid:80", "slow:80"])
        pool.refresh()
        assert [pool.acquire() for _ in range(3)] == ["fast:80", "mid:80", "slow:80"]

    def test_preset_ignores_blank_entries(self):
        pool = ProxyPool(ProxyPoolConfig(enabled=True), preset=["a:80", "  ", "", "b:80"])
        assert pool.refresh() == 2

    def test_empty_preset_falls_back_to_normal_sources(self):
        """空清单等于没给：照旧走平台/本地抓取，不能把池子搞成空的。"""
        pool = ProxyPool(ProxyPoolConfig(enabled=True, remote_url="https://panel.example.com/x"),
                         preset=[])
        pool._fetch_remote = lambda: ["from-platform:80"]
        pool.preflight = lambda: 0
        assert pool.refresh() == 1
        assert pool.acquire() == "from-platform:80"

    def test_preset_shares_ip_under_the_configured_cap(self):
        """共用上限仍按配置生效（跨进程不共享计数是已知取舍，本进程内必须守住）。"""
        pool = ProxyPool(ProxyPoolConfig(enabled=True, max_accounts_per_ip=2),
                         preset=["only:80"])
        pool.refresh()
        assert [pool.acquire() for _ in range(2)] == ["only:80"] * 2
        assert pool._share_count["only:80"] == 2
