"""流程编排：策略链 S0 -> S1 -> S2 -> S3 -> S4 -> S5，任意一级成功即短路。"""

from __future__ import annotations

import os
import threading
import time
from dataclasses import dataclass
from typing import Optional

from . import client as api
from . import logger as log
from .cf.session_store import SessionStore
from .config import SESSIONS_FILE, Account, Config
from .utils import jitter_sleep, probe_exit_ip

STRATEGY_LABEL = {
    "S0": "S0 缓存直连",
    "S1": "S1 指纹直连",
    "S2": "S2 浏览器过盾",
    "S3": "S3 AI 辅助",
    "S4": "S4 浏览器内直发",
    "S5": "S5 人工兜底",
}

# 「盾类」失败：被质询拦住、或拿不到 Turnstile token。
# 这两种都靠「换出口 IP + 重开浏览器」翻盘，所以不限次数地重试，
# 只由 ACCOUNT_DEADLINE_SECONDS 这个时间盒收口。
_SHIELD_RETRYABLE = (api.CF_BLOCKED, api.TURNSTILE_REQUIRED)
# 计次重试的结果：网络层瞬时故障、以及响应看不懂（可能是临时的网关页）
_RETRYABLE = (api.NETWORK_ERROR, api.UNKNOWN) + _SHIELD_RETRYABLE
# 这些结果说明请求本身通了，换策略也不会变，直接结束
_SETTLED = (api.SUCCESS, api.ALREADY_DONE, api.AUTH_FAILED, api.FAILED)

# 未显式指定 --parallel 时的账号级并发数
DEFAULT_ACCOUNT_PARALLELISM = 3
# 账号级并发的硬上限。账号阶段主要在等网络，开大代价很小
MAX_ACCOUNT_PARALLELISM = 16
# 浏览器实例并发的自动推导上限（每个实例约占一个核心）
MAX_BROWSER_PARALLELISM = 4
# 单账号的总时长上限（秒）。盾类失败是「不限次数重试到成功」，必须有一个
# 时间盒兜底，否则一个卡住的账号会占死一个并发位，把整轮拖到 Actions 超时。
ACCOUNT_DEADLINE_SECONDS = 900
# 盾类重试的退避上限（秒）。连续硬刚 Cloudflare 只会让质询更难过
SHIELD_RETRY_BACKOFF_MAX = 30


@dataclass
class RunOptions:
    account_names: Optional[list] = None
    dry_run: bool = False
    headful: bool = False
    manual: bool = False
    use_ai: bool = True
    use_browser: bool = True
    verbose: bool = False
    parallelism: int = 1          # 1 = 未显式指定，run() 里自动提升为默认并发数
    parallelism_explicit: bool = False   # True = 调用方明确要求这个并发数，不再自动提升
    browser_parallelism: int = 0         # 0 = 按 CPU 自动推导；浏览器实例的并发上限


class Runner:
    def __init__(self, cfg: Config, options: RunOptions):
        self.cfg = cfg
        self.options = options
        self.store = SessionStore(SESSIONS_FILE)
        self.summary = log.Summary()
        self._ip_cache: dict = {}
        self._ai = None
        self._ai_ready = False
        self._pool = None
        # 记录「由代理池分配」的代理：手动配置的代理出错时不换，池分配的才换
        self._pooled_proxies: dict[str, str] = {}
        # 并行签到时这些状态会被多个工作线程同时读写
        self._state_lock = threading.RLock()
        self._browser_attempts: dict[str, int] = {}
        self._browser_gate: Optional[threading.Semaphore] = None

    # ------------------------------------------------------------------ #
    # 懒加载资源
    # ------------------------------------------------------------------ #

    def init_proxy_pool(self, desired: Optional[int] = None,
                        accounts: Optional[int] = None) -> None:
        """抓取并测通代理池。启用了代理池就意味着「必须走代理」，不再降级直连。"""
        if not self.cfg.proxy_pool.enabled:
            log.debug("代理池未启用（proxy_pool.enabled=false）")
            return
        try:
            from .proxy_pool import ProxyPool

            self._pool = ProxyPool(self.cfg.proxy_pool)
            count = self._pool.refresh(desired=desired)
            if count:
                log.ok(f"代理池就绪: {count} 个可用代理")
                self._report_proxy_capacity(count, accounts)
            else:
                log.err(f"代理池为空: {self._pool.last_error}；"
                        f"已要求必须走代理，本轮所有账号都会被跳过（不降级直连）")
        except Exception as exc:  # noqa: BLE001 - 初始化异常不能让进程崩掉
            log.err(f"代理池初始化失败: {type(exc).__name__}: {exc}；"
                    f"已要求必须走代理，本轮所有账号都会被跳过（不降级直连）")
            self._pool = None

    def _proxy_required(self) -> bool:
        """启用代理池 = 必须走代理：宁可多个账号共用一个 IP，也不直连。"""
        return bool(self.cfg.proxy_pool.enabled)

    def _report_proxy_capacity(self, count: int, accounts: Optional[int]) -> None:
        """提前说清楚有多少账号能独占 IP、多少要共用。"""
        if not accounts:
            return
        if count >= accounts:
            log.info(f"一账号一 IP：{accounts} 个账号 / {count} 个可用代理，"
                     f"余量 {count - accounts} 个可用于换 IP")
            return
        share = accounts - count
        log.warn(f"可用代理只有 {count} 个，少于 {accounts} 个账号："
                 f"前 {count} 个账号各独占一个 IP，其余 {share} 个会与它们共用 IP"
                 f"（不会降级直连）。想让每个账号都独占就调大 proxy_pool.max_workers "
                 f"以测通更多候选、或增加 sources")

    def _assign_proxy(self, account: Account) -> None:
        """给账号分配代理。手动配置的优先；否则从池里取（用尽时共用，绝不直连）。"""
        if account.proxy:
            log.debug("使用手动配置的代理")
            return
        if self._pool is None:
            return
        proxy = self._pool.acquire()
        if proxy:
            account.proxy = proxy
            with self._state_lock:
                self._pooled_proxies[account.name] = proxy
            log.info(f"已分配代理 {proxy}")
        else:
            log.err("代理池里没有任何可用代理，且已要求必须走代理")

    def _swap_proxy(self, account: Account) -> Optional[str]:
        """换一个新代理。拿不到替代品时保留原代理，绝不清空成直连。"""
        if self._pool is None:
            return None
        with self._state_lock:
            old = self._pooled_proxies.get(account.name)
        if old:
            self._pool.mark_bad(old)
        # 先拿到替代品再切换：拿不到就继续用原来的，宁可重试失败也不直连
        proxy = self._pool.acquire()
        if proxy:
            account.proxy = proxy
            with self._state_lock:
                self._pooled_proxies[account.name] = proxy
            log.warn(f"代理 {old or '<手动>'} 连目标站点失败，换用 {proxy}")
            return proxy
        log.warn(f"代理 {old or '<手动>'} 连目标站点失败，但池里已无其他可用代理，"
                 f"继续用它重试（不降级直连）")
        return None

    def exit_ip(self, proxy: Optional[str]) -> Optional[str]:
        key = proxy or ""
        with self._state_lock:
            if key in self._ip_cache:
                return self._ip_cache[key]
        ip = probe_exit_ip(proxy)
        with self._state_lock:
            self._ip_cache.setdefault(key, ip)
            ip = self._ip_cache[key]
        log.debug(f"出口 IP: {ip or '探测失败（跳过 IP 比对）'}")
        return ip

    def ai(self):
        with self._state_lock:
            if self._ai_ready:
                return self._ai
            self._ai_ready = True
            if not self.options.use_ai:
                log.debug("已通过 --no-ai 禁用 AI 辅助")
                return None
            if not self.cfg.ai.ready:
                log.debug("AI 未配置或不完整，跳过 S3")
                return None
            try:
                from .ai.vision import VisionClient

                self._ai = VisionClient(self.cfg.ai)
                log.debug(f"AI 已就绪: {self.cfg.ai.model} @ {self.cfg.ai.chat_url}")
            except Exception as exc:  # noqa: BLE001 - AI 是可降级项，绝不能中断签到
                log.warn(f"AI 初始化失败，将跳过 S3: {exc}")
                self._ai = None
            return self._ai

    # ------------------------------------------------------------------ #
    # 主循环
    # ------------------------------------------------------------------ #

    def _parallelism(self) -> int:
        """账号级并发数，钳制在 [1, MAX_ACCOUNT_PARALLELISM]；人工模式强制串行。"""
        if self.options.manual:
            return 1
        try:
            workers = int(self.options.parallelism)
        except (TypeError, ValueError):
            workers = 1
        return max(1, min(MAX_ACCOUNT_PARALLELISM, workers))

    def _set_parallelism(self, workers: int) -> None:
        """把传入的并发数写回选项（供 CLI/调度调用方设置）。"""
        self.options.parallelism = max(1, min(MAX_ACCOUNT_PARALLELISM, int(workers)))

    def _browser_workers(self) -> int:
        """浏览器实例的并发上限。

        账号级并发可以开很大——HTTP 快路径几乎不吃 CPU，纯等网络。但浏览器不行：
        每个 Camoufox/Chromium 实例大致要吃掉一个核心和几百 MB 内存，超配之后
        单个实例会被拖慢好几倍，总耗时反而更长。

        所以自动值取「核心数 - 1」（留一个核给 Python 编排和系统），上限 4：
          2 核 -> 1、4 核 -> 3、8 核及以上 -> 4
        """
        accounts_workers = self._parallelism()
        configured = 0
        try:
            configured = int(getattr(self.options, "browser_parallelism", 0) or 0)
        except (TypeError, ValueError):
            configured = 0
        if configured > 0:
            return max(1, min(accounts_workers, configured))
        auto = max(1, min(MAX_BROWSER_PARALLELISM, (os.cpu_count() or 2) - 1))
        return max(1, min(accounts_workers, auto))

    def _take_browser_attempt(self, account: Account) -> bool:
        """记录一次浏览器过盾（只用于日志/统计，不再限次）。

        盾类失败的策略是「换 IP + 重开浏览器，一直试到成功」，所以这里不再拦；
        真正的收口是 _run_account_with_retries 里的时间盒。
        """
        with self._state_lock:
            used = self._browser_attempts.get(account.name, 0) + 1
            self._browser_attempts[account.name] = used
        if used > 1:
            log.debug(f"第 {used} 次启动浏览器过盾")
        return True

    def _should_retry(self, row: log.SummaryRow) -> bool:
        """判断这个结果值不值得再来一轮。"""
        if row.status not in _RETRYABLE:
            return False
        if row.status in _SHIELD_RETRYABLE and not self.options.use_browser:
            # 没有浏览器可用，再试也只会拿到同一个质询页
            return False
        return True

    def run(self) -> int:
        accounts = self.cfg.select(self.options.account_names)
        if not accounts:
            log.warn("没有启用的账号（检查 accounts[].enabled）")
            return 2

        # 未显式指定时用 DEFAULT_ACCOUNT_PARALLELISM（人工模式强制串行 1）
        if self.options.manual:
            self._set_parallelism(1)
        elif (not getattr(self.options, "parallelism_explicit", False)
                and self.options.parallelism in (None, 1, 0)):
            self._set_parallelism(DEFAULT_ACCOUNT_PARALLELISM)

        # 代理池：启用后就必须走代理，拿不到代理的账号会被跳过而不是直连
        # desired 比账号数多 10：留出换 IP 的余量，账号越多目标越大
        self.init_proxy_pool(desired=len(accounts) + 10, accounts=len(accounts))

        mode = "dry-run 连通性检查" if self.options.dry_run else "签到"
        log.step(f"开始{mode}，共 {len(accounts)} 个账号")
        workers = self._parallelism()
        browser_workers = self._browser_workers()
        self._browser_gate = threading.Semaphore(browser_workers)
        if workers <= 1:
            exit_code = self._run_serial(accounts)
        else:
            log.info(f"账号级并行度 {workers}，浏览器并发上限 {browser_workers}")
            exit_code = self._run_parallel(accounts, workers)

        # 邮件通知：无论成败都发；失败只 WARN 不影响退出码
        self._send_notification()
        return exit_code

    def _send_notification(self) -> None:
        """把本轮汇总表以 HTML 邮件发出。未配置/发送失败均降级为 WARN。"""
        email_cfg = self.cfg.notify.email
        if not email_cfg.enabled:
            return
        try:
            from .notify import EmailNotifier, beijing_now, build_report_html, build_subject

            date_str = beijing_now().strftime("%Y-%m-%d")
            beijing_time = beijing_now().strftime("%Y-%m-%d %H:%M")
            subject = build_subject(
                email_cfg.subject_prefix, date_str,
                failed_count=self.summary.failed, dry_run=self.options.dry_run,
            )
            html = build_report_html(
                self.summary.rows,
                date_str=date_str,
                run_context="GitHub Actions" if not self.options.manual else "本地/桌面",
                beijing_time=beijing_time,
                dry_run=self.options.dry_run,
            )
            notifier = EmailNotifier(email_cfg)
            if notifier.send(subject, html):
                log.ok(f"结果邮件已发送到 {len(email_cfg.to_addrs)} 个收件人: {subject}")
            else:
                log.warn("结果邮件发送失败（已降级，不影响签到结果）")
        except Exception as exc:  # noqa: BLE001 - 通知绝不能拖垮签到
            log.warn(f"结果邮件生成异常: {type(exc).__name__}: {exc}")

    def _run_serial(self, accounts: list) -> int:
        total = len(accounts)
        for idx, account in enumerate(accounts, start=1):
            with log.context(account.name):
                log.step(f"{idx}/{total} 开始  ->  {account.base_url}")
            try:
                row = self._run_account(account)
            except KeyboardInterrupt:
                log.warn("收到中断信号，停止后续账号")
                self.store.flush()
                self.summary.render()
                return 130
            except Exception as exc:  # noqa: BLE001 - 单账号异常不能拖垮整轮
                with log.context(account.name):
                    log.err(f"未预期异常: {type(exc).__name__}: {exc}")
                    if self.options.verbose:
                        import traceback

                        log.debug(traceback.format_exc())
                row = log.SummaryRow(account.name, api.UNKNOWN, "-", f"{type(exc).__name__}: {exc}")
            self.summary.add(row)
            self.store.flush_throttled()
            if idx < total:
                delay = jitter_sleep(self.cfg.defaults.interval_seconds)
                log.debug(f"账号间隔停顿 {delay:.1f}s")

        self.store.flush()
        self.summary.render()
        return 0 if self.summary.failed == 0 else 1

    def _run_parallel(self, accounts: list, workers: int) -> int:
        """并行跑账号，谁先结束谁先出结果。

        以前是按提交顺序逐个 future.result()：第一个账号不结束，后面全部账号的
        进度都不打印，看起来像卡死，也没法及早把结果落盘。汇总表仍按配置顺序。
        """
        from concurrent.futures import ThreadPoolExecutor, as_completed

        total = len(accounts)
        rows: dict = {}
        with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="checkin") as pool:
            futures = {}
            for idx, account in enumerate(accounts, start=1):
                with log.context(account.name):
                    log.step(f"{idx}/{total} 已提交  ->  {account.base_url}")
                futures[pool.submit(self._run_account, account)] = account
            done = 0
            for future in as_completed(futures):
                account = futures[future]
                done += 1
                try:
                    row = future.result()
                except KeyboardInterrupt:
                    log.warn("收到中断信号，停止等待后续账号")
                    for pending in futures:
                        pending.cancel()
                    self.store.flush()
                    self.summary.render()
                    return 130
                except Exception as exc:  # noqa: BLE001 - 单账号异常不能拖垮整轮
                    with log.context(account.name):
                        log.err(f"未预期异常: {type(exc).__name__}: {exc}")
                        if self.options.verbose:
                            import traceback

                            log.debug(traceback.format_exc())
                    row = log.SummaryRow(account.name, api.UNKNOWN, "-",
                                         f"{type(exc).__name__}: {exc}")
                rows[id(account)] = row
                with log.context(account.name):
                    log.step(f"{done}/{total} 结束: "
                             f"{log.STATUS_LABEL.get(row.status, row.status)}")
                self.store.flush_throttled()

        self.store.flush()
        for account in accounts:
            row = rows.get(id(account))
            if row is not None:
                self.summary.add(row)
        self.summary.render()
        return 0 if self.summary.failed == 0 else 1

    # ------------------------------------------------------------------ #
    # 单账号
    # ------------------------------------------------------------------ #

    def _run_account(self, account: Account) -> log.SummaryRow:
        """账号入口。整段执行都带上账号标签，避免并行时几个账号的日志混在一起。"""
        with log.context(account.name):
            return self._checkin_account(account)

    def _checkin_account(self, account: Account) -> log.SummaryRow:
        if not account.cookie:
            log.warn("缺少 cookie，跳过（浏览器里复制完整 Cookie 到配置）")
            return log.SummaryRow(account.name, "skipped", "-", "缺少 cookie")

        record = self.store.get(account.slug)
        if account.user_id is None and record.user_id:
            account.user_id = record.user_id
            log.debug(f"复用缓存 user_id={record.user_id}")
        if not account.checkin_path and record.checkin_path:
            account.checkin_path = record.checkin_path
            log.debug(f"复用缓存签到路径 {record.checkin_path}")

        # 一个账号一个 IP：先从代理池分配（手动配置的代理优先，不改动）
        self._assign_proxy(account)
        if self._proxy_required() and not account.proxy:
            # 启用代理池就意味着不许直连；拿不到任何代理时跳过而不是暴露真实 IP
            log.err("没有任何可用代理，跳过该账号（proxy_pool.enabled=true 不允许直连）")
            return log.SummaryRow(account.name, "skipped", "-",
                                  "无可用代理，按配置不降级直连")
        with self._state_lock:
            self._browser_attempts[account.name] = 0

        return self._run_account_with_retries(account, record)

    def _swap_pooled_proxy(self, account: Account) -> bool:
        """只换「由代理池分配」的代理；手动配置的代理原样保留。"""
        with self._state_lock:
            from_pool = account.name in self._pooled_proxies
        if not from_pool:
            return False
        return self._swap_proxy(account) is not None

    def _run_account_with_retries(self, account: Account, record) -> log.SummaryRow:
        """跑完一个账号。三类失败三种策略：

        1. 盾类（被质询拦住 / 拿不到 Turnstile token）——换出口 IP + 重开浏览器，
           **不限次数**试到成功，只受 ACCOUNT_DEADLINE_SECONDS 时间盒约束。
           AI 请求失败最终也表现为盾没过，所以自动落进这一类。
        2. 网络层失败（代理连不上目标站点）——换 IP 立即重试，
           次数由 proxy_pool.ip_swap_limit 单独限（默认 5），不占重试次数。
        3. 其余可重试结果（响应看不懂）——按 defaults.retry 计次 + 指数退避。
        """
        deadline = time.monotonic() + ACCOUNT_DEADLINE_SECONDS
        attempts = max(1, self.cfg.defaults.retry + 1)
        swaps_left = self.cfg.proxy_pool.ip_swap_limit if self._pool else 0
        row: Optional[log.SummaryRow] = None
        used = 0                 # 已消耗的计次重试（不含换 IP、不含盾类重试）
        shield_rounds = 0
        skip_backoff = False
        while True:
            if used > 0 and not skip_backoff:
                backoff = min(8, 2 ** used)
                log.info(f"第 {used + 1}/{attempts} 次尝试，退避 {backoff}s")
                time.sleep(backoff)
            skip_backoff = False

            row = self._attempt(account, record)

            # 1) 网络层失败：换 IP 立即重试，不计入重试次数
            if row.status == api.NETWORK_ERROR and swaps_left > 0:
                if self._swap_pooled_proxy(account):
                    swaps_left -= 1
                    skip_backoff = True
                    log.warn(f"网络异常，已换 IP 立即重试（不计入重试次数，"
                             f"剩余换 IP 次数 {swaps_left}）")
                    continue

            # 2) 盾类失败：换 IP + 重开浏览器，一直试到成功或时间盒用尽
            if row.status in _SHIELD_RETRYABLE and self._should_retry(row):
                left = deadline - time.monotonic()
                if left <= 0:
                    log.err(f"盾类重试已用满 {ACCOUNT_DEADLINE_SECONDS}s 时间盒，"
                            f"共尝试 {shield_rounds + 1} 轮，放弃该账号")
                    return row
                shield_rounds += 1
                swapped = self._swap_pooled_proxy(account)
                backoff = min(SHIELD_RETRY_BACKOFF_MAX, 5 * shield_rounds)
                backoff = min(backoff, max(0.0, left - 1))
                label = log.STATUS_LABEL.get(row.status, row.status)
                log.warn(f"{label}：第 {shield_rounds} 轮重试"
                         + ("（已换出口 IP）" if swapped else "（无新 IP 可换，沿用当前 IP）")
                         + f"，退避 {backoff:.0f}s，剩余时间盒 {left:.0f}s")
                if backoff > 0:
                    time.sleep(backoff)
                skip_backoff = True
                continue

            # 3) 其余可重试结果：按 defaults.retry 计次
            if not self._should_retry(row):
                return row
            used += 1
            if used >= attempts:
                if row.status == api.NETWORK_ERROR:
                    log.warn(f"已用满 {attempts} 次尝试"
                             + ("（换 IP 次数也已用尽）" if self._pool else ""))
                return row

    def _attempt(self, account: Account, record) -> log.SummaryRow:
        ip = self.exit_ip(account.proxy)

        # ---------------- S0 缓存直连 ----------------
        if record.cf is not None:
            usable, reason = record.cf.check(ip, account.proxy)
            log.debug(f"S0 缓存判定: {reason}")
            if usable:
                result = self._api_call(account, record.cf)
                if result.kind in _SETTLED:
                    return self._row(account, result, "S0")
                if result.kind == api.CF_BLOCKED:
                    log.warn("缓存的 cf_clearance 已被拒绝，作废后重新过盾")
                    self.store.clear_cf(account.slug)
                    record.cf = None
            else:
                self.store.clear_cf(account.slug)
                record.cf = None

        # ---------------- S1 纯指纹直连 ----------------
        result = self._api_call(account, None)
        if result.kind in _SETTLED:
            return self._row(account, result, "S1")
        if result.kind == api.NETWORK_ERROR:
            return self._row(account, result, "S1")

        verdict = result.verdict
        if result.kind == api.CF_BLOCKED:
            log.warn(f"命中 Cloudflare: {result.message}")
            if verdict is not None and not verdict.recoverable:
                # WAF 硬封禁不是质询，浏览器也过不去，重试只会加重风控
                result.kind = api.WAF_BLOCKED
                return self._row(
                    account, result, "S1",
                    detail=f"{verdict.label}：脚本无法绕过，需要换出口 IP 或联系站点放行",
                )
        else:
            log.warn(f"响应异常，尝试用浏览器复核: {result.message}")

        if not self.options.use_browser:
            return self._row(account, result, "S1", detail="已禁用浏览器过盾（--no-browser）")

        self._take_browser_attempt(account)
        return self._solve(account, record, ip, result)

    # ------------------------------------------------------------------ #
    # 浏览器过盾（S2 / S3 / S4 / S5）
    # ------------------------------------------------------------------ #

    def _solve_guarded(self, solve, account: Account, ip: Optional[str]):
        """在浏览器并发信号量的保护下调用过盾流程。"""
        ai = self.ai()

        def _call():
            return solve(
                cfg=self.cfg,
                account=account,
                exit_ip=ip,
                options=self.options,
                ai=ai,
            )

        def _call_bound():
            """AI 请求也走该账号的代理，别让视觉调用泄露真实出口 IP。"""
            binder = getattr(ai, "use_proxy", None)
            if binder is None or not account.proxy:
                return _call()
            with binder(account.proxy):
                return _call()

        gate = self._browser_gate
        if gate is None:
            return _call_bound()
        started = time.monotonic()
        with gate:
            waited = time.monotonic() - started
            if waited > 1.0:
                log.debug(f"等待浏览器并发配额 {waited:.1f}s")
            return _call_bound()

    def _solve(self, account: Account, record, ip: Optional[str],
               blocked: api.ApiResult) -> log.SummaryRow:
        try:
            from .cf.solver import solve
        except ImportError as exc:
            log.err(f"浏览器过盾模块不可用: {exc}")
            log.info("安装依赖: pip install -r requirements.txt && python -m camoufox fetch")
            return self._row(account, blocked, "S1", detail=f"过盾模块缺失: {exc}")

        outcome = self._solve_guarded(solve, account, ip)

        if outcome.cf is not None:
            self.store.update_cf(account.slug, outcome.cf)
            record.cf = outcome.cf

        if not outcome.ok:
            status = outcome.result_kind
            if status is None:
                status = api.WAF_BLOCKED if outcome.terminal else api.CF_BLOCKED
            row = log.SummaryRow(
                account.name,
                status,
                STRATEGY_LABEL.get(outcome.strategy, outcome.strategy),
                outcome.detail or blocked.message,
            )
            log.err(f"过盾失败: {row.detail}")
            return row

        log.ok(f"过盾成功（{STRATEGY_LABEL.get(outcome.strategy, outcome.strategy)}）"
               + (f" - {outcome.detail}" if outcome.detail else ""))

        # S4：签到已在浏览器上下文里完成，直接用它的结果
        if outcome.api_result is not None:
            if outcome.api_result.kind in (api.SUCCESS, api.ALREADY_DONE):
                self.store.remember(account.slug, checkin_path=outcome.api_result.path,
                                    user_id=outcome.api_result.user_id)
            return self._row(account, outcome.api_result, outcome.strategy)

        # S2/S3：拿到 cookie，回到快路径重发
        result = self._api_call(account, outcome.cf)
        return self._row(account, result, outcome.strategy)

    # ------------------------------------------------------------------ #
    # 单次 API 调用
    # ------------------------------------------------------------------ #

    def _api_call(self, account: Account, cf) -> api.ApiResult:
        with api.ApiClient(account, self.cfg.http, cf) as client:
            log.debug(
                f"impersonate={client.impersonate}, UA="
                + ("缓存 UA" if cf is not None and cf.user_agent else "impersonate 默认")
                + (f", cookie 条数={len(cf.cookies)}" if cf is not None else "")
            )
            if self.options.dry_run or not client.user_id:
                result = client.fetch_self()
                if not result.ok:
                    return result
                if result.user_id:
                    account.user_id = result.user_id
                    self.store.remember(account.slug, user_id=result.user_id)
                    log.ok(f"用户 id={result.user_id}"
                           + (f" ({result.message})" if result.message else ""))
                if self.options.dry_run:
                    return api.ApiResult(
                        api.SUCCESS, path=result.path, status=result.status,
                        user_id=result.user_id,
                        message=f"dry-run 连通正常（id={result.user_id}，未执行签到）",
                    )

            result = client.checkin()
            if result.kind in (api.SUCCESS, api.ALREADY_DONE) and result.path:
                self.store.remember(account.slug, checkin_path=result.path)
            return result

    def _row(self, account: Account, result: api.ApiResult, strategy: str,
             detail: Optional[str] = None) -> log.SummaryRow:
        text = detail or result.message or "-"
        if result.kind == api.SUCCESS:
            log.ok(f"{text}" + (f"，获得额度 {result.quota}" if result.quota else ""))
        elif result.kind == api.ALREADY_DONE:
            log.ok(f"今日已签到：{text}")
        elif result.kind in (api.AUTH_FAILED,):
            log.err(f"认证失败：{text}（cookie 可能已过期，重新复制）")
        elif result.kind == api.NETWORK_ERROR:
            log.err(f"网络异常：{text}")
        else:
            log.err(f"{log.STATUS_LABEL.get(result.kind, result.kind)}：{text}")
        if result.path:
            log.debug(f"命中接口 {result.path}，HTTP {result.status}")
        return log.SummaryRow(
            account.name, result.kind, STRATEGY_LABEL.get(strategy, strategy), text, result.quota
        )
