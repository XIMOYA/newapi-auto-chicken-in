#!/usr/bin/env python3
"""环境自检：依赖、浏览器、虚拟显示、配置、AI 接口、出口 IP 一次性全查。

    python scripts/doctor.py

退出码：0 全部通过（可能有警告）/ 1 有致命项未通过
"""

from __future__ import annotations

import shutil
import struct
import sys
import zlib
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from rich.table import Table  # noqa: E402

from newapi_checkin import logger as log  # noqa: E402
from newapi_checkin.config import (  # noqa: E402
    SESSIONS_FILE,
    ConfigError,
    load_config,
)

OK, WARN, FAIL = "ok", "warn", "fail"
_RESULTS: list = []


def add(name: str, status: str, detail: str = "") -> None:
    _RESULTS.append((name, status, detail))
    if status == OK:
        log.ok(f"{name}: {detail}" if detail else name)
    elif status == WARN:
        log.warn(f"{name}: {detail}")
    else:
        log.err(f"{name}: {detail}")


def tiny_png(width: int = 32, height: int = 32, color=(240, 240, 240)) -> bytes:
    """不依赖 Pillow 生成一张纯色 PNG，用于验证视觉接口能否吃图。"""
    raw = b"".join(b"\x00" + bytes(color) * width for _ in range(height))

    def chunk(tag: bytes, data: bytes) -> bytes:
        payload = tag + data
        return struct.pack(">I", len(data)) + payload + struct.pack(
            ">I", zlib.crc32(payload) & 0xFFFFFFFF
        )

    ihdr = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", ihdr)
            + chunk(b"IDAT", zlib.compress(raw)) + chunk(b"IEND", b""))


# --------------------------------------------------------------------------- #
# 各项检查
# --------------------------------------------------------------------------- #


def check_python() -> None:
    version = sys.version_info
    text = f"{version.major}.{version.minor}.{version.micro} on {sys.platform}"
    if version >= (3, 10):
        add("Python 版本", OK, text)
    else:
        add("Python 版本", FAIL, f"{text}（需要 3.10+）")


def check_deps() -> None:
    from importlib.metadata import PackageNotFoundError, version as pkg_version

    def describe(module: str) -> str:
        try:
            return pkg_version(module)
        except PackageNotFoundError:
            return "已安装"

    required = {
        "curl_cffi": "HTTP 快路径（S0/S1）",
        "rich": "日志输出",
    }
    optional = {
        "camoufox": "浏览器过盾主实现",
        "patchright": "浏览器过盾备选实现",
    }
    for module, purpose in required.items():
        try:
            __import__(module)
        except ImportError as exc:
            add(f"依赖 {module}", FAIL,
                f"缺失（{purpose}）-> pip install -r requirements.txt: {exc}")
            continue
        add(f"依赖 {module}", OK, f"{describe(module)} - {purpose}")

    available = 0
    for module, purpose in optional.items():
        try:
            __import__(module)
        except ImportError:
            add(f"依赖 {module}", WARN, f"未安装（{purpose}）")
            continue
        add(f"依赖 {module}", OK, f"{describe(module)} - {purpose}")
        available += 1
    if available == 0:
        add("浏览器驱动", FAIL, "camoufox 和 patchright 都没装，无法过盾")


def check_xvfb() -> None:
    if sys.platform.startswith("win"):
        add("虚拟显示", WARN, "Windows 无 Xvfb，headless=virtual 会自动降级为 headless=True")
        return
    path = shutil.which("Xvfb")
    if path:
        add("虚拟显示", OK, f"Xvfb -> {path}")
    else:
        add("虚拟显示", FAIL, "未找到 Xvfb -> apt-get install -y xvfb")


def check_browser(cfg) -> None:
    """真的启动一次浏览器，截个图，确认整条链路可用。"""
    from newapi_checkin.cf.driver_base import DriverUnavailable
    from newapi_checkin.cf.solver import _make_driver
    from newapi_checkin.config import Account

    probe = Account(name="__doctor__", url="https://example.com", cookie="")
    try:
        driver = _make_driver(cfg, probe, None)
    except Exception as exc:  # noqa: BLE001
        add(f"浏览器 {cfg.browser.driver}", FAIL, f"驱动构造失败: {exc}")
        return

    try:
        with driver:
            driver.page.goto("about:blank")
            ua = driver.user_agent()
            shot = driver.screenshot()
            width, height = driver.viewport()
        if shot and ua:
            add(f"浏览器 {cfg.browser.driver}", OK,
                f"启动/截图正常 {width}x{height}, UA={ua[:60]}")
        else:
            add(f"浏览器 {cfg.browser.driver}", WARN,
                f"启动成功但截图或 UA 为空（shot={len(shot)}B, ua={ua[:40]!r}）")
    except DriverUnavailable as exc:
        add(f"浏览器 {cfg.browser.driver}", FAIL, str(exc))
    except Exception as exc:  # noqa: BLE001
        add(f"浏览器 {cfg.browser.driver}", FAIL, f"{type(exc).__name__}: {exc}")
    finally:
        try:
            probe.profile_dir.exists() and shutil.rmtree(probe.profile_dir, ignore_errors=True)
        except Exception:  # noqa: BLE001
            pass


def check_ai(cfg) -> None:
    if not cfg.ai.enabled:
        add("AI 接口", WARN, "配置里 ai.enabled=false，S3 将被跳过")
        return
    if not cfg.ai.ready:
        add("AI 接口", FAIL, "base_url / api_key / model 不完整")
        return

    from newapi_checkin.ai.vision import VisionClient

    try:
        vision = VisionClient(cfg.ai)
    except Exception as exc:  # noqa: BLE001
        add("AI 接口", FAIL, f"客户端初始化失败: {exc}")
        return

    reachable, detail = vision.ping()
    add("AI 接口连通", OK if reachable else FAIL, f"{cfg.ai.models_url} -> {detail}")
    if not reachable:
        add("AI 视觉调用", WARN, "接口不可达，已跳过（先修好 base_url / api_key）")
        vision.close()
        return

    verdict = vision.classify_page(tiny_png())
    if verdict.state != "unknown" or verdict.confidence > 0:
        add("AI 视觉调用", OK,
            f"{cfg.ai.model} 可接收图片并返回结构化 JSON（本次判定 {verdict.state}）")
    else:
        add("AI 视觉调用", FAIL,
            f"{cfg.ai.model} 未返回可解析 JSON，检查该模型是否支持视觉输入（用 -v 看原始输出）")
    vision.close()


def check_network(cfg) -> None:
    from newapi_checkin.utils import probe_exit_ip

    proxies = {a.proxy for a in cfg.accounts}
    for proxy in sorted(proxies, key=lambda p: p or ""):
        label = f"出口 IP{f'（代理 {proxy}）' if proxy else '（直连）'}"
        ip = probe_exit_ip(proxy)
        if ip:
            add(label, OK, ip)
        else:
            add(label, WARN, "探测失败，将跳过 cf_clearance 的 IP 比对")


def check_sessions() -> None:
    if not SESSIONS_FILE.exists():
        add("会话缓存", WARN, f"{SESSIONS_FILE.name} 还不存在（首次运行后生成）")
        return
    from newapi_checkin.cf.session_store import SessionStore

    store = SessionStore(SESSIONS_FILE)
    cached = [slug for slug, rec in store._data.items() if rec.cf is not None]  # noqa: SLF001
    add("会话缓存", OK, f"{SESSIONS_FILE.name}，{len(cached)} 个账号有过盾缓存")


def main() -> int:
    log.setup(verbose=True)
    log.step("环境自检开始")

    check_python()
    check_deps()
    check_xvfb()

    cfg = None
    try:
        cfg = load_config()
        enabled = [a for a in cfg.accounts if a.enabled]
        add("配置文件", OK,
            f"{cfg.source} | 账号 {len(cfg.accounts)} 个（启用 {len(enabled)}）| "
            f"driver={cfg.browser.driver} headless={cfg.browser.headless}")
        for account in cfg.accounts:
            log.debug(f"  - {account.name}: {account.base_url} "
                      f"cookie={log.mask(account.cookie)} proxy={account.proxy or '直连'}")
    except ConfigError as exc:
        add("配置文件", FAIL, str(exc).replace("\n", " "))

    check_sessions()

    if cfg is not None:
        check_ai(cfg)
        check_network(cfg)
        check_browser(cfg)
    else:
        add("浏览器/AI 检查", WARN, "配置不可用，已跳过")

    table = Table(title="自检结果", header_style="bold")
    table.add_column("检查项", overflow="fold")
    table.add_column("结果")
    table.add_column("详情", overflow="fold")
    style = {OK: "[ok]通过[/ok]", WARN: "[warn]警告[/warn]", FAIL: "[err]失败[/err]"}
    for name, status, detail in _RESULTS:
        table.add_row(name, style[status], detail.replace("[", "\\["))
    log.console.print()
    log.console.print(table)

    failures = sum(1 for _, status, _ in _RESULTS if status == FAIL)
    if failures:
        log.err(f"有 {failures} 项未通过，按上面的提示修复后重跑")
        return 1
    log.ok("全部关键项通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
