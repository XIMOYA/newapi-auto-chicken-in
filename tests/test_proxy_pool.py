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
        assert cfg.ip_swap_limit == 2
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
    def test_acquire_never_repeats_until_exhausted(self):
        pool = ProxyPool(ProxyPoolConfig())
        pool._available = ["a:80", "b:80", "c:80"]
        picked = {pool.acquire() for _ in range(3)}
        assert picked == {"a:80", "b:80", "c:80"}
        assert pool.acquire() is None  # 用尽 -> None（上层降级直连）

    def test_mark_bad_excludes_proxy(self):
        pool = ProxyPool(ProxyPoolConfig())
        pool._available = ["a:80", "b:80"]
        pool.mark_bad("a:80")
        first = pool.acquire()
        assert first == "b:80"
        assert pool.acquire() is None

    def test_mark_bad_after_acquire(self):
        pool = ProxyPool(ProxyPoolConfig())
        pool._available = ["a:80", "b:80"]
        first = pool.acquire()
        pool.mark_bad(first)
        assert pool.acquire() != first

    def test_has_available(self):
        pool = ProxyPool(ProxyPoolConfig())
        pool._available = ["a:80"]
        assert pool.has_available() is True
        pool.acquire()
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


def test_pool_exhausted_falls_back_to_direct(monkeypatch, tmp_path):
    runner = _make_runner(monkeypatch, tmp_path, pool_proxies=[])
    account = runner.cfg.accounts[0]

    def fake_attempt(acct, record):
        assert acct.proxy is None  # 没分配到代理 -> 直连
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


class TestDynamicAccountScaling:
    """账号数量是动态的：目标 IP 数、候选取样量都要随账号数一起放大。"""

    def test_candidate_sampling_scales_with_account_count(self, monkeypatch):
        found = [f"10.0.{i // 250}.{i % 250}:8080" for i in range(4000)]
        pool = ProxyPool(ProxyPoolConfig(max_proxies=100, max_workers=8))
        monkeypatch.setattr(pool, "_fetch_all_sources", lambda: [found])
        seen = []

        def fake_test_many(batch, need=None, deadline=None):
            seen.append(len(batch))
            return list(batch[:3])          # 每批只有 3 条活的，逼它继续补测

        monkeypatch.setattr(pool, "_test_many", fake_test_many)
        # 60 个账号 + 10 换 IP 余量
        count = pool.refresh(desired=70)
        assert count > 0
        # 目标 70 条、每批只通 3 条 -> 必须测很多批，而不是固定 4 轮就收手
        assert len(seen) > 4
        assert all(size <= 100 for size in seen)   # 每批不超过 max_proxies

    def test_refresh_is_time_boxed(self, monkeypatch):
        import time

        found = [f"10.0.0.{i}:8080" for i in range(200)]
        pool = ProxyPool(ProxyPoolConfig(max_proxies=10, max_workers=4))
        monkeypatch.setattr(pool, "_fetch_all_sources", lambda: [found])
        monkeypatch.setattr(proxy_pool_mod, "REFRESH_BUDGET_SECONDS", 0.5)

        def slow_batch(batch, need=None, deadline=None):
            time.sleep(0.2)
            return []

        monkeypatch.setattr(pool, "_test_many", slow_batch)
        started = time.monotonic()
        pool.refresh(desired=500)
        assert time.monotonic() - started < 3.0


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

    def test_shortage_is_reported_upfront(self, monkeypatch, tmp_path):
        """池子不够时必须提前说清楚，别让「一账号一 IP」悄悄退化。"""
        runner = _make_runner(monkeypatch, tmp_path, pool_proxies=[])
        warnings = []
        monkeypatch.setattr(runner_mod.log, "warn", warnings.append)
        runner._warn_proxy_shortage(3, 10)
        assert warnings and "只有 3 个" in warnings[0] and "7 个将降级直连" in warnings[0]

    def test_sufficient_pool_reports_headroom(self, monkeypatch, tmp_path):
        runner = _make_runner(monkeypatch, tmp_path, pool_proxies=[])
        infos = []
        monkeypatch.setattr(runner_mod.log, "info", infos.append)
        runner._warn_proxy_shortage(15, 10)
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
