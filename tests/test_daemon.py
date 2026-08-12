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
    assert cfg.browser.headless is True
    assert captured[-1][1].headful is False
    assert captured[-1][1].manual is False

    assert server._run_checkin(None, manual=True)["ok"] is True
    assert cfg.browser.headless is False
    assert captured[-1][1].headful is True
    assert captured[-1][1].manual is True


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
