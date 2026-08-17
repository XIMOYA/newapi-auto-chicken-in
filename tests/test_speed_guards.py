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
        assert row.status == "skipped"
        assert len(calls) == 1  # 源站业务结果直接跳过，不进入 retry

    def test_turnstile_without_browser_is_skipped(self, tmp_path, monkeypatch):
        """没有浏览器时拿不到 token 是死局，不该空转。"""
        runner = _make_runner(tmp_path, monkeypatch, use_browser=False)
        calls = []
        monkeypatch.setattr(
            runner, "_attempt",
            lambda account, record: (calls.append(1), _row(account.name, api.TURNSTILE_REQUIRED))[1],
        )
        assert runner._run_account(runner.cfg.accounts[0]).status == "skipped"
        assert len(calls) == 1

    def test_network_error_without_new_ip_is_skipped(self, tmp_path, monkeypatch):
        """网络问题换不到新 IP 时直接跳过，不在原 IP 上浪费 retry。"""
        runner = _make_runner(tmp_path, monkeypatch)
        calls = []
        monkeypatch.setattr(
            runner, "_attempt",
            lambda account, record: (calls.append(1), _row(account.name, api.NETWORK_ERROR))[1],
        )
        row = runner._run_account(runner.cfg.accounts[0])
        assert row.status == "skipped"
        assert len(calls) == 1

    def test_cf_blocked_without_browser_is_skipped(self, tmp_path, monkeypatch):
        runner = _make_runner(tmp_path, monkeypatch, use_browser=False)
        calls = []
        monkeypatch.setattr(
            runner, "_attempt",
            lambda account, record: (calls.append(1), _row(account.name, api.CF_BLOCKED))[1],
        )
        runner._run_account(runner.cfg.accounts[0])
        assert len(calls) == 1  # 没有浏览器，盾类问题直接跳过


class TestShieldRetriesUntilDeadline:
    """被盾拦住 / 拿不到 Turnstile token：换 IP + 重开浏览器，一直试到成功。"""

    def test_account_deadline_is_1200_seconds(self):
        assert runner_mod.ACCOUNT_DEADLINE_SECONDS == 1200

    @staticmethod
    def _wire(runner, monkeypatch, statuses, *, deadline=0.6):
        """把时间盒缩到亚秒级，并让退避不真的睡。"""
        monkeypatch.setattr(runner_mod, "ACCOUNT_DEADLINE_SECONDS", deadline)
        monkeypatch.setattr(runner_mod, "SHIELD_RETRY_BACKOFF_MAX", 0)
        monkeypatch.setattr(runner_mod.time, "sleep", lambda _s: None)
        # 盾类重试的前提是能换到新出口；没有池的场景由专门测试覆盖为 skipped。
        if runner.options.use_browser and runner._pool is None:
            runner._pool = _EndlessPool()
        calls = []

        def fake_attempt(account, record):
            calls.append(account.proxy)
            index = min(len(calls) - 1, len(statuses) - 1)
            return _row(account.name, statuses[index])

        monkeypatch.setattr(runner, "_attempt", fake_attempt)
        return calls

    def test_cf_blocked_retries_many_times_then_gives_up_on_deadline(self, tmp_path, monkeypatch):
        runner = _make_runner(tmp_path, monkeypatch, use_browser=True)
        calls = self._wire(runner, monkeypatch, [api.CF_BLOCKED])
        row = runner._run_account(runner.cfg.accounts[0])
        assert row.status == api.CF_BLOCKED
        # 不再是「2 次就收手」，而是被时间盒收口，轮数远多于 defaults.retry+1
        assert len(calls) > 5

    def test_turnstile_is_treated_like_cf_blocked(self, tmp_path, monkeypatch):
        runner = _make_runner(tmp_path, monkeypatch, use_browser=True)
        calls = self._wire(runner, monkeypatch, [api.TURNSTILE_REQUIRED])
        assert runner._run_account(runner.cfg.accounts[0]).status == api.TURNSTILE_REQUIRED
        assert len(calls) > 5

    def test_stops_immediately_once_it_succeeds(self, tmp_path, monkeypatch):
        runner = _make_runner(tmp_path, monkeypatch, use_browser=True)
        calls = self._wire(
            runner, monkeypatch,
            [api.CF_BLOCKED, api.TURNSTILE_REQUIRED, api.CF_BLOCKED, api.SUCCESS],
        )
        assert runner._run_account(runner.cfg.accounts[0]).status == api.SUCCESS
        assert len(calls) == 4

    def test_each_shield_round_swaps_the_exit_ip(self, tmp_path, monkeypatch):
        """「更换浏览器和 IP」——每一轮都要换出口 IP，而不是死磕同一个。"""
        cfg = cfgmod.build_config(
            {
                "proxy_pool": {"enabled": True, "ip_swap_limit": 5},
                "defaults": {"retry": 2, "interval_seconds": [0, 0]},
                "accounts": [{"name": "A", "url": "https://a.example.com", "cookie": "c"}],
            }
        )
        monkeypatch.setattr(runner_mod, "SESSIONS_FILE", tmp_path / "sessions.json")
        monkeypatch.setattr(runner_mod, "probe_exit_ip", lambda proxy=None, timeout=5: None)
        runner = runner_mod.Runner(
            cfg, runner_mod.RunOptions(use_ai=False, use_browser=True)
        )
        runner._pool = _EndlessPool()
        calls = self._wire(runner, monkeypatch, [api.CF_BLOCKED, api.CF_BLOCKED, api.SUCCESS])
        account = runner.cfg.accounts[0]
        runner._assign_proxy(account)
        assert runner._run_account(account).status == api.SUCCESS
        assert calls == ["p1:80", "p2:80", "p3:80"]     # 每轮一个新出口 IP

    def test_shield_swaps_are_counted_in_the_log(self, tmp_path, monkeypatch):
        """盾类换 IP 不限次数，但必须计入累计数。"""
        cfg = cfgmod.build_config(
            {
                "proxy_pool": {"enabled": True, "ip_swap_limit": 5},
                "defaults": {"retry": 2, "interval_seconds": [0, 0]},
                "accounts": [{"name": "A", "url": "https://a.example.com", "cookie": "c"}],
            }
        )
        monkeypatch.setattr(runner_mod, "SESSIONS_FILE", tmp_path / "sessions.json")
        monkeypatch.setattr(runner_mod, "probe_exit_ip", lambda proxy=None, timeout=5: None)
        runner = runner_mod.Runner(
            cfg, runner_mod.RunOptions(use_ai=False, use_browser=True)
        )
        runner._pool = _EndlessPool()
        warnings: list = []
        monkeypatch.setattr(runner_mod.log, "warn", warnings.append)
        self._wire(runner, monkeypatch,
                   [api.CF_BLOCKED, api.CF_BLOCKED, api.CF_BLOCKED, api.SUCCESS])
        account = runner.cfg.accounts[0]
        runner._assign_proxy(account)
        assert runner._run_account(account).status == api.SUCCESS
        swap_logs = [w for w in warnings if "已换出口 IP" in w]
        assert len(swap_logs) == 3
        # 累计数逐轮递增，不是一直停在 1
        assert "累计已换 1 个" in swap_logs[0]
        assert "累计已换 2 个" in swap_logs[1]
        assert "累计已换 3 个" in swap_logs[2]

    def test_browser_relaunch_is_no_longer_capped(self, tmp_path, monkeypatch):
        runner = _make_runner(tmp_path, monkeypatch, accounts=2, use_browser=True)
        first, second = runner.cfg.accounts
        assert all(runner._take_browser_attempt(first) for _ in range(10))
        with runner._state_lock:
            assert runner._browser_attempts[first.name] == 10
        assert runner._take_browser_attempt(second) is True


class TestNetworkErrorSwapsIP:
    """网络异常时只要有新 IP 就无限换，换不到新 IP 就跳过。"""

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

    def test_network_swaps_are_unlimited_and_do_not_show_remaining_quota(self, tmp_path, monkeypatch):
        """网络异常不看 ip_swap_limit：有新 IP 就一直换，日志不再显示剩余次数。"""
        runner, account = self._runner(tmp_path, monkeypatch, retry=0, ip_swap_limit=0)
        runner._pool = _EndlessPool()
        warnings: list = []
        monkeypatch.setattr(runner_mod.log, "warn", warnings.append)
        results = [api.NETWORK_ERROR, api.NETWORK_ERROR, api.NETWORK_ERROR, api.SUCCESS]
        monkeypatch.setattr(
            runner, "_attempt",
            lambda acct, record: _row(acct.name, results.pop(0)),
        )
        runner._assign_proxy(account)
        assert runner._run_account(account).status == api.SUCCESS
        swap_logs = [w for w in warnings if "网络异常，已换 IP" in w]
        assert len(swap_logs) == 3
        assert all("剩余" not in w and "配额" not in w for w in swap_logs)
        assert "累计已换 3 个 IP" in swap_logs[-1]

    def test_network_swap_log_no_longer_reports_quota(self, tmp_path, monkeypatch):
        """网络异常日志只显示实际累计消耗，不显示已废弃的配额口径。"""
        runner, account = self._runner(tmp_path, monkeypatch, retry=0, ip_swap_limit=1)
        runner._pool = _EndlessPool()
        warnings: list = []
        monkeypatch.setattr(runner_mod.log, "warn", warnings.append)
        results = [api.NETWORK_ERROR, api.SUCCESS]
        monkeypatch.setattr(
            runner, "_attempt",
            lambda acct, record: _row(acct.name, results.pop(0)),
        )
        runner._assign_proxy(account)
        runner._run_account(account)
        swap_logs = [w for w in warnings if "网络异常，已换 IP" in w]
        assert len(swap_logs) == 1
        assert "剩余" not in swap_logs[0]
        assert "配额" not in swap_logs[0]
        assert "累计已换 1 个 IP" in swap_logs[0]

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

    def test_network_swap_time_is_excluded_from_account_deadline(self, tmp_path, monkeypatch):
        """网络异常换 IP 的耗时不应抢占后续盾类重试的时间盒。"""
        runner = _make_runner(tmp_path, monkeypatch, use_browser=True)
        runner._pool = _EndlessPool()
        account = runner.cfg.accounts[0]
        clock = _FakeClock()
        monkeypatch.setattr(runner_mod.time, "monotonic", clock.monotonic)
        monkeypatch.setattr(runner_mod, "ACCOUNT_DEADLINE_SECONDS", 1)
        monkeypatch.setattr(runner_mod, "SHIELD_RETRY_BACKOFF_MAX", 0)
        monkeypatch.setattr(runner_mod.time, "sleep", lambda _seconds: None)

        results = [api.NETWORK_ERROR, api.CF_BLOCKED, api.SUCCESS]
        calls = []
        monkeypatch.setattr(
            runner, "_attempt",
            lambda acct, record: (calls.append(1), _row(acct.name, results.pop(0)))[1],
        )
        swaps = []

        def slow_network_swap(_account):
            swaps.append(1)
            if len(swaps) == 1:
                clock.now += 2
            return True

        monkeypatch.setattr(runner, "_swap_pooled_proxy", slow_network_swap)

        assert runner._run_account(account).status == api.SUCCESS
        assert len(calls) == 3
        assert len(swaps) == 2

    def test_waf_swap_time_still_counts_against_account_deadline(self, tmp_path, monkeypatch):
        """WAF 换 IP 的耗时仍应计入账号时间盒。"""
        runner = _make_runner(tmp_path, monkeypatch, use_browser=True)
        runner._pool = _EndlessPool()
        account = runner.cfg.accounts[0]
        clock = _FakeClock()
        monkeypatch.setattr(runner_mod.time, "monotonic", clock.monotonic)
        monkeypatch.setattr(runner_mod, "ACCOUNT_DEADLINE_SECONDS", 1)
        monkeypatch.setattr(runner_mod, "SHIELD_RETRY_BACKOFF_MAX", 0)
        monkeypatch.setattr(runner_mod.time, "sleep", lambda _seconds: None)

        results = [api.WAF_BLOCKED, api.CF_BLOCKED, api.SUCCESS]
        calls = []
        monkeypatch.setattr(
            runner, "_attempt",
            lambda acct, record: (calls.append(1), _row(acct.name, results.pop(0)))[1],
        )

        def slow_waf_swap(_account):
            clock.now += 2
            return True

        monkeypatch.setattr(runner, "_swap_pooled_proxy", slow_waf_swap)

        row = runner._run_account(account)
        assert row.status == api.CF_BLOCKED
        assert len(calls) == 2

    def test_network_without_new_ip_is_skipped(self, tmp_path, monkeypatch):
        """网络异常但没有新的代理可换时直接跳过，不在原 IP 上重试。"""
        runner, account = self._runner(tmp_path, monkeypatch, retry=1, ip_swap_limit=1,
                                       pool=False)
        calls = []
        monkeypatch.setattr(
            runner, "_attempt",
            lambda acct, record: (calls.append(1), _row(acct.name, api.NETWORK_ERROR))[1],
        )
        row = runner._run_account(account)
        assert row.status == "skipped"
        assert len(calls) == 1

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
        row = runner._run_account(account)
        assert row.status == "skipped"
        assert proxies == ["http://fixed.example.com:8080"]

    def test_business_failure_allows_five_ip_swaps_then_skips(self, tmp_path, monkeypatch):
        """源站业务失败允许额外换 5 个 IP，仍失败后才跳过。"""
        runner = _make_runner(tmp_path, monkeypatch)
        runner._pool = _EndlessPool()
        monkeypatch.setattr(runner_mod.time, "sleep", lambda _s: None)
        account = runner.cfg.accounts[0]
        calls = []
        monkeypatch.setattr(
            runner, "_attempt",
            lambda acct, record: (calls.append(acct.proxy), _row(acct.name, api.FAILED))[1],
        )
        runner._assign_proxy(account)
        row = runner._run_account(account)
        assert row.status == "skipped"
        assert calls == ["p1:80", "p2:80", "p3:80", "p4:80", "p5:80", "p6:80"]

    def test_waf_block_allows_five_ip_swaps_then_skips(self, tmp_path, monkeypatch):
        """WAF 硬封禁允许额外换 5 个 IP，仍封禁后才跳过。"""
        runner = _make_runner(tmp_path, monkeypatch)
        runner._pool = _EndlessPool()
        monkeypatch.setattr(runner_mod.time, "sleep", lambda _s: None)
        account = runner.cfg.accounts[0]
        calls = []
        monkeypatch.setattr(
            runner, "_attempt",
            lambda acct, record: (calls.append(acct.proxy), _row(acct.name, api.WAF_BLOCKED))[1],
        )
        runner._assign_proxy(account)
        row = runner._run_account(account)
        assert row.status == "skipped"
        assert calls == ["p1:80", "p2:80", "p3:80", "p4:80", "p5:80", "p6:80"]

    def test_source_failure_waits_five_seconds_after_each_ip_swap(self, tmp_path, monkeypatch):
        """源站失败每次成功换 IP 后等待 5 秒，再进入该账号的下一次尝试。"""
        runner = _make_runner(tmp_path, monkeypatch)
        runner._pool = _EndlessPool()
        account = runner.cfg.accounts[0]
        calls = []
        slept = []
        results = [api.FAILED] * runner_mod.SOURCE_IP_SWAP_LIMIT + [api.SUCCESS]
        monkeypatch.setattr(runner_mod.time, "sleep", slept.append)
        monkeypatch.setattr(
            runner, "_attempt",
            lambda acct, record: (calls.append(acct.proxy), _row(acct.name, results.pop(0)))[1],
        )
        runner._assign_proxy(account)
        row = runner._run_account(account)
        assert row.status == api.SUCCESS
        assert calls == ["p1:80", "p2:80", "p3:80", "p4:80", "p5:80", "p6:80"]
        assert slept == [runner_mod.SOURCE_IP_SWAP_BACKOFF_SECONDS] * runner_mod.SOURCE_IP_SWAP_LIMIT

    def test_business_failure_never_uses_regular_retry_budget(self, tmp_path, monkeypatch):
        runner, account = self._runner(tmp_path, monkeypatch, retry=2, ip_swap_limit=2)
        proxies = []
        monkeypatch.setattr(
            runner, "_attempt",
            lambda acct, record: (proxies.append(acct.proxy),
                                  _row(acct.name, api.FAILED))[1],
        )
        runner._assign_proxy(account)
        row = runner._run_account(account)
        assert row.status == "skipped"
        assert proxies == ["p1:80", "p2:80", "p3:80", "p4:80", "p5:80", "p6:80"]

    def test_without_pool_network_error_is_skipped(self, tmp_path, monkeypatch):
        runner, account = self._runner(tmp_path, monkeypatch, retry=2, pool=False)
        calls = []
        monkeypatch.setattr(
            runner, "_attempt",
            lambda acct, record: (calls.append(1),
                                  _row(acct.name, api.NETWORK_ERROR))[1],
        )
        row = runner._run_account(account)
        assert row.status == "skipped"
        assert len(calls) == 1


class _FakeClock:
    def __init__(self):
        self.now = 0.0

    def monotonic(self):
        return self.now


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

    def test_explicit_browser_parallel_is_ignored(self, tmp_path, monkeypatch):
        runner = _make_runner(tmp_path, monkeypatch, parallelism=8, browser_parallelism=1)
        assert runner._browser_workers() == 2

    def test_fixed_browser_workers_stay_at_two(self, tmp_path, monkeypatch):
        runner = _make_runner(tmp_path, monkeypatch, parallelism=8)
        assert runner._browser_workers() == 2
        assert runner_mod.FIXED_BROWSER_PARALLELISM == 2

    def test_browser_workers_do_not_depend_on_cpu_count(self, tmp_path, monkeypatch):
        """浏览器并发固定为 2，不随 CPU 核数变化。"""
        runner = _make_runner(tmp_path, monkeypatch, parallelism=16)
        assert runner._browser_workers() == 2

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


class TestFixedParallelism:
    def test_explicit_one_is_overridden_for_automated_runs(self, tmp_path, monkeypatch):
        runner = _make_runner(tmp_path, monkeypatch, accounts=3,
                              parallelism=1, parallelism_explicit=True)
        monkeypatch.setattr(runner, "_run_account",
                            lambda account: _row(account.name, api.SUCCESS))
        assert runner.run() == 0
        assert runner.options.parallelism == 4

    def test_unspecified_is_promoted_to_fixed_default(self, tmp_path, monkeypatch):
        runner = _make_runner(tmp_path, monkeypatch, accounts=3, parallelism=1)
        monkeypatch.setattr(runner, "_run_account",
                            lambda account: _row(account.name, api.SUCCESS))
        assert runner.run() == 0
        assert runner.options.parallelism == runner_mod.DEFAULT_ACCOUNT_PARALLELISM
        assert runner_mod.DEFAULT_ACCOUNT_PARALLELISM == 4


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


class TestBrowserQueueDoesNotEatDeadline:
    """排队等浏览器槽位是等全局资源，不该从账号自己的时间盒里扣。

    线上现象：73 个账号抢 2 个浏览器槽位，日志出现「等待浏览器并发配额 102.1s」，
    盾类只跑了 3 轮就报「已用满 1200s 时间盒」——预算被排队吃掉了。
    """

    def test_gate_wait_is_added_back_to_deadline(self, tmp_path, monkeypatch):
        runner = _make_runner(tmp_path, monkeypatch, use_browser=True)
        runner._pool = _EndlessPool()
        clock = _FakeClock()
        monkeypatch.setattr(runner_mod.time, "monotonic", clock.monotonic)
        monkeypatch.setattr(runner_mod.time, "sleep", lambda _s: None)
        monkeypatch.setattr(runner_mod, "ACCOUNT_DEADLINE_SECONDS", 10)
        monkeypatch.setattr(runner_mod, "SHIELD_RETRY_BACKOFF_MAX", 0)

        calls = []

        def fake_attempt(account, record):
            calls.append(len(calls))
            # 每轮：排队 100 秒（应被补偿）+ 真正尝试 4 秒（应计入）
            runner._add_gate_wait(100.0)
            clock.now += 104.0
            return _row(account.name, api.CF_BLOCKED)

        monkeypatch.setattr(runner, "_attempt", fake_attempt)
        row = runner._run_account(runner.cfg.accounts[0])
        assert row.status == api.CF_BLOCKED
        # 时间盒 10s、每轮真实耗时 4s -> 能跑到第 3 轮才耗尽；
        # 若排队时间没被加回，第 1 轮后就会直接放弃
        assert len(calls) == 3

    def test_without_compensation_the_box_would_die_in_one_round(self, tmp_path, monkeypatch):
        """对照组：不喂排队读数时，同样的耗时会在第一轮就把时间盒用光。"""
        runner = _make_runner(tmp_path, monkeypatch, use_browser=True)
        runner._pool = _EndlessPool()
        clock = _FakeClock()
        monkeypatch.setattr(runner_mod.time, "monotonic", clock.monotonic)
        monkeypatch.setattr(runner_mod.time, "sleep", lambda _s: None)
        monkeypatch.setattr(runner_mod, "ACCOUNT_DEADLINE_SECONDS", 10)

        calls = []

        def fake_attempt(account, record):
            calls.append(len(calls))
            clock.now += 104.0
            return _row(account.name, api.CF_BLOCKED)

        monkeypatch.setattr(runner, "_attempt", fake_attempt)
        runner._run_account(runner.cfg.accounts[0])
        assert len(calls) == 1

    def test_gate_wait_is_recorded_by_the_semaphore_path(self, tmp_path, monkeypatch):
        """真正走信号量时也要记账，不能只在测试里手动喂。"""
        runner = _make_runner(tmp_path, monkeypatch, use_browser=True)
        runner._browser_gate = threading.Semaphore(1)
        runner._browser_gate.acquire()  # 占满，迫使下面的调用排队

        def release_soon():
            time.sleep(0.15)
            runner._browser_gate.release()

        threading.Thread(target=release_soon, daemon=True).start()
        runner._solve_guarded(lambda **_kw: "done", runner.cfg.accounts[0], None)
        assert runner._take_gate_wait() >= 0.1

    def test_take_gate_wait_clears_the_counter(self, tmp_path, monkeypatch):
        runner = _make_runner(tmp_path, monkeypatch)
        runner._add_gate_wait(3.0)
        assert runner._take_gate_wait() == 3.0
        assert runner._take_gate_wait() == 0.0

    def test_negative_and_zero_waits_are_ignored(self, tmp_path, monkeypatch):
        runner = _make_runner(tmp_path, monkeypatch)
        runner._add_gate_wait(0.0)
        runner._add_gate_wait(-5.0)
        assert runner._take_gate_wait() == 0.0


class TestShieldRetriesWithoutNewIP:
    """盾类重试次数不限：换不到新 IP 也要沿用当前出口重开浏览器继续试。

    Turnstile 失败未必是 IP 问题（机房指纹同样会被拒），代理池空了就白扔一个
    账号不合理；原先的日志文案本就写着「无新 IP 可换，沿用当前 IP」，但控制流
    在那之前已经 return，那段是死代码。
    """

    @staticmethod
    def _wire_shield(runner, monkeypatch, *, deadline=0.6):
        monkeypatch.setattr(runner_mod, "ACCOUNT_DEADLINE_SECONDS", deadline)
        monkeypatch.setattr(runner_mod, "SHIELD_RETRY_BACKOFF_MAX", 0)
        monkeypatch.setattr(runner_mod.time, "sleep", lambda _s: None)
        calls = []

        def fake_attempt(account, record):
            calls.append(account.proxy)
            return _row(account.name, api.TURNSTILE_REQUIRED)

        monkeypatch.setattr(runner, "_attempt", fake_attempt)
        return calls

    def test_no_pool_still_retries_until_deadline(self, tmp_path, monkeypatch):
        """完全没有代理池：不再判 skipped，而是沿用直连一直重试到时间盒。"""
        runner = _make_runner(tmp_path, monkeypatch, use_browser=True)
        assert runner._pool is None
        calls = self._wire_shield(runner, monkeypatch)
        row = runner._run_account(runner.cfg.accounts[0])
        assert row.status == api.TURNSTILE_REQUIRED
        assert row.status != "skipped"
        assert len(calls) > 3

    def test_exhausted_pool_still_retries(self, tmp_path, monkeypatch):
        """池子给不出新 IP 时同样继续，出口保持不变。"""
        class _DryPool:
            def acquire(self):
                return None

            def mark_bad(self, proxy):
                pass

        runner = _make_runner(tmp_path, monkeypatch, use_browser=True)
        runner._pool = _DryPool()
        runner._pooled_proxies[runner.cfg.accounts[0].name] = "p0:80"
        runner.cfg.accounts[0].proxy = "p0:80"
        calls = self._wire_shield(runner, monkeypatch)
        runner._run_account(runner.cfg.accounts[0])
        assert len(calls) > 3
        # 拿不到替代品时绝不降级直连，始终沿用原代理
        assert set(calls) == {"p0:80"}

    def test_log_says_it_reuses_current_ip(self, tmp_path, monkeypatch, capsys):
        runner = _make_runner(tmp_path, monkeypatch, use_browser=True)
        monkeypatch.setattr(log, "VERBOSE", True, raising=False)
        self._wire_shield(runner, monkeypatch, deadline=0.2)
        runner._run_account(runner.cfg.accounts[0])
        # logger 会按宽度折行，比对前先把换行和多余空白压掉
        out = "".join(capsys.readouterr().out.split())
        assert "无新IP可换，沿用当前IP重开浏览器" in out
        assert "停止该账号的全部重试" in out

    def test_manual_proxy_account_keeps_retrying(self, tmp_path, monkeypatch):
        """手动配置代理的账号从不换 IP，但盾类重试不该因此被取消。"""
        runner = _make_runner(tmp_path, monkeypatch, use_browser=True)
        runner._pool = _EndlessPool()
        runner.cfg.accounts[0].proxy = "http://manual:8080"
        calls = self._wire_shield(runner, monkeypatch)
        runner._run_account(runner.cfg.accounts[0])
        assert len(calls) > 3
        assert set(calls) == {"http://manual:8080"}


class TestDeadlineStopsAllRetries:
    """时间盒是所有重试的统一上限：耗尽后连网络异常和源站换 IP 也不再继续。"""

    @staticmethod
    def _wire(runner, monkeypatch, status):
        clock = _FakeClock()
        monkeypatch.setattr(runner_mod.time, "monotonic", clock.monotonic)
        monkeypatch.setattr(runner_mod.time, "sleep", lambda _s: None)
        monkeypatch.setattr(runner_mod, "ACCOUNT_DEADLINE_SECONDS", 5)
        runner._pool = _EndlessPool()
        runner._pooled_proxies[runner.cfg.accounts[0].name] = "p0:80"
        calls = []

        def fake_attempt(account, record):
            calls.append(len(calls))
            clock.now += 10.0  # 一轮就把时间盒烧穿
            return _row(account.name, status)

        monkeypatch.setattr(runner, "_attempt", fake_attempt)
        return calls

    def test_network_error_stops_when_box_is_empty(self, tmp_path, monkeypatch):
        runner = _make_runner(tmp_path, monkeypatch, use_browser=True)
        calls = self._wire(runner, monkeypatch, api.NETWORK_ERROR)
        row = runner._run_account(runner.cfg.accounts[0])
        assert len(calls) == 1
        assert row.status == api.NETWORK_ERROR

    def test_waf_block_stops_when_box_is_empty(self, tmp_path, monkeypatch):
        runner = _make_runner(tmp_path, monkeypatch, use_browser=True)
        calls = self._wire(runner, monkeypatch, api.WAF_BLOCKED)
        row = runner._run_account(runner.cfg.accounts[0])
        assert len(calls) == 1
        assert row.status == api.WAF_BLOCKED

    def test_success_is_never_blocked_by_the_deadline(self, tmp_path, monkeypatch):
        """时间盒只掐重试，已经拿到结论的成功结果照常返回。"""
        runner = _make_runner(tmp_path, monkeypatch, use_browser=True)
        calls = self._wire(runner, monkeypatch, api.SUCCESS)
        row = runner._run_account(runner.cfg.accounts[0])
        assert len(calls) == 1
        assert row.status == api.SUCCESS
