"""明文/密文 JSON 配置文档读写测试。"""

import json

import pytest

from newapi_checkin.config import ConfigError
from newapi_checkin.config_store import (
    change_key,
    export_plain,
    import_json,
    load_document,
    save_document,
)


@pytest.fixture
def raw_config():
    return {
        "ai": {
            "enabled": False,
            "base_url": "",
            "api_key": "sk-secret",
            "model": "gpt-4o-mini",
        },
        "accounts": [
            {
                "name": "站点A",
                "url": "https://a.example.com",
                "cookie": "session=secret",
                "proxy": "http://127.0.0.1:1080",
                "enabled": True,
            }
        ],
    }


def test_plain_document_is_backward_compatible(tmp_path, raw_config):
    path = tmp_path / "config.json"

    document = save_document(raw_config, path, encryption_enabled=False)

    assert document.encrypted is False
    loaded = load_document(path)
    assert loaded.raw["accounts"][0]["cookie"] == "session=secret"
    assert loaded.safe_meta()["accounts"][0]["has_cookie"] is True


def test_encrypted_document_keeps_bootstrap_secret_free(tmp_path, raw_config):
    path = tmp_path / "config.json"
    encrypted_path = tmp_path / "data" / "config.encrypted.json"
    raw_config["security"] = {
        "encryption_enabled": True,
        "config_key": "file-key-123",
        "encrypted_file": str(encrypted_path),
    }

    document = save_document(raw_config, path, encryption_enabled=True, key="file-key-123")

    bootstrap = json.loads(path.read_text(encoding="utf-8"))
    assert bootstrap["security"]["encryption_enabled"] is True
    assert "accounts" not in bootstrap
    assert "cookie" not in path.read_text(encoding="utf-8")
    assert encrypted_path.is_file()
    assert document.raw["accounts"][0]["cookie"] == "session=secret"
    assert load_document(path).raw["ai"]["api_key"] == "sk-secret"


def test_environment_key_can_override_security_config(tmp_path, raw_config, monkeypatch):
    path = tmp_path / "config.json"
    raw_config["security"] = {
        "encryption_enabled": True,
        "config_key": "file-key-123",
        "encrypted_file": "data/config.encrypted.json",
    }
    monkeypatch.setenv("CHECKIN_CONFIG_KEY", "env-key-123")

    save_document(raw_config, path, encryption_enabled=True)
    assert load_document(path).raw["accounts"][0]["name"] == "站点A"

    monkeypatch.setenv("CHECKIN_CONFIG_KEY", "wrong-key-123")
    with pytest.raises(ConfigError, match="密钥错误"):
        load_document(path)


def test_export_plain_and_import_json(tmp_path, raw_config):
    source = tmp_path / "config.json"
    plain_export = tmp_path / "exported.json"
    target = tmp_path / "imported.json"

    save_document(raw_config, source, encryption_enabled=True, key="file-key-123")
    export_plain(source, plain_export)

    exported = json.loads(plain_export.read_text(encoding="utf-8"))
    assert exported["security"]["encryption_enabled"] is False
    assert exported["accounts"][0]["cookie"] == "session=secret"

    imported = import_json(plain_export, target, encrypt=True, key="new-key-123")
    assert imported.encrypted is True
    assert load_document(target).raw["accounts"][0]["proxy"] == "http://127.0.0.1:1080"


def test_change_key_reencrypts_existing_document(tmp_path, raw_config):
    path = tmp_path / "config.json"

    save_document(raw_config, path, encryption_enabled=True, key="old-key-123")
    changed = change_key(path, "new-key-123")

    assert changed.encrypted is True
    with pytest.raises(ConfigError, match="密钥错误"):
        # 直接改环境变量可验证旧密钥已经失效。
        import os

        old = os.environ.get("CHECKIN_CONFIG_KEY")
        os.environ["CHECKIN_CONFIG_KEY"] = "old-key-123"
        try:
            load_document(path)
        finally:
            if old is None:
                os.environ.pop("CHECKIN_CONFIG_KEY", None)
            else:
                os.environ["CHECKIN_CONFIG_KEY"] = old

    assert load_document(path).raw["accounts"][0]["name"] == "站点A"
