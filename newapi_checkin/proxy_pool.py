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

# 测通不设数量上限，时间盒按工作量推导，这里只是额外留出的余量（秒）
REFRESH_SLACK_SECONDS = 15

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
    max_workers: int = 25       # 并发测通数。候选上千时把它调大才跑得快
    max_proxies: int = 250      # 已废弃：测通不再设数量上限，保留只为配置兼容
    ip_swap_limit: int = 10     # 目标站点连不上时最多换几次 IP
    sources: list = field(default_factory=list)   # 空 = 用内置默认源
    # 服务器端代理池预取：配置管理平台已提前抓取+测通，签到前直接拉现成列表，
    # 省去本地抓源+测通（remote_url 非空且请求成功时优先使用，失败降级本地抓取）
    remote_url: str = ""
    remote_token: str = ""
    remote_token_header: str = "Authorization"
    remote_token_prefix: str = "Bearer"

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
            # 上限放宽到 512：全量测通几千条候选时，25 并发要跑十几分钟
            max_workers=max(1, min(512, _as_int(raw.get("max_workers"), 25))),
            max_proxies=max(1, min(100000, _as_int(raw.get("max_proxies"), 250))),
            ip_swap_limit=max(0, min(50, _as_int(raw.get("ip_swap_limit"), 10))),
            sources=sources,
            remote_url=str(raw.get("remote_url") or "").strip(),
            remote_token=str(raw.get("remote_token") or "").strip(),
            remote_token_header=str(raw.get("remote_token_header") or "Authorization").strip() or "Authorization",
            remote_token_prefix=str(raw.get("remote_token_prefix") or "Bearer").strip(),
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
            "remote_url": self.remote_url,
            "remote_token": self.remote_token,
            "remote_token_header": self.remote_token_header,
            "remote_token_prefix": self.remote_token_prefix,
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
        # 代理 -> 正在用它的账号数。池子用尽时按这个值把账号摊到各 IP 上
        self._share_count: dict[str, int] = {}
        self._lock = threading.RLock()
        self.last_error = ""

    # ------------------------------------------------------------------ #
    # 抓取 + 测通
    # ------------------------------------------------------------------ #

    @property
    def sources(self) -> list[str]:
        return list(self.cfg.sources or DEFAULT_SOURCES)

    def refresh(self, desired: Optional[int] = None) -> int:
        """获取可用代理。优先服务器预取列表（remote_url），失败降级本地抓源测通。

        服务器（配置管理平台）已提前抓取+测通并保存可用列表，直接拉取可以
        省掉「现场抓 6 个源 + 并发测通」的几十秒。连不上的仍由上层 mark_bad
        换 IP 兜底，不会变差。
        """
        if self.cfg.remote_url:
            remote = self._fetch_remote()
            if remote:
                with self._lock:
                    self._available = list(remote)
                    self._used.clear()
                    self._bad.clear()
                    # 代理列表整体换了，旧的共用计数必须一起清，否则
                    # acquire() 的「挑账号数最少的代理复用」会按残留计数判断，
                    # 把新代理误判成已被多账号占用，破坏均衡。
                    self._share_count.clear()
                log.ok(f"代理池就绪（服务器预取）: {len(remote)} 个可用代理")
                return len(remote)
            log.warn(f"服务器代理池预取失败/为空，降级本地抓取: {self.last_error or 'remote_url 无返回'}")

        return self._refresh_local(desired)

    def _fetch_remote(self) -> Optional[list]:
        """请求配置管理平台 /api/proxies/available，返回可用代理地址列表。"""
        from curl_cffi import requests as cffi

        headers = {"Accept": "application/json, text/plain, */*"}
        if self.cfg.remote_token:
            prefix = self.cfg.remote_token_prefix.strip()
            headers[self.cfg.remote_token_header] = f"{prefix} {self.cfg.remote_token}".strip()
        try:
            resp = cffi.get(
                self.cfg.remote_url,
                headers=headers,
                timeout=self.cfg.timeout,
                impersonate="chrome",
                verify=True,
            )
        except Exception as exc:  # noqa: BLE001 - 预取是可降级项
            self.last_error = f"远程代理预取请求失败: {type(exc).__name__}: {exc}"
            log.debug(self.last_error)
            return None
        if resp.status_code != 200:
            self.last_error = f"远程代理预取 HTTP {resp.status_code}"
            log.debug(self.last_error)
            return None
        try:
            data = resp.json()
        except Exception as exc:  # noqa: BLE001
            self.last_error = f"远程代理预取响应非 JSON: {type(exc).__name__}: {exc}"
            log.debug(self.last_error)
            return None
        raw_list = data.get("proxies") if isinstance(data, dict) else None
        if not isinstance(raw_list, (list, tuple)) or not raw_list:
            self.last_error = "远程代理预取返回空列表"
            return None
        addrs = [str(p).strip() for p in raw_list if str(p).strip()]
        if not addrs:
            self.last_error = "远程代理预取列表为空"
            return None
        log.debug(f"远程代理预取: {len(addrs)} 条（来源 {data.get('checked_at', '?')}）")
        return addrs

    def _refresh_local(self, desired: Optional[int] = None) -> int:
        """本地抓取所有源 -> 去重 -> 并发测通 -> 保留可用。返回可用数量。

        免费代理源列表通常把「刚测过/存活率高」的排在前面，所以抽样用
        「按源轮转」而不是随机：优先各源最前面的几条，存活率更高。
        第一轮测通不足时自动补测下一批，直到达到目标或候选耗尽。

        抓源是并发的：串行逐个抓时，每个源超时一次就要白等 timeout 秒，
        6 个源最坏能在签到开始前白占将近一分钟。

        测通不设数量上限：抓到多少条就全测多少条，也不会「凑够目标就收手」。
        时间盒按工作量推导（波数 × 单条超时），只用来兜住卡死，不再充当数量上限。
        """
        started = time.monotonic()
        per_source = self._fetch_all_sources()

        if not per_source:
            self.last_error = "所有代理源都未返回可用条目"
            log.warn(self.last_error)
            return 0

        # 全量候选：只做跨源去重，不截断
        candidates = self._round_robin(per_source)
        workers = max(1, min(self.cfg.max_workers, len(candidates)))
        waves = -(-len(candidates) // workers)          # 向上取整
        # 理论最坏耗时 = 波数 × 单条超时；再留一点余量。这不是数量上限，
        # 只在探测整体卡死时兜底，正常情况下会在此之前自然跑完。
        budget = waves * max(1, self.cfg.timeout) + REFRESH_SLACK_SECONDS
        log.info(f"代理池开始测通 {len(candidates)} 条候选"
                 f"（并发 {workers}，预计最多 {budget:.0f}s）")
        alive = self._test_many(candidates, deadline=started + budget)

        with self._lock:
            self._available = alive
            self._used.clear()
            self._bad.clear()
            self._share_count.clear()
        elapsed = time.monotonic() - started
        if not alive:
            self.last_error = "代理候选均未通过连通性测试"
            log.warn(self.last_error)
            return 0
        log.info(f"代理池测通完成: {len(alive)}/{len(candidates)} 条可用，"
                 f"耗时 {elapsed:.1f}s")
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
    def _round_robin(per_source: list[list[str]], limit: Optional[int] = None) -> list[str]:
        """按源轮转合并去重，优先各源前部的条目。limit 为 None 表示全取。"""
        out: list[str] = []
        seen: set[str] = set()
        idx = 0
        while limit is None or len(out) < limit:
            progressed = False
            for entries in per_source:
                if idx < len(entries):
                    progressed = True
                    item = entries[idx]
                    if item not in seen:
                        seen.add(item)
                        out.append(item)
                        if limit is not None and len(out) >= limit:
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
        """当前还能独占分配的代理数量（未使用且未拉黑）。"""
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
        """分配一个代理。优先独占且按质量择优；池子用尽时复用已分配的，绝不返回「直连」。

        调用方的要求是「宁可几个账号共用一个代理 IP，也不要降级直连」，所以只有
        池里连一个未拉黑的代理都没有时才返回 None。共用时挑「当前使用账号数最少」
        的那个，把账号尽量摊平到各个出口 IP 上。

        择优取代理：服务器预取时 available 已按速度/延迟排序（speed_bps 高、
        延迟低在前），本地抓取时 _available 也是按存活+延迟排好序的，所以这里
        顺序取第一个空闲代理就能实现「优选」，而不是随机选。
        """
        with self._lock:
            healthy = [p for p in self._available if p not in self._bad]
            if not healthy:
                return None
            fresh = [p for p in healthy if p not in self._used]
            if fresh:
                proxy = fresh[0]  # 顺序取优：列表头 = 质量最好（服务器已排序）
                self._used.add(proxy)
                self._share_count[proxy] = 1
                return proxy
            # 池已用尽：复用被共用得最少的那个
            proxy = min(healthy, key=lambda p: self._share_count.get(p, 1))
            self._share_count[proxy] = self._share_count.get(proxy, 1) + 1
            shared = self._share_count[proxy]
        log.warn(f"代理池已无空闲 IP，改为共用 {proxy}（该 IP 上已有 {shared} 个账号）")
        return proxy

    def mark_bad(self, proxy: Optional[str]) -> None:
        """目标站点连不上时拉黑该代理，之后不再分配。"""
        if not proxy:
            return
        with self._lock:
            self._bad.add(proxy)
            self._share_count.pop(proxy, None)

    def has_available(self) -> bool:
        """还有没有「未拉黑」的代理可分配（含可共用的）。"""
        with self._lock:
            return any(p not in self._bad for p in self._available)

    def has_exclusive(self) -> bool:
        """还有没有完全空闲、可独占分配的代理。"""
        with self._lock:
            return any(p not in self._used and p not in self._bad for p in self._available)
