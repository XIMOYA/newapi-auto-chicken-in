"""Patchright 驱动（备选实现）。

补丁版 Playwright/Chromium，修掉了 Runtime.Enable 等 CDP 泄露。
Camoufox 在某些 VPS 上装不上时用它兜底。

无头 Linux 上 Chromium 的 headless 模式仍然容易被识别，所以优先走虚拟显示：
DISPLAY 已存在就直接用；否则自己拉一个尺寸正常的 Xvfb。
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Optional, Tuple

from .. import logger as log
from ..config import ROOT
from .driver_base import BrowserDriver, DriverUnavailable


def _spawn_virtual_display(width: int, height: int):
    """借用 Camoufox 的 Xvfb 启动逻辑，但换成正常屏幕尺寸（它默认是 1x1）。"""
    from camoufox.virtdisplay import VirtualDisplay

    class SizedVirtualDisplay(VirtualDisplay):
        xvfb_args = (
            "-screen", "0", f"{width}x{height}x24",
            "-ac", "-nolisten", "tcp",
            "+extension", "GLX",
            "-nocursor", "-br",
        )

    display = SizedVirtualDisplay()
    display.get()
    return display


class PatchrightDriver(BrowserDriver):
    name = "patchright"

    def _resolve_display(self) -> Tuple[bool, Optional[object]]:
        """返回 (headless, 虚拟显示对象)。"""
        headless = self.cfg.browser.headless
        if headless != "virtual":
            return bool(headless), None
        if sys.platform.startswith("win"):
            return True, None
        if os.environ.get("DISPLAY"):
            log.debug(f"复用已有 DISPLAY={os.environ['DISPLAY']}，用有头模式")
            return False, None
        try:
            width, height = int(self.cfg.browser.window[0]), int(self.cfg.browser.window[1])
            display = _spawn_virtual_display(width, height)
            os.environ["DISPLAY"] = display.get()
            log.debug(f"已启动虚拟显示 {os.environ['DISPLAY']} ({width}x{height})")
            return False, display
        except Exception as exc:  # noqa: BLE001
            log.warn("无 DISPLAY 且无法启动 Xvfb，退回 headless=True（更容易被识别）；"
                     "建议 apt-get install -y xvfb 后用 xvfb-run -a 启动")
            log.debug(f"虚拟显示启动失败: {exc}")
            return True, None

    def _resolve_executable_path(self) -> Optional[Path]:
        """解析配置或发布目录内的 Chromium 可执行文件。"""
        configured = self.cfg.browser.executable_path
        if configured:
            path = Path(os.path.expandvars(os.path.expanduser(configured)))
            if not path.is_absolute():
                path = ROOT / path
            if path.is_dir():
                for name in ("chromium", "chromium-browser", "chromium-headless-shell", "chrome"):
                    candidate = path / name
                    if candidate.is_file():
                        return candidate
            if path.is_file():
                return path
            raise DriverUnavailable(f"配置的 Chromium 不存在: {path}")

        browser_dir = ROOT / "browser"
        for name in ("chromium", "chromium-browser", "chromium-headless-shell", "chrome"):
            candidate = browser_dir / name
            if candidate.is_file():
                return candidate
        return None

    def _chrome_args(self, executable_path: Optional[Path] = None) -> list:
        args: list = []
        if sys.platform.startswith("linux"):
            args.append("--disable-dev-shm-usage")
            # 发布包内 Chromium 不携带可迁移的 setuid sandbox，统一使用 no-sandbox。
            if executable_path is not None or (hasattr(os, "geteuid") and os.geteuid() == 0):
                args.append("--no-sandbox")
        return args

    @staticmethod
    def _hint(exc: BaseException) -> str:
        text = f"{type(exc).__name__}: {exc}"
        low = text.lower()
        if "executable doesn't exist" in low or "please run the following" in low:
            return f"Chromium 未安装 -> 执行: patchright install chromium（原始错误: {text}）"
        if "libx" in low or "cannot open display" in low or "missing dependencies" in low:
            return ("缺少系统库 -> 执行: patchright install-deps 或 "
                    f"apt-get install -y libgtk-3-0 libasound2 libnss3（原始错误: {text}）")
        return f"Patchright 启动失败: {text}"

    def _launch(self):
        try:
            from patchright.sync_api import sync_playwright
        except ImportError as exc:
            raise DriverUnavailable(
                "patchright 未安装 -> 执行: pip install patchright && patchright install chromium"
            ) from exc

        headless, display = self._resolve_display()
        if display is not None:
            self._closers.append(display.kill)

        manager = sync_playwright()
        playwright = manager.__enter__()
        self._closers.append(lambda: manager.__exit__(None, None, None))

        executable_path = self._resolve_executable_path()
        kwargs = {
            "user_data_dir": str(self.account.profile_dir),
            "headless": headless,
            "no_viewport": True,
            "locale": self.cfg.browser.locale,
            "args": self._chrome_args(executable_path),
        }
        proxy = self._proxy()
        if proxy:
            kwargs["proxy"] = proxy

        if executable_path is not None:
            kwargs["executable_path"] = str(executable_path)
            try:
                context = playwright.chromium.launch_persistent_context(**kwargs)
                log.debug(f"Patchright 已启动 (Chromium: {executable_path})")
                return context
            except Exception as exc:
                raise DriverUnavailable(
                    f"包内 Chromium 启动失败: {executable_path}（原始错误: {exc}）"
                ) from exc

        # Patchright 官方建议优先用系统真实 Chrome，指纹比自带 Chromium 干净
        try:
            context = playwright.chromium.launch_persistent_context(channel="chrome", **kwargs)
            log.debug("Patchright 已启动 (channel=chrome)")
            return context
        except Exception as exc:
            log.debug(f"channel=chrome 启动失败，退回自带 Chromium: {exc}")

        try:
            context = playwright.chromium.launch_persistent_context(**kwargs)
            log.debug("Patchright 已启动 (自带 Chromium)")
            return context
        except Exception as exc:
            raise DriverUnavailable(self._hint(exc)) from exc

