"""AES-GCM 配置加解密单元测试。"""

import json

import pytest

from newapi_checkin.secure_config import (
    ConfigEncryptionError,
    config_key_from_environment,
    decrypt_json,
    encrypt_json,
)


def test_encrypt_json_round_trip_and_metadata():
    payload = {"accounts": [{"name": "站点A", "cookie": "session=secret"}]}

    encrypted = encrypt_json(payload, "correct-key-123")

    assert encrypted["algorithm"] == "AES-256-GCM"
    assert encrypted["version"] == 1
    assert encrypted["ciphertext"]
    assert "secret" not in json.dumps(encrypted, ensure_ascii=False)
    assert decrypt_json(encrypted, "correct-key-123") == payload


def test_decrypt_json_rejects_wrong_key():
    encrypted = encrypt_json({"value": 1}, "correct-key-123")

    with pytest.raises(ConfigEncryptionError, match="密钥错误"):
        decrypt_json(encrypted, "wrong-key-123")


def test_config_key_environment_overrides_file_key(monkeypatch):
    monkeypatch.setenv("CHECKIN_CONFIG_KEY", "env-key-123")

    assert config_key_from_environment("file-key-123") == "env-key-123"

    monkeypatch.delenv("CHECKIN_CONFIG_KEY")
    assert config_key_from_environment("file-key-123") == "file-key-123"
