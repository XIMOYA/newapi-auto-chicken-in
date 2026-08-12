"""流程编排：策略链 S0 -> S1 -> S2 -> S3 -> S4 -> S5，任意一级成功即短路。"""

from __future__ import annotations

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

# 这些结果重试没有意义
_TERMINAL = (
    api.SUCCESS,
    api.ALREADY_DONE,
    api.AUTH_FAILED,
    api.LOGIN_REQUIRED,
    api.WAF_BLOCKED,
    "skipped",
)
# 这些结果说明请求本身通了，换策略也不会变，直接结束
_SETTLED = (api.SUCCESS, api.ALREADY_DONE, api.AUTH_FAILED, api.FAILED)


@dataclass
class RunOptions:
    account_names: Optional[list] = None
    dry_run: bool = False
    headful: bool = False
    manual: bool = False
    use_ai: bool = True
    use_browser: bool = True
    verbose: bool = False
    parallelism: int = 1          # 1 = 未显式指定，run() 里自动提升为默认 5


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

    # ------------------------------------------------------------------ #
    # 懒加载资源
    # ------------------------------------------------------------------ #

    def init_proxy_pool(self, desired: Optional[int] = None) -> None:
        """抓取并测通代理池。失败/为空时降级直连，绝不中断签到。"""
        if not self.cfg.proxy_pool.enabled:
            log.debug("代理池未启用（proxy_pool.enabled=false）")
            return
        try:
            from .proxy_pool import ProxyPool

            self._pool = ProxyPool(self.cfg.proxy_pool)
            count = self._pool.refresh(desired=desired)
            if count:
                log.ok(f"代理池就绪: {count} 个可用代理")
            else:
                log.warn(f"代理池为空，本次签到降级直连: {self._pool.last_error}")
        except Exception as exc:  # noqa: BLE001 - 代理池是可降级项，绝不能中断签到
            log.warn(f"代理池初始化失败，降级直连: {type(exc).__name__}: {exc}")
            self._pool = None

    def _assign_proxy(self, account: Account) -> None:
        """给账号分配代理。手动配置的优先；否则从池随机取（一个账号一个 IP）。"""
        if account.proxy:
            return
        if self._pool is None:
            return
        proxy = self._pool.acquire()
        if proxy:
            account.proxy = proxy
            self._pooled_proxies[account.name] = proxy
            log.debug(f"账号 {account.name} 已分配代理 {proxy}")
        else:
            log.warn(f"账号 {account.name} 未分配到代理，降级直连")

    def _swap_proxy(self, account: Account) -> Optional[str]:
        """目标站点连不上时换一个新代理；返回 None 表示没得换（降级直连）。"""
        if self._pool is None:
            return None
        old = self._pooled_proxies.get(account.name)
        if old:
            self._pool.mark_bad(old)
        self._pooled_proxies.pop(account.name, None)
        account.proxy = None
        proxy = self._pool.acquire()
        if proxy:
            account.proxy = proxy
            self._pooled_proxies[account.name] = proxy
            log.warn(f"账号 {account.name} 代理 {old or '<手动>'} 连目标站点失败，换用 {proxy}")
            return proxy
        log.warn(f"账号 {account.name} 代理连目标站点失败且池无剩余，降级直连")
        return None

    def exit_ip(self, proxy: Optional[str]) -> Optional[str]:
        key = proxy or ""
        if key not in self._ip_cache:
            ip = probe_exit_ip(proxy)
            self._ip_cache[key] = ip
            log.debug(f"出口 IP: {ip or '探测失败（跳过 IP 比对）'}")
        return self._ip_cache[key]

    def ai(self):
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
        """账号级并发数，钳制在 [1, 8]；人工模式强制串行。"""
        if self.options.manual:
            return 1
        try:
            workers = int(self.options.parallelism)
        except (TypeError, ValueError):
            workers = 1
        return max(1, min(8, workers))

    def _set_parallelism(self, workers: int) -> None:
        """把传入的并发数写回选项（供 CLI/调度调用方设置）。"""
        self.options.parallelism = max(1, min(8, int(workers)))
    def run(self) -> int:
        accounts = self.cfg.select(self.options.account_names)
        if not accounts:
            log.warn("没有启用的账号（检查 accounts[].enabled）")
            return 2

        # 默认并行度 5：效率与资源占用折中（人工模式强制串行 1）
        if not self.options.manual and self.options.parallelism in (None, 1, 0):
            self._set_parallelism(5)
        elif self.options.manual:
            self._set_parallelism(1)

        # 代理池是可选资源：抓取+测通失败只降级直连，不影响签到
        self.init_proxy_pool(desired=len(accounts) + 10)

        mode = "dry-run 连通性检查" if self.options.dry_run else "签到"
        log.step(f"开始{mode}，共 {len(accounts)} 个账号")
        workers = self._parallelism()
        if workers <= 1:
            return self._run_serial(accounts)
        log.info(f"账号级并行度 {workers}")
        return self._run_parallel(accounts, workers)

    def _run_serial(self, accounts: list) -> int:
        total = len(accounts)
        for idx, account in enumerate(accounts, start=1):
            log.step(f"[{idx}/{total}] {account.name}  ->  {account.base_url}")
            try:
                row = self._run_account(account)
            except KeyboardInterrupt:
                log.warn("收到中断信号，停止后续账号")
                self.store.flush()
                self.summary.render()
                return 130
            except Exception as exc:  # noqa: BLE001 - 单账号异常不能拖垮整轮
                log.err(f"未预期异常: {type(exc).__name__}: {exc}")
                if self.options.verbose:
                    import traceback

                    log.debug(traceback.format_exc())
                row = log.SummaryRow(account.name, api.UNKNOWN, "-", f"{type(exc).__name__}: {exc}")
            self.summary.add(row)
            self.store.flush()
            if idx < total:
                delay = jitter_sleep(self.cfg.defaults.interval_seconds)
                log.debug(f"账号间隔停顿 {delay:.1f}s")

        self.summary.render()
        return 0 if self.summary.failed == 0 else 1

    def _run_parallel(self, accounts: list, workers: int) -> int:
        from concurrent.futures import ThreadPoolExecutor

        total = len(accounts)
        rows: list = []
        with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="checkin") as pool:
            futures = [pool.submit(self._run_account, account) for account in accounts]
            for idx, (account, future) in enumerate(zip(accounts, futures), start=1):
                log.step(f"[{idx}/{total}] {account.name}  ->  {account.base_url}")
                try:
                    rows.append(future.result())
                except KeyboardInterrupt:
                    log.warn("收到中断信号，停止等待后续账号")
                    for pending in futures:
                        pending.cancel()
                    self.store.flush()
                    self.summary.render()
                    return 130
                except Exception as exc:  # noqa: BLE001 - 单账号异常不能拖垮整轮
                    log.err(f"未预期异常: {type(exc).__name__}: {exc}")
                    if self.options.verbose:
                        import traceback

                        log.debug(traceback.format_exc())
                    rows.append(
                        log.SummaryRow(account.name, api.UNKNOWN, "-",
                                       f"{type(exc).__name__}: {exc}")
                    )
                self.store.flush()

        for account, row in zip(accounts, rows):
            self.summary.add(row)
        self.summary.render()
        return 0 if self.summary.failed == 0 else 1

    # ------------------------------------------------------------------ #
    # 单账号
    # ------------------------------------------------------------------ #

    def _run_account(self, account: Account) -> log.SummaryRow:
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

        # 目标站点连不上（网络层失败）时换 IP 重试；业务失败（cookie/已签/风控）不换
        swap_left = self.cfg.proxy_pool.ip_swap_limit if self._pool else 0
        while True:
            row = self._run_account_with_retries(account, record)
            if (row.status == api.NETWORK_ERROR and swap_left > 0
                    and account.name in self._pooled_proxies):
                if self._swap_proxy(account):
                    swap_left -= 1
                    log.warn(f"账号 {account.name} 换 IP 后重试签到")
                    continue
            return row

    def _run_account_with_retries(self, account: Account, record) -> log.SummaryRow:
        attempts = self.cfg.defaults.retry + 1
        row: Optional[log.SummaryRow] = None
        for attempt in range(1, attempts + 1):
            if attempt > 1:
                backoff = min(30, 2 ** (attempt - 1))
                log.info(f"第 {attempt}/{attempts} 次尝试，退避 {backoff}s")
                time.sleep(backoff)
            row = self._attempt(account, record)
            if row.status in _TERMINAL:
                return row
        return row or log.SummaryRow(account.name, api.UNKNOWN, "-", "未产生任何结果")

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

        return self._solve(account, record, ip, result)

    # ------------------------------------------------------------------ #
    # 浏览器过盾（S2 / S3 / S4 / S5）
    # ------------------------------------------------------------------ #

    def _solve(self, account: Account, record, ip: Optional[str],
               blocked: api.ApiResult) -> log.SummaryRow:
        try:
            from .cf.solver import solve
        except ImportError as exc:
            log.err(f"浏览器过盾模块不可用: {exc}")
            log.info("安装依赖: pip install -r requirements.txt && python -m camoufox fetch")
            return self._row(account, blocked, "S1", detail=f"过盾模块缺失: {exc}")

        outcome = solve(
            cfg=self.cfg,
            account=account,
            exit_ip=ip,
            options=self.options,
            ai=self.ai(),
        )

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
