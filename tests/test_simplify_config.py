"""scripts/simplify_config.py 的单元测试。

盯住两条底线：瘦身只删「与默认值等价」的字段，有效值一律不动；
凭据体检要能识别出实际不会签到成功的形态（空值、裸串）。
"""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
_spec = importlib.util.spec_from_file_location(
    "simplify_config", ROOT / "scripts" / "simplify_config.py")
simplify_config = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(simplify_config)


def account(**kw):
    base = {
        "name": "A", "url": "https://a.com", "login_method": "newapi_cookie",
        "cookie": "session=ok", "github_user_session": "", "github_client_id": "",
        "user_id": 1, "proxy": None, "checkin_path": None,
        "browser_path": "/dashboard", "enabled": True,
    }
    base.update(kw)
    return base


class TestSimplifyAccount:
    def test_default_valued_fields_are_dropped(self):
        stats = {"dropped_fields": 0, "login_method_migrated": 0}
        out = simplify_config.simplify_account(account(), stats)
        for key in ("browser_path", "proxy", "checkin_path",
                    "github_user_session", "github_client_id"):
            assert key not in out
        assert stats["dropped_fields"] == 5

    def test_effective_values_are_never_touched(self):
        stats = {"dropped_fields": 0, "login_method_migrated": 0}
        out = simplify_config.simplify_account(account(
            cookie="session=keepme", user_id=42, proxy="http://1.2.3.4:8080",
            checkin_path="/api/user/checkin", browser_path="/profile",
        ), stats)
        assert out["cookie"] == "session=keepme"
        assert out["user_id"] == 42
        assert out["proxy"] == "http://1.2.3.4:8080"
        assert out["checkin_path"] == "/api/user/checkin"
        assert out["browser_path"] == "/profile"
        assert stats["dropped_fields"] == 2  # 只掉两个空的签发原料字段

    @pytest.mark.parametrize("legacy", ["github_cookie", "GitHub_Cookie", "github-cookie"])
    def test_legacy_login_method_is_normalized(self, legacy):
        stats = {"dropped_fields": 0, "login_method_migrated": 0}
        out = simplify_config.simplify_account(account(login_method=legacy), stats)
        assert out["login_method"] == "tabiai"
        assert stats["login_method_migrated"] == 1

    def test_tabiai_keeps_issue_material_even_when_empty(self):
        """user_session 是 tabiai 一键签发的原料，空着也要留字段位。"""
        stats = {"dropped_fields": 0, "login_method_migrated": 0}
        out = simplify_config.simplify_account(
            account(login_method="tabiai", cookie="new_api_refresh=sid.secret"), stats)
        assert "github_user_session" in out
        assert "github_client_id" in out

    def test_missing_login_method_defaults_to_newapi(self):
        stats = {"dropped_fields": 0, "login_method_migrated": 0}
        raw = account()
        del raw["login_method"]
        out = simplify_config.simplify_account(raw, stats)
        assert out["login_method"] == "newapi_cookie"


class TestAudit:
    def test_empty_credential_is_flagged(self):
        assert "跳过" in simplify_config.audit_account(account(cookie=""))

    def test_bare_token_is_flagged_as_unusable(self):
        """裸串没有 name=value，Cookie 解析后整条丢弃，等同于没填。"""
        problem = simplify_config.audit_account(account(cookie="GsUTpUyW8ZN6kSq3RVrb"))
        assert "name=value" in problem

    def test_normal_cookie_passes(self):
        assert simplify_config.audit_account(account()) == ""

    def test_tabiai_refresh_shape_is_checked(self):
        ok = account(login_method="tabiai", cookie="new_api_refresh=sid.secret")
        assert simplify_config.audit_account(ok) == ""
        bare = account(login_method="tabiai", cookie="new_api_refresh=nodot")
        assert "new_api_refresh" in simplify_config.audit_account(bare)

    def test_tabiai_accepts_value_without_prefix(self):
        assert simplify_config.audit_account(
            account(login_method="tabiai", cookie="sid.secret")) == ""


class TestSimplifyDocument:
    def test_version_and_missing_sections_are_filled(self):
        cfg, stats = simplify_config.simplify({
            "config_version": 2, "accounts": [account()],
            "config_sync": {"enabled": False, "url": ""},
        })
        assert cfg["config_version"] == 3
        assert cfg["tabiai"]["token_interval_minutes"] == 21
        assert cfg["config_sync"]["writeback_url"] == ""
        assert stats["version"] == "2 -> 3"

    def test_existing_tabiai_section_is_respected(self):
        cfg, stats = simplify_config.simplify({
            "accounts": [], "tabiai": {"enabled": True, "cdp_url": "http://x:9333"},
        })
        assert cfg["tabiai"] == {"enabled": True, "cdp_url": "http://x:9333"}
        assert "tabiai" not in stats["added_sections"]

    def test_input_is_not_mutated(self):
        raw = {"config_version": 2, "accounts": [account()]}
        snapshot = json.dumps(raw, sort_keys=True)
        simplify_config.simplify(raw)
        assert json.dumps(raw, sort_keys=True) == snapshot

    def test_other_sections_pass_through_untouched(self):
        raw = {
            "accounts": [],
            "ai": {"enabled": True, "api_key": "sk-x", "model": "m"},
            "proxy_pool": {"enabled": True, "save_limit": 2000, "ip_swap_limit": 3},
            "notify": {"email": {"enabled": True, "smtp_port": 465}},
            "security": {"encryption_enabled": False},
        }
        cfg, _ = simplify_config.simplify(raw)
        for key in ("ai", "proxy_pool", "notify", "security"):
            assert cfg[key] == raw[key]


class TestCLI:
    def test_check_mode_writes_nothing(self, tmp_path, capsys):
        src = tmp_path / "in.json"
        src.write_text(json.dumps({"config_version": 2, "accounts": [account(cookie="")]}),
                       encoding="utf-8")
        assert simplify_config.main([str(src), "--check"]) == 0
        assert not (tmp_path / "in.v3.json").exists()
        assert "跳过" in capsys.readouterr().out

    def test_output_is_written_and_parseable(self, tmp_path):
        src = tmp_path / "in.json"
        src.write_text(json.dumps({"config_version": 2, "accounts": [account()]}),
                       encoding="utf-8")
        dst = tmp_path / "out.json"
        assert simplify_config.main([str(src), "-o", str(dst)]) == 0
        cfg = json.loads(dst.read_text(encoding="utf-8"))
        assert cfg["config_version"] == 3 and len(cfg["accounts"]) == 1

    def test_bad_json_exits_with_error(self, tmp_path, capsys):
        src = tmp_path / "bad.json"
        src.write_text("{not json", encoding="utf-8")
        assert simplify_config.main([str(src)]) == 2
        assert "读取失败" in capsys.readouterr().err

    def test_non_object_root_is_rejected(self, tmp_path, capsys):
        src = tmp_path / "arr.json"
        src.write_text("[]", encoding="utf-8")
        assert simplify_config.main([str(src)]) == 2
        assert "必须是对象" in capsys.readouterr().err

    def test_stdin_requires_explicit_output(self, capsys):
        assert simplify_config.main(["-"]) == 2
        assert "-o" in capsys.readouterr().err
