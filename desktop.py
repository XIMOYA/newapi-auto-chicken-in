#!/usr/bin/env python3
"""Windows 桌面版统一入口：默认启动 PySide6，--daemon 启动后台服务。"""

from __future__ import annotations

import sys

from newapi_checkin import __version__


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if "--version" in args:
        print(f"newapi-checkin {__version__}")
        return 0
    if "--help" in args:
        print("用法: newapi-checkin [--smoke-test] | --daemon [--autostart]")
        print("默认启动 PySide6 面板；--daemon 启动独立后台服务。")
        return 0
    if "--daemon" in args:
        from newapi_checkin.daemon import main as daemon_main

        return daemon_main(args)
    from newapi_checkin.gui import run_gui

    return run_gui([sys.argv[0], *args])


if __name__ == "__main__":
    sys.exit(main())
