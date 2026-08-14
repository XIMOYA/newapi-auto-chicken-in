"""日志的账号标签：并行签到时每条日志都要能看出是哪个账号。"""

import threading

from newapi_checkin import config as cfgmod
from newapi_checkin import logger as log
from newapi_checkin import runner as runner_mod


def _capture():
    """订阅纯文本日志，返回 (收集列表, 取消订阅函数)。"""
    lines: list[str] = []
    unsubscribe = log.subscribe(lines.append)
    return lines, unsubscribe


def _body(line: str) -> str:
    """剥掉行首的 [HH:MM:SS] 时间戳，只留正文。"""
    return line.split("] ", 1)[1] if line.startswith("[") else line


def _label(line: str) -> str:
    """取出正文里的账号标签，没有标签返回空串。"""
    body = _body(line).lstrip()
    for marker in ("==> ", "OK   ", "WARN ", "FAIL ", "dbg  "):
        if body.startswith(marker):
            body = body[len(marker):]
            break
    if body.startswith("["):
        return body[1:].split("]", 1)[0]
    return ""


class TestContextLabel:
    def test_every_level_carries_the_label(self, monkeypatch):
        monkeypatch.setattr(log, "_VERBOSE", True)
        lines, unsubscribe = _capture()
        try:
            with log.context("站点A"):
                log.step("开始")
                log.info("信息")
                log.ok("成功")
                log.warn("注意")
                log.err("失败")
                log.debug("细节")
        finally:
            unsubscribe()
        assert len(lines) == 6
        assert all("[站点A]" in line for line in lines)
        # 等级标记仍在标签前面，缩进对齐不被破坏
        assert "==> [站点A] 开始" in lines[0]
        assert "OK   [站点A] 成功" in lines[2]
        assert "WARN [站点A] 注意" in lines[3]
        assert "FAIL [站点A] 失败" in lines[4]
        assert "dbg  [站点A] 细节" in lines[5]

    def test_no_label_outside_context(self):
        lines, unsubscribe = _capture()
        try:
            log.info("全局信息")
        finally:
            unsubscribe()
        assert _label(lines[0]) == ""
        assert lines[0].endswith("全局信息")

    def test_context_is_restored_on_exit(self):
        lines, unsubscribe = _capture()
        try:
            with log.context("外层"):
                with log.context("内层"):
                    log.info("里面")
                log.info("外面")
        finally:
            unsubscribe()
        assert _label(lines[0]) == "内层"
        assert _label(lines[1]) == "外层"
        assert log.get_context() == ""

    def test_context_survives_exception(self):
        try:
            with log.context("A"):
                raise RuntimeError("boom")
        except RuntimeError:
            pass
        assert log.get_context() == ""

    def test_label_is_thread_local(self):
        lines, unsubscribe = _capture()
        barrier = threading.Barrier(2)

        def worker(name: str) -> None:
            with log.context(name):
                barrier.wait()          # 强制两个线程在持有标签时交错
                log.info("干活")

        try:
            threads = [threading.Thread(target=worker, args=(n,)) for n in ("甲", "乙")]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join()
        finally:
            unsubscribe()
        assert len(lines) == 2
        assert sorted(_label(line) for line in lines) == ["乙", "甲"]
        # 任何一条都不能同时出现两个账号的标签
        assert not any("甲" in line and "乙" in line for line in lines)

    def test_markup_in_account_name_is_escaped(self, capsys):
        """账号名里带方括号不能被 rich 当成样式标记吞掉。"""
        lines, unsubscribe = _capture()
        try:
            with log.context("[bold red]怪名字"):
                log.info("内容")
        finally:
            unsubscribe()
        assert "[bold red]怪名字" in lines[0]
        assert "[bold red]怪名字" in capsys.readouterr().out


class TestRunnerLabelsAccountLogs:
    """一处埋点即可：账号执行期间所有模块的日志都自动带标签。"""

    @staticmethod
    def _runner(tmp_path, monkeypatch, name, cookie="c"):
        raw = {"name": name, "url": "https://x.example.com"}
        if cookie:
            raw["cookie"] = cookie
        cfg = cfgmod.build_config({"accounts": [raw]})
        monkeypatch.setattr(runner_mod, "SESSIONS_FILE", tmp_path / "sessions.json")
        runner = runner_mod.Runner(
            cfg, runner_mod.RunOptions(use_ai=False, use_browser=False)
        )
        return runner, cfg.accounts[0]

    def test_run_account_labels_everything_inside(self, tmp_path, monkeypatch):
        runner, account = self._runner(tmp_path, monkeypatch, "站点B")

        def fake_checkin(acct):
            log.info("正在签到")
            log.warn("顺便警告一句")
            return log.SummaryRow(acct.name, "success", "S1", "ok")

        monkeypatch.setattr(runner, "_checkin_account", fake_checkin)
        lines, unsubscribe = _capture()
        try:
            row = runner._run_account(account)
        finally:
            unsubscribe()
        assert row.status == "success"
        assert [_label(line) for line in lines] == ["站点B", "站点B"]
        assert log.get_context() == ""

    def test_missing_cookie_warning_is_labelled(self, tmp_path, monkeypatch):
        runner, account = self._runner(tmp_path, monkeypatch, "无 cookie 账号", cookie="")
        lines, unsubscribe = _capture()
        try:
            row = runner._run_account(account)
        finally:
            unsubscribe()
        assert row.status == "skipped"
        assert any(_label(line) == "无 cookie 账号" and "缺少 cookie" in line
                   for line in lines)

    def test_label_survives_account_exception(self, tmp_path, monkeypatch):
        runner, account = self._runner(tmp_path, monkeypatch, "炸了的账号")

        def boom(_acct):
            raise RuntimeError("boom")

        monkeypatch.setattr(runner, "_checkin_account", boom)
        try:
            runner._run_account(account)
        except RuntimeError:
            pass
        assert log.get_context() == ""

    def test_parallel_run_labels_each_account(self, tmp_path, monkeypatch):
        cfg = cfgmod.build_config({
            "defaults": {"interval_seconds": [0, 0]},
            "accounts": [
                {"name": f"号{i}", "url": f"https://a{i}.example.com", "cookie": "c"}
                for i in range(4)
            ],
        })
        monkeypatch.setattr(runner_mod, "SESSIONS_FILE", tmp_path / "sessions.json")
        runner = runner_mod.Runner(
            cfg, runner_mod.RunOptions(use_ai=False, use_browser=False, parallelism=4,
                                       parallelism_explicit=True)
        )

        def fake_checkin(account):
            log.info("干活中")
            return log.SummaryRow(account.name, "success", "S1", "ok")

        monkeypatch.setattr(runner, "_checkin_account", fake_checkin)
        lines, unsubscribe = _capture()
        try:
            assert runner.run() == 0
        finally:
            unsubscribe()
        worklines = [line for line in lines if "干活中" in line]
        assert sorted(_label(line) for line in worklines) == ["号0", "号1", "号2", "号3"]
        # 每个账号的「已提交 / 结束」进度行也带标签
        for name in ("号0", "号1", "号2", "号3"):
            assert any(_label(line) == name and "已提交" in line for line in lines)
            assert any(_label(line) == name and "结束" in line for line in lines)

