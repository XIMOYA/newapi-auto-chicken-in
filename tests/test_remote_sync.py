"""远程配置 API 同步与解密落盘测试。"""

import json

from newapi_checkin.config_store import load_document, save_document
from newapi_checkin.remote_sync import sync_remote_config
from newapi_checkin.secure_config import encrypt_json


class FakeResponse:
    def __init__(self, payload, status_code=200):
        self.payload = payload
        self.status_code = status_code
        self.text = json.dumps(payload, ensure_ascii=False)

    def json(self):
        return self.payload


def base_config(*, sync=None):
    return {
        "security": {
            "encryption_enabled": False,
            "config_key": "config-key-123",
            "encrypted_file": "data/config.encrypted.json",
        },
        "config_sync": sync
        or {
            "enabled": True,
            "url": "https://config.example.com/api",
            "method": "GET",
            "auto_before_checkin": True,
        },
        "accounts": [{"name": "旧账号", "url": "https://old.example.com", "cookie": "old"}],
    }


def test_sync_decrypts_envelope_and_saves_plaintext(tmp_path, monkeypatch):
    path = tmp_path / "config.json"
    raw = base_config()
    save_document(raw, path, encryption_enabled=True, key="config-key-123")
    remote_payload = {
        "accounts": [{"name": "新账号", "url": "https://new.example.com", "cookie": "new"}],
    }
    envelope = encrypt_json(remote_payload, "config-key-123")
    monkeypatch.setenv("CHECKIN_CONFIG", str(path))
    monkeypatch.setattr(
        "newapi_checkin.remote_sync.cffi.request",
        lambda method, url, **kwargs: FakeResponse({"data": envelope}),
    )

    result = sync_remote_config()

    assert result["ok"] is True
    assert result["encrypted_response"] is True
    document = load_document(path)
    assert document.encrypted is False
    assert document.raw["accounts"][0]["name"] == "新账号"
    assert document.raw["security"]["config_key"] == "config-key-123"
    assert document.raw["config_sync"]["url"] == "https://config.example.com/api"
    assert not (tmp_path / "data" / "config.encrypted.json").exists()


def test_sync_sends_token_and_post_body(tmp_path, monkeypatch):
    path = tmp_path / "config.json"
    sync = {
        "enabled": True,
        "url": "https://config.example.com/api",
        "method": "POST",
        "token": "secret-token",
        "token_header": "X-Config-Token",
        "token_prefix": "Token",
        "headers": {"X-Client": "checkin"},
        "body": {"site": "demo"},
    }
    save_document(base_config(sync=sync), path, encryption_enabled=False)
    captured = {}
    monkeypatch.setenv("CHECKIN_CONFIG", str(path))

    def fake_request(method, url, **kwargs):
        captured.update(method=method, url=url, kwargs=kwargs)
        return FakeResponse({"accounts": [{"name": "更新", "url": "https://new.example.com"}]})

    monkeypatch.setattr("newapi_checkin.remote_sync.cffi.request", fake_request)

    result = sync_remote_config()

    assert result["ok"] is True
    assert captured["method"] == "POST"
    assert captured["url"] == "https://config.example.com/api"
    assert captured["kwargs"]["headers"]["X-Client"] == "checkin"
    assert captured["kwargs"]["headers"]["X-Config-Token"] == "Token secret-token"
    assert captured["kwargs"]["json"] == {"site": "demo"}


def test_sync_failure_does_not_replace_existing_config(tmp_path, monkeypatch):
    path = tmp_path / "config.json"
    save_document(base_config(), path, encryption_enabled=False)
    monkeypatch.setenv("CHECKIN_CONFIG", str(path))
    monkeypatch.setattr(
        "newapi_checkin.remote_sync.cffi.request",
        lambda method, url, **kwargs: FakeResponse({"message": "server error"}, status_code=503),
    )

    result = sync_remote_config()

    assert result["ok"] is False
    assert load_document(path).raw["accounts"][0]["name"] == "旧账号"


def test_auto_only_skips_when_auto_sync_is_off(tmp_path, monkeypatch):
    path = tmp_path / "config.json"
    save_document(
        base_config(sync={"enabled": True, "url": "https://config.example.com", "auto_before_checkin": False}),
        path,
        encryption_enabled=False,
    )
    monkeypatch.setenv("CHECKIN_CONFIG", str(path))

    result = sync_remote_config(auto_only=True)

    assert result["ok"] is True
    assert result["skipped"] is True
