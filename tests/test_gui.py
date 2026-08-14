"""GUI 关键交互测试。"""

from pathlib import Path
from types import SimpleNamespace

import pytest


def test_close_to_tray_is_silent(monkeypatch):
    pytest.importorskip("PySide6")
    from PySide6.QtGui import QCloseEvent
    from PySide6.QtWidgets import QApplication

    from newapi_checkin import gui as gui_module

    if gui_module._QT_ERROR is not None:
        pytest.skip(f"PySide6 不可用: {gui_module._QT_ERROR}")

    app = QApplication.instance() or QApplication(["test-gui"])
    document = SimpleNamespace(
        raw={"accounts": [], "ai": {}},
        encrypted=False,
        encrypted_path=Path("config.encrypted.json"),
        safe_meta=lambda: {"key_configured": False},
    )
    config = SimpleNamespace(accounts=[])
    monkeypatch.setattr(gui_module, "load_document", lambda: document)
    monkeypatch.setattr(gui_module, "load_config", lambda: config)
    monkeypatch.setattr(gui_module.daemon_control, "is_enabled", lambda: True)
    monkeypatch.setattr(gui_module.autostart, "supported", lambda: False)
    monkeypatch.setattr(gui_module.autostart, "is_enabled", lambda: False)

    window = gui_module.MainWindow(smoke_test=True)
    assert window.minimumWidth() <= 760
    assert window.minimumHeight() <= 540
    assert window.parallel_spin.value() == 3
    assert window.sync_auto_check.isChecked() is True
    assert window.sync_token_edit.echoMode().name == "Password"
    notifications = []
    monkeypatch.setattr(window.tray, "showMessage", lambda *args: notifications.append(args))

    event = QCloseEvent()
    window.closeEvent(event)

    assert event.isAccepted() is False
    assert window.isHidden() is True
    assert notifications == []

    window._closing = True
    window.tray.hide()
    window.deleteLater()
    app.processEvents()


def _make_window(monkeypatch):
    pytest.importorskip("PySide6")
    from PySide6.QtWidgets import QApplication

    from newapi_checkin import gui as gui_module

    if gui_module._QT_ERROR is not None:
        pytest.skip(f"PySide6 不可用: {gui_module._QT_ERROR}")

    app = QApplication.instance() or QApplication(["test-gui"])
    document = SimpleNamespace(
        raw={"accounts": [], "ai": {}},
        encrypted=False,
        encrypted_path=Path("config.encrypted.json"),
        safe_meta=lambda: {"key_configured": False},
    )
    config = SimpleNamespace(accounts=[])
    monkeypatch.setattr(gui_module, "load_document", lambda: document)
    monkeypatch.setattr(gui_module, "load_config", lambda: config)
    monkeypatch.setattr(gui_module.daemon_control, "is_enabled", lambda: True)
    monkeypatch.setattr(gui_module.autostart, "supported", lambda: False)
    monkeypatch.setattr(gui_module.autostart, "is_enabled", lambda: False)
    window = gui_module.MainWindow(smoke_test=True)
    return app, window


def test_schedule_refresh_does_not_overwrite_user_edit(monkeypatch):
    app, window = _make_window(monkeypatch)
    window._apply_schedule({"enabled": True, "times": ["09:00"], "account_names": [], "parallelism": 4})
    assert window.parallel_spin.value() == 4
    window.times_edit.setText("10:30, 18:00")
    window._mark_schedule_dirty()
    window._apply_schedule({"enabled": True, "times": ["09:00"], "account_names": []})

    assert window.times_edit.text() == "10:30, 18:00"
    assert window._schedule_dirty is True

    window._closing = True
    window.tray.hide()
    window.deleteLater()
    app.processEvents()


def test_schedule_save_uses_all_enabled_accounts(monkeypatch):
    app, window = _make_window(monkeypatch)
    captured = {}
    monkeypatch.setattr(window, "_request", lambda command, **payload: captured.update(command=command, payload=payload))
    window.times_edit.setText("10:30, 18:00")
    window._save_schedule()

    assert captured["command"] == "set_schedule"
    assert captured["payload"]["schedule"]["account_names"] == []
    assert captured["payload"]["schedule"]["times"] == ["10:30", "18:00"]
    assert captured["payload"]["schedule"]["parallelism"] == 3

    window._closing = True
    window.tray.hide()
    window.deleteLater()
    app.processEvents()
