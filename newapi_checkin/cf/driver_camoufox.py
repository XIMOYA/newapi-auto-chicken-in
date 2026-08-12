"""Camoufox 驱动（主实现）。

Firefox 系反检测浏览器，指纹在 C++ 层伪造而非 JS 注入补丁，无头场景检测率最低。
headless="virtual" 会自动拉起 Xvfb，正好适配无头 VPS。
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Optional

from .. import logger as log
from ..config import ROOT
from .driver_base import BrowserDriver, DriverUnavailable

_VALID_OS = ("windows", "macos", "linux")
_META_FILE = "camoufox_meta.json"


class CamoufoxDriver(BrowserDriver):
    name = "camoufox"

    # ------------------------------------------------------------------ #
    # 指纹固定：同一账号每次都用同一套 OS 画像，指纹天天变本身就是异常信号
    # ------------------------------------------------------------------ #

    def _meta_path(self) -> Path:
        return self.account.profile_dir / _META_FILE

    def _pinned_os(self) -> str:
        path = self._meta_path()
        try:
            value = json.loads(path.read_text(encoding="utf-8")).get("os")
            if value in _VALID_OS:
                return value
        except (OSError, json.JSONDecodeError, AttributeError):
            pass
        value = "windows"
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps({"os": value}), encoding="utf-8")
        except OSError:
            pass
        return value

    def _headless(self):
        headless = self.cfg.browser.headless
        if headless == "virtual" and sys.platform.startswith("win"):
            log.debug("Windows 上没有 Xvfb，headless=virtual 降级为 headless=True")
            return True
        if (
            sys.platform.startswith("linux")
            and headless is False
            and not (os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY"))
        ):
            log.warn(
                "Linux 当前没有 DISPLAY/WAYLAND_DISPLAY，自动降级为 headless=True；"
                "如需人工验证，请先配置 Xvfb/VNC 并使用 --manual"
            )
            return True
        return headless

    # ------------------------------------------------------------------ #

    def _resolve_executable_path(self) -> Optional[Path]:
        """解析配置中的 Camoufox 可执行文件路径。"""
        configured = self.cfg.browser.executable_path
        if not configured:
            return None

        path = Path(os.path.expandvars(os.path.expanduser(configured)))
        if not path.is_absolute():
            path = ROOT / path
        if path.is_dir():
            path = path / "camoufox.exe"
        if path.is_file():
            return path
        raise DriverUnavailable(f"配置的 Camoufox 不存在: {path}")

    @staticmethod
    def _resolve_ff_version(executable_path: Path) -> Optional[int]:
        """从内置浏览器的 version.json 读取 Firefox 主版本号。"""
        for parent in (executable_path.parent, *executable_path.parents):
            metadata = parent / "version.json"
            if not metadata.is_file():
                continue
            try:
                raw = json.loads(metadata.read_text(encoding="utf-8"))
                major = int(str(raw.get("version", "")).split(".", 1)[0])
                if major > 0:
                    return major
            except (OSError, ValueError, TypeError, json.JSONDecodeError):
                continue
        return None

    def _base_options(self) -> dict:
        opts = {
            "persistent_context": True,
            "user_data_dir": str(self.account.profile_dir),
            "headless": self._headless(),
            "humanize": self.cfg.browser.humanize,
            "locale": self.cfg.browser.locale,
            "window": (int(self.cfg.browser.window[0]), int(self.cfg.browser.window[1])),
            "enable_cache": True,
            "os": self._pinned_os(),
        }
        executable_path = self._resolve_executable_path()
        if executable_path is not None:
            opts["executable_path"] = str(executable_path)
            ff_version = self._resolve_ff_version(executable_path)
            if ff_version is not None:
                # Explicit packaged browsers must not depend on a user-level
                # Camoufox installation just to infer the Firefox major version.
                opts["ff_version"] = ff_version

        proxy = self._proxy()
        if proxy:
            opts["proxy"] = proxy
            # 让时区/地理位置跟代理出口一致，否则时区与 IP 不符会被扣分
            opts["geoip"] = True
        return opts

    @staticmethod
    def _hint(exc: Optional[BaseException]) -> str:
        text = f"{type(exc).__name__}: {exc}" if exc else "未知错误"
        low = text.lower()
        if "manifest.json" in low or "addon" in low:
            return (f"默认扩展（uBlock Origin）下载不完整 -> 删除缓存后重试: "
                    f"rm -rf ~/.cache/camoufox ~/.local/share/camoufox 再 python -m camoufox "
                    f"fetch（原始错误: {text}）")
        if isinstance(exc, FileNotFoundError) or "executable doesn't exist" in low \
                or "no such file" in low or "not installed" in low:
            return f"Camoufox 浏览器未下载 -> 执行: python -m camoufox fetch（原始错误: {text}）"
        if "xvfb" in low or "display" in low:
            return f"缺少虚拟显示 -> 执行: apt-get install -y xvfb（原始错误: {text}）"
        if "geoip" in low or "maxmind" in low or "mmdb" in low:
            return f"GeoIP 数据库缺失 -> 执行: python -m camoufox fetch（原始错误: {text}）"
        if "proxy" in low:
            return f"代理不可用，检查 accounts[].proxy（原始错误: {text}）"
        return f"Camoufox 启动失败: {text}"

    def _launch(self):
        try:
            from camoufox.sync_api import Camoufox
        except ImportError as exc:
            raise DriverUnavailable(
                "camoufox 未安装 -> 执行: pip install -r requirements.txt && python -m camoufox fetch"
            ) from exc

        options = self._base_options()
        attempts = [options]
        if options.get("geoip"):
            # geoip 库缺失不该导致整体失败，去掉再试一次
            fallback = dict(options)
            fallback.pop("geoip", None)
            attempts.append(fallback)
        # uBlock Origin 从 addons.mozilla.org 下载失败时会留下坏目录，直接阻塞启动。
        # 追加一次「不带默认扩展」的尝试，扩展只是锦上添花，不该拖死签到。
        try:
            from camoufox.addons import DefaultAddons

            without_addons = dict(attempts[-1])
            without_addons["exclude_addons"] = [DefaultAddons.UBO]
            attempts.append(without_addons)
        except ImportError:
            pass

        # A read-only/containerized /tmp can prevent Camoufox's virtual Xvfb
        # display from creating /tmp/.X11-unix. True headless does not need it.
        if options.get("headless") == "virtual":
            no_virtual = dict(attempts[-1])
            no_virtual["headless"] = True
            attempts.append(no_virtual)

        last_exc: Optional[BaseException] = None
        for idx, attempt in enumerate(attempts, start=1):
            manager = Camoufox(**attempt)
            try:
                context = manager.__enter__()
            except Exception as exc:  # noqa: BLE001 - 启动失败原因很杂，统一转成可执行提示
                last_exc = exc
                if (
                    attempt.get("headless") == "virtual"
                    and idx < len(attempts)
                    and attempts[idx].get("headless") is True
                ):
                    log.warn("虚拟显示启动失败，自动退回真正 headless=True")
                log.debug(f"Camoufox 第 {idx} 次启动失败: {exc}")
                continue
            self._closers.append(lambda mgr=manager: mgr.__exit__(None, None, None))
            log.debug(f"Camoufox 已启动 (os={attempt['os']}, headless={attempt['headless']}, "
                      f"profile={self.account.profile_dir.name})")
            return context

        raise DriverUnavailable(self._hint(last_exc))
