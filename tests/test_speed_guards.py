"""耗时防回归：重试语义、尝试预算、浏览器并发、探测竞速、落盘节流。

这些断言存在的原因是「签到太慢」的具体成因，改动相关逻辑时不要放宽它们。
"""

import threading
import time

from newapi_checkin import client as api
from newapi_checkin import config as cfgmod
from newapi_checkin import logger as log
from newapi_checkin import runner as runner_mod
from newapi_checkin import utils
from newapi_checkin.cf import session_store as ss


def _make_runner(tmp_path, monkeypatch, *, accounts=1, use_browser=False, **opts):
    cfg = cfgmod.build_config(
        {
            "defaults": {"retry": 2, "interval_seconds": [0, 0]},
            "accounts": [
                {"name": f"A{i}", "url": f"https://a{i}.example.com", "cookie": "c"}
                for i in range(accounts)
            ],
        }
    )
    monkeypatch.setattr(runner_mod, "SESSIONS_FILE", tmp_path / "sessions.json")
    monkeypatch.setattr(runner_mod, "probe_exit_ip", lambda proxy=None, timeout=5: None)
    options = runner_mod.RunOptions(use_ai=False, use_browser=use_browser, **opts)
    return runner_mod.Runner(cfg, options)


def _row(name, status):
    return log.SummaryRow(name, status, "S1", "stub")


class TestRetrySemantics:
    """retry 只该覆盖瞬时故障。业务已经给出结论的失败重跑整条链路是纯浪费。"""

    def test_business_failure_is_not_retried(self, tmp_path, monkeypatch):
        runner = _make_runner(tmp_path, monkeypatch)
        calls = []

        def fake_attempt(account, record):
            calls.append(1)
            return _row(account.name, api.FAILED)

        monkeypatch.setattr(runner, "_attempt", fake_attempt)
        row = runner._run_account(runner.cfg.accounts[0])
        assert row.status == api.FAILED
        assert len(calls) == 1  # retry=2 也只跑一次

    def test_turnstile_required_is_not_retried(self, tmp_path, monkeypatch):
        runner = _make_runner(tmp_path, monkeypatch)
        calls = []
        monkeypatch.setattr(
            runner, "_attempt",
            lambda account, record: (calls.append(1), _row(account.name, api.TURNSTILE_REQUIRED))[1],
        )
        assert runner._run_account(runner.cfg.accounts[0]).status == api.TURNSTILE_REQUIRED
        assert len(calls) == 1

    def test_network_error_is_retried(self, tmp_path, monkeypatch):
        runner = _make_runner(tmp_path, monkeypatch)
        calls = []
        monkeypatch.setattr(
            runner, "_attempt",
            lambda account, record: (calls.append(1), _row(account.name, api.NETWORK_ERROR))[1],
        )
        runner._run_account(runner.cfg.accounts[0])
        assert len(calls) == 3  # retry=2 -> 3 次

    def test_cf_blocked_without_browser_is_not_retried(self, tmp_path, monkeypatch):
        runner = _make_runner(tmp_path, monkeypatch, use_browser=False)
        calls = []
        monkeypatch.setattr(
            runner, "_attempt",
            lambda account, record: (calls.append(1), _row(account.name, api.CF_BLOCKED))[1],
        )
        runner._run_account(runner.cfg.accounts[0])
        assert len(calls) == 1  # 没有浏览器，重试只会拿到同一个质询页

    def test_cf_blocked_stops_after_browser_quota(self, tmp_path, monkeypatch):
        runner = _make_runner(tmp_path, monkeypatch, use_browser=True)
        calls = []

        def fake_attempt(account, record):
            calls.append(1)
            runner._take_browser_attempt(account)   # 模拟真实路径里的配额消耗
            return _row(account.name, api.CF_BLOCKED)

        monkeypatch.setattr(runner, "_attempt", fake_attempt)
        runner._run_account(runner.cfg.accounts[0])
        assert len(calls) == runner_mod._BROWSER_ATTEMPTS_PER_ACCOUNT


class TestBrowserAttemptQuota:
    def test_quota_is_per_account_and_finite(self, tmp_path, monkeypatch):
        runner = _make_runner(tmp_path, monkeypatch, accounts=2, use_browser=True)
        first, second = runner.cfg.accounts
        taken = [runner._take_browser_attempt(first) for _ in range(4)]
        assert taken == [True] * runner_mod._BROWSER_ATTEMPTS_PER_ACCOUNT + \
            [False] * (4 - runner_mod._BROWSER_ATTEMPTS_PER_ACCOUNT)
        # 另一个账号有自己的配额
        assert runner._take_browser_attempt(second) is True


class TestNetworkErrorSwapsIP:
    """网络异常时换 IP 是「修复动作」，不消耗 defaults.retry 的次数。"""

    @staticmethod
    def _runner(tmp_path, monkeypatch, *, retry=2, ip_swap_limit=2, pool=True):
        cfg = cfgmod.build_config(
            {
                "proxy_pool": {"enabled": pool, "ip_swap_limit": ip_swap_limit},
                "defaults": {"retry": retry, "interval_seconds": [0, 0]},
                "accounts": [{"name": "A", "url": "https://a.example.com", "cookie": "c"}],
            }
        )
        monkeypatch.setattr(runner_mod, "SESSIONS_FILE", tmp_path / "sessions.json")
        monkeypatch.setattr(runner_mod, "probe_exit_ip", lambda proxy=None, timeout=5: None)
        # 退避会真的 sleep，这些用例只关心次数与顺序
        monkeypatch.setattr(runner_mod.time, "sleep", lambda _s: None)
        runner = runner_mod.Runner(
            cfg, runner_mod.RunOptions(use_ai=False, use_browser=False)
        )
        if pool:
            runner._pool = _EndlessPool()
        return runner, cfg.accounts[0]

    def test_swap_does_not_consume_retry_budget(self, tmp_path, monkeypatch):
        runner, account = self._runner(tmp_path, monkeypatch, retry=2, ip_swap_limit=2)
        proxies = []
        monkeypatch.setattr(
            runner, "_attempt",
            lambda acct, record: (proxies.append(acct.proxy),
                                  _row(acct.name, api.NETWORK_ERROR))[1],
        )
        runner._assign_proxy(account)
        row = runner._run_account(account)
        assert row.status == api.NETWORK_ERROR
        # 2 次换 IP（不计次）+ retry=2 允许的 3 次尝试 = 5 次
        assert len(proxies) == 5
        # 前三次各用一个新 IP，换完之后就在最后一个 IP 上做真正的重试
        assert proxies[:3] == ["p1:80", "p2:80", "p3:80"]
        assert proxies[3:] == ["p3:80", "p3:80"]

    def test_swap_is_immediate_without_backoff(self, tmp_path, monkeypatch):
        """换 IP 后立刻重试，不走退避——新 IP 没有必要先罚站。"""
        runner, account = self._runner(tmp_path, monkeypatch, retry=2, ip_swap_limit=2)
        slept: list = []
        monkeypatch.setattr(runner_mod.time, "sleep", slept.append)
        results = [api.NETWORK_ERROR, api.SUCCESS]
        monkeypatch.setattr(
            runner, "_attempt",
            lambda acct, record: _row(acct.name, results.pop(0)),
        )
        runner._assign_proxy(account)
        assert runner._run_account(account).status == api.SUCCESS
        assert slept == []          # 全程没有退避

    def test_retry_budget_still_applies_after_swaps_run_out(self, tmp_path, monkeypatch):
        runner, account = self._runner(tmp_path, monkeypatch, retry=1, ip_swap_limit=1)
        calls = []
        monkeypatch.setattr(
            runner, "_attempt",
            lambda acct, record: (calls.append(1),
                                  _row(acct.name, api.NETWORK_ERROR))[1],
        )
        runner._assign_proxy(account)
        runner._run_account(account)
        assert len(calls) == 3      # 1 次换 IP + retry=1 允许的 2 次

    def test_manual_proxy_is_never_swapped(self, tmp_path, monkeypatch):
        runner, account = self._runner(tmp_path, monkeypatch, retry=1, ip_swap_limit=2)
        account.proxy = "http://fixed.example.com:8080"
        proxies = []
        monkeypatch.setattr(
            runner, "_attempt",
            lambda acct, record: (proxies.append(acct.proxy),
                                  _row(acct.name, api.NETWORK_ERROR))[1],
        )
        runner._assign_proxy(account)          # 手动代理不会被池覆盖
        runner._run_account(account)
        assert set(proxies) == {"http://fixed.example.com:8080"}
        assert len(proxies) == 2               # 退回纯重试：retry=1 -> 2 次

    def test_business_failure_never_swaps(self, tmp_path, monkeypatch):
        runner, account = self._runner(tmp_path, monkeypatch, retry=2, ip_swap_limit=2)
        proxies = []
        monkeypatch.setattr(
            runner, "_attempt",
            lambda acct, record: (proxies.append(acct.proxy),
                                  _row(acct.name, api.FAILED))[1],
        )
        runner._assign_proxy(account)
        runner._run_account(account)
        assert proxies == ["p1:80"]            # 业务失败既不重试也不换 IP

    def test_without_pool_falls_back_to_plain_retry(self, tmp_path, monkeypatch):
        runner, account = self._runner(tmp_path, monkeypatch, retry=2, pool=False)
        calls = []
        monkeypatch.setattr(
            runner, "_attempt",
            lambda acct, record: (calls.append(1),
                                  _row(acct.name, api.NETWORK_ERROR))[1],
        )
        runner._run_account(account)
        assert len(calls) == 3


class _EndlessPool:
    """够用就行的假代理池：每次 acquire 给一个新 IP。"""

    def __init__(self):
        self.n = 0
        self.bad: list = []

    def acquire(self):
        self.n += 1
        return f"p{self.n}:80"

    def mark_bad(self, proxy):
        self.bad.append(proxy)


class TestBrowserConcurrency:
    def test_browser_workers_never_exceed_account_workers(self, tmp_path, monkeypatch):
        runner = _make_runner(tmp_path, monkeypatch, parallelism=2, browser_parallelism=8)
        assert runner._browser_workers() == 2

    def test_explicit_browser_parallel_is_honoured(self, tmp_path, monkeypatch):
        runner = _make_runner(tmp_path, monkeypatch, parallelism=8, browser_parallelism=1)
        assert runner._browser_workers() == 1

    def test_auto_browser_workers_stay_small(self, tmp_path, monkeypatch):
        runner = _make_runner(tmp_path, monkeypatch, parallelism=8)
        assert 1 <= runner._browser_workers() <= runner_mod.MAX_BROWSER_PARALLELISM

    def test_auto_browser_workers_leave_one_core_free(self, tmp_path, monkeypatch):
        """自动值 = 核心数 - 1，留一个核给 Python 编排和系统。"""
        runner = _make_runner(tmp_path, monkeypatch, parallelism=16)
        for cores, expected in ((1, 1), (2, 1), (4, 3), (8, 4), (32, 4)):
            monkeypatch.setattr(runner_mod.os, "cpu_count", lambda c=cores: c)
            assert runner._browser_workers() == expected

    def test_gate_limits_concurrent_solves(self, tmp_path, monkeypatch):
        runner = _make_runner(tmp_path, monkeypatch, use_browser=True)
        runner._browser_gate = threading.Semaphore(2)
        state = {"active": 0, "maximum": 0}
        lock = threading.Lock()

        def fake_solve(**_kwargs):
            with lock:
                state["active"] += 1
                state["maximum"] = max(state["maximum"], state["active"])
            time.sleep(0.05)
            with lock:
                state["active"] -= 1
            return "done"

        account = runner.cfg.accounts[0]
        threads = [
            threading.Thread(target=runner._solve_guarded, args=(fake_solve, account, None))
            for _ in range(6)
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        assert state["maximum"] == 2


class TestExplicitParallelism:
    def test_explicit_one_stays_serial(self, tmp_path, monkeypatch):
        runner = _make_runner(tmp_path, monkeypatch, accounts=3,
                              parallelism=1, parallelism_explicit=True)
        monkeypatch.setattr(runner, "_run_account",
                            lambda account: _row(account.name, api.SUCCESS))
        assert runner.run() == 0
        assert runner.options.parallelism == 1

    def test_unspecified_is_promoted_to_default(self, tmp_path, monkeypatch):
        runner = _make_runner(tmp_path, monkeypatch, accounts=3, parallelism=1)
        monkeypatch.setattr(runner, "_run_account",
                            lambda account: _row(account.name, api.SUCCESS))
        assert runner.run() == 0
        assert runner.options.parallelism == runner_mod.DEFAULT_ACCOUNT_PARALLELISM
        assert runner_mod.DEFAULT_ACCOUNT_PARALLELISM == 3


class TestExitIpProbe:
    def test_returns_first_successful_endpoint(self, monkeypatch):
        """三个端点竞速：前面的慢/失败不该拖住整体。"""
        def fake_probe_one(url, _proxy, _timeout):
            if url == utils.IP_PROBE_URLS[0]:
                time.sleep(0.5)
                return "1.1.1.1"
            return "8.8.8.8"

        monkeypatch.setattr(utils, "_probe_one", fake_probe_one)
        started = time.monotonic()
        assert utils.probe_exit_ip(timeout=3) == "8.8.8.8"
        assert time.monotonic() - started < 0.4

    def test_all_failures_return_none(self, monkeypatch):
        monkeypatch.setattr(utils, "_probe_one", lambda *_a: None)
        assert utils.probe_exit_ip(timeout=1) is None

    def test_runner_caches_probe_per_proxy(self, tmp_path, monkeypatch):
        runner = _make_runner(tmp_path, monkeypatch)
        calls = []
        monkeypatch.setattr(runner_mod, "probe_exit_ip",
                            lambda proxy=None, timeout=5: (calls.append(proxy), "9.9.9.9")[1])
        assert runner.exit_ip("p1:80") == "9.9.9.9"
        assert runner.exit_ip("p1:80") == "9.9.9.9"
        assert len(calls) == 1


class TestFlushThrottle:
    def test_throttled_flush_skips_rapid_writes(self, tmp_path):
        path = tmp_path / "sessions.json"
        store = ss.SessionStore(path)
        store.remember("a", user_id=1)
        assert store.flush_throttled() is True
        assert path.exists()
        store.remember("b", user_id=2)
        assert store.flush_throttled() is False   # 距上次不足节流间隔
        assert "\"b\"" not in path.read_text(encoding="utf-8")
        store.flush()                              # 收尾强制落盘
        assert "\"b\"" in path.read_text(encoding="utf-8")

    def test_throttled_flush_is_noop_without_changes(self, tmp_path):
        store = ss.SessionStore(tmp_path / "sessions.json")
        assert store.flush_throttled() is False
