"""后台 daemon 的用户运行开关持久化。"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Optional

from .config import DATA_DIR

CONTROL_FILE = DATA_DIR / "daemon_control.json"
DEFAULT_ENABLED = True


def control_path(path: Optional[Path] = None) -> Path:
    return path or CONTROL_FILE


def is_enabled(path: Optional[Path] = None) -> bool:
    """读取用户是否允许 daemon 被 GUI/开机自启拉起；损坏时安全默认为启用。"""
    target = control_path(path)
    try:
        raw = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, TypeError, ValueError):
        return DEFAULT_ENABLED
    if not isinstance(raw, dict):
        return DEFAULT_ENABLED
    value = raw.get("enabled")
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)) and value in (0, 1):
        return bool(value)
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in {"1", "true", "yes", "on"}:
            return True
        if normalized in {"0", "false", "no", "off"}:
            return False
    return DEFAULT_ENABLED


def set_enabled(enabled: bool, path: Optional[Path] = None) -> bool:
    """原子保存 daemon 运行开关，返回是否写入成功。"""
    target = control_path(path)
    temporary = target.with_name(target.name + ".tmp")
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        temporary.write_text(
            json.dumps({"enabled": bool(enabled)}, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        temporary.replace(target)
        return True
    except OSError:
        try:
            temporary.unlink()
        except OSError:
            pass
        return False
