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
    assert state["maximum"] == 2
    assert [row.name for row in runner.summary.rows] == ["A", "B", "C", "D"]


def test_runner_parallelism_is_clamped_to_safe_range(tmp_path, monkeypatch):
    cfg = cfgmod.build_config(
        {"accounts": [{"name": "A", "url": "https://a.example.com", "cookie": "c"}]}
    )
    monkeypatch.setattr(runner_mod, "SESSIONS_FILE", tmp_path / "sessions.json")
    runner = runner_mod.Runner(cfg, runner_mod.RunOptions(parallelism=99, use_ai=False, use_browser=False))

    assert runner._parallelism() == runner_mod.MAX_ACCOUNT_PARALLELISM
    assert runner_mod.MAX_ACCOUNT_PARALLELISM == 16


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

        def mark_bad(self, _proxy):
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

