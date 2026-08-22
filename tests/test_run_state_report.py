"""签到运行状态上报与网页端锁的测试。

这些断言守的是一条容易被忽视的链路：TaBiAI 的 new_api_refresh 有代次轮转 +
重放检测，签到进程和网页端同时动同一条 sid 就会把整条会话打死。上报做不到位
（漏报、心跳断在账号进度上、异常没吞住）的后果不是「提示不准」，而是账号被废
或者网页端被永久锁死，所以每条路径都要有测试压着。
"""

import threading
import time

import pytest

from newapi_checkin import config as cfgmod
from newapi_checkin import runner as runner_mod


def _cfg(*, sync_enabled=True, tabiai_account=True):
    accounts = []
    if tabiai_account:
        accounts.append({
            "name": "T", "url": "https://t.example.com",
            "login_method": "tabiai", "cookie": "new_api_refresh=sid.secret",
        })
    else:
        accounts.append({"name": "A", "url": "https://a.example.com", "cookie": "c"})
    return cfgmod.build_config({
        "defaults": {"retry": 0, "interval_seconds": [0, 0]},
        "config_sync": {
            "enabled": sync_enabled,
            "url": "https://panel.example.com/api/config/raw",
            "token": "k" * 20,
        },
        "accounts": accounts,
    })


def _runner(tmp_path, monkeypatch, cfg=None, **opts):
    cfg = cfg or _cfg()
    monkeypatch.setattr(runner_mod, "SESSIONS_FILE", tmp_path / "sessions.json")
    monkeypatch.setattr(runner_mod, "probe_exit_ip", lambda proxy=None, timeout=5: None)
    options = runner_mod.RunOptions(use_ai=False, use_browser=False, **opts)
    return runner_mod.Runner(cfg, options)


class TestNeedsRunLock:
    """只在真正可能撞代次的时候锁网页端，别白锁。"""

    def test_tabiai_account_needs_lock(self, tmp_path, monkeypatch):
        runner = _runner(tmp_path, monkeypatch)
        assert runner._needs_run_lock(runner.cfg.accounts) is True

    def test_pure_cookie_round_does_not_lock(self, tmp_path, monkeypatch):
        """一轮里全是站点 Cookie 账号：它们是静态凭据，锁网页端毫无收益。"""
        runner = _runner(tmp_path, monkeypatch, cfg=_cfg(tabiai_account=False))
        assert runner._needs_run_lock(runner.cfg.accounts) is False

    def test_without_config_sync_there_is_nothing_to_report_to(self, tmp_path, monkeypatch):
        runner = _runner(tmp_path, monkeypatch, cfg=_cfg(sync_enabled=False))
        assert runner._needs_run_lock(runner.cfg.accounts) is False

    def test_empty_account_list_does_not_lock(self, tmp_path, monkeypatch):
        runner = _runner(tmp_path, monkeypatch)
        assert runner._needs_run_lock([]) is False


class TestStartAndStopReport:
    def test_start_reports_and_stop_releases(self, tmp_path, monkeypatch):
        runner = _runner(tmp_path, monkeypatch)
        calls = []
        monkeypatch.setattr(
            "newapi_checkin.remote_sync.report_run_start",
            lambda sync, source: (calls.append(("start", source)), (True, "ep", 0))[1],
        )
        monkeypatch.setattr(
            "newapi_checkin.remote_sync.report_run_stop",
            lambda sync: (calls.append(("stop", None)), True)[1],
        )
        runner._start_run_report(runner.cfg.accounts)
        assert runner._run_lock_active is True
        runner._stop_run_report()
        assert runner._run_lock_active is False
        assert [c[0] for c in calls] == ["start", "stop"]

    def test_failed_start_does_not_pretend_to_hold_the_lock(self, tmp_path, monkeypatch):
        """上报失败时不能记成已加锁，否则收尾会去解一把并不存在的锁。"""
        runner = _runner(tmp_path, monkeypatch)
        stops = []
        monkeypatch.setattr(
            "newapi_checkin.remote_sync.report_run_start",
            lambda sync, source: (False, "HTTP 500", 0),
        )
        monkeypatch.setattr(
            "newapi_checkin.remote_sync.report_run_stop",
            lambda sync: (stops.append(1), True)[1],
        )
        runner._start_run_report(runner.cfg.accounts)
        assert runner._run_lock_active is False
        runner._stop_run_report()
        assert stops == []

    def test_stop_swallows_exceptions(self, tmp_path, monkeypatch):
        """收尾在 finally 里跑，抛异常会盖掉真正的签到结果。"""
        runner = _runner(tmp_path, monkeypatch)
        monkeypatch.setattr(
            "newapi_checkin.remote_sync.report_run_start",
            lambda sync, source: (True, "ep", 0),
        )

        def boom(_sync):
            raise RuntimeError("平台挂了")

        monkeypatch.setattr("newapi_checkin.remote_sync.report_run_stop", boom)
        runner._start_run_report(runner.cfg.accounts)
        runner._stop_run_report()  # 不抛就算过
        assert runner._run_lock_active is False

    def test_source_marks_github_actions(self, tmp_path, monkeypatch):
        runner = _runner(tmp_path, monkeypatch)
        monkeypatch.setenv("GITHUB_ACTIONS", "true")
        monkeypatch.setenv("GITHUB_REPOSITORY", "me/repo")
        source = runner._run_source()
        assert "GitHub Actions" in source and "me/repo" in source

    def test_source_falls_back_to_hostname(self, tmp_path, monkeypatch):
        runner = _runner(tmp_path, monkeypatch)
        monkeypatch.delenv("GITHUB_ACTIONS", raising=False)
        assert runner._run_source() != ""


class TestHeartbeat:
    def test_heartbeat_keeps_beating_independently_of_accounts(self, tmp_path, monkeypatch):
        """心跳必须独立于账号进度：一个账号过盾十几分钟也不能让锁过期。"""
        runner = _runner(tmp_path, monkeypatch)
        beats = threading.Event()
        count = []

        def fake_beat(_sync):
            count.append(1)
            beats.set()
            return True, True

        monkeypatch.setattr("newapi_checkin.remote_sync.report_run_heartbeat", fake_beat)
        runner._start_heartbeat(0.05)
        assert beats.wait(3), "心跳线程没有按间隔上报"
        runner._stop_run_report()
        assert len(count) >= 1

    def test_stop_terminates_the_heartbeat_thread(self, tmp_path, monkeypatch):
        runner = _runner(tmp_path, monkeypatch)
        monkeypatch.setattr(
            "newapi_checkin.remote_sync.report_run_heartbeat",
            lambda _sync: (True, True),
        )
        runner._start_heartbeat(0.05)
        thread = runner._heartbeat_thread
        assert thread is not None and thread.is_alive()
        runner._stop_run_report()
        time.sleep(0.2)
        assert not thread.is_alive(), "收尾后心跳线程必须退出"
        assert runner._heartbeat_thread is None

    def test_heartbeat_is_daemon_so_it_never_blocks_exit(self, tmp_path, monkeypatch):
        monkeypatch.setattr(
            "newapi_checkin.remote_sync.report_run_heartbeat",
            lambda _sync: (True, True),
        )
        runner = _runner(tmp_path, monkeypatch)
        runner._start_heartbeat(60)
        assert runner._heartbeat_thread.daemon is True
        runner._stop_run_report()

    def test_heartbeat_failure_does_not_kill_the_thread(self, tmp_path, monkeypatch):
        """网络抖一下不能让后续心跳全断，否则锁会莫名过期。"""
        runner = _runner(tmp_path, monkeypatch)
        results = [(False, False), (True, True), (True, True)]
        seen = []
        done = threading.Event()

        def flaky(_sync):
            item = results.pop(0) if results else (True, True)
            seen.append(item)
            if len(seen) >= 2:
                done.set()
            return item

        monkeypatch.setattr("newapi_checkin.remote_sync.report_run_heartbeat", flaky)
        runner._start_heartbeat(0.05)
        assert done.wait(3), "首次心跳失败后线程就停了"
        runner._stop_run_report()

    def test_platform_saying_not_running_is_warned_not_fatal(self, tmp_path, monkeypatch):
        """管理员强制解锁后，客户端要继续跑完，只是得把风险说出来。"""
        runner = _runner(tmp_path, monkeypatch)
        done = threading.Event()

        def unlocked(_sync):
            done.set()
            return True, False

        monkeypatch.setattr("newapi_checkin.remote_sync.report_run_heartbeat", unlocked)
        runner._start_heartbeat(0.05)
        assert done.wait(3)
        runner._stop_run_report()
        assert runner._heartbeat_thread is None


class TestRunIntegration:
    """整轮集成。

    注意必须把 _run_serial 与 _run_parallel 都换掉：自动签到会把并发度设成
    DEFAULT_ACCOUNT_PARALLELISM，只挡串行那条会漏进真实网络请求，测试还会假通过。
    """

    @staticmethod
    def _stub_round(runner, monkeypatch, body):
        monkeypatch.setattr(runner, "_run_serial", body)
        monkeypatch.setattr(runner, "_run_parallel", lambda accounts, workers: body(accounts))
        monkeypatch.setattr(runner, "init_proxy_pool", lambda **kw: None)

    def test_run_reports_start_and_stop_around_the_round(self, tmp_path, monkeypatch):
        """上报要真的包住签到本体，顺序不能错。"""
        runner = _runner(tmp_path, monkeypatch, dry_run=True)
        order = []
        monkeypatch.setattr(
            "newapi_checkin.remote_sync.report_run_start",
            lambda sync, source: (order.append("start"), (True, "ep", 0))[1],
        )
        monkeypatch.setattr(
            "newapi_checkin.remote_sync.report_run_stop",
            lambda sync: (order.append("stop"), True)[1],
        )
        monkeypatch.setattr(
            "newapi_checkin.remote_sync.report_run_heartbeat",
            lambda _sync: (True, True),
        )
        monkeypatch.setattr(runner, "_send_notification", lambda: None)
        self._stub_round(runner, monkeypatch, lambda accounts: (order.append("checkin"), 0)[1])
        assert runner.run() == 0
        assert order == ["start", "checkin", "stop"]

    def test_run_still_unlocks_when_the_round_blows_up(self, tmp_path, monkeypatch):
        """整轮异常时也必须解锁，否则网页端要干等到过期。"""
        runner = _runner(tmp_path, monkeypatch, dry_run=True)
        order = []
        monkeypatch.setattr(
            "newapi_checkin.remote_sync.report_run_start",
            lambda sync, source: (order.append("start"), (True, "ep", 0))[1],
        )
        monkeypatch.setattr(
            "newapi_checkin.remote_sync.report_run_stop",
            lambda sync: (order.append("stop"), True)[1],
        )

        def boom(_accounts):
            order.append("boom")
            raise RuntimeError("跑崩了")

        self._stub_round(runner, monkeypatch, boom)
        try:
            runner.run()
        except RuntimeError:
            pass
        assert order == ["start", "boom", "stop"]

    def test_pure_cookie_round_reports_nothing(self, tmp_path, monkeypatch):
        """全是站点 Cookie 的一轮不该锁网页端，也就不该有任何上报。"""
        runner = _runner(tmp_path, monkeypatch, cfg=_cfg(tabiai_account=False), dry_run=True)
        calls = []
        monkeypatch.setattr(
            "newapi_checkin.remote_sync.report_run_start",
            lambda sync, source: (calls.append("start"), (True, "ep", 0))[1],
        )
        monkeypatch.setattr(
            "newapi_checkin.remote_sync.report_run_stop",
            lambda sync: (calls.append("stop"), True)[1],
        )
        monkeypatch.setattr(runner, "_send_notification", lambda: None)
        self._stub_round(runner, monkeypatch, lambda accounts: 0)
        assert runner.run() == 0
        assert calls == []


class TestKeepaliveYield:
    """签到开跑前给平台上的凭据保活让路。

    保活协程也会真 refresh，跟签到撞上就是旧代重放。保活那边已经会避让签到，
    但「保活跑到一半、签到才启动」这个窗口只能由客户端这边堵。

    判定必须落在 source 上：run_state 是引用计数锁，分片并行时几个 job 同时持锁
    是常态，按「有人持锁」判定会让分片互等直接死锁。
    """

    @staticmethod
    def _states(monkeypatch, sequence):
        """把 fetch_run_state 换成按调用次序吐 sequence 的假实现，返回调用记录。"""
        calls = []

        def fake(_sync):
            calls.append(True)
            return sequence[min(len(calls) - 1, len(sequence) - 1)]

        monkeypatch.setattr("newapi_checkin.remote_sync.fetch_run_state", fake)
        return calls

    def test_waits_until_keepalive_finishes(self, tmp_path, monkeypatch):
        runner = _runner(tmp_path, monkeypatch)
        calls = self._states(monkeypatch, [
            (True, {"running": True, "source": "tabiai-keepalive"}),
            (True, {"running": True, "source": "tabiai-keepalive"}),
            (True, {"running": False}),
        ])
        slept = []
        monkeypatch.setattr(runner_mod.time, "sleep", slept.append)
        runner._wait_for_keepalive(runner.cfg.accounts)
        assert len(calls) == 3                    # 一直问到它跑完
        assert slept == [runner_mod.KEEPALIVE_POLL_SECONDS] * 2

    def test_does_not_wait_for_sibling_shards(self, tmp_path, monkeypatch):
        """别的签到分片持锁是同伴而不是竞争者 —— 互等会死锁。"""
        runner = _runner(tmp_path, monkeypatch)
        calls = self._states(monkeypatch, [
            (True, {"running": True, "source": "github-actions shard 2/4"}),
        ])
        monkeypatch.setattr(runner_mod.time, "sleep",
                            lambda _s: pytest.fail("不该为同伴分片等待"))
        runner._wait_for_keepalive(runner.cfg.accounts)
        assert len(calls) == 1

    def test_timeout_proceeds_with_a_warning(self, tmp_path, monkeypatch):
        """保活卡死不能把签到一起拖死：到点带告警继续跑。"""
        runner = _runner(tmp_path, monkeypatch)
        self._states(monkeypatch, [(True, {"running": True, "source": "tabiai-keepalive"})])
        clock = {"t": 0.0}
        monkeypatch.setattr(runner_mod.time, "monotonic", lambda: clock["t"])
        monkeypatch.setattr(runner_mod.time, "sleep",
                            lambda s: clock.__setitem__("t", clock["t"] + s))
        warnings = []
        monkeypatch.setattr(runner_mod.log, "warn", warnings.append)
        runner._wait_for_keepalive(runner.cfg.accounts)
        assert clock["t"] >= runner_mod.KEEPALIVE_WAIT_MAX_SECONDS
        assert warnings and "保活" in warnings[0]

    def test_wait_ceiling_is_five_minutes(self):
        assert runner_mod.KEEPALIVE_WAIT_MAX_SECONDS == 300
        assert runner_mod.KEEPALIVE_RUN_SOURCE == "tabiai-keepalive"

    def test_query_failure_is_treated_as_unlocked(self, tmp_path, monkeypatch):
        """平台不可达时照常签到 —— 查不到锁不能变成不干活。"""
        runner = _runner(tmp_path, monkeypatch)
        calls = self._states(monkeypatch, [(False, {})])
        monkeypatch.setattr(runner_mod.time, "sleep",
                            lambda _s: pytest.fail("查询失败不该等待"))
        runner._wait_for_keepalive(runner.cfg.accounts)
        assert len(calls) == 1

    def test_pure_cookie_round_never_asks(self, tmp_path, monkeypatch):
        """没有 tabiai 账号就不存在代次冲突，压根不用查。"""
        runner = _runner(tmp_path, monkeypatch, cfg=_cfg(tabiai_account=False))
        calls = self._states(monkeypatch, [
            (True, {"running": True, "source": "tabiai-keepalive"}),
        ])
        runner._wait_for_keepalive(runner.cfg.accounts)
        assert calls == []

    def test_run_waits_before_reporting_start(self, tmp_path, monkeypatch):
        """让路必须排在 start 上报之前，否则两边同时持锁等于没让。"""
        runner = _runner(tmp_path, monkeypatch, dry_run=True)
        order = []
        monkeypatch.setattr(
            "newapi_checkin.remote_sync.fetch_run_state",
            lambda _sync: (order.append("wait"), (True, {"running": False}))[1],
        )
        monkeypatch.setattr(
            "newapi_checkin.remote_sync.report_run_start",
            lambda sync, source: (order.append("start"), (True, "ep", 0))[1],
        )
        monkeypatch.setattr("newapi_checkin.remote_sync.report_run_stop",
                            lambda sync: (order.append("stop"), True)[1])
        monkeypatch.setattr("newapi_checkin.remote_sync.report_run_heartbeat",
                            lambda _sync: (True, True))
        monkeypatch.setattr(runner, "_send_notification", lambda: None)
        TestRunIntegration._stub_round(
            runner, monkeypatch, lambda accounts: (order.append("checkin"), 0)[1]
        )
        assert runner.run() == 0
        assert order == ["wait", "start", "checkin", "stop"]
