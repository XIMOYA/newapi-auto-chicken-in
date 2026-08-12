"""代理池单元测试：解析、配置、分配、拉黑、降级直连。"""

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
        assert cfg.max_workers == 8
        assert cfg.max_proxies == 100
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
