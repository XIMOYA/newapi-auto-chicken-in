from pathlib import Path


WORKFLOW = Path(__file__).parents[1] / ".github" / "workflows" / "checkin.yml"


def test_actions_prefers_base64_config_secret_for_log_stability():
    """结构化 JSON Secret 会让 Actions 脱敏误伤日志中的普通字段。"""
    text = WORKFLOW.read_text(encoding="utf-8")

    assert "CONFIG_JSON_B64: ${{ secrets.CONFIG_JSON_B64 }}" in text
    assert "printf '%s' \"$CONFIG_JSON_B64\" | base64 --decode > config.json" in text
    assert "CONFIG_JSON（兼容旧配置，建议迁移到 CONFIG_JSON_B64）" in text
    assert "CONFIG_JSON_B64（推荐）" in text


def test_actions_keeps_legacy_config_secret_fallback():
    text = WORKFLOW.read_text(encoding="utf-8")

    assert 'elif [ -n "$CONFIG_JSON" ]; then' in text
    assert 'printf \'%s\' "$CONFIG_JSON" > config.json' in text
    assert "CONFIG_JSON_B64" in text
