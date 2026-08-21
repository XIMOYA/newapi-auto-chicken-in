"""newapi_checkin/pricing.py
站点定价表：拉 /api/pricing，算「这笔余额还能发多少次请求」。

为什么单独一个模块：定价是**站点级**信息（一个域名一份），而余额是账号级的。
签到流程按账号跑，汇总时才需要把同一站点的账号并到一起、配上那份定价表。

每轮实时拉、不落缓存 —— 站点随时可能调价，用昨天的价格算出来的次数是假的。
接口公开，不需要 cookie，但裸请求会被 Cloudflare 拦，必须走 curl_cffi 的
指纹伪装（和 S1 快路径同一套 impersonate）。

两种计费模式（New API 的 quota_type）：
  1 = 按次计费，model_price 就是每次请求的美元单价 -> 次数可以精确算
  0 = 按 token 计费，价格由 model_ratio / completion_ratio 决定 -> 没有「次数」
      这个概念，只能标明计费方式，不硬套一个假设的 token 数糊弄人
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Optional

from curl_cffi import requests as cffi

from . import logger as log
from .utils import sanitize_header_value

PRICING_PATH = "/api/pricing"
# 按次计费
QUOTA_TYPE_PER_CALL = 1
# 按 token 计费
QUOTA_TYPE_PER_TOKEN = 0
# 默认只关心 opus 系列。匹配模型名的子串，不区分大小写
DEFAULT_MODEL_FILTER = ("opus",)
# 非代理原因失败时最多重试几次。代理原因不受这个数限制，换一个继续
MAX_NON_PROXY_ATTEMPTS = 5


def _proxy_error_codes() -> frozenset:
    """带代理时基本都指向代理本身的 libcurl 错误码。

    用 CurlECode 常量而不是写死数字：版本升级时数字可能变，名字不会。取不到
    常量表（老版本 curl_cffi）就退回空集合，那时只靠错误信息里的关键词判断。
    """
    try:
        from curl_cffi.const import CurlECode as C
    except Exception:  # noqa: BLE001 - 拿不到就降级
        return frozenset()
    names = ("COULDNT_RESOLVE_PROXY", "COULDNT_CONNECT", "PROXY", "SEND_ERROR",
             "RECV_ERROR", "SSL_CONNECT_ERROR", "OPERATION_TIMEDOUT",
             "GOT_NOTHING", "PARTIAL_FILE", "NO_CONNECTION_AVAILABLE")
    return frozenset(int(getattr(C, n)) for n in names if hasattr(C, n))


_PROXY_ERROR_CODES = _proxy_error_codes()


def is_proxy_fault(exc: Exception, proxy: Optional[str]) -> bool:
    """这次失败该不该记在代理头上。

    判定口径：没走代理就一定不是代理的错。走了代理时，网络层错误（解析不到代理、
    连不上、隧道建不起来、传输被打断、超时）都算代理问题 —— 从客户端分不清
    「代理死了」和「目标站不通」，而带代理时前者概率高得多。目标站自己的问题会
    表现成 HTTP 状态码或非 JSON 响应，压根走不到这个异常分支。
    """
    if not proxy:
        return False
    code = getattr(exc, "code", None)
    if code is not None:
        try:
            if int(code) in _PROXY_ERROR_CODES:
                return True
        except (TypeError, ValueError):
            pass
    # 兜底看错误文本：curl 的报错里带代理时会明确写 over proxy / proxy CONNECT
    return "proxy" in f"{exc}".lower()


@dataclass
class ModelPrice:
    name: str
    quota_type: int = QUOTA_TYPE_PER_CALL
    price: float = 0.0              # quota_type=1：每次请求的美元单价
    model_ratio: float = 0.0        # quota_type=0：输入倍率
    completion_ratio: float = 0.0   # quota_type=0：输出倍率（相对输入）

    @property
    def per_call(self) -> bool:
        return self.quota_type == QUOTA_TYPE_PER_CALL and self.price > 0

    def unit_price(self, group_ratio: float = 1.0) -> Optional[float]:
        """一次请求的实付美元单价。按 token 计费时返回 None（没有「一次」的价格）。"""
        if not self.per_call:
            return None
        return self.price * (group_ratio if group_ratio > 0 else 1.0)

    def calls_for(self, balance_usd: Optional[float],
                  group_ratio: float = 1.0) -> Optional[int]:
        """这笔余额还能发多少次。向下取整 —— 半次请求发不出去。

        按 token 计费、单价为 0 或余额未知时返回 None，由展示层显示成「不适用」，
        绝不编一个数字出来。
        """
        unit = self.unit_price(group_ratio)
        if unit is None or balance_usd is None:
            return None
        try:
            return max(0, math.floor(float(balance_usd) / unit))
        except (TypeError, ValueError, ZeroDivisionError):
            return None


@dataclass
class PricingTable:
    """一个站点的定价表。models 已按单价从低到高排好（越便宜的能发越多次）。"""

    models: list = field(default_factory=list)
    group_ratio: dict = field(default_factory=dict)
    usable_groups: dict = field(default_factory=dict)

    def ratio_for(self, group: Optional[str]) -> float:
        """取某个用户分组的倍率。拿不到分组信息就按 1 算，不瞎猜。"""
        if group and isinstance(self.group_ratio, dict):
            raw = self.group_ratio.get(group)
            try:
                value = float(raw)
                if value > 0:
                    return value
            except (TypeError, ValueError):
                pass
        return 1.0


def _as_float(raw, default: float = 0.0) -> float:
    try:
        return float(raw)
    except (TypeError, ValueError):
        return default


def _matches(name: str, keywords) -> bool:
    low = (name or "").lower()
    return any(k.lower() in low for k in keywords) if keywords else True


def parse_pricing(payload, model_filter=DEFAULT_MODEL_FILTER) -> PricingTable:
    """解析 /api/pricing 的响应。

    只认实测过的字段（2026-08 实拉 gorouter.app 与 tabitoken.com）：
      data[]: model_name / quota_type / model_price / model_ratio / completion_ratio
      顶层:   group_ratio / usable_group
    结构不对或字段缺失都当这个模型不可用，跳过它而不是让整张表失败。
    """
    if not isinstance(payload, dict) or not isinstance(payload.get("data"), list):
        return PricingTable()

    models = []
    for item in payload["data"]:
        if not isinstance(item, dict):
            continue
        name = str(item.get("model_name") or "").strip()
        if not name or not _matches(name, model_filter):
            continue
        models.append(ModelPrice(
            name=name,
            quota_type=int(_as_float(item.get("quota_type"), QUOTA_TYPE_PER_CALL)),
            price=_as_float(item.get("model_price")),
            model_ratio=_as_float(item.get("model_ratio")),
            completion_ratio=_as_float(item.get("completion_ratio")),
        ))

    # 按次计费的按单价升序排前面，按 token 计费的排最后：邮件里先看到「能发几次」
    models.sort(key=lambda m: (not m.per_call, m.price, m.name))
    ratio = payload.get("group_ratio") if isinstance(payload.get("group_ratio"), dict) else {}
    groups = payload.get("usable_group") if isinstance(payload.get("usable_group"), dict) else {}
    return PricingTable(models=models, group_ratio=ratio, usable_groups=groups)


def fetch_pricing(base_url: str, http_cfg, proxy: Optional[str] = None,
                  model_filter=DEFAULT_MODEL_FILTER) -> Optional[PricingTable]:
    """拉一个站点的定价表。失败返回 None，由调用方降级（总览里少这一段）。

    接口不需要鉴权，但裸请求会吃 Cloudflare 的闭门羹，所以照 S1 那套带上
    impersonate 指纹。代理跟着账号走：站点可能只对特定出口 IP 放行。
    """
    table, _fault = _fetch_once(base_url, http_cfg, proxy, model_filter)
    return table


def _fetch_once(base_url: str, http_cfg, proxy: Optional[str],
                model_filter) -> tuple:
    """拉一次，返回 (定价表或 None, 是否算代理的错)。

    第二个返回值决定上层怎么重试：代理的错换个代理接着来，别的错只给有限次数。
    """
    base = (base_url or "").rstrip("/")
    if not base:
        return None, False
    try:
        with cffi.Session(
            impersonate=getattr(http_cfg, "impersonate", None) or "chrome",
            verify=getattr(http_cfg, "verify", True),
            timeout=getattr(http_cfg, "timeout", 30),
            proxies={"http": proxy, "https": proxy} if proxy else None,
        ) as session:
            resp = session.get(base + PRICING_PATH, headers={
                "Accept": "application/json, text/plain, */*",
                "Referer": sanitize_header_value(base + "/"),
            })
    except Exception as exc:  # noqa: BLE001 - 拉定价失败只影响总览那一段
        fault = is_proxy_fault(exc, proxy)
        log.debug(f"拉定价表失败 {base}（{'代理问题' if fault else '非代理问题'}）: "
                  f"{type(exc).__name__}: {exc}")
        return None, fault

    if resp.status_code != 200:
        log.debug(f"拉定价表 {base} 返回 HTTP {resp.status_code}")
        return None, False
    try:
        payload = resp.json()
    except Exception as exc:  # noqa: BLE001 - 被盾拦时返回的是 HTML
        log.debug(f"定价表 {base} 不是 JSON: {type(exc).__name__}: {exc}")
        return None, False
    table = parse_pricing(payload, model_filter)
    log.debug(f"定价表 {base}: 命中 {len(table.models)} 个模型"
              + (f"（经 {proxy}）" if proxy else "（直连）"))
    return table, False


def fetch_pricing_resilient(base_url: str, http_cfg, *, proxy_provider=None,
                            max_non_proxy_attempts: int = MAX_NON_PROXY_ATTEMPTS,
                            model_filter=DEFAULT_MODEL_FILTER) -> Optional[PricingTable]:
    """带重试地拉定价表。

    两种失败分开对待：
      - 代理的错：换一个代理接着来，不计次数。池里掏不出新代理时自然停下 ——
        代理是有限的，这就是「无限更换」的天然终点。
      - 别的错（HTTP 非 200、非 JSON、被盾、解析不出模型）：最多 max_non_proxy_attempts
        次。这类错换代理也大概率一样，多试只是浪费时间。

    proxy_provider(bad=None) -> Optional[str]：给 None 表示要个可用代理，给 bad
    表示这个代理不行了、标坏并换一个。传 None 就是不走代理直连。
    """
    proxy = None
    if proxy_provider is not None:
        try:
            proxy = proxy_provider()
        except Exception as exc:  # noqa: BLE001 - 拿不到代理就直连试试
            log.debug(f"取定价请求用的代理失败，改直连: {type(exc).__name__}: {exc}")

    swaps, others = 0, 0
    while True:
        table, proxy_fault = _fetch_once(base_url, http_cfg, proxy, model_filter)
        if table is not None:
            return table
        if proxy_fault and proxy_provider is not None:
            try:
                new_proxy = proxy_provider(proxy)
            except Exception as exc:  # noqa: BLE001
                log.debug(f"换代理失败，停止重试: {type(exc).__name__}: {exc}")
                return None
            if not new_proxy or new_proxy == proxy:
                log.debug(f"拉 {base_url} 的定价表：池里已无其他可用代理，放弃")
                return None
            swaps += 1
            proxy = new_proxy
            log.debug(f"拉定价表换用代理 {new_proxy}（第 {swaps} 次更换）")
            continue
        others += 1
        if others >= max_non_proxy_attempts:
            log.debug(f"拉 {base_url} 的定价表失败 {others} 次（非代理原因），放弃")
            return None


@dataclass
class SiteQuota:
    """一个站点的额度总览：账号并起来的余额 + 那份定价表算出来的可发次数。"""

    site: str                       # base_url
    label: str                      # 显示名，取域名
    accounts: int = 0               # 该站点参与本轮的账号数
    known: int = 0                   # 其中真的查到余额的账号数
    total_usd: float = 0.0          # 已知余额合计（美元）
    table: Optional[PricingTable] = None

    @property
    def complete(self) -> bool:
        """所有账号的余额都查到了，总额才是完整的。"""
        return self.accounts > 0 and self.known == self.accounts

    def rows(self, group: Optional[str] = None) -> list:
        """展开成 (模型名, 单价文案, 次数文案) 三元组，供展示层直接铺表。"""
        if self.table is None:
            return []
        ratio = self.table.ratio_for(group)
        out = []
        for m in self.table.models:
            unit = m.unit_price(ratio)
            if unit is None:
                # 按 token 计费：没有「一次」的价格，标明计费方式而不是编个数字
                out.append((m.name, f"按 token 计费（倍率 {m.model_ratio:g}）", "—"))
                continue
            calls = m.calls_for(self.total_usd, ratio)
            out.append((m.name, f"${unit:.4f}".rstrip("0").rstrip("."),
                        f"{calls} 次" if calls is not None else "—"))
        return out


def _site_label(base_url: str) -> str:
    """站点显示名取域名，去掉协议和末尾斜杠 —— 邮件里够认人又不啰嗦。"""
    text = (base_url or "").strip().rstrip("/")
    for prefix in ("https://", "http://"):
        if text.startswith(prefix):
            text = text[len(prefix):]
            break
    return text or "未知站点"


def summarize_by_site(rows: list, http_cfg, *, model_filter=DEFAULT_MODEL_FILTER,
                      fetcher=None, proxy_provider=None) -> list:
    """按站点把余额并起来，并为每个站点实时拉一次定价表。

    定价每轮都重新拉：站点随时可能调价，拿旧价格算出来的次数是假的。同一站点的多个
    账号只拉一次（域名去重），不会因为账号多而重复打接口。

    proxy_provider 透传给 fetch_pricing_resilient：定价接口和签到一样吃 Cloudflare，
    走代理能显著提高成功率，代理坏了就换一个接着来。

    fetcher 参数是给测试注入假实现用的，默认走带重试的真实拉取。
    """
    from .config import DEFAULT_QUOTA_PER_UNIT

    fetch = fetcher or (lambda base: fetch_pricing_resilient(
        base, http_cfg, proxy_provider=proxy_provider, model_filter=model_filter))
    buckets: dict = {}
    for r in rows:
        site = (getattr(r, "site", "") or "").rstrip("/")
        if not site:
            continue
        bucket = buckets.get(site)
        if bucket is None:
            bucket = SiteQuota(site=site, label=_site_label(site))
            buckets[site] = bucket
        bucket.accounts += 1
        balance = getattr(r, "balance", None)
        if balance is None:
            continue
        unit = getattr(r, "quota_per_unit", None)
        try:
            unit = int(unit) if unit and int(unit) > 0 else DEFAULT_QUOTA_PER_UNIT
        except (TypeError, ValueError):
            unit = DEFAULT_QUOTA_PER_UNIT
        try:
            bucket.total_usd += float(balance) / unit
        except (TypeError, ValueError):
            continue
        bucket.known += 1

    for site, bucket in buckets.items():
        try:
            bucket.table = fetch(site)
        except Exception as exc:  # noqa: BLE001 - 拉不到就少这一段，不能拖垮发信
            log.debug(f"站点 {site} 定价表不可用: {type(exc).__name__}: {exc}")
    # 余额多的站点排前面，一眼看到主力号在哪
    return sorted(buckets.values(), key=lambda b: (-b.total_usd, b.label))
