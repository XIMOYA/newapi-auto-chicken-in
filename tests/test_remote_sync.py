"""远程配置 API 同步与解密落盘测试。"""

import json

from newapi_checkin.config_store import load_document, save_document
from newapi_checkin.remote_sync import _merge_payload, sync_remote_config
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


def test_sync_preserves_local_encryption(tmp_path, monkeypatch):
    """同步后加密状态必须保留：不能明文落盘，也不能删掉密文文件。"""
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
    assert document.encrypted is True
    assert document.raw["accounts"][0]["name"] == "新账号"
    assert document.raw["security"]["config_key"] == "config-key-123"
    assert document.raw["config_sync"]["url"] == "https://config.example.com/api"
    # config.json 仍只是 security bootstrap，密文文件被更新而不是被删
    bootstrap = json.loads(path.read_text(encoding="utf-8"))
    assert "accounts" not in bootstrap
    assert "cookie" not in path.read_text(encoding="utf-8")
    encrypted_path = tmp_path / "data" / "config.encrypted.json"
    assert encrypted_path.exists()
    assert document.raw["accounts"][0]["cookie"] == "new"


def test_sync_keeps_local_modules_missing_from_remote(tmp_path, monkeypatch):
    """远端没带本地已有的业务模块时，保留本地模块并正常同步账号。"""
    path = tmp_path / "config.json"
    raw = base_config()
    raw["ai"] = {
        "enabled": True,
        "base_url": "https://ai.example.com",
        "api_key": "sk-local-secret",
        "model": "gpt-4o-mini",
    }
    raw["proxy_pool"] = {"enabled": True, "sources": ["http://src.example.com:80"]}
    save_document(raw, path, encryption_enabled=False)
    monkeypatch.setenv("CHECKIN_CONFIG", str(path))
    monkeypatch.setattr(
        "newapi_checkin.remote_sync.cffi.request",
        lambda method, url, **kwargs: FakeResponse(
            {"accounts": [{"name": "新账号", "url": "https://new.example.com", "cookie": "c"}]}
        ),
    )

    result = sync_remote_config()

    assert result["ok"] is True
    document = load_document(path)
    assert document.raw["ai"]["api_key"] == "sk-local-secret"
    assert document.raw["proxy_pool"]["enabled"] is True
    assert document.raw["accounts"][0]["name"] == "新账号"


def test_merge_remote_keeps_local_when_missing_and_applies_empty_arrays():
    """合并语义：远端缺键保留本地；远端显式空数组按远端意图清空。"""
    local = {
        "accounts": [{"name": "A", "url": "https://a.example.com"}],
        "ai": {"enabled": False, "api_key": "local-key"},
        "proxy_pool": {"enabled": True},
    }
    merged = _merge_payload(local, {"accounts": [], "ai": {"enabled": True}})
    assert merged["accounts"] == []
    assert merged["ai"] == {"enabled": True}
    assert merged["proxy_pool"] == {"enabled": True}  # 远端没带 -> 保留本地

    # security / config_sync 永远不能被远端覆盖
    local["security"] = {"encryption_enabled": True}
    merged = _merge_payload(local, {"security": {"encryption_enabled": False}})
    assert merged["security"] == {"encryption_enabled": True}


def test_merge_never_overwrites_local_tabiai_section():
    """tabiai.cdp_url 指向本机 Chrome 调试端口，远端下发不能把它改坏。"""
    local = {
        "accounts": [],
        "tabiai": {"enabled": True, "cdp_url": "http://127.0.0.1:9222"},
    }
    merged = _merge_payload(local, {
        "accounts": [],
        "tabiai": {"enabled": False, "cdp_url": "http://10.0.0.1:9222"},
    })
    assert merged["tabiai"] == {"enabled": True, "cdp_url": "http://127.0.0.1:9222"}


def test_missing_tabiai_is_not_reported_as_missing_module():
    """远端本来就不管 tabiai，不该每次同步都刷一条无意义告警。"""
    from newapi_checkin.remote_sync import _missing_modules

    local = {"accounts": [], "tabiai": {"enabled": True}, "proxy_pool": {"enabled": True}}
    missing = _missing_modules(local, {"accounts": []})
    assert "tabiai" not in missing
    assert "proxy_pool" in missing


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
