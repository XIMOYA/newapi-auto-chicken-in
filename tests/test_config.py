"""配置构建、校验、旧格式迁移、环境变量覆盖。"""

import pytest

from newapi_checkin import config as cfgmod


def build(**overrides):
    raw = {
        "accounts": [{"name": "站点A", "url": "https://a.example.com/", "cookie": "session=x"}],
    }
    raw.update(overrides)
    return cfgmod.build_config(raw)


class TestAccounts:
    def test_defaults(self):
        cfg = build()
        account = cfg.accounts[0]
        assert account.base_url == "https://a.example.com"
        assert account.enabled is True
        assert account.checkin_candidates == cfgmod.CHECKIN_PATH_CANDIDATES

    def test_explicit_checkin_path_wins(self):
        cfg = build(accounts=[{"name": "A", "url": "https://a.com",
                               "cookie": "c", "checkin_path": "api/user/sign"}])
        assert cfg.accounts[0].checkin_candidates == ("/api/user/sign",)

    def test_browser_path_defaults_to_dashboard(self):
        cfg = build()
        assert cfg.accounts[0].browser_path == "/dashboard"
        assert cfg.accounts[0].browser_url == "https://a.example.com/dashboard"

    def test_browser_path_can_be_overridden(self):
        cfg = build(accounts=[{"name": "A", "url": "https://a.com",
                               "browser_path": "console"}])
        assert cfg.accounts[0].browser_path == "/console"
        assert cfg.accounts[0].browser_url == "https://a.com/console"

    def test_browser_full_url_reduces_to_path(self):
        cfg = build(accounts=[{"name": "A", "url": "https://a.com",
                               "browser_path": "https://a.com/dashboard?tab=checkin"}])
        assert cfg.accounts[0].browser_path == "/dashboard?tab=checkin"

    def test_legacy_userid_key(self):
        cfg = build(accounts=[{"name": "A", "url": "https://a.com", "userId": "7"}])
        assert cfg.accounts[0].user_id == 7

    def test_missing_url_raises(self):
        with pytest.raises(cfgmod.ConfigError, match="缺少 url"):
            build(accounts=[{"name": "A", "cookie": "c"}])

    def test_bad_scheme_raises(self):
        with pytest.raises(cfgmod.ConfigError, match="http"):
            build(accounts=[{"name": "A", "url": "ftp://a.com"}])

    def test_empty_accounts_raises(self):
        with pytest.raises(cfgmod.ConfigError, match="至少配置一个账号"):
            build(accounts=[])

    def test_duplicate_names_get_suffixed(self):
        cfg = build(accounts=[
            {"name": "A", "url": "https://a.com"},
            {"name": "A", "url": "https://b.com"},
        ])
        assert [a.name for a in cfg.accounts] == ["A", "A#2"]

    def test_auto_name(self):
        cfg = build(accounts=[{"url": "https://a.com"}])
        assert cfg.accounts[0].name == "Task_1"


class TestSelect:
    def test_only_enabled_by_default(self):
        cfg = build(accounts=[
            {"name": "A", "url": "https://a.com"},
            {"name": "B", "url": "https://b.com", "enabled": False},
        ])
        assert [a.name for a in cfg.select()] == ["A"]

    def test_explicit_names_include_disabled(self):
        cfg = build(accounts=[
            {"name": "A", "url": "https://a.com"},
            {"name": "B", "url": "https://b.com", "enabled": False},
        ])
        assert [a.name for a in cfg.select(["B"])] == ["B"]

    def test_unknown_name_raises(self):
        with pytest.raises(cfgmod.ConfigError, match="找不到账号"):
            build().select(["不存在"])


class TestSecurity:
    def test_security_defaults_and_parsing(self):
        cfg = build(security={"encryption_enabled": True, "config_key": "config-key-123", "encrypted_file": "data/secret.json"})
        assert cfg.security.encryption_enabled is True
        assert cfg.security.config_key == "config-key-123"
        assert cfg.security.encrypted_file == "data/secret.json"

    def test_security_invalid_boolean_uses_safe_default(self):
        cfg = build(security={"encryption_enabled": "not-a-bool"})
        assert cfg.security.encryption_enabled is False


class TestConfigSync:
    def test_defaults_are_safe_and_auto_ready(self):
        cfg = build()
        assert cfg.config_sync.enabled is False
        assert cfg.config_sync.method == "GET"
        assert cfg.config_sync.auto_before_checkin is True

    def test_sync_fields_are_normalized(self):
        cfg = build(
            config_sync={
                "enabled": "true",
                "url": " https://config.example.com/api ",
                "method": "post",
                "token": " secret ",
                "headers": {"X-Test": 123},
                "timeout": 999,
                "auto_before_checkin": False,
            }
        )
        assert cfg.config_sync.enabled is True
        assert cfg.config_sync.url == "https://config.example.com/api"
        assert cfg.config_sync.method == "POST"
        assert cfg.config_sync.token == "secret"
        assert cfg.config_sync.headers == {"X-Test": "123"}
        assert cfg.config_sync.timeout == 300
        assert cfg.config_sync.auto_before_checkin is False


class TestAI:
    def test_endpoint_from_bare_host(self):
        ai = cfgmod.AIConfig(base_url="https://api.example.com", api_key="k", model="m")
        assert ai.chat_url == "https://api.example.com/v1/chat/completions"
        assert ai.models_url == "https://api.example.com/v1/models"

    def test_endpoint_with_v1(self):
        ai = cfgmod.AIConfig(base_url="https://api.example.com/v1/", api_key="k")
        assert ai.chat_url == "https://api.example.com/v1/chat/completions"

    def test_endpoint_already_full(self):
        ai = cfgmod.AIConfig(base_url="https://api.example.com/v1/chat/completions")
        assert ai.chat_url == "https://api.example.com/v1/chat/completions"

    def test_ready_requires_all_fields(self):
        assert cfgmod.AIConfig(enabled=True, base_url="u", api_key="k", model="m").ready is True
        assert cfgmod.AIConfig(enabled=True, base_url="u", model="m").ready is False
        assert cfgmod.AIConfig(enabled=False, base_url="u", api_key="k", model="m").ready is False

    def test_enabled_but_incomplete_raises(self):
        with pytest.raises(cfgmod.ConfigError, match="ai.enabled"):
            build(ai={"enabled": True, "base_url": "https://x.com"})


class TestBrowser:
    def test_headless_tri_state(self):
        assert cfgmod.parse_headless("virtual") == "virtual"
        assert cfgmod.parse_headless("true") is True
        assert cfgmod.parse_headless(False) is False
        assert cfgmod.parse_headless("乱七八糟", "virtual") == "virtual"

    def test_executable_path_is_preserved(self):
        cfg = build(browser={"driver": "patchright", "executable_path": "browser/chromium"})
        assert cfg.browser.driver == "patchright"
        assert cfg.browser.executable_path == "browser/chromium"

    def test_runtime_root_uses_frozen_executable_directory(self, monkeypatch, tmp_path):
        monkeypatch.setattr(cfgmod.sys, "frozen", True, raising=False)
        monkeypatch.setattr(cfgmod.sys, "executable", str(tmp_path / "app" / "checkin"))
        assert cfgmod.runtime_root() == tmp_path / "app"

    def test_bad_driver_raises(self):
        with pytest.raises(cfgmod.ConfigError, match="browser.driver"):
            build(browser={"driver": "selenium"})

    def test_defaults_interval_is_sorted(self):
        cfg = build(defaults={"interval_seconds": [9, 2]})
        assert cfg.defaults.interval_seconds == (2.0, 9.0)


class TestEnvOverride:
    def test_ai_key_from_env(self, monkeypatch):
        monkeypatch.setenv("CHECKIN_AI_API_KEY", "sk-from-env")
        monkeypatch.setenv("CHECKIN_AI_ENABLED", "true")
        monkeypatch.setenv("CHECKIN_AI_BASE_URL", "https://env.example.com/v1")
        cfg = build(ai={"model": "gpt-4o-mini"})
        assert cfg.ai.api_key == "sk-from-env"
        assert cfg.ai.enabled is True

    def test_account_cookie_from_env(self, monkeypatch):
        monkeypatch.setenv("CHECKIN_ACCOUNT_MY_SITE_COOKIE", "session=env")
        cfg = build(accounts=[{"name": "my site", "url": "https://a.com", "cookie": "old"}])
        assert cfg.accounts[0].cookie == "session=env"


class TestMigration:
    def test_legacy_array(self):
        legacy = [
            {"name": "站点A", "url": "https://a.com/", "cookie": "session=1", "userId": 3},
            {"url": "https://b.com", "cookie": "session=2", "proxy": "http://127.0.0.1:1080"},
        ]
        cfg = cfgmod.build_config(cfgmod.migrate_legacy(legacy))
        assert [a.name for a in cfg.accounts] == ["站点A", "Task_2"]
        assert cfg.accounts[0].user_id == 3
        assert cfg.accounts[1].proxy == "http://127.0.0.1:1080"
        assert cfg.ai.enabled is False

    def test_non_dict_entries_are_dropped(self):
        cfg = cfgmod.build_config(cfgmod.migrate_legacy(
            ["垃圾", {"name": "A", "url": "https://a.com"}]
        ))
        assert len(cfg.accounts) == 1


class TestSlug:
    def test_stable_and_filesystem_safe(self):
        first = cfgmod.slugify("站点A / 生产")
        assert first == cfgmod.slugify("站点A / 生产")
        assert all(ch.isalnum() or ch in "-._" for ch in first)

    def test_different_names_differ(self):
        assert cfgmod.slugify("站点A") != cfgmod.slugify("站点B")
