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
        assert cfg.test_url == "https://api.ipify.org"
        assert cfg.timeout == 8
        assert cfg.max_workers == 25
        assert cfg.max_proxies == 250
        assert cfg.ip_swap_limit == 5
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

    def test_default_sources_are_configured(self):
        assert len(DEFAULT_SOURCES) >= 5
        assert any("89ip.cn" in s for s in DEFAULT_SOURCES)


# --------------------------------------------------------------------------- #
# 分配 / 拉黑 / 降级
# --------------------------------------------------------------------------- #


class TestProxyPoolAllocation:
    """核心约定：优先独占；池子用尽时共用，绝不返回「直连」。"""

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

    def acquire(self):
        for proxy in self._queue:
            if proxy not in self._used and proxy not in self._bad:
                self._used.add(proxy)
                return proxy
        return None

    def mark_bad(self, proxy):
        if proxy:
            self._bad.add(proxy)


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


def test_swap_limit_respected(monkeypatch, tmp_path):
    runner = _make_runner(monkeypatch, tmp_path, pool_proxies=["p1:80", "p2:80", "p3:80"])
    calls = []

    def fake_attempt(account, record):
        calls.append(account.proxy)
        return runner_mod.log.SummaryRow(account.name, "network_error", "S1", "连不上")

    monkeypatch.setattr(runner, "_attempt", fake_attempt)
    account = runner.cfg.accounts[0]
    runner._assign_proxy(account)
    row = runner._run_account(account)
    assert row.status == "network_error"
    # ip_swap_limit=2：p1 失败换 p2，p2 失败换 p3，p3 失败没得换，共 3 次尝试
    assert len(calls) == 3


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
            return True

        monkeypatch.setattr(pool, "_test_one", fake_test)
        alive = pool._test_many([f"1.1.1.{i}:80" for i in range(20)], need=3)
        assert len(alive) == 3
        # 不应该把 20 条全测完（并发 2，允许少量已在飞行中的多测几条）
        assert len(tested) < 20

    def test_without_need_tests_everything(self, monkeypatch):
        pool = ProxyPool(ProxyPoolConfig(max_workers=4))
        monkeypatch.setattr(pool, "_test_one", lambda proxy: proxy.endswith(":80"))
        alive = pool._test_many(["1.1.1.1:80", "2.2.2.2:8080", "3.3.3.3:80"])
        assert sorted(alive) == ["1.1.1.1:80", "3.3.3.3:80"]

    def test_deadline_stops_slow_batch(self, monkeypatch):
        """时间盒到了就带着已有结果返回，不让慢代理拖住签到开始。"""
        import time

        pool = ProxyPool(ProxyPoolConfig(max_workers=2, timeout=30))

        def slow_test(_proxy):
            time.sleep(5)
            return True

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
        assert infos and "一账号一 IP" in infos[0] and "余量 5" in infos[0]

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
