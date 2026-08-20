"""tests/test_proxy_feedback.py

代理反馈回路：计数、快照、端点推导、上报开关，以及 Runner 收尾时是否真的会上报。

守的是优选的因果链 —— Actions 里试出来的结果必须能回到平台。链子断在任何一环，
下次预取还是那套跟 runner 网络无关的延迟/测速排序，坏代理照样排在前面被再踩一遍。
"""
import pytest

from newapi_checkin.proxy_pool import ProxyPool, ProxyPoolConfig


def _pool(**kwargs) -> ProxyPool:
    cfg = ProxyPoolConfig(
        enabled=True,
        remote_url=kwargs.pop("remote_url", "https://cfg.example.com/api/proxies/available"),
        remote_token=kwargs.pop("remote_token", "ncf_deadbeef"),
        **kwargs,
    )
    return ProxyPool(cfg)


class TestFeedbackCounters:
    def test_mark_bad_splits_net_and_block(self):
        pool = _pool()
        pool.mark_bad("a:80", "net")
        pool.mark_bad("a:80", "net")
        pool.mark_bad("b:80", "block")
        snap = {i["addr"]: i for i in pool.feedback_snapshot()}
        assert snap["a:80"] == {"addr": "a:80", "ok": 0, "net_fail": 2, "block_fail": 0}
        assert snap["b:80"] == {"addr": "b:80", "ok": 0, "net_fail": 0, "block_fail": 1}

    def test_mark_bad_defaults_to_net(self):
        """老调用方不传 reason 时按网络层失败记，不能因为少个参数就丢计数。"""
        pool = _pool()
        pool.mark_bad("a:80")
        assert pool.feedback_snapshot()[0]["net_fail"] == 1

    def test_mark_ok_and_mixed(self):
        pool = _pool()
        pool.mark_ok("a:80")
        pool.mark_ok("a:80")
        pool.mark_bad("a:80", "block")
        assert pool.feedback_snapshot() == [
            {"addr": "a:80", "ok": 2, "net_fail": 0, "block_fail": 1}
        ]

    def test_none_proxy_is_ignored(self):
        pool = _pool()
        pool.mark_ok(None)
        pool.mark_bad(None, "net")
        assert pool.feedback_snapshot() == []

    def test_snapshot_survives_refresh(self):
        """中途换过列表也要留着统计：上报的是「这轮用过谁、表现如何」，
        跟当前池里还剩谁无关。"""
        pool = _pool()
        pool.mark_bad("gone:80", "net")
        pool._fetch_remote = lambda: ["new:80"]
        assert pool.refresh() == 1
        assert pool.feedback_snapshot() == [
            {"addr": "gone:80", "ok": 0, "net_fail": 1, "block_fail": 0}
        ]


class TestFeedbackEndpoint:
    def test_derives_from_remote_url(self):
        pool = _pool(remote_url="https://cfg.example.com/api/proxies/available?limit=50")
        assert pool._feedback_endpoint() == "https://cfg.example.com/api/proxies/feedback"

    def test_keeps_port_and_scheme(self):
        pool = _pool(remote_url="http://10.0.0.5:8080/api/proxies/available")
        assert pool._feedback_endpoint() == "http://10.0.0.5:8080/api/proxies/feedback"

    @pytest.mark.parametrize("bad", ["", "not-a-url", "/api/proxies/available"])
    def test_unresolvable_returns_empty(self, bad):
        assert _pool(remote_url=bad)._feedback_endpoint() == ""


class _FakeResponse:
    def __init__(self, status=200, payload=None):
        self.status_code = status
        self._payload = payload if payload is not None else {"ok": True}
        self.text = "fake"

    def json(self):
        return self._payload


class TestReportFeedback:
    @staticmethod
    def _capture(monkeypatch, response=None):
        calls = []

        def fake_post(url, **kwargs):
            calls.append({"url": url, **kwargs})
            return response or _FakeResponse()

        monkeypatch.setattr("curl_cffi.requests.post", fake_post)
        return calls

    def test_posts_snapshot_with_auth(self, monkeypatch):
        calls = self._capture(monkeypatch)
        pool = _pool()
        pool.mark_ok("good:80")
        pool.mark_bad("bad:80", "block")
        ok, detail = pool.report_feedback()
        assert ok, detail
        assert len(calls) == 1
        sent = calls[0]
        assert sent["url"] == "https://cfg.example.com/api/proxies/feedback"
        assert sent["headers"]["Authorization"] == "Bearer ncf_deadbeef"
        assert sent["headers"]["Content-Type"] == "application/json"
        assert sent["json"]["source"] == "github-actions"
        by_addr = {i["addr"]: i for i in sent["json"]["items"]}
        assert by_addr["good:80"]["ok"] == 1
        assert by_addr["bad:80"]["block_fail"] == 1

    def test_disabled_switch_sends_nothing(self, monkeypatch):
        calls = self._capture(monkeypatch)
        pool = _pool(report_feedback=False)
        pool.mark_ok("good:80")
        ok, detail = pool.report_feedback()
        assert ok is False and "已关闭" in detail
        assert calls == []

    def test_no_records_sends_nothing(self, monkeypatch):
        calls = self._capture(monkeypatch)
        ok, detail = _pool().report_feedback()
        assert ok is False and "没有可回传" in detail
        assert calls == []

    def test_missing_remote_url_sends_nothing(self, monkeypatch):
        calls = self._capture(monkeypatch)
        pool = _pool(remote_url="")
        pool.mark_ok("good:80")
        ok, detail = pool.report_feedback()
        assert ok is False and "无法确定回传地址" in detail
        assert calls == []

    def test_http_error_is_reported_not_raised(self, monkeypatch):
        self._capture(monkeypatch, _FakeResponse(status=500))
        pool = _pool()
        pool.mark_ok("good:80")
        ok, detail = pool.report_feedback()
        assert ok is False and "HTTP 500" in detail

    def test_network_exception_is_swallowed(self, monkeypatch):
        def boom(url, **kwargs):
            raise RuntimeError("connection reset")

        monkeypatch.setattr("curl_cffi.requests.post", boom)
        pool = _pool()
        pool.mark_ok("good:80")
        ok, detail = pool.report_feedback()
        assert ok is False and "RuntimeError" in detail


class TestRunnerWiring:
    """Runner 侧的接线：计数记在哪、收尾时会不会真的上报。"""

    @staticmethod
    def _runner(monkeypatch, tmp_path, proxies):
        # tests/ 没有 __init__.py，conftest 把仓库根塞进 sys.path，测试模块按顶层名导入
        from test_proxy_pool import FakePool, _make_runner

        runner = _make_runner(monkeypatch, tmp_path, pool_proxies=proxies)
        assert isinstance(runner._pool, FakePool)
        return runner

    def test_success_records_ok_on_the_proxy_in_use(self, monkeypatch, tmp_path):
        from newapi_checkin import runner as runner_mod

        runner = self._runner(monkeypatch, tmp_path, ["p1:80", "p2:80"])
        monkeypatch.setattr(
            runner, "_attempt",
            lambda account, record: runner_mod.log.SummaryRow(
                account.name, "success", "S1", "ok"),
        )
        account = runner.cfg.accounts[0]
        runner._assign_proxy(account)
        row = runner._run_account(account)
        assert row.status == "success"
        assert runner._pool.ok == ["p1:80"]

    def test_already_done_also_counts_as_ok(self, monkeypatch, tmp_path):
        from newapi_checkin import runner as runner_mod

        runner = self._runner(monkeypatch, tmp_path, ["p1:80"])
        monkeypatch.setattr(
            runner, "_attempt",
            lambda account, record: runner_mod.log.SummaryRow(
                account.name, "already_done", "S1", "今天已签"),
        )
        account = runner.cfg.accounts[0]
        runner._assign_proxy(account)
        runner._run_account(account)
        assert runner._pool.ok == ["p1:80"]

    def test_skipped_account_records_nothing(self, monkeypatch, tmp_path):
        """跳过的账号既没成也没失败，不该给代理记任何一笔。"""
        from newapi_checkin import runner as runner_mod

        runner = self._runner(monkeypatch, tmp_path, ["p1:80"])
        monkeypatch.setattr(
            runner, "_attempt",
            lambda account, record: runner_mod.log.SummaryRow(
                account.name, "skipped", "-", "跳过"),
        )
        account = runner.cfg.accounts[0]
        runner._assign_proxy(account)
        runner._run_account(account)
        assert runner._pool.ok == []

    def test_run_reports_feedback_even_without_run_lock(self, monkeypatch, tmp_path):
        """收尾上报必须独立于运行锁。

        运行锁只有「config_sync 启用且本轮含 tabiai 账号」时才拿，这个用例两个条件
        都不满足。如果把上报塞进 _stop_run_report 搭车，纯站点 Cookie 的轮次就会整份
        丢掉反馈 —— 而那恰恰是最常见的轮次。
        """
        from newapi_checkin import runner as runner_mod

        runner = self._runner(monkeypatch, tmp_path, ["p1:80", "p2:80"])
        monkeypatch.setattr(
            runner, "_attempt",
            lambda account, record: runner_mod.log.SummaryRow(
                account.name, "success", "S1", "ok"),
        )
        reported = []
        monkeypatch.setattr(
            runner._pool, "report_feedback",
            lambda source="github-actions": (reported.append(source), (True, "ok"))[1],
            raising=False,
        )
        assert runner._needs_run_lock(runner.cfg.accounts) is False
        runner.run()
        assert reported == ["github-actions"]

    def test_report_failure_does_not_break_run(self, monkeypatch, tmp_path):
        """上报炸了也不能改变退出码：签到已经跑完，统计没送出去不算这轮失败。"""
        from newapi_checkin import runner as runner_mod

        runner = self._runner(monkeypatch, tmp_path, ["p1:80", "p2:80"])
        monkeypatch.setattr(
            runner, "_attempt",
            lambda account, record: runner_mod.log.SummaryRow(
                account.name, "success", "S1", "ok"),
        )

        def boom(source="github-actions"):
            raise RuntimeError("平台挂了")

        monkeypatch.setattr(runner._pool, "report_feedback", boom, raising=False)
        assert runner.run() == 0
