from newapi_checkin import autostart


def test_startup_command_contains_daemon_marker():
    command = autostart.startup_command()
    assert "--daemon" in command
    assert "--autostart" in command


def test_non_windows_api_is_safe(monkeypatch):
    monkeypatch.setattr(autostart, "winreg", None)
    assert autostart.supported() is False
    assert autostart.get_command() is None
    assert autostart.enable() is False
    assert autostart.disable() is False
