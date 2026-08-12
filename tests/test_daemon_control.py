"""daemon 运行开关持久化测试。"""

import json

from newapi_checkin import daemon_control


def test_missing_control_file_defaults_to_enabled(tmp_path):
    assert daemon_control.is_enabled(tmp_path / "daemon_control.json") is True


def test_set_enabled_is_atomic_and_round_trips(tmp_path):
    path = tmp_path / "data" / "daemon_control.json"

    assert daemon_control.set_enabled(False, path) is True
    assert daemon_control.is_enabled(path) is False
    assert json.loads(path.read_text(encoding="utf-8")) == {"enabled": False}
    assert not path.with_name(path.name + ".tmp").exists()

    assert daemon_control.set_enabled(True, path) is True
    assert daemon_control.is_enabled(path) is True


def test_invalid_control_file_falls_back_to_enabled(tmp_path):
    path = tmp_path / "daemon_control.json"
    path.write_text("not-json", encoding="utf-8")
    assert daemon_control.is_enabled(path) is True

    path.write_text(json.dumps({"enabled": "false"}), encoding="utf-8")
    assert daemon_control.is_enabled(path) is False

    path.write_text(json.dumps({"enabled": "unexpected"}), encoding="utf-8")
    assert daemon_control.is_enabled(path) is True
