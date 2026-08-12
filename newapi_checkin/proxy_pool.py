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
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import Optional

from . import logger as log

# 目标站点无关的纯测通接口
DEFAULT_TEST_URL = "https://api.ipify.org"

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
    max_workers: int = 8
    max_proxies: int = 100      # 每次最多测通多少条（太多测太慢）
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
            max_workers=max(1, min(32, _as_int(raw.get("max_workers"), 8))),
            max_proxies=max(1, min(1000, _as_int(raw.get("max_proxies"), 100))),
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
        """
        per_source: list[list[str]] = []
        for source in self.sources:
            try:
                from curl_cffi import requests as cffi

                resp = cffi.get(
                    source, timeout=self.cfg.timeout,
                    impersonate="chrome", verify=True,
                )
                if resp.status_code != 200:
                    log.debug(f"代理源 {source} HTTP {resp.status_code}，跳过")
                    continue
                found = parse_proxy_lines(resp.text)
                if not found:
                    log.debug(f"代理源 {source} 未提取到代理，跳过")
                    continue
                per_source.append(found)
                log.debug(f"代理源 {source}: 提取 {len(found)} 条")
            except Exception as exc:  # noqa: BLE001 - 单个源失败不影响整体
                log.debug(f"代理源 {source} 抓取失败: {type(exc).__name__}: {exc}")

        if not per_source:
            self.last_error = "所有代理源都未返回可用条目"
            log.warn(self.last_error)
            return 0

        alive: list[str] = []
        # 目标可用数：够账号数 + 换 IP 余量即可，避免无限测通拖时间
        target = max(desired or 0, 10, min(self.cfg.max_proxies, 30))
        rounds = 0
        max_rounds = 4
        # 轮转合并候选，优先各源最新鲜的条目
        candidates = self._round_robin(per_source, target * max_rounds)
        while candidates and len(alive) < target and rounds < max_rounds:
            batch = candidates[: self.cfg.max_proxies]
            candidates = candidates[self.cfg.max_proxies :]
            rounds += 1
            batch_alive = self._test_many(batch)
            alive.extend(batch_alive)
            log.info(
                f"代理池第 {rounds} 轮: 测 {len(batch)} 条，新通 {len(batch_alive)} 条"
                f"（累计 {len(alive)}）"
            )

        with self._lock:
            self._available = alive
            self._used.clear()
            self._bad.clear()
        if not alive:
            self.last_error = "代理候选均未通过连通性测试"
            log.warn(self.last_error)
            return 0
        log.ok(f"代理池就绪: {len(alive)} 个可用代理")
        return len(alive)

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

    def _test_many(self, candidates: list[str]) -> list[str]:
        if not candidates:
            return []
        workers = max(1, min(self.cfg.max_workers, len(candidates)))
        with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="probe") as pool:
            results = list(pool.map(self._test_one, candidates))
        return [proxy for proxy, ok in zip(candidates, results) if ok]

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
