"""Windows 当前用户开机启动。"""

from __future__ import annotations

import os
import shlex
import sys
from pathlib import Path
from typing import Optional

VALUE_NAME = "NewAPICheckin"
RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"

try:  # pragma: no cover - Windows 分支在非 Windows 构建机上不可导入
    import winreg
except ImportError:  # pragma: no cover
    winreg = None  # type: ignore[assignment]


def supported() -> bool:
    return os.name == "nt" and winreg is not None


def startup_command() -> str:
    """生成当前安装位置的 daemon 启动命令。"""
    if getattr(sys, "frozen", False):
        executable = Path(sys.executable).resolve()
        return f'"{executable}" --daemon --autostart'
    script = Path(__file__).resolve().parent.parent / "desktop.py"
    executable = Path(sys.executable).resolve()
    if executable.name.lower() == "python.exe":
        pythonw = executable.with_name("pythonw.exe")
        if pythonw.exists():
            executable = pythonw
    return f'"{executable}" "{script}" --daemon --autostart'


def get_command() -> Optional[str]:
    if not supported():
        return None
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_READ) as key:
            value, _ = winreg.QueryValueEx(key, VALUE_NAME)
            return str(value)
    except FileNotFoundError:
        return None
    except OSError:
        return None


def is_enabled() -> bool:
    return bool(get_command())


def enable() -> bool:
    if not supported():
        return False
    try:
        with winreg.CreateKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as key:
            winreg.SetValueEx(key, VALUE_NAME, 0, winreg.REG_SZ, startup_command())
        return True
    except OSError:
        return False


def disable() -> bool:
    if not supported():
        return False
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_SET_VALUE) as key:
            winreg.DeleteValue(key, VALUE_NAME)
        return True
    except FileNotFoundError:
        return True
    except OSError:
        return False


def quote_for_debug(command: str) -> list[str]:
    """仅供测试/诊断解析注册表中的命令。"""
    return shlex.split(command, posix=False)
