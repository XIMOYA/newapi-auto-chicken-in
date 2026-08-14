"""免费代理池：多源抓取 -> 合并去重 -> 并发测通 -> 随机分配。

设计要点（对应需求）：
  - 测通只打探活接口（默认 api.ipify.org），绝不打目标站点，避免给站点增压
  - 池空了降级直连签到，绝不中断流程（与 AI 可降级同一哲学）
  - 一个账号对应一个 IP：acquire() 保证同一次运行内不重复分配
  - 目标站点连不上（网络层失败）时 mark_bad() 拉黑该代理，由上层换一个新 IP
  - 89ip.cn 返回的是 HTML（IP:PORT<br> 嵌在广告脚本里），用正则提取；
    其余 GitHub raw 源是纯文本，同一正则兼容
"""

from __future__ import annotations

import random
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from concurrent.futures import TimeoutError as FuturesTimeout
from dataclasses import dataclass, field
from typing import Optional

from . import logger as log

# 目标站点无关的纯测通接口
DEFAULT_TEST_URL = "https://api.ipify.org"

# 抓取+测通的总时长上限：账号多时要尽量凑够 IP，但不能无限拖住签到开始
REFRESH_BUDGET_SECONDS = 45
# 免费代理的存活率通常只有 5%~10%，候选池要按目标数放大取样才可能凑够
CANDIDATE_MULTIPLIER = 20

# 默认代理源。sources 配置为空时用这里；填了就替换
DEFAULT_SOURCES = (
    "https://raw.githubusercontent.com/monosans/proxy-list/main/proxies/http.txt",
    "https://raw.githubusercontent.com/TheSpeedX/PROXY-List/master/http.txt",
    "https://raw.githubusercontent.com/proxy4parsing/proxy-list/main/http.txt",
    "https://raw.githubusercontent.com/clarketm/proxy-list/master/proxy-list-raw.txt",
    "https://raw.githubusercontent.com/jetkai/proxy-list/main/online-proxies/txt/proxies-http.txt",
    "https://www.89ip.cn/tqdl.html?api=1&num=200",
)

# IP:PORT 提取。同时兼容纯文本行与 HTML（<br> 分隔、广告脚本夹杂）
_IP_PORT_RE = re.compile(r"\b(\d{1,3}(?:\.\d{1,3}){3}):(\d{1,5})\b")


def _valid_ip(ip: str) -> bool:
    try:
        return all(0 <= int(part) <= 255 for part in ip.split("."))
    except ValueError:
        return False


def _valid_port(port: int) -> bool:
    return 1 <= port <= 65535


def parse_proxy_lines(text: str) -> list[str]:
    """从纯文本或 HTML 中提取 host:port 列表，过滤非法 IP/端口。"""
    out: list[str] = []
    for ip, port in _IP_PORT_RE.findall(text or ""):
        if _valid_ip(ip) and _valid_port(int(port)):
            out.append(f"{ip}:{port}")
    return out


@dataclass
class ProxyPoolConfig:
    enabled: bool = False
    test_url: str = DEFAULT_TEST_URL
    timeout: int = 8
    max_workers: int = 25
    max_proxies: int = 250      # 每次最多测通多少条（太多测太慢）
    ip_swap_limit: int = 2      # 目标站点连不上时最多换几次 IP
    sources: list = field(default_factory=list)   # 空 = 用内置默认源

    @classmethod
    def from_raw(cls, raw: Optional[dict]) -> "ProxyPoolConfig":
        raw = raw if isinstance(raw, dict) else {}
        sources = raw.get("sources")
        if isinstance(sources, (list, tuple)):
            sources = [str(s).strip() for s in sources if str(s).strip()]
        else:
            sources = []
        return cls(
            enabled=bool(raw.get("enabled", False)),
            test_url=str(raw.get("test_url") or DEFAULT_TEST_URL).strip() or DEFAULT_TEST_URL,
            timeout=max(2, min(60, _as_int(raw.get("timeout"), 8))),
            max_workers=max(1, min(32, _as_int(raw.get("max_workers"), 25))),
            max_proxies=max(1, min(1000, _as_int(raw.get("max_proxies"), 250))),
            ip_swap_limit=max(0, min(10, _as_int(raw.get("ip_swap_limit"), 2))),
            sources=sources,
        )

    def to_dict(self) -> dict:
        return {
            "enabled": self.enabled,
            "test_url": self.test_url,
            "timeout": self.timeout,
            "max_workers": self.max_workers,
            "max_proxies": self.max_proxies,
            "ip_swap_limit": self.ip_swap_limit,
            "sources": list(self.sources),
        }


def _as_int(value, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


class ProxyPool:
    """抓取/测通/分配一体化。线程安全，可被 Runner 并行使用。"""

    def __init__(self, cfg: ProxyPoolConfig):
        self.cfg = cfg
        self._available: list[str] = []
        self._used: set[str] = set()
        self._bad: set[str] = set()
        self._lock = threading.RLock()
        self.last_error = ""

    # ------------------------------------------------------------------ #
    # 抓取 + 测通
    # ------------------------------------------------------------------ #

    @property
    def sources(self) -> list[str]:
        return list(self.cfg.sources or DEFAULT_SOURCES)

    def refresh(self, desired: Optional[int] = None) -> int:
        """抓取所有源 -> 去重 -> 并发测通 -> 保留可用。返回可用数量。

        免费代理源列表通常把「刚测过/存活率高」的排在前面，所以抽样用
        「按源轮转」而不是随机：优先各源最前面的几条，存活率更高。
        第一轮测通不足时自动补测下一批，直到达到目标或候选耗尽。

        抓源是并发的：串行逐个抓时，每个源超时一次就要白等 timeout 秒，
        6 个源最坏能在签到开始前白占将近一分钟。

        账号数越多需要的独立 IP 越多，所以候选取样量按目标数放大；整个过程被
        REFRESH_BUDGET_SECONDS 时间盒约束，凑不够就凑多少算多少（上层降级直连）。
        """
        started = time.monotonic()
        per_source = self._fetch_all_sources()

        if not per_source:
            self.last_error = "所有代理源都未返回可用条目"
            log.warn(self.last_error)
            return 0

        alive: list[str] = []
        # 目标可用数：够账号数 + 换 IP 余量即可，避免无限测通拖时间
        target = max(desired or 0, 10, min(self.cfg.max_proxies, 30))
        total_found = sum(len(entries) for entries in per_source)
        candidate_limit = min(total_found, max(target * CANDIDATE_MULTIPLIER,
                                              self.cfg.max_proxies))
        # 轮转合并候选，优先各源最新鲜的条目
        candidates = self._round_robin(per_source, candidate_limit)
        deadline = started + REFRESH_BUDGET_SECONDS
        rounds = 0
        while candidates and len(alive) < target:
            remaining = deadline - time.monotonic()
            if remaining <= 1.0:
                log.warn(f"代理池测通已用满 {REFRESH_BUDGET_SECONDS}s 预算，"
                         f"以当前 {len(alive)} 个可用代理继续")
                break
            batch = candidates[: self.cfg.max_proxies]
            candidates = candidates[self.cfg.max_proxies :]
            rounds += 1
            batch_alive = self._test_many(batch, need=target - len(alive),
                                         deadline=deadline)
            alive.extend(batch_alive)
            log.info(
                f"代理池第 {rounds} 轮: 测 {len(batch)} 条，新通 {len(batch_alive)} 条"
                f"（累计 {len(alive)}/{target}）"
            )

        with self._lock:
            self._available = alive
            self._used.clear()
            self._bad.clear()
        if not alive:
            self.last_error = "代理候选均未通过连通性测试"
            log.warn(self.last_error)
            return 0
        log.debug(f"代理池测通完成: {len(alive)} 个可用代理，"
                  f"候选 {candidate_limit}/{total_found} 条，"
                  f"耗时 {time.monotonic() - started:.1f}s")
        return len(alive)

    def _fetch_all_sources(self) -> list[list[str]]:
        """并发抓取所有代理源，返回每个源各自的条目列表（保持源内顺序）。"""
        sources = self.sources
        if not sources:
            return []
        results: dict[str, list[str]] = {}
        workers = max(1, min(len(sources), 8))
        with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="proxysrc") as pool:
            futures = {pool.submit(self._fetch_source, src): src for src in sources}
            for future in as_completed(futures):
                source = futures[future]
                try:
                    found = future.result()
                except Exception as exc:  # noqa: BLE001 - 单个源失败不影响整体
                    log.debug(f"代理源 {source} 抓取失败: {type(exc).__name__}: {exc}")
                    continue
                if found:
                    results[source] = found
        # 按配置顺序还原，保证 _round_robin 的优先级稳定可预期
        return [results[src] for src in sources if src in results]

    def _fetch_source(self, source: str) -> list[str]:
        from curl_cffi import requests as cffi

        resp = cffi.get(source, timeout=self.cfg.timeout, impersonate="chrome", verify=True)
        if resp.status_code != 200:
            log.debug(f"代理源 {source} HTTP {resp.status_code}，跳过")
            return []
        found = parse_proxy_lines(resp.text)
        if not found:
            log.debug(f"代理源 {source} 未提取到代理，跳过")
            return []
        log.debug(f"代理源 {source}: 提取 {len(found)} 条")
        return found

    @staticmethod
    def _round_robin(per_source: list[list[str]], limit: int) -> list[str]:
        """按源轮转合并去重，优先各源前部的条目，最多取 limit 条。"""
        out: list[str] = []
        seen: set[str] = set()
        idx = 0
        while len(out) < limit:
            progressed = False
            for entries in per_source:
                if idx < len(entries):
                    progressed = True
                    item = entries[idx]
                    if item not in seen:
                        seen.add(item)
                        out.append(item)
                        if len(out) >= limit:
                            return out
            if not progressed:
                break
            idx += 1
        return out

    def _test_many(self, candidates: list[str], need: Optional[int] = None,
                   deadline: Optional[float] = None) -> list[str]:
        """并发测通。凑够 need 条或到达 deadline 就立刻收手，不等剩下的慢连接超时。"""
        if not candidates:
            return []
        workers = max(1, min(self.cfg.max_workers, len(candidates)))
        alive: list[str] = []
        pool = ThreadPoolExecutor(max_workers=workers, thread_name_prefix="probe")
        try:
            futures = {pool.submit(self._test_one, proxy): proxy for proxy in candidates}
            timeout = None if deadline is None else max(0.1, deadline - time.monotonic())
            try:
                for future in as_completed(futures, timeout=timeout):
                    try:
                        ok = future.result()
                    except Exception:  # noqa: BLE001 - 探测失败一律按不通
                        ok = False
                    if ok:
                        alive.append(futures[future])
                        if need is not None and len(alive) >= need:
                            break
            except FuturesTimeout:
                log.debug(f"代理测通到达时间盒，本批只收到 {len(alive)} 条可用")
        finally:
            # 已经够用了就别再等剩余候选各自超时；wait=False 立即返回
            pool.shutdown(wait=False, cancel_futures=True)
        return alive

    def available_count(self) -> int:
        """当前还能分配出去的代理数量（未使用且未拉黑）。"""
        with self._lock:
            return sum(1 for p in self._available if p not in self._used and p not in self._bad)

    def _test_one(self, proxy: str) -> bool:
        try:
            from curl_cffi import requests as cffi

            resp = cffi.get(
                self.cfg.test_url,
                timeout=self.cfg.timeout,
                proxies={"http": proxy, "https": proxy},
                impersonate="chrome",
                verify=True,
            )
            return resp.status_code == 200 and bool((resp.text or "").strip())
        except Exception:  # noqa: BLE001 - 代理不通原因很多，统一按失败
            return False

    # ------------------------------------------------------------------ #
    # 分配
    # ------------------------------------------------------------------ #

    def acquire(self) -> Optional[str]:
        """随机分配一个未使用且未拉黑的代理；没有则返回 None（上层降级直连）。"""
        with self._lock:
            candidates = [
                p for p in self._available
                if p not in self._used and p not in self._bad
            ]
            if not candidates:
                return None
            proxy = random.choice(candidates)
            self._used.add(proxy)
            return proxy

    def mark_bad(self, proxy: Optional[str]) -> None:
        """目标站点连不上时拉黑该代理，之后不再分配。"""
        if not proxy:
            return
        with self._lock:
            self._bad.add(proxy)

    def has_available(self) -> bool:
        with self._lock:
            return any(p not in self._used and p not in self._bad for p in self._available)
