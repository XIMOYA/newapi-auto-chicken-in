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

    assert runner._parallelism() == 8
