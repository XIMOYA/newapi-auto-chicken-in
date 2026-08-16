"""流程编排：策略链 S0 -> S1 -> S2 -> S3 -> S4 -> S5，任意一级成功即短路。"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass
from typing import Optional

from . import client as api
from . import logger as log
from .cf.session_store import SessionStore
from .config import (
    LOGIN_METHOD_GITHUB_COOKIE,
    SESSIONS_FILE,
    Account,
    Config,
)
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
# 源站业务失败/WAF 硬封禁可能是出口 IP 被源站临时风控，允许有限换 IP 后再判定。
SOURCE_IP_SWAP_LIMIT = 5
SOURCE_IP_SWAP_BACKOFF_SECONDS = 5
_SOURCE_IP_RETRYABLE = (api.FAILED, api.WAF_BLOCKED)
# 源站/凭据/环境已经给出不可恢复结论：不再浪费重试，直接把账号标记为跳过。
_SKIP_ON_FAILURE = (
    api.AUTH_FAILED,
    api.LOGIN_REQUIRED,
    api.UNKNOWN,
)
# 这些结果说明请求本身通了，换策略也不会变，直接结束
_SETTLED = (api.SUCCESS, api.ALREADY_DONE, api.AUTH_FAILED, api.FAILED)

# 自动签到固定账号级并发：HTTP 快路径主要等待网络，统一保持 4 个账号并发。
# --parallel / 调度配置仍保留兼容字段，但实际运行不再按调用方或 CPU 动态调整。
DEFAULT_ACCOUNT_PARALLELISM = 4
# 账号级并发的历史硬上限（保留兼容，实际运行固定使用 DEFAULT_ACCOUNT_PARALLELISM）。
MAX_ACCOUNT_PARALLELISM = 16
# 浏览器实例固定并发：最多同时开 2 个 Camoufox，不再按 CPU 核数推导。
FIXED_BROWSER_PARALLELISM = 2
MAX_BROWSER_PARALLELISM = FIXED_BROWSER_PARALLELISM
# 单账号的总时长上限（秒）。盾类失败是「不限次数重试到成功」，必须有一个
# 时间盒兜底，否则一个卡住的账号会占死一个并发位，把整轮拖到 Actions 超时。
ACCOUNT_DEADLINE_SECONDS = 1200
# 盾类重试的退避上限（秒）。连续硬刚 Cloudflare 只会让质询更难过
SHIELD_RETRY_BACKOFF_MAX = 30
# 出口 IP 探测结果缓存的上限。代理池在换 IP 时地址是有限的，但 daemon 长跑
# 或手动配置频繁变更时不该让这个 dict 无界增长，超出后丢弃最早的一条。
IP_CACHE_MAX = 256


@dataclass
class RunOptions:
    account_names: Optional[list] = None
    dry_run: bool = False
    headful: bool = False
    manual: bool = False
    use_ai: bool = True
    use_browser: bool = True
    verbose: bool = False
    cookie_test: Optional[str] = None  # 仅检查指定登录方式的 Cookie，不执行签到
    parallelism: int = 1          # 兼容调用方字段；自动签到实际固定为 6，人工模式为 1
    parallelism_explicit: bool = False   # 兼容字段；固定并发模式下不改变实际账号并发
    browser_parallelism: int = 0         # 兼容调用方字段；浏览器实际固定为 2（人工模式为 1）


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
                        f"已要求必须走代理，没有自带代理的账号都会被跳过"
                        f"（配了 accounts[].proxy 的账号照常执行，也不降级直连）")
        except Exception as exc:  # noqa: BLE001 - 初始化异常不能让进程崩掉
            log.err(f"代理池初始化失败: {type(exc).__name__}: {exc}；"
                    f"已要求必须走代理，没有自带代理的账号都会被跳过"
                    f"（配了 accounts[].proxy 的账号照常执行，也不降级直连）")
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
                     f"多出的 {count - accounts} 个是所有账号共享的换 IP 备用池"
                     f"（网络异常时持续换 IP，直到池里没有新的可用 IP）")
            return
        share = accounts - count
        log.warn(f"可用代理只有 {count} 个，少于 {accounts} 个账号："
                 f"前 {count} 个账号各独占一个 IP，其余 {share} 个会与它们共用 IP"
                 f"（不会降级直连；网络异常时持续换 IP，直到没有新的可用 IP）。代理来自服务器"
                 f"预取时，数量由服务端 proxy_pool.save_limit 决定（0 = 不限制）；本地"
                 f"抓取时可调大 proxy_pool.max_workers 或增加 sources")

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
            # 缓存有界：超出上限时丢弃最早的一条，避免长跑进程里无界增长
            if len(self._ip_cache) >= IP_CACHE_MAX and key not in self._ip_cache:
                self._ip_cache.pop(next(iter(self._ip_cache)))
            self._ip_cache.setdefault(key, ip)
            ip = self._ip_cache[key]
        log.debug(f"出口 IP: {ip or '探测失败（跳过 IP 比对）'}")
        return ip

    def _close_ai(self) -> None:
        """释放懒加载创建的 AI 客户端（关闭各线程的 curl session）。

        AI 是可选降级项，关闭失败不影响退出码；run() 结束后调用，
        避免 daemon 长跑或频繁轮次里泄漏 session 资源。
        """
        with self._state_lock:
            ai = self._ai
            self._ai = None
        if ai is None:
            return
        try:
            ai.close()
        except Exception as exc:  # noqa: BLE001 - 关闭失败不影响退出码
            log.debug(f"关闭 AI 客户端失败: {type(exc).__name__}: {exc}")

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
                # AI 请求也走代理：启用代理池时强制走，且换 IP 不限次数
                self._ai.set_proxy_source(self._acquire_proxy_for_ai,
                                          require=self._proxy_required(),
                                          on_failed=self._ai_proxy_failed)
                log.debug(f"AI 已就绪: {self.cfg.ai.model} @ {self.cfg.ai.chat_url}")
            except Exception as exc:  # noqa: BLE001 - AI 是可降级项，绝不能中断签到
                log.warn(f"AI 初始化失败，将跳过 S3: {exc}")
                self._ai = None
            return self._ai

    def _acquire_proxy_for_ai(self) -> Optional[str]:
        """给 AI 请求要一个代理。池子用尽时会返回共用的 IP，不会返回 None。"""
        if self._pool is None:
            return None
        return self._pool.acquire()

    def _ai_proxy_failed(self, proxy: Optional[str]) -> None:
        """AI 彻底放弃了某个代理，决定要不要把它从池里拉黑。

        AI 端点和签到目标站点是两个不同的域名，连不上前者不代表连不上
        后者。所以只拉黑「没有账号在用」的 IP：
        - 该 IP 正绑在某个账号上 -> 它连目标站点是通的，拉黑会把那个账号
          的代理一起废掉，属于误伤，宁可让 AI 换下一个。
        - 没人在用（AI 自己 acquire 来的）-> 真拉黑。否则它既不在 _used
          的空闲候选里、又不在 _bad 里，池子用尽时还会被当共用候选分给
          别的账号，等于明知连不通还往外发。
        """
        if self._pool is None or not proxy:
            return
        with self._state_lock:
            in_use = proxy in self._pooled_proxies.values()
        if in_use:
            log.debug(f"AI 经 {proxy} 请求失败，但该 IP 正被账号使用，不拉黑")
            return
        self._pool.mark_bad(proxy)
        log.debug(f"AI 经 {proxy} 请求失败且无账号在用，已从池中拉黑")

    # ------------------------------------------------------------------ #
    # 主循环
    # ------------------------------------------------------------------ #

    def _parallelism(self) -> int:
        """自动签到固定 4 个账号并发；人工模式仍强制串行。"""
        if self.options.manual:
            return 1
        return DEFAULT_ACCOUNT_PARALLELISM

    def _set_parallelism(self, workers: int) -> None:
        """把固定并发写回选项；保留参数仅为兼容 CLI/调度调用方。"""
        self.options.parallelism = max(1, min(MAX_ACCOUNT_PARALLELISM, int(workers)))

    def _browser_workers(self) -> int:
        """返回固定的浏览器实例并发上限，不再按 CPU 核数计算。"""
        if self.options.manual:
            return 1
        return min(self._parallelism(), FIXED_BROWSER_PARALLELISM)

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
        if self.options.cookie_test:
            accounts = [a for a in accounts if a.login_method == self.options.cookie_test]
            if not accounts:
                log.warn(f"没有匹配 {self.options.cookie_test} 的启用账号")
                return 2
        if not accounts:
            log.warn("没有启用的账号（检查 accounts[].enabled）")
            return 2

        # 自动签到固定 4 个账号并发；人工模式强制串行 1。
        self._set_parallelism(1 if self.options.manual else DEFAULT_ACCOUNT_PARALLELISM)

        # 代理池：启用后就必须走代理，拿不到代理的账号会被跳过而不是直连
        # desired 比账号数多 10：留出换 IP 的余量，账号越多目标越大
        self.init_proxy_pool(desired=len(accounts) + 10, accounts=len(accounts))

        try:
            if self.options.cookie_test:
                mode = f"{self.options.cookie_test} Cookie 可用性检查"
            else:
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
        finally:
            # 释放懒加载创建的 AI 客户端（含各线程的 curl session），
            # 避免 daemon 长跑或多轮执行时泄漏资源。
            self._close_ai()

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
        interrupted = False
        pool = ThreadPoolExecutor(max_workers=workers, thread_name_prefix="checkin")
        try:
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
                    interrupted = True
                    break
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
        finally:
            # 中断时不能 shutdown(wait=True)：正在运行的账号可能卡在网络重试或
            # 退避 sleep 里，等待会让 Ctrl+C 变成「挂起」。cancel_futures 取消
            # 尚未开始的任务，已运行的工作线程是 daemon，进程可正常退出。
            pool.shutdown(wait=False, cancel_futures=interrupted)

        self.store.flush()
        for account in accounts:
            row = rows.get(id(account))
            if row is not None:
                self.summary.add(row)
        self.summary.render()
        if interrupted:
            return 130
        return 0 if self.summary.failed == 0 else 1

    # ------------------------------------------------------------------ #
    # 单账号
    # ------------------------------------------------------------------ #

    def _run_account(self, account: Account) -> log.SummaryRow:
        """账号入口。整段执行都带上账号标签，避免并行时几个账号的日志混在一起。"""
        with log.context(account.name):
            return self._checkin_account(account)

    def _checkin_account(self, account: Account) -> log.SummaryRow:
        if account.login_method == LOGIN_METHOD_GITHUB_COOKIE:
            if not account.github_user_session:
                log.warn("缺少 GitHub Cookie，跳过（配置 github_user_session）")
                return log.SummaryRow(
                    account.name, "skipped", "-", "缺少 GitHub Cookie（github_user_session）"
                )
        elif not account.cookie:
            log.warn("缺少 cookie（NewAPI Cookie），跳过（配置 accounts[].cookie）")
            return log.SummaryRow(account.name, "skipped", "-", "缺少 NewAPI Cookie")

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

    def _skip_after_failure(self, account: Account, row: log.SummaryRow,
                            reason: str) -> log.SummaryRow:
        """把源站/不可恢复问题统一记为 skipped，避免无意义重试。"""
        detail = f"{reason}：{row.detail}" if row.detail else reason
        log.warn(f"跳过账号：{detail}")
        return log.SummaryRow(account.name, "skipped", row.strategy, detail, row.quota)

    def _run_account_with_retries(self, account: Account, record) -> log.SummaryRow:
        """跑完一个账号。失败按可恢复性分流：

        1. 网络层失败：只要代理池还能给出新 IP，就无限换 IP 立即重试，不计入
           defaults.retry，也不受 ip_swap_limit 影响；成功换 IP 的耗时不计入账号时间盒，
           换不到新 IP 就跳过。
        2. 源站业务失败/WAF 硬封禁：额外换最多 5 次 IP，每次等待 5 秒；
           换 IP 和等待耗时仍计入账号时间盒，仍是同类问题就跳过。
        3. 盾类失败（Cloudflare/Turnstile）：换 IP + 重开浏览器，按账号总时间盒
           重试；这是独立于网络异常的恢复路径，换 IP 和退避耗时都计入时间盒。
        4. 认证、未知或其他不可恢复结果：直接跳过，不浪费重试次数。
        """
        deadline = time.monotonic() + ACCOUNT_DEADLINE_SECONDS
        # 网络异常成功换 IP 的耗时会加回 deadline；源站/WAF/盾类换 IP及退避仍计入。
        # 网络换 IP 不限次数；源站/WAF 只允许额外换五次；两类都记入真实累计数。
        swapped_total = 0
        source_swaps = 0
        row: Optional[log.SummaryRow] = None
        shield_rounds = 0
        while True:
            row = self._attempt(account, record)

            # 1) 网络层失败：只要拿得到新 IP 就无限换，换不到就直接跳过。
            if row.status == api.NETWORK_ERROR:
                swap_started = time.monotonic()
                swapped = self._swap_pooled_proxy(account)
                if swapped:
                    # 代理连接失败时，等待池子切换出口属于恢复网络本身，不能
                    # 抢占后续盾类重试的账号时间盒；WAF 分支不走这里，仍照常计时。
                    deadline += max(0.0, time.monotonic() - swap_started)
                    swapped_total += 1
                    log.warn(f"网络异常，已换 IP 立即重试（不计入重试次数，"
                             f"本账号累计已换 {swapped_total} 个 IP）")
                    continue
                return self._skip_after_failure(
                    account, row, "网络异常且没有可用的新 IP，无法继续换出口"
                )

            # 2) 源站业务失败/WAF 硬封禁：最多额外换五次 IP，每次等待 5 秒，
            #    仍是同类问题就跳过。sleep 只阻塞当前账号 worker，不持有全局锁。
            if row.status in _SOURCE_IP_RETRYABLE:
                if source_swaps < SOURCE_IP_SWAP_LIMIT and self._swap_pooled_proxy(account):
                    source_swaps += 1
                    swapped_total += 1
                    label = "WAF 硬封禁" if row.status == api.WAF_BLOCKED else "源站返回失败"
                    log.warn(f"{label}，换 IP 重试（已换第 {source_swaps}/{SOURCE_IP_SWAP_LIMIT} 个，"
                             f"本账号累计已换 {swapped_total} 个 IP，等待 "
                             f"{SOURCE_IP_SWAP_BACKOFF_SECONDS}s）")
                    time.sleep(SOURCE_IP_SWAP_BACKOFF_SECONDS)
                    continue
                return self._skip_after_failure(
                    account, row,
                    f"{('WAF 硬封禁' if row.status == api.WAF_BLOCKED else '源站问题')}"
                    f"连续重试 {SOURCE_IP_SWAP_LIMIT} 次后仍未恢复"
                    if source_swaps >= SOURCE_IP_SWAP_LIMIT
                    else "源站/WAF 问题且没有可用的新 IP",
                )

            # 3) 盾类失败：换 IP + 重开浏览器，一直试到成功或时间盒用尽
            if row.status in _SHIELD_RETRYABLE and self._should_retry(row):
                left = deadline - time.monotonic()
                if left <= 0:
                    log.err(f"盾类重试已用满 {ACCOUNT_DEADLINE_SECONDS}s 时间盒，"
                            f"共尝试 {shield_rounds + 1} 轮，放弃该账号")
                    return row
                shield_rounds += 1
                swapped = self._swap_pooled_proxy(account)
                if not swapped:
                    return self._skip_after_failure(
                        account, row, "盾类问题且没有可用的新 IP，无法继续恢复"
                    )
                swapped_total += 1
                backoff = min(SHIELD_RETRY_BACKOFF_MAX, 5 * shield_rounds)
                backoff = min(backoff, max(0.0, left - 1))
                label = log.STATUS_LABEL.get(row.status, row.status)
                log.warn(f"{label}：第 {shield_rounds} 轮重试"
                         + (f"（已换出口 IP，本账号累计已换 {swapped_total} 个）" if swapped
                            else "（无新 IP 可换，沿用当前 IP）")
                         + f"，退避 {backoff:.0f}s，剩余时间盒 {left:.0f}s")
                if backoff > 0:
                    time.sleep(backoff)
                continue

            # 源站业务/认证/WAF/未知响应：已经给出不可恢复结论，直接跳过。
            if row.status in _SKIP_ON_FAILURE:
                return self._skip_after_failure(account, row, "源站或不可恢复问题")

            # 没有浏览器时，盾类问题无法自行恢复，直接跳过而不是空转。
            if row.status in _SHIELD_RETRYABLE and not self.options.use_browser:
                return self._skip_after_failure(account, row, "盾类问题且未启用浏览器")

            # 3) 其他结果不再做无意义重试
            return row

    def _attempt(self, account: Account, record) -> log.SummaryRow:
        ip = self.exit_ip(account.proxy)

        if account.login_method == LOGIN_METHOD_GITHUB_COOKIE:
            return self._attempt_github(account, record, ip)

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

    def _attempt_github(self, account: Account, record, ip: Optional[str]) -> log.SummaryRow:
        """GitHub Cookie 账号：复用站点 CF 缓存，但 OAuth 回调由专用客户端完成。"""
        if record.cf is not None:
            usable, reason = record.cf.check(ip, account.proxy)
            log.debug(f"GitHub S0 缓存判定: {reason}")
            if usable:
                result = self._github_api_call(account, record.cf)
                if result.kind in _SETTLED:
                    return self._row(account, result, "S0")
                if result.kind == api.CF_BLOCKED:
                    log.warn("缓存的 cf_clearance 已被拒绝，作废后重新过盾")
                    self.store.clear_cf(account.slug)
                    record.cf = None
            else:
                self.store.clear_cf(account.slug)
                record.cf = None

        result = self._github_api_call(account, None)
        if result.kind in _SETTLED:
            return self._row(account, result, "S1")
        if result.kind == api.NETWORK_ERROR:
            return self._row(account, result, "S1")

        verdict = result.verdict
        if result.kind == api.CF_BLOCKED:
            log.warn(f"GitHub OAuth 站点命中 Cloudflare: {result.message}")
            if verdict is not None and not verdict.recoverable:
                result.kind = api.WAF_BLOCKED
                return self._row(
                    account, result, "S1",
                    detail=f"{verdict.label}：脚本无法绕过，需要换出口 IP 或联系站点放行",
                )
        else:
            log.warn(f"GitHub OAuth 响应异常，尝试用浏览器复核: {result.message}")

        if not self.options.use_browser:
            return self._row(account, result, "S1", detail="已禁用浏览器过盾（--no-browser）")

        self._take_browser_attempt(account)
        return self._solve(account, record, ip, result)

    def _github_api_call(self, account: Account, cf) -> api.ApiResult:
        from .github_oauth import GithubOAuthClient

        with GithubOAuthClient(account, self.cfg.http, cf) as client:
            log.debug(
                f"GitHub OAuth 协议={account.github_protocol}, "
                f"impersonate={client.impersonate}, "
                + ("缓存 UA" if cf is not None and cf.user_agent else "默认 UA")
                + (f", cookie 条数={len(cf.cookies)}" if cf is not None else "")
            )
            result = client.checkin(dry_run=bool(self.options.cookie_test or self.options.dry_run))
        if result.kind in _SETTLED and result.user_id:
            account.user_id = result.user_id
            self.store.remember(account.slug, user_id=result.user_id)
        return result

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

        # S2/S3：拿到 CF session，回到对应登录链路重发
        result = (
            self._github_api_call(account, outcome.cf)
            if account.login_method == LOGIN_METHOD_GITHUB_COOKIE
            else self._api_call(account, outcome.cf)
        )
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
