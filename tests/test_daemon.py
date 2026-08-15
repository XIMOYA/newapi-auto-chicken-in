from threading import Thread
from time import monotonic, sleep
from types import SimpleNamespace

from newapi_checkin import daemon as daemon_module
from newapi_checkin.config import build_config
from newapi_checkin.daemon import DaemonClient, DaemonServer


def wait_for_client(path, timeout=3.0):
    deadline = monotonic() + timeout
    client = DaemonClient(path=path)
    while monotonic() < deadline:
        if client.ping():
            return client
        sleep(0.03)
    assert client.ping()
    return client


def test_daemon_ipc_status_and_schedule(tmp_path):
    server = DaemonServer(tmp_path)
    thread = Thread(target=server.run, daemon=True)
    thread.start()
    client = wait_for_client(tmp_path / "daemon.json")
    try:
        ping = client.request("ping")
        assert ping["ok"] is True
        schedule = client.request("get_schedule")
        assert schedule["ok"] is True
        assert schedule["schedule"]["times"] == ["09:00"]

        updated = client.request(
            "set_schedule",
            schedule={"enabled": True, "times": ["08:30", "08:30"], "account_names": ["A"]},
        )
        assert updated["ok"] is True
        assert updated["schedule"]["times"] == ["08:30"]
        assert updated["status"]["account_names"] == ["A"]

        status = client.request("status")
        assert status["ok"] is True
        assert status["status"]["pid"] > 0
    finally:
        server.stop()
        thread.join(timeout=3)
    assert not (tmp_path / "daemon.json").exists()


def test_daemon_ipc_stop_unblocks_server(tmp_path):
    server = DaemonServer(tmp_path)
    thread = Thread(target=server.run, daemon=True)
    thread.start()
    client = wait_for_client(tmp_path / "daemon.json")
    response = client.request("stop")
    assert response["ok"] is True
    thread.join(timeout=3)
    assert not thread.is_alive()
    assert not (tmp_path / "daemon.json").exists()


def test_daemon_single_instance_lock_blocks_duplicate(tmp_path):
    first = DaemonServer(tmp_path)
    thread = Thread(target=first.run, daemon=True)
    thread.start()
    client = wait_for_client(tmp_path / "daemon.json")
    try:
        second = DaemonServer(tmp_path)
        assert second.run() == 0
        assert client.ping() is True
    finally:
        first.stop()
        thread.join(timeout=3)


def test_daemon_rejects_unknown_command(tmp_path):
    server = DaemonServer(tmp_path)
    thread = Thread(target=server.run, daemon=True)
    thread.start()
    client = wait_for_client(tmp_path / "daemon.json")
    try:
        response = client.request("not-a-command")
        assert response["ok"] is False
        assert "未知 IPC 命令" in response["error"]
    finally:
        server.stop()
        thread.join(timeout=3)


def test_daemon_save_config_ipc(tmp_path, monkeypatch):
    config_path = tmp_path / "config.json"
    monkeypatch.setenv("CHECKIN_CONFIG", str(config_path))
    server = DaemonServer(tmp_path / "data")

    response = server._dispatch(
        {
            "command": "save_config",
            "config": {
                "accounts": [{"name": "A", "url": "https://a.example.com", "cookie": "c"}],
            },
            "encryption_enabled": False,
        }
    )

    assert response["ok"] is True
    assert response["meta"]["accounts"][0]["name"] == "A"
    assert config_path.is_file()


def test_daemon_sync_config_ipc(monkeypatch):
    expected = {"ok": True, "operation": "sync_config", "message": "done"}
    monkeypatch.setattr(daemon_module, "sync_remote_config", lambda **kwargs: expected)

    assert DaemonServer()._dispatch({"command": "sync_config"}) == expected


def test_daemon_forces_browser_mode_for_manual_only(monkeypatch):
    cfg = build_config({"accounts": [{"name": "A", "url": "https://a.example.com", "cookie": "c"}]})
    captured = []

    class FakeRunner:
        def __init__(self, config, options):
            captured.append((config, options))
            self.summary = SimpleNamespace(rows=[])

        def run(self):
            return 0

    monkeypatch.setattr(daemon_module, "load_config", lambda: cfg)
    monkeypatch.setattr(daemon_module, "sync_remote_config", lambda **kwargs: {"ok": True, "skipped": True})
    monkeypatch.setattr(daemon_module, "Runner", FakeRunner)
    server = DaemonServer()

    assert server._run_checkin(None, manual=False)["ok"] is True
    # 默认 "virtual"(Xvfb) 本来就是无头，不能被无条件改写成真 headless
    assert cfg.browser.headless == "virtual"
    assert captured[-1][1].headful is False
    assert captured[-1][1].manual is False

    assert server._run_checkin(None, manual=True)["ok"] is True
    assert cfg.browser.headless is False
    assert captured[-1][1].headful is True
    assert captured[-1][1].manual is True


def test_daemon_keeps_virtual_headless_for_scheduled(monkeypatch):
    """定时/立即签到保留 virtual headless，不能强制成真 headless。"""
    cfg = build_config({
        "browser": {"headless": "virtual"},
        "accounts": [{"name": "A", "url": "https://a.example.com", "cookie": "c"}],
    })

    class FakeRunner:
        def __init__(self, config, options):
            self.summary = SimpleNamespace(rows=[])

        def run(self):
            return 0

    monkeypatch.setattr(daemon_module, "load_config", lambda: cfg)
    monkeypatch.setattr(daemon_module, "sync_remote_config", lambda **kwargs: {"ok": True, "skipped": True})
    monkeypatch.setattr(daemon_module, "Runner", FakeRunner)
    server = DaemonServer()

    assert server._run_checkin(None, manual=False)["ok"] is True
    assert cfg.browser.headless == "virtual"


def test_daemon_forces_explicit_headful_to_headless_for_scheduled(monkeypatch):
    """只有显式配了真 headful(false) 时，定时签到才改写成无头。"""
    cfg = build_config({
        "browser": {"headless": False},
        "accounts": [{"name": "A", "url": "https://a.example.com", "cookie": "c"}],
    })

    class FakeRunner:
        def __init__(self, config, options):
            self.summary = SimpleNamespace(rows=[])

        def run(self):
            return 0

    monkeypatch.setattr(daemon_module, "load_config", lambda: cfg)
    monkeypatch.setattr(daemon_module, "sync_remote_config", lambda **kwargs: {"ok": True, "skipped": True})
    monkeypatch.setattr(daemon_module, "Runner", FakeRunner)
    server = DaemonServer()

    assert server._run_checkin(None, manual=False)["ok"] is True
    assert cfg.browser.headless is True
    assert server._run_checkin(None, manual=True)["ok"] is True
    assert cfg.browser.headless is False


def test_daemon_stop_flushes_logs(tmp_path, monkeypatch):
    flushed = []
    monkeypatch.setattr(daemon_module.log, "flush", lambda: flushed.append(True))
    server = DaemonServer(tmp_path)
    server.stop()
    assert flushed


def test_daemon_stop_grace_is_bounded_and_passed_to_scheduler(tmp_path):
    """stop 最多优雅等待 10 秒：scheduler.stop 的 timeout 由该上限推导。"""
    server = DaemonServer(tmp_path)
    captured = {}
    server._scheduler = SimpleNamespace(
        stop=lambda timeout: captured.setdefault("timeout", timeout)
    )
    server.stop()
    assert daemon_module.STOP_GRACE_SECONDS <= 10.0
    assert captured.get("timeout") == daemon_module.STOP_GRACE_SECONDS


def test_daemon_concurrent_stop_is_safe(tmp_path):
    """多个线程同时 stop 不能死锁/重复清理，第二次及以后应直接返回。"""
    server = DaemonServer(tmp_path)
    server._scheduler = SimpleNamespace(stop=lambda timeout: None)
    errors = []

    def do_stop():
        try:
            server.stop()
        except Exception as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [Thread(target=do_stop) for _ in range(3)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=3)
    assert errors == []
    assert server._stopped_event.is_set()
    # 重复 stop 是幂等 no-op
    server.stop()
    assert server._stopped_event.is_set()


def test_logger_rolls_over_across_midnight(tmp_path, monkeypatch):
    """daemon 跨日运行时自动切换到新日期的日志文件。"""
    from datetime import datetime

    from newapi_checkin import logger as log_mod

    class FirstDay(datetime):
        @classmethod
        def now(cls, tz=None):
            return cls(2024, 1, 1, 23, 59, 59)

    class NextDay(datetime):
        @classmethod
        def now(cls, tz=None):
            return cls(2024, 1, 2, 0, 0, 5)

    monkeypatch.setattr(log_mod, "datetime", FirstDay)
    try:
        log_mod.setup(log_dir=tmp_path)
        log_mod.info("before midnight")
        monkeypatch.setattr(log_mod, "datetime", NextDay)
        log_mod.info("after midnight")
    finally:
        log_mod.shutdown()

    first = tmp_path / "2024-01-01.log"
    second = tmp_path / "2024-01-02.log"
    assert first.exists()
    assert second.exists()
    assert "before midnight" in first.read_text(encoding="utf-8")
    assert "after midnight" not in first.read_text(encoding="utf-8")
    assert "after midnight" in second.read_text(encoding="utf-8")


def test_daemon_uses_scheduler_parallelism_and_serializes_manual(monkeypatch):
    cfg = build_config({"accounts": [{"name": "A", "url": "https://a.example.com", "cookie": "c"}]})
    captured = []

    class FakeRunner:
        def __init__(self, config, options):
            captured.append(options.parallelism)
            self.summary = SimpleNamespace(rows=[])

        def run(self):
            return 0

    monkeypatch.setattr(daemon_module, "load_config", lambda: cfg)
    monkeypatch.setattr(daemon_module, "sync_remote_config", lambda **kwargs: {"ok": True, "skipped": True})
    monkeypatch.setattr(daemon_module, "Runner", FakeRunner)
    server = DaemonServer()
    server._scheduler = SimpleNamespace(config=SimpleNamespace(parallelism=3))

    assert server._run_checkin(None, manual=False)["ok"] is True
    assert server._run_checkin(None, manual=True)["ok"] is True
    assert captured == [3, 1]


def test_daemon_runs_auto_sync_before_runner(monkeypatch):
    cfg = build_config({"accounts": [{"name": "A", "url": "https://a.example.com", "cookie": "c"}]})
    order = []

    class FakeRunner:
        def __init__(self, config, options):
            order.append("runner")
            self.summary = SimpleNamespace(rows=[])

        def run(self):
            return 0

    def fake_sync(**kwargs):
        order.append(kwargs)
        return {"ok": True, "operation": "sync_config", "skipped": False, "message": "updated"}

    monkeypatch.setattr(daemon_module, "load_config", lambda: cfg)
    monkeypatch.setattr(daemon_module, "sync_remote_config", fake_sync)
    monkeypatch.setattr(daemon_module, "Runner", FakeRunner)

    result = DaemonServer()._run_checkin(None, manual=False)

    assert result["ok"] is True
    assert order == [{"auto_only": True}, "runner"]


def test_autostart_command_respects_disabled_state(monkeypatch):
    called = []
    monkeypatch.setattr(daemon_module.daemon_control, "is_enabled", lambda: False)
    monkeypatch.setattr(DaemonServer, "run", lambda self: called.append(True) or 1)

    assert daemon_module.main(["--daemon", "--autostart"]) == 0
    assert called == []
