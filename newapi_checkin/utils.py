"""通用工具：代理解析、出口 IP 探测、随机间隔、宽松 JSON 解析。"""

from __future__ import annotations

import json
import random
import re
import time
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from typing import Any, Optional, Tuple
from urllib.parse import urlparse

IP_PROBE_URLS = (
    "https://api.ipify.org",
    "https://ifconfig.me/ip",
    "https://icanhazip.com",
)

_IPV4 = re.compile(r"^\d{1,3}(?:\.\d{1,3}){3}$")


def now() -> float:
    return time.time()


def parse_proxy(proxy_url: Optional[str]) -> Optional[dict]:
    """把 http://user:pass@host:port 转成 Playwright/Camoufox 需要的 dict 形式。"""
    if not proxy_url:
        return None
    parsed = urlparse(proxy_url if "://" in proxy_url else f"http://{proxy_url}")
    if not parsed.hostname:
        return None
    port = f":{parsed.port}" if parsed.port else ""
    out = {"server": f"{parsed.scheme or 'http'}://{parsed.hostname}{port}"}
    if parsed.username:
        out["username"] = parsed.username
    if parsed.password:
        out["password"] = parsed.password
    return out


def jitter_sleep(bounds: Tuple[float, float]) -> float:
    """账号之间随机停顿，避免同 IP 集中请求。返回实际睡眠秒数。"""
    lo, hi = float(bounds[0]), float(bounds[1])
    if hi <= 0:
        return 0.0
    delay = random.uniform(max(0.0, lo), hi)
    time.sleep(delay)
    return delay


def _probe_one(url: str, proxy: Optional[str], timeout: int) -> Optional[str]:
    try:
        from curl_cffi import requests as cffi_requests
    except ImportError:
        return None
    try:
        resp = cffi_requests.get(
            url, timeout=timeout, proxies={"http": proxy, "https": proxy} if proxy else None,
            impersonate="chrome",
        )
    except Exception:  # noqa: BLE001 - 探测是可选项，任何失败都直接跳过
        return None
    text = (resp.text or "").strip()
    if _IPV4.match(text) or ":" in text:
        return text
    return None


def probe_exit_ip(proxy: Optional[str] = None, timeout: int = 5) -> Optional[str]:
    """探测当前出口 IP。cf_clearance 与 IP 绑定，缓存复用前必须比对。

    三个探测端点并发竞速，第一个返回合法 IP 就立刻采用——串行逐个试的话，
    前两个端点各超时一次就要白等 2 * timeout 秒，而这只是可降级的辅助校验。

    探测失败返回 None —— 此时不阻断流程，只是放弃 IP 比对这一层校验。
    """
    try:
        import curl_cffi  # noqa: F401
    except ImportError:
        return None

    pool = ThreadPoolExecutor(max_workers=len(IP_PROBE_URLS), thread_name_prefix="exitip")
    try:
        pending = {pool.submit(_probe_one, url, proxy, timeout) for url in IP_PROBE_URLS}
        deadline = time.monotonic() + timeout + 1
        while pending:
            left = max(0.1, deadline - time.monotonic())
            done, pending = wait(pending, timeout=left, return_when=FIRST_COMPLETED)
            if not done:
                break
            for future in done:
                try:
                    ip = future.result()
                except Exception:  # noqa: BLE001
                    ip = None
                if ip:
                    return ip
        return None
    finally:
        # 不等剩余探测收尾：出口 IP 只是辅助校验，不该拖住签到主流程
        pool.shutdown(wait=False, cancel_futures=True)


_FENCE = re.compile(r"```(?:json)?\s*(.*?)\s*```", re.S | re.I)


def loose_json(text: Any) -> Optional[dict]:
    """容错解析模型输出的 JSON。

    模型经常在 JSON 外面套 ```json 围栏或加一段解释，直接 json.loads 会炸。
    依次尝试：整体解析 -> 剥围栏 -> 提取首个平衡的 {...}。全失败返回 None。
    """
    if isinstance(text, dict):
        return text
    if not isinstance(text, str):
        return None
    raw = text.strip()
    if not raw:
        return None

    for candidate in (raw, *(m.group(1) for m in _FENCE.finditer(raw))):
        try:
            parsed = json.loads(candidate)
        except (json.JSONDecodeError, TypeError):
            continue
        if isinstance(parsed, dict):
            return parsed

    block = _first_json_object(raw)
    if block is None:
        return None
    try:
        parsed = json.loads(block)
    except json.JSONDecodeError:
        return None
    return parsed if isinstance(parsed, dict) else None


def _first_json_object(text: str) -> Optional[str]:
    """扫描出第一个花括号平衡的片段，跳过字符串字面量里的括号。"""
    start = text.find("{")
    if start < 0:
        return None
    depth = 0
    in_str = False
    escaped = False
    for idx in range(start, len(text)):
        ch = text[idx]
        if in_str:
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == '"':
                in_str = False
            continue
        if ch == '"':
            in_str = True
        elif ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return text[start : idx + 1]
    return None


def clamp(value: float, lo: float = 0.0, hi: float = 1.0) -> float:
    return max(lo, min(hi, value))
