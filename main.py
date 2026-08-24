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
    python main.py --summary-out data/summary/shard-1.json  本片结果落盘，且不自己发邮件
    python main.py --send-summary data/summary   合并各片结果，只发一封汇总邮件
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
        help="本进程是第 I 个分片（共 N 片）：只跑分到自己名下的那批账号，"
             "并只向平台领属于本片的代理。几个 job 并行时用它切分，"
             "避免重复签到与同一出口 IP 被多个账号共用",
    )
    parser.add_argument(
        "--summary-out", metavar="PATH",
        help="签到完把本片汇总结果写成 JSON 到 PATH，并且**不再由本进程发邮件**；"
             "配合 --send-summary 让多个分片只发一封合并后的邮件",
    )
    parser.add_argument(
        "--send-summary", metavar="DIR",
        help="不签到：递归读 DIR 下各分片的结果 JSON，合并成一封邮件发出。"
             "缺片会在邮件里明确标注，不会静默少人",
    )
    parser.add_argument(
        "--proxy-sweep", action="store_true",
        help="不签到：把平台上的存活代理从本机视角全量实测一遍，把成败回传平台供优选"
             "排序。平台自测用的是服务器出口，Actions 的出口未必一样，所以要单独跑",
    )
    parser.add_argument(
        "--proxy-sweep-minutes", type=int, default=50, metavar="N",
        help="--proxy-sweep 的时间盒（分钟，默认 50）。到点带着已测出的结论收工，"
             "没轮到的不记账",
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


def _shard_account_names(cfg, args, shard: tuple) -> list:
    """按分片切出本 job 该跑的账号名。

    切法是连续块：先按 --account（若给了）过滤，再把剩下的均分成 N 份取第 I 份，
    块大小 = ceil(总数 / N)。各片不重叠、合起来覆盖全部。

    为什么不让 plan job 把名单直接发下来：账号名来自 secret 解出的配置，GitHub 会判定
    job output「可能含 secret」而**整个跳过**该 output，下游 fromJson('') 就报
    empty input。所以 output 里只放数字，名单由各 job 在内部自己算。

    这也意味着 plan 与各 job 的切法不必一致 —— plan 只负责算出「要开几个 job」，
    只要这里切出来的各片不重叠且覆盖全部，结果就是对的。
    """
    index, total = shard
    names = [a.name for a in cfg.select(_split_accounts(args.account))]
    if total <= 1 or not names:
        return names
    chunk = -(-len(names) // total)  # 向上取整
    return names[(index - 1) * chunk: index * chunk]


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


def _pricing_proxy_provider(cfg):
    """汇总发信时拉定价用的取代理回调。

    这一步跑在独立的 notify job 里，没有签到时那份已 refresh 的代理池，所以自己
    建一个。只要少量代理够用（定价接口每个站点就拉一次），desired 给小值免得为了
    一封邮件把整池探一遍。代理池没启用或一个都探不到就返回 None，直连去拉。
    """
    pool_cfg = getattr(cfg, "proxy_pool", None)
    if pool_cfg is None or not getattr(pool_cfg, "enabled", False):
        return None
    try:
        from newapi_checkin.proxy_pool import ProxyPool

        pool = ProxyPool(pool_cfg)
        if pool.refresh(desired=5) <= 0:
            log.debug("代理池没探到可用代理，定价表改为直连拉取")
            return None
    except Exception as exc:  # noqa: BLE001 - 代理是可降级项
        log.debug(f"汇总发信初始化代理池失败，改直连: {type(exc).__name__}: {exc}")
        return None

    def provider(bad=None):
        if bad:
            pool.mark_bad(bad, "net")
        return pool.acquire()

    return provider


def _merged_quota_overview(cfg, rows: list) -> str:
    """汇总发信时的额度总览。定价拉不到就返回空串，邮件正文照发。

    分片各自只看得见自己那几个账号，按站点合并余额这件事只能在这里做 —— 这也是
    汇总 job 存在的意义之一。
    """
    from newapi_checkin.notify import build_quota_overview
    from newapi_checkin.pricing import summarize_by_site

    try:
        sites = summarize_by_site(rows, cfg.http,
                                  proxy_provider=_pricing_proxy_provider(cfg))
        return build_quota_overview(sites)
    except Exception as exc:  # noqa: BLE001 - 总览是附加信息，不能拖垮发信
        log.debug(f"额度总览生成失败，本封邮件省略: {type(exc).__name__}: {exc}")
        return ""


def _sweep_proxies(cfg, minutes: int) -> int:
    """全量体检平台上的存活代理，把成败回传平台。不签到、不碰浏览器。

    单独跑一趟的理由：平台自己的刷新和测速走的是**服务器出口**，而签到跑在 GitHub
    Actions 上。代理商封机房 IP 段是常事，服务器那边通的代理到了 Actions 手里可能全是
    废的。这趟体检让平台的优选顺序反映「Actions 用起来好不好」，紧接着的签到就能直接
    受益。
    """
    from newapi_checkin.proxy_pool import ProxyPool

    if not cfg.proxy_pool.enabled:
        log.err("proxy_pool.enabled 为 false，没有代理池可体检")
        return 2
    if not cfg.proxy_pool.remote_url:
        log.err("proxy_pool.remote_url 未配置：体检的对象是平台上的代理，必须能拉到它")
        return 2

    pool = ProxyPool(cfg.proxy_pool)
    stats = pool.sweep_remote(minutes)
    if not stats.get("total"):
        log.err(f"没有可体检的代理：{stats.get('reason', '未知原因')}")
        return 1

    ok, tested, total = stats["ok"], stats["tested"], stats["total"]
    rate = (ok / tested * 100) if tested else 0.0
    log.info(f"体检完成：测了 {tested}/{total} 条，通 {ok} 条、不通 {stats['fail']} 条"
             f"（可用率 {rate:.0f}%，耗时 {stats.get('elapsed', 0):.0f}s）")

    # 回传是这趟的唯一产出，失败要明确报错 —— 不回传的话平台的排序不会有任何变化，
    # 整趟体检就白跑了
    sent, detail = pool.report_feedback(source=_run_source())
    if not sent:
        log.err(f"实测结果回传平台失败：{detail}")
        return 1
    log.ok(f"实测结果已回传平台（{detail}）")
    return 0


def _run_source() -> str:
    """回传时标注来源，方便在平台日志里区分是哪个环境测的。"""
    repo = os.environ.get("GITHUB_REPOSITORY")
    if repo:
        return f"github-actions（{repo}）"
    import socket

    return socket.gethostname() or "proxy-sweep"


def _send_merged_report(cfg, directory) -> int:




    """把各分片落盘的结果合并成一封邮件发出（Actions 汇总 job 的入口）。

    退出码只反映「邮件有没有发出去」：缺片不算这一步的错，缺的那个分片 job 自己
    已经是红的了，这里只负责把缺片写进邮件正文，不再重复报警。
    """
    from newapi_checkin.notify import build_quota_overview, send_report
    from newapi_checkin.pricing import summarize_by_site
    from newapi_checkin.shard_report import merge_shard_summaries

    merged = merge_shard_summaries(directory)
    if not merged.rows:
        log.warn("没有任何可汇总的账号结果，跳过发信")
        return 0

    # 汇总日志里也打一遍完整表格：邮件万一发失败，结果还能在 Actions 日志里查到
    summary = log.Summary()
    summary.rows.extend(merged.rows)
    summary.render()
    if merged.complete:
        log.ok(f"已合并 {merged.expected} 个分片、共 {len(merged.rows)} 个账号的结果")
    else:
        log.warn(f"预期 {merged.expected} 片，实到 {len(merged.present)} 片"
                 f"（缺第 {'、'.join(str(i) for i in merged.missing)} 片）")

    email_cfg = cfg.notify.email
    if not email_cfg.enabled:
        log.warn("邮件通知未启用（notify.email.enabled=false），只打印汇总不发信")
        return 0
    sent, subject = send_report(
        email_cfg, merged.rows,
        dry_run=merged.dry_run,
        run_context="GitHub Actions",
        extra_note=merged.note(),
        # 缺片是「上表不是全部账号」，必须显眼；齐了就只是背景说明
        note_level="info" if merged.complete else "warn",
        quota_overview=_merged_quota_overview(cfg, merged.rows),
    )
    if sent:
        log.ok(f"汇总邮件已发送到 {len(email_cfg.to_addrs)} 个收件人: {subject}")
        return 0
    log.err("汇总邮件发送失败")
    return 1


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

    # 汇总发信：只读各片落盘的结果拼一封邮件，不签到也不碰浏览器。
    # 同样放在远程同步之后 —— SMTP 配置也可能是从远程配置平台拉下来的
    if args.send_summary:
        return _send_merged_report(cfg, args.send_summary)

    # 代理全量体检：只测代理并回传，不签到。放在远程同步之后，保证 remote_url 和
    # 令牌用的都是平台上的最新值
    if args.proxy_sweep:
        return _sweep_proxies(cfg, args.proxy_sweep_minutes)

    # 按分片挑出本进程该跑的账号。放在远程同步之后：平台上刚加的账号要能被切进来
    shard_names = None
    if shard is not None:
        try:
            shard_names = _shard_account_names(cfg, args, shard)
        except ConfigError as exc:
            log.err(str(exc))
            return 2
        if not shard_names:
            # 分片数多于账号数时靠后的片会是空的。空名单不能往下传：account_names
            # 为空等于「不过滤」，会让这个 job 把所有账号又跑一遍
            log.warn(f"第 {shard[0]}/{shard[1]} 片没有分到账号，本次无需签到")
            return 0
        log.info(f"分片 {shard[0]}/{shard[1]}：本片 {len(shard_names)} 个账号")

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
        account_names=shard_names if shard_names is not None else _split_accounts(args.account),
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
        summary_out=args.summary_out,
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
