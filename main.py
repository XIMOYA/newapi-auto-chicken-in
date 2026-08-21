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
    python main.py --shard-plan 30    只算分片名单：每 30 个账号一片，写 data/shard-plan.json
    python main.py -v                 详细日志

退出码：0 全部成功 / 1 有失败 / 2 配置错误 / 130 被中断
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

from newapi_checkin import __version__
from newapi_checkin import logger as log
from newapi_checkin.config import LOGS_DIR, ConfigError, load_config
from newapi_checkin.runner import RunOptions, Runner

# 分片计划的落盘位置。写文件而不是打到 stdout：日志走 rich Console（也是 stdout），
# 混在一起 workflow 没法干净地把 JSON 取出来。
SHARD_PLAN_PATH = Path("data") / "shard-plan.json"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="main.py",
        description="New API 自动签到（Cloudflare 过盾 + AI 视觉辅助）",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--config", metavar="PATH", help="指定配置文件路径，默认 config.json")
    parser.add_argument("--account", action="append", metavar="NAME",
                        help="只跑指定账号，可重复传入或用逗号分隔")
    parser.add_argument(
        "--shard-plan", type=int, metavar="SIZE",
        help="不签到：把启用账号每 SIZE 个切一片，写出 GitHub Actions matrix 用的 "
             f"{SHARD_PLAN_PATH}，供分片并行的前置 job 读取",
    )
    parser.add_argument(
        "--shard", metavar="I/N",
        help="告诉平台自己是第 I 个分片（共 N 片），只领属于本片的代理；"
             "几个 job 并行时避免同一个出口 IP 被分给多个账号",
    )
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


def _write_shard_plan(cfg, args, size: int) -> int:
    """把启用账号每 size 个切一片，写出 Actions matrix 能直接吃的 JSON。

    走的是和签到完全一样的配置加载路径（含远程同步），否则平台上刚加的账号会被漏掉，
    或者刚停用的账号还占着一个分片。

    账号名不能含逗号：分片是靠 `--account A,B,C` 传给各个 job 的，而 --account 本来
    就按逗号切，含逗号的名字会在传递途中被拆成两个不存在的账号。这里提前拦下来，
    比让某个 job 在半夜报「找不到账号」好。
    """
    if size <= 0:
        log.err(f"--shard-plan 需要正整数，收到 {size}")
        return 2
    try:
        accounts = cfg.select(_split_accounts(args.account))
    except ConfigError as exc:
        log.err(str(exc))
        return 2

    names = [a.name for a in accounts]
    bad = [n for n in names if "," in n]
    if bad:
        log.err(f"账号名不能包含逗号（分片按逗号传递）: {', '.join(bad)}")
        return 2

    shards = [
        {"index": i + 1, "accounts": ",".join(names[i * size:(i + 1) * size])}
        for i in range((len(names) + size - 1) // size)
    ]
    plan = {"count": len(names), "total": len(shards), "shards": shards}
    SHARD_PLAN_PATH.parent.mkdir(parents=True, exist_ok=True)
    SHARD_PLAN_PATH.write_text(json.dumps(plan, ensure_ascii=False), encoding="utf-8")
    if not names:
        log.warn(f"没有启用的账号，分片计划为空（已写入 {SHARD_PLAN_PATH}）")
    else:
        log.ok(f"分片计划已写入 {SHARD_PLAN_PATH}：{len(names)} 个启用账号 "
               f"-> {len(shards)} 个分片（每片最多 {size} 个）")
    return 0


def _parse_shard(raw) -> tuple:
    """解析 --shard I/N。返回 (序号, 总片数)；没传返回 None。

    格式做窄：只认「正整数/正整数」且序号在范围内。写错不静默忽略 —— 那会让这个 job
    以为自己领到的是独占的一批代理，实际上和别的 job 撞了，问题要在启动时就暴露。
    """
    if raw is None:
        return None
    text = str(raw).strip()
    left, _, right = text.partition("/")
    try:
        index, total = int(left.strip()), int(right.strip())
    except ValueError:
        raise ConfigError(f"--shard 需要 I/N 形式的正整数，收到 {raw!r}") from None
    if total < 1:
        raise ConfigError(f"--shard 的总片数必须 >= 1，收到 {total}")
    if index < 1 or index > total:
        raise ConfigError(f"--shard 的序号必须在 1..{total} 之间，收到 {index}")
    return index, total


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

    # 分片号先解析：写错就没必要往下走了，早失败比跑到一半发现代理领重了好
    try:
        shard = _parse_shard(args.shard)
    except ConfigError as exc:
        log.err(str(exc))
        return 2

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

    # 分片计划：只算名单不签到，放在远程同步之后，保证与真正开跑时看到的是同一份账号
    if args.shard_plan is not None:
        return _write_shard_plan(cfg, args, args.shard_plan)

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
        proxy_shard=shard,
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
