"""Runner 账号级并发测试。"""

import threading
import time

from newapi_checkin import config as cfgmod
from newapi_checkin import logger as log
from newapi_checkin import runner as runner_mod


def test_runner_parallelizes_accounts_and_keeps_result_order(tmp_path, monkeypatch):
    cfg = cfgmod.build_config(
        {
            "defaults": {"interval_seconds": [0, 0]},
            "accounts": [
                {"name": "A", "url": "https://a.example.com", "cookie": "c"},
                {"name": "B", "url": "https://b.example.com", "cookie": "c"},
                {"name": "C", "url": "https://c.example.com", "cookie": "c"},
                {"name": "D", "url": "https://d.example.com", "cookie": "c"},
            ],
        }
    )
    monkeypatch.setattr(runner_mod, "SESSIONS_FILE", tmp_path / "sessions.json")
    runner = runner_mod.Runner(cfg, runner_mod.RunOptions(parallelism=2, use_ai=False, use_browser=False))

    state = {"active": 0, "maximum": 0}
    lock = threading.Lock()

    def fake_run(account):
        with lock:
            state["active"] += 1
            state["maximum"] = max(state["maximum"], state["active"])
        time.sleep(0.05)
        with lock:
            state["active"] -= 1
        return log.SummaryRow(account.name, "success", "fake", "ok")

    monkeypatch.setattr(runner, "_run_account", fake_run)

    assert runner.run() == 0
    assert state["maximum"] == 4
    assert [row.name for row in runner.summary.rows] == ["A", "B", "C", "D"]


def test_runner_parallelism_is_fixed_for_automated_runs(tmp_path, monkeypatch):
    cfg = cfgmod.build_config(
        {"accounts": [{"name": "A", "url": "https://a.example.com", "cookie": "c"}]}
    )
    monkeypatch.setattr(runner_mod, "SESSIONS_FILE", tmp_path / "sessions.json")
    runner = runner_mod.Runner(cfg, runner_mod.RunOptions(parallelism=99, use_ai=False, use_browser=False))

    assert runner._parallelism() == 6
    assert runner_mod.DEFAULT_ACCOUNT_PARALLELISM == 6


def test_source_ip_backoff_only_blocks_that_account(tmp_path, monkeypatch):
    """源站/WAF 账号等待换 IP 时，其他账号仍能正常完成。"""
    cfg = cfgmod.build_config(
        {
            "proxy_pool": {"enabled": True},
            "defaults": {"interval_seconds": [0, 0]},
            "accounts": [
                {"name": "A", "url": "https://a.example.com", "cookie": "c"},
                {"name": "B", "url": "https://b.example.com", "cookie": "c"},
            ],
        }
    )
    monkeypatch.setattr(runner_mod, "SESSIONS_FILE", tmp_path / "sessions.json")
    runner = runner_mod.Runner(
        cfg,
        runner_mod.RunOptions(
            use_ai=False,
            use_browser=False,
            parallelism=2,
            parallelism_explicit=True,
        ),
    )

    class Pool:
        def __init__(self):
            self._lock = threading.Lock()
            self._next = 0

        def acquire(self):
            with self._lock:
                self._next += 1
                return f"p{self._next}:80"

        def mark_bad(self, _proxy, reason="net"):
            return None

        def mark_ok(self, _proxy):
            return None

    runner._pool = Pool()
    real_sleep = time.sleep
    sleeps = []
    finished = []
    lock = threading.Lock()
    attempts = {"A": 0}

    def fake_sleep(seconds):
        sleeps.append(seconds)
        real_sleep(0.05)

    def fake_attempt(account, _record):
        with lock:
            attempts[account.name] = attempts.get(account.name, 0) + 1
        if account.name == "A":
            return log.SummaryRow(account.name, "failed", "S1", "源站失败")
        return log.SummaryRow(account.name, "success", "S1", "成功")

    original_run = runner._run_account

    def wrapped_run(account):
        row = original_run(account)
        with lock:
            finished.append(account.name)
        return row

    monkeypatch.setattr(runner_mod.time, "sleep", fake_sleep)
    monkeypatch.setattr(runner, "_attempt", fake_attempt)
    monkeypatch.setattr(runner, "_run_account", wrapped_run)

    assert runner._run_parallel(cfg.accounts, workers=2) == 0
    assert finished.index("B") < finished.index("A")
    assert attempts["A"] == runner_mod.SOURCE_IP_SWAP_LIMIT + 1
    assert sleeps == [runner_mod.SOURCE_IP_SWAP_BACKOFF_SECONDS] * runner_mod.SOURCE_IP_SWAP_LIMIT


def test_parallel_keyboard_interrupt_returns_130_and_does_not_wait(tmp_path, monkeypatch):
    """并行模式下 Ctrl+C：立即返回 130，不 shutdown(wait=True) 卡住。

    以前的实现用 with ThreadPoolExecutor 包住循环，中断后 cancel() 只能取消
    未开始的任务，正在运行的任务要等它跑完才能退出——如果卡在网络重试或
    退避 sleep 里，Ctrl+C 就退不掉了。
    """
    cfg = cfgmod.build_config(
        {
            "defaults": {"interval_seconds": [0, 0]},
            "accounts": [
                {"name": "A", "url": "https://a.example.com", "cookie": "c"},
                {"name": "B", "url": "https://b.example.com", "cookie": "c"},
                {"name": "C", "url": "https://c.example.com", "cookie": "c"},
            ],
        }
    )
    monkeypatch.setattr(runner_mod, "SESSIONS_FILE", tmp_path / "sessions.json")
    runner = runner_mod.Runner(
        cfg,
        runner_mod.RunOptions(parallelism=3, use_ai=False, use_browser=False),
    )

    # B 模拟正在网络重试里卡死的账号（跑得慢）
    def fake_run(account):
        if account.name == "B":
            time.sleep(5)
            return log.SummaryRow(account.name, "success", "fake", "ok")
        raise KeyboardInterrupt()

    monkeypatch.setattr(runner, "_run_account", fake_run)

    started = time.monotonic()
    assert runner._run_parallel(cfg.accounts, workers=3) == 130
    elapsed = time.monotonic() - started
    # 不能等卡住的 B 跑完（否则至少 5 秒）；中断后应立即返回
    assert elapsed < 2.0


def test_parallel_keyboard_interrupt_keeps_finished_rows(tmp_path, monkeypatch):
    """中断前已经结束的账号结果不能丢，汇总表仍要渲染。"""
    cfg = cfgmod.build_config(
        {
            "defaults": {"interval_seconds": [0, 0]},
            "accounts": [
                {"name": "A", "url": "https://a.example.com", "cookie": "c"},
                {"name": "B", "url": "https://b.example.com", "cookie": "c"},
            ],
        }
    )
    monkeypatch.setattr(runner_mod, "SESSIONS_FILE", tmp_path / "sessions.json")
    runner = runner_mod.Runner(
        cfg,
        runner_mod.RunOptions(parallelism=2, use_ai=False, use_browser=False),
    )
    done_a = threading.Event()

    def fake_run(account):
        if account.name == "A":
            time.sleep(0.05)
            done_a.set()
            return log.SummaryRow(account.name, "success", "fake", "ok")
        done_a.wait(2)
        raise KeyboardInterrupt()

    monkeypatch.setattr(runner, "_run_account", fake_run)
    assert runner._run_parallel(cfg.accounts, workers=2) == 130
    assert [row.name for row in runner.summary.rows] == ["A"]


def test_runner_closes_ai_client_after_run(tmp_path, monkeypatch):
    """run() 结束后 AI 客户端必须被关闭，避免 daemon 长跑泄漏 curl session。"""
    cfg = cfgmod.build_config(
        {
            "defaults": {"interval_seconds": [0, 0]},
            "accounts": [{"name": "A", "url": "https://a.example.com", "cookie": "c"}],
        }
    )
    monkeypatch.setattr(runner_mod, "SESSIONS_FILE", tmp_path / "sessions.json")
    runner = runner_mod.Runner(
        cfg,
        runner_mod.RunOptions(use_ai=True, use_browser=False),
    )
    closed: list = []

    class FakeAI:
        def close(self):
            closed.append(True)

    # 模拟懒加载已经创建过 AI 客户端（S3 路径用到了）
    runner._ai = FakeAI()
    runner._ai_ready = True
    monkeypatch.setattr(
        runner, "_run_account",
        lambda account: log.SummaryRow(account.name, "success", "fake", "ok"),
    )

    assert runner.run() == 0
    assert closed == [True]


def test_ip_cache_is_bounded(tmp_path, monkeypatch):
    """_ip_cache 不能无界增长：超限后丢弃最旧条目，只保留最近 IP_CACHE_MAX 条。"""
    cfg = cfgmod.build_config(
        {"accounts": [{"name": "A", "url": "https://a.example.com", "cookie": "c"}]}
    )
    monkeypatch.setattr(runner_mod, "SESSIONS_FILE", tmp_path / "sessions.json")
    monkeypatch.setattr(runner_mod, "IP_CACHE_MAX", 10)
    runner = runner_mod.Runner(
        cfg,
        runner_mod.RunOptions(use_ai=False, use_browser=False),
    )
    monkeypatch.setattr(
        runner_mod, "probe_exit_ip",
        lambda proxy: "203.0.113." + str(abs(hash(proxy or "")) % 250 + 1),
    )

    for i in range(50):
        runner.exit_ip(f"proxy-{i}:80")

    assert len(runner._ip_cache) <= 10
    # 最新一条一定还在缓存里
    assert "proxy-49:80" in runner._ip_cache

