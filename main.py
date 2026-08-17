#!/usr/bin/env python3
"""New API 自动签到入口。

    python main.py                    跑全部启用账号
    python main.py --account 站点A     只跑指定账号（可重复或用逗号分隔）
    python main.py --dry-run          只验证连通性与 cookie，不真签到
    python main.py --headful          强制有头浏览器（本地/X 转发调试）
    python main.py --headless         强制真正无图形浏览器（不依赖 Xvfb/VNC）
    python main.py --manual           人工兜底：等你手动过盾后保存 profile
    python main.py --no-ai            关掉 AI 辅助
    python main.py --no-browser       只走 HTTP 快路径，不启浏览器
    python main.py --parallel 6       账号级并行度（自动签到固定 6，人工模式 1）
    python main.py --browser-parallel 3   浏览器实例并发上限（自动签到固定 3）
    python main.py -v                 详细日志

退出码：0 全部成功 / 1 有失败 / 2 配置错误 / 130 被中断
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

from newapi_checkin import __version__
from newapi_checkin import logger as log
from newapi_checkin.config import LOGS_DIR, ConfigError, load_config
from newapi_checkin.runner import RunOptions, Runner


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="main.py",
        description="New API 自动签到（Cloudflare 过盾 + AI 视觉辅助）",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--config", metavar="PATH", help="指定配置文件路径，默认 config.json")
    parser.add_argument("--account", action="append", metavar="NAME",
                        help="只跑指定账号，可重复传入或用逗号分隔")
    parser.add_argument("--dry-run", action="store_true",
                        help="只验证当前登录方式的凭据与连通性，不执行签到")
    parser.add_argument(
        "--cookie-test",
        choices=("newapi_cookie", "tabiai", "github_cookie"),
        help="只检查指定类型凭据的可用性；两种登录方式需分别执行"
             "（github_cookie 为旧写法，等价于 tabiai）",
    )
    display_group = parser.add_mutually_exclusive_group()
    display_group.add_argument("--headful", action="store_true", help="强制有头浏览器")
    display_group.add_argument(
        "--headless", action="store_true",
        help="强制真正无图形浏览器，不依赖 Xvfb/VNC",
    )
    parser.add_argument(
        "--manual", action="store_true",
        help="人工兜底模式：需要可交互显示，完成验证后保存会话",
    )
    parser.add_argument("--no-ai", dest="use_ai", action="store_false", default=True,
                        help="禁用 AI 视觉辅助（S3）")
    parser.add_argument("--no-browser", dest="use_browser", action="store_false", default=True,
                        help="禁用浏览器过盾，只走 HTTP 快路径")
    parser.add_argument(
        "--parallel", type=int, choices=range(1, 17), default=None, metavar="N",
        help="兼容参数；自动签到固定 6 个账号并发，人工模式固定 1 个",
    )
    parser.add_argument(
        "--browser-parallel", type=int, choices=range(1, 9), default=None, metavar="N",
        help="兼容参数；自动签到固定 2 个浏览器实例，人工模式固定 1 个",
    )
    parser.add_argument("-v", "--verbose", action="store_true", help="详细日志")
    parser.add_argument("--version", action="version", version=f"newapi-checkin {__version__}")
    return parser


def _split_accounts(values) -> list:
    names: list = []
    for item in values or []:
        names.extend(part.strip() for part in str(item).split(",") if part.strip())
    return names


def _maybe_sync_remote_config(cfg) -> None:
    """签到前同步远程配置；失败只降级用本地配置，绝不中断签到。

    仅在 config_sync.enabled 时请求远程配置管理平台（自建网站 / Gist / 任意 API）。
    同步成功后 config.json 已被覆盖，调用方需重新 load_config()。
    """
    sync_cfg = getattr(cfg, "config_sync", None)
    if sync_cfg is None or not getattr(sync_cfg, "enabled", False):
        log.debug("远程配置同步未启用（config_sync.enabled=false）")
        return
    try:
        from newapi_checkin.remote_sync import sync_remote_config

        result = sync_remote_config()
        if result.get("ok") and not result.get("skipped"):
            log.ok(f"远程配置已同步: {result.get('message', '')}".rstrip())
        elif not result.get("ok"):
            log.warn(f"远程配置同步失败，继续使用本地配置: {result.get('error')}")
        else:
            log.debug("远程配置同步未启用或跳过")
    except Exception as exc:  # noqa: BLE001 - 同步是可降级项，绝不能中断签到
        log.warn(f"远程配置同步异常，继续使用本地配置: {type(exc).__name__}: {exc}")


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    log.setup(verbose=args.verbose, log_dir=LOGS_DIR)

    try:
        cfg = load_config(Path(args.config) if args.config else None)
    except ConfigError as exc:
        log.err(str(exc))
        return 2

    # 远程配置同步（Actions 场景的核心）：拉取成功后重新加载最新配置
    _maybe_sync_remote_config(cfg)
    if getattr(getattr(cfg, "config_sync", None), "enabled", False):
        try:
            cfg = load_config(Path(args.config) if args.config else None)
        except ConfigError as exc:
            log.err(f"远程同步后的配置无效: {exc}")
            return 2

    if cfg.migrated_from is not None:
        log.warn(f"已把旧配置 {cfg.migrated_from.name} 迁移为 config.json，"
                 f"请补上 ai 段落后再启用 AI 辅助")

    # CLI 覆盖配置
    if args.manual and args.headless:
        log.err("--manual 与 --headless 不能同时使用：人工验证需要可交互显示")
        return 2
    if args.manual:
        args.headful = True

    has_display = bool(os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY"))
    if args.headful and sys.platform.startswith("linux") and not has_display:
        log.err(
            "有头浏览器需要 DISPLAY 或 WAYLAND_DISPLAY；"
            "纯命令行 VPS 请使用 --headless，人工验证请先配置 Xvfb/VNC"
        )
        return 2
    if args.headful:
        cfg.browser.headless = False
    elif args.headless:
        cfg.browser.headless = True

    log.debug(f"配置来源: {cfg.source}")
    log.debug(f"浏览器驱动: {cfg.browser.driver}, headless={cfg.browser.headless}, "
              f"humanize={cfg.browser.humanize}")
    log.debug(f"HTTP impersonate: {cfg.http.impersonate}")
    if cfg.ai.enabled:
        log.debug(f"AI: {cfg.ai.model} @ {cfg.ai.chat_url} key={log.mask(cfg.ai.api_key)}")
    else:
        log.debug("AI: 未启用")

    options = RunOptions(
        account_names=_split_accounts(args.account),
        dry_run=args.dry_run,
        headful=args.headful,
        manual=args.manual,
        use_ai=args.use_ai,
        use_browser=args.use_browser,
        verbose=args.verbose,
        # github_cookie 是旧写法，登录方式已并入 tabiai，这里归一化后再交给 Runner
        cookie_test=("tabiai" if args.cookie_test == "github_cookie" else args.cookie_test),
        parallelism=1 if args.manual else (args.parallel or 1),
        # 参数保留兼容；Runner 自动签到统一固定 6 个账号并发。
        parallelism_explicit=args.parallel is not None and not args.manual,
        browser_parallelism=args.browser_parallel or 0,
    )

    try:
        return Runner(cfg, options).run()
    except ConfigError as exc:
        log.err(str(exc))
        return 2
    except KeyboardInterrupt:
        log.warn("已中断")
        return 130


if __name__ == "__main__":
    sys.exit(main())
