"""
newapi_checkin/remail.py
ReMail 开放 API 多 key 客户端：搜订单定位邮箱、取件读验证码。

用途：GitHub 登录触发设备验证码时，从收件服务把验证码取回来填进去。

两个必须本地兜住的点（都是被踩过才知道的）：
- 订单列表接口不返回 serviceToken（purchase 订单尤甚），命中后要用
  GET /v1/open/orders/{orderNo} 补全，否则拿不到取件凭证
- MailMessage.verificationCode 是选填字段，规则没命中就是空。所以验证码提取必须有
  正则兜底，优先级：单封正文精确格式 → bodyPreview 精确 → verificationCode 字段

命中判定不信服务端 search 语义：文档没写它匹配哪些字段，所以本地再严格校验一次
deliveryEmail 的 @ 前缀与目标名全等（忽略大小写）。

全程不走代理：收件服务是第三方 API，套代理只是多一个失败点。
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass
from datetime import datetime, timezone

import curl_cffi.requests as requests

# GitHub 验证码邮件的精确格式："Verification code: 228311"
_GH_CODE_RE = re.compile(r"verification code[:\s]+(\d{4,8})", re.IGNORECASE)
# 通用兜底：正文里孤立的 6-8 位数字
_CODE_RE = re.compile(r"\b(\d{6,8})\b")

# 订单可用的状态。其余状态（pending/expired 等）拿不到能用的取件凭证
_USABLE_ORDER_STATUS = ("completed", "active")


class RemailError(Exception):
    """ReMail 请求失败，message 是可直接展示的中文原因。"""


@dataclass
class EmailHit:
    """一次搜索命中的邮箱及其取件凭证。"""

    key_index: int          # 命中的 key 序号（从 1 起），排查用
    email: str              # deliveryEmail 完整地址
    service_token: str      # 取件凭证
    order_no: str = ""      # 排查用
    allocation_id: int = 0  # purchase 订单的资源分配 ID


def parse_rfc3339(value: str | None) -> datetime | None:
    """RFC3339 → aware datetime。解析失败返回 None（排序时按最老处理）。"""
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def is_retryable(message: str) -> bool:
    """这个错误值不值得重试。

    可重试：网络错误、超时、非 JSON、5xx。
    不重试：认证失败、404、其他 4xx（重试无用），以及限流信号
    （限流由取件轮询按 Retry-After 单独处理，不该占用重试次数）。
    """
    text = str(message)
    if text.startswith("__rate_limited__"):
        return False
    for keyword in ("认证失败", "限流", "401", "403", "404"):
        if keyword in text:
            return False
    if re.search(r"HTTP 4\d\d", text):
        return False
    return True


def extract_github_code(preview: str, verification_code: str = "") -> str:
    """从一封邮件的可用字段里取验证码，按可靠性排序。

    先精确格式（"Verification code: 123456"），再服务端解析好的
    verificationCode 字段，最后才是通用的 6-8 位数字正则 —— 通用正则会误抓
    邮件里的其它数字（年份、订单号），只能垫最后一层。
    """
    match = _GH_CODE_RE.search(preview or "")
    if match:
        return match.group(1)
    code = (verification_code or "").strip()
    if code:
        return code
    match = _CODE_RE.search(preview or "")
    return match.group(1) if match else ""


def pick_usable_order(items: list[dict], name: str) -> dict | None:
    """从订单列表里挑出目标邮箱最新的一条可用订单。

    命中判定本地严格做：deliveryEmail 的 @ 前缀与 name 全等（忽略大小写）。
    服务端 search 匹配哪些字段没有文档，拿它的语义当真会取到别人的邮箱。
    """
    prefix = (name or "").strip().lower()
    if not prefix:
        return None
    candidates = []
    for order in items or []:
        email = (order.get("deliveryEmail") or "").strip()
        if not email or email.split("@", 1)[0].strip().lower() != prefix:
            continue
        if (order.get("status") or "") not in _USABLE_ORDER_STATUS:
            continue
        candidates.append(order)
    if not candidates:
        return None
    # createdAt 最新优先：同一邮箱可能有多条历史订单，旧的取件凭证可能已失效
    candidates.sort(
        key=lambda o: parse_rfc3339(o.get("createdAt"))
        or datetime(1970, 1, 1, tzinfo=timezone.utc),
        reverse=True,
    )
    return candidates[0]


class Remail:
    """多 key 客户端。每把 key 一个 session（连接复用 + 头隔离）。"""

    def __init__(self, base_url: str, api_keys: list[str], timeout: int = 20):
        self.base = (base_url or "").rstrip("/")
        self.api_keys = [k.strip() for k in (api_keys or []) if (k or "").strip()]
        self.timeout = timeout
        self._sessions = [self._make_session(k) for k in self.api_keys]
        # 取件接口不需要 API Key，单独一个 session
        self._pickup = requests.Session()

    @staticmethod
    def _make_session(key: str):
        session = requests.Session()
        session.headers.update({
            "Authorization": f"Bearer {key}",
            "Accept": "application/json",
        })
        return session

    # -------------------------------------------------------------- 内部请求

    def _get(self, session, path: str, params: dict | None = None,
             desc: str = "请求") -> dict:
        """发一次 GET 并把各类失败翻译成 RemailError。"""
        try:
            resp = session.get(f"{self.base}{path}", params=params, timeout=self.timeout)
        except Exception as exc:  # curl_cffi 的异常类型随后端变，统一兜住
            raise RemailError(f"{desc}网络错误: {type(exc).__name__}: {exc}") from exc

        if resp.status_code in (401, 403):
            raise RemailError(f"{desc}认证失败（HTTP {resp.status_code}）")
        if resp.status_code == 429:
            wait = resp.headers.get("Retry-After", "5")
            try:
                wait = float(wait)
            except (TypeError, ValueError):
                wait = 5.0
            # 内部信号：调用方按 Retry-After 等待而不是当成硬错误
            raise RemailError(f"__rate_limited__:{wait}")
        if resp.status_code >= 400:
            raise RemailError(f"{desc}失败 HTTP {resp.status_code}")
        try:
            body = resp.json()
        except Exception:
            raise RemailError(f"{desc}响应非 JSON") from None
        return body if isinstance(body, dict) else {}

    def _with_retry(self, fn, *args, desc: str = "请求", retries: int = 5, **kwargs):
        """可重试错误按指数退避重试（1→2→4→8→10s 封顶）。

        不可重试的（认证/404/限流信号）原样抛出，不浪费次数。
        """
        last = None
        for attempt in range(1, retries + 1):
            try:
                return fn(*args, **kwargs)
            except RemailError as exc:
                last = exc
                if not is_retryable(str(exc)) or attempt >= retries:
                    raise
                time.sleep(min(2 ** (attempt - 1), 10))
        if last:
            raise last

    # -------------------------------------------------------------- 搜邮箱

    def _list_orders(self, key_index: int, search: str) -> list[dict]:
        body = self._get(self._sessions[key_index - 1], "/v1/open/orders",
                         params={"search": search, "limit": 100},
                         desc=f"key#{key_index} 搜订单")
        return body.get("items") or []

    def _order_detail(self, key_index: int, order_no: str) -> dict:
        return self._get(self._sessions[key_index - 1], f"/v1/open/orders/{order_no}",
                         desc=f"key#{key_index} 订单详情")

    def find_email(self, name: str) -> EmailHit | None:
        """多 key 依序搜 name，返回第一个可用命中；全都没有返回 None。

        单把 key 失效（401/403）只记为跳过继续下一把 —— 一把过期不该让整轮停摆。
        """
        for index in range(1, len(self._sessions) + 1):
            try:
                items = self._with_retry(self._list_orders, index, name,
                                         desc=f"key#{index} 搜订单")
            except RemailError:
                continue
            order = pick_usable_order(items, name)
            if not order:
                continue

            token = (order.get("serviceToken") or "").strip()
            allocation = int(order.get("allocationId") or 0)
            order_no = (order.get("orderNo") or "").strip()
            # 列表接口不返回 serviceToken，用订单详情补全
            if not token and order_no:
                try:
                    detail = self._with_retry(self._order_detail, index, order_no,
                                              desc=f"key#{index} 订单详情")
                except RemailError:
                    detail = {}
                token = (detail.get("serviceToken") or "").strip()
                allocation = int(detail.get("allocationId") or allocation)
            if not token:
                continue  # 没有取件凭证等于拿不到验证码，这条命中作废
            return EmailHit(key_index=index, email=(order.get("deliveryEmail") or "").strip(),
                            service_token=token, order_no=order_no, allocation_id=allocation)
        return None


    # -------------------------------------------------------------- 取件

    def _pickup_once(self, email: str, token: str) -> dict:
        return self._get(self._pickup, "/v1/pickup",
                         params={"email": email, "token": token}, desc="取件")

    def message_body(self, email: str, token: str, message_id: str) -> str:
        """拉单封完整正文。失败返回空串 —— 调用方还有 bodyPreview 可退。"""
        try:
            body = self._get(self._pickup, f"/v1/pickup/messages/{message_id}",
                             params={"email": email, "token": token}, desc="取正文")
        except RemailError:
            return ""
        for key in ("body", "text", "html", "content"):
            value = body.get(key)
            if isinstance(value, str) and value.strip():
                return value
        message = body.get("message")
        if isinstance(message, dict):
            for key in ("body", "text", "html"):
                value = message.get(key)
                if isinstance(value, str) and value.strip():
                    return value
        return ""

    def pick_code_from_pickup(self, body: dict, since: datetime,
                              email: str = "", token: str = "") -> tuple[str, str]:
        """从一次取件结果里找 since 之后的 GitHub 验证码，最新邮件优先。

        必须取最新那封：一个账号多次登录会攒下多封验证码邮件，填到旧码就是
        「验证码不对」，而且看日志完全看不出为什么。
        """
        candidates = []
        for msg in body.get("items") or []:
            received = parse_rfc3339(msg.get("receivedAt"))
            if received and received < since:
                continue  # 本次登录之前的旧邮件
            if "github" not in (msg.get("sender") or "").lower():
                continue
            candidates.append((received or datetime(1970, 1, 1, tzinfo=timezone.utc), msg))
        candidates.sort(key=lambda pair: pair[0], reverse=True)

        for _received, msg in candidates:
            mid = str(msg.get("id") or "")
            if mid and email and token:
                full = self.message_body(email, token, mid)
                code = extract_github_code(full)
                if code:
                    return code, f"mail#{mid}.body"
            code = extract_github_code(msg.get("bodyPreview") or "",
                                       msg.get("verificationCode") or "")
            if code:
                return code, f"mail#{mid}.preview"
        return "", ""

    def poll_for_code(self, hit: EmailHit, since: datetime, max_tries: int = 10,
                      fallback_poll_sec: int = 8, on_wait=None) -> tuple[str, str]:
        """轮询取件直到拿到验证码。取满次数仍没有则抛 RemailError。

        轮询节奏优先听服务端的 fetch.nextFetchAllowedAt（它按 token 逐个限流），
        没给才用兜底间隔 —— 自己猛刷只会换来 429。
        """
        since = since.astimezone(timezone.utc)
        for attempt in range(1, max(1, max_tries) + 1):
            try:
                body = self._with_retry(self._pickup_once, hit.email, hit.service_token,
                                        desc="取件")
            except RemailError as exc:
                text = str(exc)
                if text.startswith("__rate_limited__:"):
                    wait = float(text.split(":", 1)[1])
                    if on_wait:
                        on_wait(wait, "取件被限流")
                    time.sleep(wait)
                    continue
                raise

            code, source = self.pick_code_from_pickup(
                body, since, hit.email, hit.service_token)
            if code:
                return code, source
            if attempt >= max_tries:
                break

            wait = float(fallback_poll_sec)
            nxt = parse_rfc3339((body.get("fetch") or {}).get("nextFetchAllowedAt"))
            if nxt:
                delta = (nxt - datetime.now(timezone.utc)).total_seconds()
                if delta > 0:
                    wait = min(max(delta, 1.0), 30.0)
            if on_wait:
                on_wait(wait, f"等验证码邮件（第 {attempt}/{max_tries} 次）")
            time.sleep(wait)

        raise RemailError(f"取件 {max_tries} 次仍未收到 GitHub 验证码邮件")
