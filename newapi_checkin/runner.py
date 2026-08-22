"""流程编排：策略链 S0 -> S1 -> S2 -> S3 -> S4 -> S5，任意一级成功即短路。"""

from __future__ import annotations

import math
import threading
import time
from dataclasses import dataclass
from typing import Optional

from . import client as api
from . import logger as log
from .cf.session_store import SessionStore
from .config import (
    LOGIN_METHOD_TABIAI,
    SESSIONS_FILE,
    Account,
    Config,
)
from .utils import jitter_sleep, now, probe_exit_ip

STRATEGY_LABEL = {
    "S0": "S0 缓存直连",
    "S1": "S1 指纹直连",
    "S2": "S2 浏览器过盾",
    "S3": "S3 AI 辅助",
    "S4": "S4 浏览器内直发",
    "S5": "S5 人工兜底",
}

# 「盾类」失败：被质询拦住、或拿不到 Turnstile token。
# 这两种都靠「换出口 IP + 重开浏览器」翻盘，所以不限次数地重试；
# 默认连时间也不限（见 ACCOUNT_DEADLINE_SECONDS），一直试到成功或拿到定论。
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

# 自动签到固定账号级并发：HTTP 快路径主要等待网络，统一保持 6 个账号并发。
# 比浏览器并发高，是为了让「已签到 / 凭据失效」这类不进浏览器的账号快速流过，
# 不被卡在盾类账号后面排队。--parallel / 调度配置仍保留兼容字段，实际不生效。
DEFAULT_ACCOUNT_PARALLELISM = 6
# 账号级并发的历史硬上限（保留兼容，实际运行固定使用 DEFAULT_ACCOUNT_PARALLELISM）。
MAX_ACCOUNT_PARALLELISM = 16
# 浏览器实例固定并发：最多同时开 3 个 Camoufox，不再按 CPU 核数推导。
# 上限卡在 3 是因为 GitHub 公开库 runner 只有 4 vCPU，而过 Turnstile 交互质询
# 是 CPU 密集的；开到 4 个会互相抢核，让本来能过的实例撞上 browser.timeout。
FIXED_BROWSER_PARALLELISM = 3
MAX_BROWSER_PARALLELISM = FIXED_BROWSER_PARALLELISM
# 单账号的总时长上限（秒）。<=0 表示不限时：盾类与网络失败一直重试到出结果。
#
# 默认关闭是有意的取舍。之前设 1200s 是怕一个卡住的账号占死并发位、把整轮拖到
# Actions 超时；但代价是过盾本来就慢的站点会被半途掐掉，白扔一个账号的签到。
# 现在选择「宁可整轮慢，也不放弃账号」，唯一的兜底是 Actions 自己的 6 小时硬上限
# （workflow 有意不设 timeout-minutes，走平台默认 360 分钟）。
#
# 注意这不等于「卡死也不管」：只有盾类和网络层失败会无限重试，源站业务失败与 WAF
# 硬封禁仍只换 SOURCE_IP_SWAP_LIMIT 次 IP 就跳过，认证失败等定论直接跳过。
# 想恢复收口只改这一个数字，下面的时间盒逻辑仍在。
ACCOUNT_DEADLINE_SECONDS = 0
# 盾类重试的退避上限（秒）。连续硬刚 Cloudflare 只会让质询更难过
SHIELD_RETRY_BACKOFF_MAX = 30
# 平台没下发心跳间隔时用这个兜底（老版本平台、或响应结构变了）
RUN_HEARTBEAT_FALLBACK_SECONDS = 60
# 只用于日志文案：告诉用户漏解锁大概多久会自动过期。真实阈值在平台侧
RUN_LOCK_STALE_HINT_MINUTES = 5
# 人工模式（--manual）下账号之间的随机停顿区间（秒）。
#
# 以前这是 defaults.interval_seconds 配置项，但自动签到早就改成固定 6 账号并发了，
# 并发路径压根没有"账号间隔"这个概念，配置只在人工串行时生效，形同摆设。
# 现在收成常量：人工模式仍然逐个来、中间喘口气，其余场景由并发度控制节奏。
MANUAL_INTERVAL_SECONDS = (3.0, 8.0)
# 代理池预取的余量：在「按共用上限折算出的占用量」之外多备这么多个出口。
#
# 盾类失败靠换 IP 翻盘，而且是不限次数地换，所以余量比占用量更值钱 —— 占用量算少了
# 顶多几个账号挤一个 IP，余量算少了会在最需要换 IP 的时候拿不到新的。
PROXY_SPARE_COUNT = 50
# 平台上凭据保活占锁时用的 source 值，必须和 Go 侧 tabiaiKeepaliveSource 一致。
KEEPALIVE_RUN_SOURCE = "tabiai-keepalive"
# 开跑前最多等保活多久（秒）。保活一轮的超时是 10 分钟，但签到本身有价值，
# 不能因为等锁彻底不跑；超时就带着告警继续。
KEEPALIVE_WAIT_MAX_SECONDS = 300
# 等待期间的轮询间隔（秒）。保活一轮通常几十秒，5 秒够灵敏又不刷接口
KEEPALIVE_POLL_SECONDS = 5
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
    browser_parallelism: int = 0         # 兼容调用方字段；浏览器实际固定为 3（人工模式为 1）
    # proxy_shard = (序号, 总片数)，1-based。Actions 分片并行时透传给平台，让每个 job
    # 只拿属于自己的那份代理，几个 job 之间不会把同一个出口 IP 同时分给不同账号
    proxy_shard: Optional[tuple] = None
    # 本片汇总结果的落盘路径。给值就写 JSON 并且**不再自己发邮件** —— Actions 分片
    # 并行时各片都发会把一天的结果拆成好几封，改由汇总 job 读齐所有片发一封
    summary_out: Optional[str] = None


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
        # 排队等浏览器槽位的耗时。并行签到时每个账号 worker 记自己的，
        # 由重试主循环取走并加回时间盒：等全局资源不该算这个账号的过盾时间。
        self._gate_waits = threading.local()
        # Turnstile 取 token 有频率限制，必须跨账号串行 + 间隔
        self._turnstile_lock = threading.Lock()
        self._last_turnstile_at: float = 0.0
        # 签到期间在平台上占的那把锁：网页端据此禁止动 TaBiAI 凭据
        self._run_lock_active = False
        self._heartbeat_thread: Optional[threading.Thread] = None
        self._heartbeat_stop: Optional[threading.Event] = None

    # ------------------------------------------------------------------ #
    # 懒加载资源
    # ------------------------------------------------------------------ #

    def _desired_proxies(self, accounts: int) -> int:
        """按共用上限折算这一轮该预取多少代理。

        以前是「账号数 + 10」，那是一账号一 IP 的年代。现在同一出口最多给
        max_accounts_per_ip 个账号用，占用量按上限除下来，省掉的抓取+测通时间很可观：
        64 个账号从探 74 个降到探 16+50。

        上限设成 0（不限共用）时按账号数算 —— 不限共用不等于只需要一个 IP，
        换 IP 的余量还是得留够。
        """
        limit = int(getattr(self.cfg.proxy_pool, "max_accounts_per_ip", 0) or 0)
        needed = accounts if limit <= 0 else -(-accounts // limit)  # 向上取整
        return max(1, needed) + PROXY_SPARE_COUNT

    def init_proxy_pool(self, desired: Optional[int] = None,
                        accounts: Optional[int] = None) -> None:
        """抓取并测通代理池。启用了代理池就意味着「必须走代理」，不再降级直连。"""
        if not self.cfg.proxy_pool.enabled:
            log.debug("代理池未启用（proxy_pool.enabled=false）")
            return
        try:
            from .proxy_pool import ProxyPool

            self._pool = ProxyPool(self.cfg.proxy_pool, shard=self.options.proxy_shard)
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

    def _swap_proxy(self, account: Account, reason: str = "net") -> Optional[str]:
        """换一个新代理。拿不到替代品时保留原代理，绝不清空成直连。"""
        if self._pool is None:
            return None
        with self._state_lock:
            old = self._pooled_proxies.get(account.name)
        if old:
            self._pool.mark_bad(old, reason)
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
        """自动签到固定 6 个账号并发；人工模式仍强制串行。"""
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

        # 自动签到固定 6 个账号并发；人工模式强制串行 1。
        self._set_parallelism(1 if self.options.manual else DEFAULT_ACCOUNT_PARALLELISM)

        # 代理池：启用后就必须走代理，拿不到代理的账号会被跳过而不是直连。
        # desired 按共用上限折算（详见 _desired_proxies）：同一出口可以服务多个账号，
        # 所以要的不是「一账号一 IP」，而是「够摊平 + 够换」
        self.init_proxy_pool(desired=self._desired_proxies(len(accounts)),
                             accounts=len(accounts))

        try:
            if self.options.cookie_test:
                mode = f"{self.options.cookie_test} Cookie 可用性检查"
            else:
                mode = "dry-run 连通性检查" if self.options.dry_run else "签到"
            log.step(f"开始{mode}，共 {len(accounts)} 个账号")
            # 先等平台上的凭据保活跑完：它也会真 refresh，撞上会让旧代被判重放。
            # 保活那边会避让签到，但「它跑到一半我们才启动」这个窗口只能由这里堵
            self._wait_for_keepalive(accounts)
            # 告诉平台「我开始跑了」，网页端据此锁住 TaBiAI 检测与签发。
            # 放在账号筛选之后：没有账号可跑时压根不该上报，省得白锁一段时间。
            self._start_run_report(accounts)
            workers = self._parallelism()
            browser_workers = self._browser_workers()
            self._browser_gate = threading.Semaphore(browser_workers)
            if workers <= 1:
                exit_code = self._run_serial(accounts)
            else:
                log.info(f"账号级并行度 {workers}，浏览器并发上限 {browser_workers}")
                exit_code = self._run_parallel(accounts, workers)

            # 分片结果落盘：给汇总 job 合并成一封邮件用。放在发信之前，且写失败只
            # WARN —— 这一轮真正的产出是签到本身，不能让一个写文件的错抹掉它
            self._dump_summary()
            # 邮件通知：无论成败都发；失败只 WARN 不影响退出码
            self._send_notification()
            return exit_code
        finally:
            # 先解锁再收资源：网页端多锁一会儿没损失，但漏解锁要等 5 分钟过期
            self._stop_run_report()
            # 代理表现回传：单独一步，不搭 _stop_run_report 的车 —— 那个函数只在运行锁
            # 激活时才真发请求（锁只有「config_sync 启用且本轮含 tabiai 账号」时才拿），
            # 搭车会让纯站点 Cookie 的轮次整份丢掉反馈
            self._report_proxy_feedback()
            # 释放懒加载创建的 AI 客户端（含各线程的 curl session），
            # 避免 daemon 长跑或多轮执行时泄漏资源。
            self._close_ai()

    # ------------------------------------------------------------------ #
    # 运行状态上报（配合平台锁住高危凭据操作）
    # ------------------------------------------------------------------ #

    def _wait_for_keepalive(self, accounts: list) -> None:
        """签到开跑前，等平台上的凭据保活跑完。

        保活也会真 refresh，两边同时动同一条 sid 会让旧代被判重放、整条会话被站点
        撤销。保活自己会避让签到，但「保活跑到一半、签到才启动」那个窗口只能由这边堵。

        只等保活，不等别的签到进程：run_state 是引用计数锁，分片并行时几个 job 互相
        持锁是正常的，互等会直接死锁。所以判定落在 source 上而不是「有人持锁」。

        查询失败一律当没锁 —— 平台不可达时不该连签到都做不了。
        """
        if not self._needs_run_lock(accounts):
            return
        from .remote_sync import fetch_run_state

        deadline = time.monotonic() + KEEPALIVE_WAIT_MAX_SECONDS
        waited = False
        while True:
            ok, state = fetch_run_state(self.cfg.config_sync)
            if not ok:
                return
            if not state.get("running"):
                if waited:
                    log.ok("凭据保活已结束，继续签到")
                return
            if KEEPALIVE_RUN_SOURCE not in str(state.get("source") or ""):
                # 是别的分片 job 在跑，那是同伴不是竞争者
                return
            left = deadline - time.monotonic()
            if left <= 0:
                log.warn(f"等凭据保活超过 {KEEPALIVE_WAIT_MAX_SECONDS} 秒仍未结束，"
                         "继续签到（存在与保活撞代次的风险）")
                return
            if not waited:
                log.info("平台上的凭据保活正在运行，先等它跑完"
                         f"（最多 {KEEPALIVE_WAIT_MAX_SECONDS} 秒）")
                waited = True
            time.sleep(min(KEEPALIVE_POLL_SECONDS, left))

    def _needs_run_lock(self, accounts: list) -> bool:
        """这一轮值不值得上报。

        只有 tabiai 账号的凭据会轮转，也只有它怕跟网页端撞代次；一轮里全是
        站点 Cookie 账号时上报纯属白锁网页端。dry-run 与 cookie_test 模式
        同样会真的 refresh，所以照样要锁。
        """
        sync = getattr(self.cfg, "config_sync", None)
        if sync is None or not getattr(sync, "enabled", False):
            return False
        return any(a.login_method == LOGIN_METHOD_TABIAI for a in accounts)

    def _start_run_report(self, accounts: list) -> None:
        """上报开跑并起心跳线程。上报失败只告警，绝不影响签到。"""
        if not self._needs_run_lock(accounts):
            return
        from .remote_sync import report_run_start

        ok, detail, gap = report_run_start(self.cfg.config_sync, self._run_source())
        if not ok:
            log.warn(f"未能上报签到状态（{detail}）：网页端不会被锁定，"
                     "此时做 TaBiAI 凭据检测可能撞代次")
            return
        log.debug(f"已上报签到状态，网页端 TaBiAI 操作已锁定: {detail}")
        self._run_lock_active = True
        self._start_heartbeat(gap if gap > 0 else RUN_HEARTBEAT_FALLBACK_SECONDS)

    def _start_heartbeat(self, interval: int) -> None:
        """起一个守护线程持续续期。

        必须独立于账号进度：一个账号过盾可能耗上十几分钟，若把心跳挂在账号
        循环里，平台会在中途就把锁判过期。
        """
        self._heartbeat_stop = threading.Event()

        def beat() -> None:
            from .remote_sync import report_run_heartbeat

            while not self._heartbeat_stop.wait(interval):
                ok, running = report_run_heartbeat(self.cfg.config_sync)
                if not ok:
                    log.debug("签到状态心跳上报失败（网络问题？），下一拍再试")
                    continue
                if not running:
                    log.warn("平台上的签到锁已被强制解锁：网页端现在可以动 TaBiAI 凭据，"
                             "若此时做检测或签发会与本轮撞代次")

        self._heartbeat_thread = threading.Thread(
            target=beat, name="run-state-heartbeat", daemon=True)
        self._heartbeat_thread.start()

    def _stop_run_report(self) -> None:
        """停心跳并解锁。异常一律吞掉：收尾阶段不能再抛错盖掉真正的结果。"""
        if self._heartbeat_stop is not None:
            self._heartbeat_stop.set()
        thread = self._heartbeat_thread
        if thread is not None and thread.is_alive():
            # 只等一小会儿：线程是 daemon，拖着不退也不会阻止进程结束
            thread.join(timeout=3)
        self._heartbeat_thread = None
        self._heartbeat_stop = None
        if not self._run_lock_active:
            return
        self._run_lock_active = False
        try:
            from .remote_sync import report_run_stop

            if report_run_stop(self.cfg.config_sync):
                log.debug("已解除网页端的 TaBiAI 操作锁定")
            else:
                log.warn(f"未能解除网页端锁定：约 {RUN_LOCK_STALE_HINT_MINUTES} 分钟后会自动过期，"
                         "也可在「Cookie 测试」页强制解锁")
        except Exception as exc:  # noqa: BLE001 - 收尾失败不能影响退出码
            log.debug(f"解除签到锁定时异常: {type(exc).__name__}: {exc}")

    def _run_source(self) -> str:
        """自报来源，只用于网页端展示「谁在跑」。"""
        import os
        import socket

        if os.environ.get("GITHUB_ACTIONS") == "true":
            repo = os.environ.get("GITHUB_REPOSITORY", "")
            return f"GitHub Actions（{repo}）" if repo else "GitHub Actions"
        try:
            return socket.gethostname() or "签到客户端"
        except Exception:  # noqa: BLE001 - 取不到主机名无所谓
            return "签到客户端"

    def _dump_summary(self) -> None:
        """把本片汇总行写成 JSON，交给 Actions 汇总 job 合并后统一发信。

        落盘失败只 WARN：汇总那边会把「预期 N 片、实到 M 片」写进邮件，缺的片一眼
        能看出来，比在这里抛异常把整轮搞挂要好。
        """
        if not self.options.summary_out:
            return
        try:
            from .shard_report import dump_shard_summary

            path = dump_shard_summary(
                self.options.summary_out, self.summary.rows,
                shard=self.options.proxy_shard, dry_run=self.options.dry_run,
            )
            log.ok(f"本片 {len(self.summary.rows)} 条结果已写入 {path}")
        except Exception as exc:  # noqa: BLE001 - 落盘不能拖垮签到
            log.warn(f"本片结果落盘失败: {type(exc).__name__}: {exc}")

    def _pricing_proxy_provider(self):
        """给拉定价表用的取代理回调。没有代理池就返回 None（直连）。

        签到已经把池 refresh 过了，这里直接领现成的，不额外触发探测。
        代理坏了就 mark_bad 再领下一个 —— 和签到失败换 IP 走同一套账本，
        坏代理的反馈最后会一起回传给平台优选。
        """
        if self._pool is None:
            return None

        def provider(bad: Optional[str] = None) -> Optional[str]:
            if bad:
                self._pool.mark_bad(bad, "net")
            return self._pool.acquire()

        return provider

    def _build_quota_overview(self) -> str:
        """拼邮件底部的额度总览。拉不到定价就返回空串，正文照发。

        定价每轮实时拉（站点会调价），同一站点只拉一次。失败绝不冒泡 —— 少一段总览
        比丢一封结果邮件强得多。
        """
        try:
            from .notify import build_quota_overview
            from .pricing import summarize_by_site

            sites = summarize_by_site(self.summary.rows, self.cfg.http,
                                      proxy_provider=self._pricing_proxy_provider())
            return build_quota_overview(sites)
        except Exception as exc:  # noqa: BLE001 - 总览是附加信息
            log.debug(f"额度总览生成失败，本封邮件省略: {type(exc).__name__}: {exc}")
            return ""

    def _send_notification(self) -> None:

        """把本轮汇总表以 HTML 邮件发出。未配置/发送失败均降级为 WARN。

        Actions 分片并行时这里不发：每片各发一封会把一天的结果拆成好几封邮件，
        改由 --summary-out 落盘、汇总 job 合并后统一发一封（见 shard_report）。
        """
        email_cfg = self.cfg.notify.email
        if not email_cfg.enabled:
            return
        if self.options.summary_out:
            log.info("本片结果已落盘，邮件交由汇总步骤统一发送")
            return
        try:
            from .notify import send_report

            sent, subject = send_report(
                email_cfg, self.summary.rows,
                dry_run=self.options.dry_run,
                run_context="GitHub Actions" if not self.options.manual else "本地/桌面",
                quota_overview=self._build_quota_overview(),
            )
            if sent:
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
            if idx < total and self.options.manual:
                # 只有人工模式才需要这个停顿：自动签到走并发路径，节奏由并发度决定
                delay = jitter_sleep(MANUAL_INTERVAL_SECONDS)
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
        if account.login_method == LOGIN_METHOD_TABIAI:
            if not (account.cookie or self.store.get(account.slug).refresh_cookie):
                log.warn("缺少 TaBiAI 凭据，跳过（配置 cookie=new_api_refresh=... 或用管理端签发）")
                return log.SummaryRow(
                    account.name, "skipped", "-", "缺少 TaBiAI 凭据（new_api_refresh）"
                )
        elif not account.cookie:
            log.warn("缺少 cookie（站点 Cookie），跳过（配置 accounts[].cookie）")
            return log.SummaryRow(account.name, "skipped", "-", "缺少站点 Cookie")

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

        row = self._run_account_with_retries(account, record)
        self._record_proxy_success(account, row)
        return row

    def _record_proxy_success(self, account: Account, row: log.SummaryRow) -> None:
        """账号跑完且成了，给它当时用的池内代理记一笔。

        只补成功这一个方向：失败已经在 _swap_proxy 换 IP 时当场记过了。中途换过 IP 的
        话，成功记在最后那个代理名下，前面失败的几个各自记在自己名下，正好是我们想让
        平台看到的因果。手动配置的代理不记 —— 它不参与池子的优选排序。
        """
        if self._pool is None or row.status not in log.OK_STATUSES:
            return
        with self._state_lock:
            proxy = self._pooled_proxies.get(account.name)
        if proxy:
            self._pool.mark_ok(proxy)

    def _report_proxy_feedback(self) -> None:
        """把本轮各代理的成败计数回传给平台，供下次预取时优选。

        整段都是尽力而为：没配预取地址、关了开关、网络不通，一律只留一行 debug。
        回传失败绝不能改变退出码 —— 签到已经跑完了，统计没送出去不该算这轮失败。
        """
        if self._pool is None:
            return
        try:
            ok, detail = self._pool.report_feedback()
        except Exception as exc:  # noqa: BLE001 - 回传异常不能拖垮收尾
            log.debug(f"代理反馈回传异常: {type(exc).__name__}: {exc}")
            return
        if ok:
            log.debug(f"代理反馈已回传：{detail}")
        else:
            log.debug(f"代理反馈未回传：{detail}")

    def _swap_pooled_proxy(self, account: Account, reason: str = "net") -> bool:
        """只换「由代理池分配」的代理；手动配置的代理原样保留。

        reason 一路传到 mark_bad，用来区分「代理本身不通」和「出口 IP 被目标站拦」，
        跑完回传给平台后优选才知道该怎么给这个 IP 降权。
        """
        with self._state_lock:
            from_pool = account.name in self._pooled_proxies
        if not from_pool:
            return False
        return self._swap_proxy(account, reason) is not None

    def _skip_after_failure(self, account: Account, row: log.SummaryRow,
                            reason: str) -> log.SummaryRow:
        """把源站/不可恢复问题统一记为 skipped，避免无意义重试。"""
        detail = f"{reason}：{row.detail}" if row.detail else reason
        log.warn(f"跳过账号：{detail}")
        # balance/quota_per_unit 照原样带走：这一行只是换个状态和说明，
        # 之前已经查到的额度信息不该在这里丢掉
        return log.SummaryRow(account.name, "skipped", row.strategy, detail, row.quota,
                              balance=row.balance, quota_per_unit=row.quota_per_unit,
                              site=row.site or account.base_url)

    def _give_up_on_deadline(self, row: log.SummaryRow, shield_rounds: int) -> log.SummaryRow:
        """时间盒耗尽：关掉该账号的全部重试，带着当前结果收工。

        排队等浏览器的耗时已经在主循环里加回过 deadline，所以走到这里说明
        真正花在尝试上的时间确实满了，不是被别的账号排队挤掉的。
        """
        log.err(f"已用满 {ACCOUNT_DEADLINE_SECONDS}s 时间盒（不含排队等浏览器），"
                f"共尝试 {shield_rounds + 1} 轮，停止该账号的全部重试")
        return row

    def _run_account_with_retries(self, account: Account, record) -> log.SummaryRow:
        """跑完一个账号。失败按可恢复性分流：

        1. 网络层失败：只要代理池还能给出新 IP，就无限换 IP 立即重试，不受
           ip_swap_limit 影响；成功换 IP 的耗时不计入账号时间盒，
           换不到新 IP 就跳过。
        2. 源站业务失败/WAF 硬封禁：额外换最多 5 次 IP，每次等待 5 秒；
           换 IP 和等待耗时仍计入账号时间盒，仍是同类问题就跳过。
        3. 盾类失败（Cloudflare/Turnstile）：换 IP + 重开浏览器，次数不限；换不到新 IP
           时沿用当前出口继续试。
        4. 认证、未知或其他不可恢复结果：直接跳过，不浪费重试次数。

        时间盒默认关闭（ACCOUNT_DEADLINE_SECONDS <= 0），此时 1 与 3 一直试到出结果，
        只有 Actions 的 6 小时硬上限能中止它们；2 与 4 的收口与时间盒无关，照旧生效。
        设成正数时它就是所有重试的统一上限：一旦耗尽，本账号不再做任何重试，带着当前
        结果收工；排队等浏览器槽位的耗时会加回时间盒 —— 那是等全局资源，
        不属于这个账号自己的过盾时间。
        """
        # inf 让下面所有 deadline 运算与比较都自然退化成「永不耗尽」，
        # 不必在每个分支上再套一层「是否限时」的判断
        unlimited = ACCOUNT_DEADLINE_SECONDS <= 0
        deadline = math.inf if unlimited else time.monotonic() + ACCOUNT_DEADLINE_SECONDS
        # 网络异常成功换 IP 的耗时会加回 deadline；源站/WAF/盾类换 IP及退避仍计入。
        # 网络换 IP 不限次数；源站/WAF 只允许额外换五次；两类都记入真实累计数。
        swapped_total = 0
        source_swaps = 0
        row: Optional[log.SummaryRow] = None
        shield_rounds = 0
        self._take_gate_wait()  # 清掉上一个账号可能残留的读数
        while True:
            row = self._attempt(account, record)
            # 排队等浏览器槽位不算这个账号的过盾耗时，先把它加回来再判时间盒
            queued = self._take_gate_wait()
            if queued > 0:
                deadline += queued
                log.debug(f"排队等浏览器 {queued:.1f}s，已加回时间盒（不计入）")
            exhausted = time.monotonic() >= deadline

            # 1) 网络层失败：只要拿得到新 IP 就无限换，换不到就直接跳过。
            if row.status == api.NETWORK_ERROR:
                if exhausted:
                    return self._give_up_on_deadline(row, shield_rounds)
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
                if exhausted:
                    return self._give_up_on_deadline(row, shield_rounds)
                if source_swaps < SOURCE_IP_SWAP_LIMIT and self._swap_pooled_proxy(account, "block"):
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

            # 3) 盾类失败：换 IP + 重开浏览器，次数不限；限了时才由时间盒收口
            if row.status in _SHIELD_RETRYABLE and self._should_retry(row):
                left = deadline - time.monotonic()
                if left <= 0:
                    return self._give_up_on_deadline(row, shield_rounds)
                shield_rounds += 1
                # 换不到新 IP 也继续：Turnstile 未必是 IP 问题，重开浏览器本身
                # 就有机会拿到 token，没理由因为池子空了就白扔一个账号
                # 记 block：盾把请求拦下来说明代理是通的，问题在这个出口 IP 的声誉
                swapped = self._swap_pooled_proxy(account, "block")
                if swapped:
                    swapped_total += 1
                backoff = min(SHIELD_RETRY_BACKOFF_MAX, 5 * shield_rounds)
                backoff = min(backoff, max(0.0, left - 1))
                label = log.STATUS_LABEL.get(row.status, row.status)
                log.warn(f"{label}：第 {shield_rounds} 轮重试"
                         + (f"（已换出口 IP，本账号累计已换 {swapped_total} 个）" if swapped
                            else "（无新 IP 可换，沿用当前 IP 重开浏览器）")
                         + f"，退避 {backoff:.0f}s，"
                         + ("时间盒不限，试到出结果为止" if unlimited
                            else f"剩余时间盒 {left:.0f}s"))
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

        if account.login_method == LOGIN_METHOD_TABIAI:
            return self._attempt_tabiai(account, record, ip)

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

    def _attempt_tabiai(self, account: Account, record, ip: Optional[str]) -> log.SummaryRow:
        """TaBiAI 账号：复用站点 CF 缓存，但换令牌与签到由 TabiAIClient 完成。"""
        if record.cf is not None:
            usable, reason = record.cf.check(ip, account.proxy)
            log.debug(f"TaBiAI S0 缓存判定: {reason}")
            if usable:
                result = self._tabiai_api_call(account, record.cf)
                if result.kind in _SETTLED:
                    return self._row(account, result, "S0")
                if result.kind == api.CF_BLOCKED:
                    log.warn("缓存的 cf_clearance 已被拒绝，作废后重新过盾")
                    self.store.clear_cf(account.slug)
                    record.cf = None
            else:
                self.store.clear_cf(account.slug)
                record.cf = None

        result = self._tabiai_api_call(account, None)
        if result.kind in _SETTLED:
            return self._row(account, result, "S1")
        if result.kind == api.NETWORK_ERROR:
            return self._row(account, result, "S1")
        # Turnstile 不是 CF 盾，但脚本浏览器（S2/S3 那套 + AI）能代为点选拿 token。
        # 首选仍是 CDP 接管本机真实 Chrome；走到这里说明那条路没给出 token
        # （最典型的是 Actions 云端根本没有 Chrome 可接管），于是把这一轮交给过盾链，
        # 让它顺手在浏览器里现取一个 token 回来立刻用掉。没有浏览器时还是死局，照旧跳过。
        if result.kind == api.TURNSTILE_REQUIRED:
            if not self.options.use_browser:
                return self._row(account, result, "S1", detail=result.message)
            log.warn(f"TaBiAI 需要 Turnstile token，转由浏览器代取: {result.message}")
            self._take_browser_attempt(account)
            return self._solve(account, record, ip, result, want_turnstile_token=True)

        verdict = result.verdict
        if result.kind == api.CF_BLOCKED:
            log.warn(f"TaBiAI 站点命中 Cloudflare: {result.message}")
            if verdict is not None and not verdict.recoverable:
                result.kind = api.WAF_BLOCKED
                return self._row(
                    account, result, "S1",
                    detail=f"{verdict.label}：脚本无法绕过，需要换出口 IP 或联系站点放行",
                )
        else:
            log.warn(f"TaBiAI 响应异常，尝试用浏览器复核: {result.message}")

        if not self.options.use_browser:
            return self._row(account, result, "S1", detail="已禁用浏览器过盾（--no-browser）")

        self._take_browser_attempt(account)
        return self._solve(account, record, ip, result)

    def _tabiai_rotate_callback(self, account: Account):
        """凭据轮转的统一落盘回调。

        HTTP 链路（TabiAIClient）和浏览器页内 refresh 都会轮转 new_api_refresh，
        两条路必须用同一套处理：先落本地盘（当轮与后续本机运行都靠它），
        再尽力回写平台。少写一处，下轮就会拿旧代次去撞重放检测。
        """
        from .tabiai import normalize_refresh_cookie

        def on_rotate(value: str) -> None:
            self.store.remember_refresh_cookie(account.slug, value)
            account.cookie = normalize_refresh_cookie(value)
            self._writeback_refresh_cookie(account, value)

        return on_rotate

    def _tabiai_api_call(self, account: Account, cf, turnstile_token: str = "") -> api.ApiResult:
        """跑一轮 TaBiAI 签到。

        turnstile_token 是过盾链刚在浏览器里取到的 token：传了就用它，没传就照旧
        由 _turnstile_provider 决定要不要开 CDP 去取。
        """
        from .tabiai import TabiAIClient

        cookie = self._tabiai_cookie(account)
        if not cookie:
            return api.ApiResult(
                api.AUTH_FAILED,
                message="缺少 TaBiAI 凭据（new_api_refresh），请在管理端签发或从浏览器复制",
            )

        on_rotate = self._tabiai_rotate_callback(account)

        dry = bool(self.options.cookie_test or self.options.dry_run)
        with TabiAIClient(account, self.cfg.http, cookie, cf, on_rotate=on_rotate) as client:
            log.debug(
                f"TaBiAI impersonate={client.impersonate}, "
                + ("缓存 UA" if cf is not None and cf.user_agent else "默认 UA")
                + (f", cookie 条数={len(cf.cookies)}" if cf is not None else "")
            )
            provider = None if dry else self._turnstile_provider(account, turnstile_token)
            result = client.checkin(turnstile_provider=provider, dry_run=dry)
            # 余额已由 TabiAIClient 在 access_token 还在手上时补好（见 attach_balance），
            # 这里只需要把站点换算率探出来缓存，供展示层把额度换算成 $
            if result.kind in (api.SUCCESS, api.ALREADY_DONE):
                try:
                    self._ensure_quota_per_unit(account, client)
                except Exception as exc:  # noqa: BLE001 - 只影响金额显示
                    log.debug(f"探换算率异常，跳过: {type(exc).__name__}: {exc}")
        if result.kind in _SETTLED and result.user_id:
            account.user_id = result.user_id
            self.store.remember(account.slug, user_id=result.user_id)
        return result

    def _tabiai_cookie(self, account: Account) -> str:
        """凭据优先级：配置（可能刚被管理端签发/回写）> 本地 store 的最新代次。"""
        from .tabiai import normalize_refresh_cookie

        configured = normalize_refresh_cookie(account.cookie)
        if configured:
            return configured
        return normalize_refresh_cookie(self.store.get(account.slug).refresh_cookie or "")

    def _writeback_refresh_cookie(self, account: Account, cookie: str) -> None:
        """把轮转后的凭据同步回管理平台，避免网页端下次检测踩旧代。"""
        from .remote_sync import writeback_refresh_cookie

        ok, detail = writeback_refresh_cookie(self.cfg.config_sync, account.name, cookie)
        if ok:
            log.debug(f"新凭据已回写管理平台: {detail}")
        else:
            log.warn(f"新凭据未能回写管理平台（{detail}）：平台仍持有旧代次，"
                     "网页端检测会失败，请重新签发或检查 config_sync.writeback_url")

    def _turnstile_provider(self, account: Account, ready_token: str = ""):
        """返回 () -> (token, error)；没有可用取 token 手段时返回 None。

        ready_token 是过盾链刚在浏览器上下文里现取的 token。它短时、一次性且绑当前
        浏览器上下文，只能在这一轮里立刻用掉，所以做成「用完即弃」的 provider：
        不落盘、不进 store，也不占 Turnstile 的频率配额（token 已经拿到手了，
        再走 _wait_turnstile_slot 只会白等一个间隔）。
        """
        if ready_token:
            def ready():
                return ready_token, ""

            return ready

        tabiai_cfg = getattr(self.cfg, "tabiai", None)
        if tabiai_cfg is None or not tabiai_cfg.enabled:
            return None

        def provider():
            from .cf.driver_cdp import fetch_turnstile_token

            self._wait_turnstile_slot()
            return fetch_turnstile_token(tabiai_cfg, account)

        return provider

    def _wait_turnstile_slot(self) -> None:
        """Turnstile 有频率限制（实测 20 分钟内反复 reset 拿不到新 token），账号间强制间隔。"""
        tabiai_cfg = getattr(self.cfg, "tabiai", None)
        gap = max(0, int(getattr(tabiai_cfg, "token_interval_minutes", 0) or 0)) * 60
        if gap <= 0:
            return
        with self._turnstile_lock:
            last = self._last_turnstile_at
            if last > 0:
                wait = gap - (now() - last)
                if wait > 0:
                    log.info(f"Turnstile 频率限制：等待 {int(wait)} 秒后再取下一个 token")
                    time.sleep(wait)
            self._last_turnstile_at = now()



    # ------------------------------------------------------------------ #
    # 浏览器过盾（S2 / S3 / S4 / S5）
    # ------------------------------------------------------------------ #

    def _solve_guarded(self, solve, account: Account, ip: Optional[str],
                       want_turnstile_token: bool = False, on_rotate=None):
        """在浏览器并发信号量的保护下调用过盾流程。"""
        ai = self.ai()

        def _call():
            return solve(
                cfg=self.cfg,
                account=account,
                exit_ip=ip,
                options=self.options,
                ai=ai,
                want_turnstile_token=want_turnstile_token,
                on_rotate=on_rotate,
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
            self._add_gate_wait(waited)
            return _call_bound()

    def _add_gate_wait(self, seconds: float) -> None:
        """累计本线程排队等浏览器的耗时，等主循环来取。"""
        if seconds <= 0:
            return
        current = getattr(self._gate_waits, "total", 0.0)
        self._gate_waits.total = current + seconds

    def _take_gate_wait(self) -> float:
        """取走并清零本轮排队耗时；调用方负责把它加回时间盒。"""
        total = getattr(self._gate_waits, "total", 0.0)
        self._gate_waits.total = 0.0
        return max(0.0, total)

    def _solve(self, account: Account, record, ip: Optional[str],
               blocked: api.ApiResult, want_turnstile_token: bool = False) -> log.SummaryRow:
        """浏览器过盾链。两种登录方式共用，都能在 S4 里把签到做完：

        - 站点 Cookie：注入登录 cookie，页内直接 POST 签到
        - TaBiAI：注入 new_api_refresh，页内先 refresh 换 Bearer token 再 POST 签到

        TaBiAI 走页内是有意的：Turnstile token 短时一次性，站点校验时会连带看请求环境，
        「浏览器生成、curl_cffi 使用」容易被拒。页内直发让 token 在哪生成就在哪用掉。
        页内路走不通时仍会退回「拿 CF 会话回 HTTP 链路重发」的老路。

        want_turnstile_token 只由 TaBiAI 的 TURNSTILE_REQUIRED 分支传 True。
        """
        try:
            from .cf.solver import solve
        except ImportError as exc:
            log.err(f"浏览器过盾模块不可用: {exc}")
            log.info("安装依赖: pip install -r requirements.txt && python -m camoufox fetch")
            return self._row(account, blocked, "S1", detail=f"过盾模块缺失: {exc}")

        # 只有 TaBiAI 需要轮转回调：页内 refresh 会换代次，必须能落盘才敢走页内。
        # dry-run / cookie_test 模式下不做页内签到，交回原链路按「只检查」处理。
        dry = bool(self.options.cookie_test or self.options.dry_run)
        on_rotate = None
        if account.login_method == LOGIN_METHOD_TABIAI and not dry:
            on_rotate = self._tabiai_rotate_callback(account)

        outcome = self._solve_guarded(solve, account, ip, want_turnstile_token, on_rotate)

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
        if account.login_method == LOGIN_METHOD_TABIAI:
            token = outcome.turnstile_token or ""
            if want_turnstile_token and not token:
                # 本轮取 token 的两条路（CDP 接管真实 Chrome、脚本浏览器现取）都空手而归。
                # 再调一次 _tabiai_api_call 是纯浪费：refresh 会白轮转一代凭据，
                # 而 CDP 那条路还得先干等一个 token_interval 间隔才失败第二次。
                # 直接把 TURNSTILE_REQUIRED 交回主循环，由它换 IP + 重开浏览器再来一轮。
                message = "浏览器过盾成功但仍未取到 Turnstile token"
                if blocked.message:
                    message += f"；上一步原因：{blocked.message}"
                return self._row(account, api.ApiResult(
                    api.TURNSTILE_REQUIRED,
                    message=message,
                    status=blocked.status,
                    path=blocked.path,
                    user_id=blocked.user_id,
                ), outcome.strategy)
            # 浏览器现取的 token 只在这一轮有效，紧接着就交给 TabiAIClient 用掉；
            # 没取到时传空串，_turnstile_provider 会退回原来的 CDP 判定。
            result = self._tabiai_api_call(account, outcome.cf, token)
        else:
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
                        # self 刚查过，余额就在手上，零额外请求就能显示
                        balance=result.balance,
                        message=f"dry-run 连通正常（id={result.user_id}，未执行签到）",
                    )

            result = client.checkin()
            if result.kind in (api.SUCCESS, api.ALREADY_DONE) and result.path:
                self.store.remember(account.slug, checkin_path=result.path)
            if result.kind in (api.SUCCESS, api.ALREADY_DONE):
                self._attach_balance(account, result, client)
            return result

    def _ensure_quota_per_unit(self, account: Account, client) -> None:
        """站点的额度换算率探一次就够，之后从 sessions.json 复用。

        额度在接口里是内部整数（TaBiAI 500000 = $1），要按站点自己的 quota_per_unit
        换算才能显示成钱。不同 fork 这个值不一定一样，所以宁可探一次也不写死。
        """
        if self.store.get(account.slug).quota_per_unit:
            return
        unit = client.fetch_quota_per_unit()
        if unit:
            self.store.remember(account.slug, quota_per_unit=unit)
            log.debug(f"站点额度换算率 quota_per_unit={unit}")

    def _attach_balance(self, account: Account, result: api.ApiResult, client) -> None:
        """签到有结论后补查一次账户余额，顺手把换算率探出来缓存。

        只在签到成功 / 今日已签时查：失败的账号连凭据都可能是坏的，再打一次接口
        既拿不到余额也是白费请求。全程异常吞掉只留 debug —— 余额是邮件里多一列，
        不能因为它把一个已经签成功的账号变成失败。
        """
        try:
            self._ensure_quota_per_unit(account, client)
            me = client.fetch_self()
            if me.balance is None:
                log.debug(f"未能取到剩余额度（{me.kind}: {me.message}）")
                return
            result.balance = me.balance
        except Exception as exc:  # noqa: BLE001 - 补充信息失败不许影响签到
            log.debug(f"查余额异常，跳过: {type(exc).__name__}: {exc}")

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
            account.name, result.kind, STRATEGY_LABEL.get(strategy, strategy), text, result.quota,
            balance=result.balance,
            # 换算率跟着行走：几个站点混在一封邮件里时，每行要按自己站点的比例换算
            quota_per_unit=self.store.get(account.slug).quota_per_unit,
            site=account.base_url,
        )
