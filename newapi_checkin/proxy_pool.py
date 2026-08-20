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
    ip_swap_limit: int = 10     # 已废弃：网络异常换 IP 不限次数，保留只为兼容旧配置
    sources: list = field(default_factory=list)   # 空 = 用内置默认源
    # 服务器端代理池预取：配置管理平台已提前抓取+测通，签到前直接拉现成列表，
    # 省去本地抓源+测通（remote_url 非空且请求成功时优先使用，失败降级本地抓取）
    remote_url: str = ""
    remote_token: str = ""
    remote_token_header: str = "Authorization"
    remote_token_prefix: str = "Bearer"
    # 跑完把每个代理的成败计数回传给平台（POST /api/proxies/feedback），下次预取时
    # 服务器据此优选。关掉的话排序只剩服务器自测的延迟/测速，而那是服务器到代理的
    # 链路，跟 Actions runner 那边能不能用不是一回事
    report_feedback: bool = True

    @classmethod
    def from_raw(cls, raw: Optional[dict]) -> "ProxyPoolConfig":
        raw = raw if isinstance(raw, dict) else {}
        sources = raw.get("sources")
        if isinstance(sources, (list, tuple)):
            sources = [str(s).strip() for s in sources if str(s).strip()]
        else:
            sources = []
        return cls(
            # enabled 严格布尔解析：字符串 "false"/"0"/"no" 不能因为非空而被
            # bool() 误判成 True（旧实现 bool("false") == True，属于配置解析 bug）
            enabled=_as_bool(raw.get("enabled"), False),
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
            report_feedback=_as_bool(raw.get("report_feedback"), True),
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
            "report_feedback": self.report_feedback,
        }


def _as_int(value, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _as_bool(value, default: bool = False) -> bool:
    """严格布尔解析：接受 bool、0/1、'true'/'false'/'yes'/'no'/'on'/'off' 等，
    其余一律回退到 default。避免 bool("false") == True 这类误判。"""
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value != 0
    if isinstance(value, str):
        text = value.strip().lower()
        if text in ("true", "1", "yes", "on", "y"):
            return True
        if text in ("false", "0", "no", "off", "n"):
            return False
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
        # 上一次 _test_many 真正测完的候选数（时间盒截断时小于候选总数），
        # 只用于日志口径：分母必须是「测完的」而不是「提交的」
        self._last_tested = 0
        # 代理 -> {"ok": n, "net_fail": n, "block_fail": n}，跑完回传给平台做优选。
        # 必须在失败当场记：_pooled_proxies 换过 IP 后只剩最新值，事后反推会漏掉
        # 中途被换掉的那些坏代理，而它们恰恰是最该记下来的
        self._stats: dict[str, dict[str, int]] = {}
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

    def _auth_headers(self) -> dict:
        """预取和回传共用的请求头（带上平台 API Key）。"""
        headers = {"Accept": "application/json, text/plain, */*"}
        if self.cfg.remote_token:
            prefix = self.cfg.remote_token_prefix.strip()
            headers[self.cfg.remote_token_header] = f"{prefix} {self.cfg.remote_token}".strip()
        return headers

    def _fetch_remote(self) -> Optional[list]:
        """请求配置管理平台 /api/proxies/available，返回可用代理地址列表。"""
        from curl_cffi import requests as cffi

        headers = self._auth_headers()
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
        log.debug(f"远程代理预取: {len(addrs)} 条（服务端测通时间 "
                  f"{data.get('checked_at', '?')}）")
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
        # 分母必须是「真正测完的候选数」。时间盒截断时若拿总候选数当分母，
        # 会打出「12/3000 条可用」，让人误以为 3000 条全测过、可用率极低。
        tested = self._last_tested or len(candidates)
        truncated = tested < len(candidates)
        if not alive:
            self.last_error = (f"已测的 {tested} 条代理候选均未通过连通性测试"
                               if truncated else "代理候选均未通过连通性测试")
            log.warn(self.last_error)
            return 0
        if truncated:
            log.info(f"代理池测通完成: {len(alive)}/{tested} 条可用"
                     f"（{budget:.0f}s 时间盒内只测完 {tested}/{len(candidates)} 条候选），"
                     f"耗时 {elapsed:.1f}s")
        else:
            log.info(f"代理池测通完成: {len(alive)}/{tested} 条可用，"
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
        """受控派发并发测通，结果按延迟升序排列。

        不一次性 submit 全部候选：候选常以千计，一次全提交会把几万个 future
        同时挂在内存和线程池队列里。这里维护一个与并发数等宽的在飞窗口，
        完成一个补一个，窗口大小有界。

        返回的可用代理按延迟从小到大排序（快的在前），这样 acquire() 顺序
        取优时拿到的就是响应最快的出口 IP。凑够 need 条或到达 deadline 就
        立刻收手，不等剩下的慢连接超时。
        """
        if not candidates:
            return []
        workers = max(1, min(self.cfg.max_workers, len(candidates)))
        # (延迟, 代理)：测通后按延迟排序，快的排前面
        alive: list[tuple[float, str]] = []
        tested = 0
        pool = ThreadPoolExecutor(max_workers=workers, thread_name_prefix="probe")
        futures: dict = {}
        try:
            # 首波：只提交并发数等宽的一批，其余候选等有坑位再补
            next_idx = 0
            while next_idx < workers and next_idx < len(candidates):
                proxy = candidates[next_idx]
                futures[pool.submit(self._test_one, proxy)] = proxy
                next_idx += 1
            while futures:
                timeout = None if deadline is None else max(0.1, deadline - time.monotonic())
                try:
                    for future in as_completed(futures, timeout=timeout):
                        proxy = futures.pop(future)
                        tested += 1
                        try:
                            latency = future.result()
                        except Exception:  # noqa: BLE001 - 探测失败一律按不通
                            latency = None
                        if latency is not None:
                            alive.append((latency, proxy))
                            if need is not None and len(alive) >= need:
                                # 已经够用了：取消剩余在飞任务，立即收手
                                for pending in futures:
                                    pending.cancel()
                                futures.clear()
                                break
                        # 受控派发：完成一个就补一个，保持窗口宽度 = 并发数
                        if next_idx < len(candidates):
                            proxy = candidates[next_idx]
                            futures[pool.submit(self._test_one, proxy)] = proxy
                            next_idx += 1
                except FuturesTimeout:
                    log.debug(f"代理测通到达时间盒，本批只收到 {len(alive)} 条可用"
                              f"（已测 {tested}/{len(candidates)} 条）")
                    break
        finally:
            self._last_tested = tested
            # 已经够用了就别再等剩余候选各自超时；wait=False 立即返回
            pool.shutdown(wait=False, cancel_futures=True)
        alive.sort(key=lambda item: item[0])
        return [proxy for _latency, proxy in alive]

    def available_count(self) -> int:
        """当前还能独占分配的代理数量（未使用且未拉黑）。"""
        with self._lock:
            return sum(1 for p in self._available if p not in self._used and p not in self._bad)

    def _test_one(self, proxy: str) -> Optional[float]:
        """测通单个代理。成功返回探测耗时（秒，用于按延迟排序），失败返回 None。"""
        try:
            from curl_cffi import requests as cffi

            started = time.monotonic()
            resp = cffi.get(
                self.cfg.test_url,
                timeout=self.cfg.timeout,
                proxies={"http": proxy, "https": proxy},
                impersonate="chrome",
                verify=True,
            )
            if resp.status_code == 200 and bool((resp.text or "").strip()):
                return time.monotonic() - started
            return None
        except Exception:  # noqa: BLE001 - 代理不通原因很多，统一按失败
            return None

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
        log.warn(f"代理池已无空闲 IP，改为共用 {proxy}（含本账号，该 IP 上共 {shared} 个账号在用）")
        return proxy

    def mark_bad(self, proxy: Optional[str], reason: str = "net") -> None:
        """目标站点连不上时拉黑该代理，之后不再分配。

        reason 分「网络层连不上」(net) 和「连上了但被拦」(block)。对签到来说两者后果
        一样都得换 IP，但分开回传后排查时能看出是代理死了，还是这个出口 IP 被目标站
        盯上了 —— 后者换个同机房的 IP 往往还是被拦。
        """
        if not proxy:
            return
        with self._lock:
            self._bad.add(proxy)
            self._share_count.pop(proxy, None)
            self._bump(proxy, "block_fail" if reason == "block" else "net_fail")

    def mark_ok(self, proxy: Optional[str]) -> None:
        """经这个代理签到成功，记一笔供平台优选。"""
        if not proxy:
            return
        with self._lock:
            self._bump(proxy, "ok")

    def _bump(self, proxy: str, field: str) -> None:
        """给某个代理的计数加一。调用方必须已持有 _lock。"""
        entry = self._stats.setdefault(proxy, {"ok": 0, "net_fail": 0, "block_fail": 0})
        entry[field] = entry.get(field, 0) + 1

    def _feedback_endpoint(self) -> str:
        """按预取 URL 同源推导 /api/proxies/feedback。

        remote_url 可能带 ?limit= 之类的查询串，所以只取 scheme+netloc 重新拼路径。
        推不出来就不回传 —— 这是尽力而为的事，绝不能反过来影响签到。
        """
        from urllib.parse import urlsplit, urlunsplit

        if not self.cfg.remote_url:
            return ""
        parts = urlsplit(self.cfg.remote_url)
        if not parts.scheme or not parts.netloc:
            return ""
        return urlunsplit((parts.scheme, parts.netloc, "/api/proxies/feedback", "", ""))

    def report_feedback(self, source: str = "github-actions") -> tuple:
        """把本轮各代理的成败计数回传给平台。失败只返回原因，不抛异常。

        平台那边只累加不覆盖，所以重复上报同一轮会把计数记重 —— 调用方保证一轮只调
        一次（Runner 放在 run() 的 finally 里）。
        """
        if not self.cfg.report_feedback:
            return False, "report_feedback 已关闭"
        items = self.feedback_snapshot()
        if not items:
            return False, "本轮没有可回传的代理记录"
        endpoint = self._feedback_endpoint()
        if not endpoint:
            return False, "无法确定回传地址（proxy_pool.remote_url 未配置或非法）"

        from curl_cffi import requests as cffi

        try:
            resp = cffi.post(
                endpoint,
                headers={**self._auth_headers(), "Content-Type": "application/json"},
                json={"source": source, "items": items},
                timeout=self.cfg.timeout,
                impersonate="chrome",
                verify=True,
            )
        except Exception as exc:  # noqa: BLE001 - 网络库异常类型不固定
            return False, f"{type(exc).__name__}: {exc}"[:160]
        status = int(getattr(resp, "status_code", 0) or 0)
        if status >= 400:
            return False, f"HTTP {status}: {str(getattr(resp, 'text', '') or '')[:160]}"
        return True, f"已回传 {len(items)} 条"

    def feedback_snapshot(self) -> list:
        """导出本次运行各代理的成败计数，形状与平台 /api/proxies/feedback 的 items 对齐。

        故意不随 refresh 一起清：中途换过列表的话，之前那批代理的表现照样有参考价值。
        这里导出的是「这轮用过的代理表现如何」，跟当前池里还剩谁无关。
        """
        with self._lock:
            return [
                {
                    "addr": addr,
                    "ok": c.get("ok", 0),
                    "net_fail": c.get("net_fail", 0),
                    "block_fail": c.get("block_fail", 0),
                }
                for addr, c in self._stats.items()
                if c.get("ok", 0) or c.get("net_fail", 0) or c.get("block_fail", 0)
            ]

    def has_available(self) -> bool:
        """还有没有「未拉黑」的代理可分配（含可共用的）。"""
        with self._lock:
            return any(p not in self._bad for p in self._available)

    def has_exclusive(self) -> bool:
        """还有没有完全空闲、可独占分配的代理。"""
        with self._lock:
            return any(p not in self._used and p not in self._bad for p in self._available)
