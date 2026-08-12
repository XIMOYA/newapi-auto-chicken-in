from datetime import datetime
from threading import Event
from time import monotonic, sleep

import pytest

from newapi_checkin.scheduler import ScheduleConfig, ScheduleError, SchedulerService


def wait_until(predicate, timeout=2.0):
    deadline = monotonic() + timeout
    while monotonic() < deadline:
        if predicate():
            return True
        sleep(0.02)
    return predicate()


def test_schedule_normalizes_and_deduplicates_times():
    schedule = ScheduleConfig.from_dict({"times": ["9:00", "09:00", "18:30"], "account_names": ["A", "A"]})
    assert schedule.times == ("09:00", "18:30")
    assert schedule.account_names == ("A",)


def test_schedule_rejects_invalid_time():
    with pytest.raises(ScheduleError, match="超出范围"):
        ScheduleConfig.from_dict({"times": ["25:00"]})


def test_schedule_normalizes_parallelism_and_supports_alias():
    assert ScheduleConfig.from_dict({"times": ["09:00"], "parallelism": 99}).parallelism == 8
    assert ScheduleConfig.from_dict({"times": ["09:00"], "max_workers": 3}).parallelism == 3
    assert ScheduleConfig.from_dict({"times": ["09:00"], "parallelism": 0}).parallelism == 1


def test_next_run_rolls_to_next_day_after_slot():
    schedule = ScheduleConfig.from_dict({"times": ["09:00", "18:30"]})
    assert schedule.next_run(datetime(2026, 8, 6, 8, 0)).isoformat() == "2026-08-06T09:00:00"
    assert schedule.next_run(datetime(2026, 8, 6, 19, 0)).isoformat() == "2026-08-07T09:00:00"


def test_scheduler_persists_config_atomically(tmp_path):
    calls = []
    service = SchedulerService(tmp_path / "scheduler.json", lambda names, manual: calls.append((names, manual)) or {"ok": True})
    service.set_config({"enabled": False, "times": ["12:00"], "account_names": ["A"]})
    assert service.config_path.read_text(encoding="utf-8").endswith("\n")
    assert service.config.to_dict() == {
        "enabled": False,
        "times": ["12:00"],
        "account_names": ["A"],
        "run_on_start": False,
        "headless": True,
        "parallelism": 2,
    }
    assert not service.config_path.with_name("scheduler.json.tmp").exists()


def test_scheduler_migrates_legacy_account_selection(tmp_path):
    path = tmp_path / "scheduler.json"
    path.write_text(
        '{"enabled": true, "times": ["08:30"], "account_names": ["旧账号"]}\n',
        encoding="utf-8",
    )

    service = SchedulerService(path, lambda names, manual: {"ok": True})

    assert service.config.account_names == ()
    assert service.config.parallelism == 2
    assert '"account_names": []' in path.read_text(encoding="utf-8")
    assert '"parallelism": 2' in path.read_text(encoding="utf-8")


def test_scheduler_prevents_concurrent_runs(tmp_path):
    entered = Event()
    release = Event()

    def callback(names, manual):
        entered.set()
        release.wait(2)
        return {"ok": True, "names": names, "manual": manual}

    service = SchedulerService(tmp_path / "scheduler.json", callback)
    first = service.run_now(["A"], manual=False)
    assert first["ok"] is True
    assert entered.wait(1)
    second = service.run_now(["B"], manual=True)
    assert second == {"ok": False, "running": True, "message": "已有签到任务正在运行"}
    release.set()
    assert wait_until(lambda: service.snapshot()["running"] is False)
    assert service.snapshot()["last_result"]["names"] == ["A"]


def test_scheduler_deduplicates_retried_run_request(tmp_path):
    entered = Event()
    release = Event()
    calls = []

    def callback(names, manual):
        calls.append((names, manual))
        entered.set()
        release.wait(2)
        return {"ok": True, "names": names, "manual": manual}

    service = SchedulerService(tmp_path / "scheduler.json", callback)
    first = service.run_now(["A"], manual=False, request_id="req-1")
    assert first["ok"] is True
    assert entered.wait(1)

    duplicate = service.run_now(["A"], manual=False, request_id="req-1")
    assert duplicate == first
    assert calls == [(["A"], False)]

    release.set()
    assert wait_until(lambda: service.snapshot()["running"] is False)

