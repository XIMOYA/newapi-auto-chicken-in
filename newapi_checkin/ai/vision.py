"""OpenAI 兼容视觉客户端。

设计要点：
  - temperature=0，先试 response_format=json_object，模型不支持就自动降级到 prompt 约束
  - 输出用 loose_json 容错解析（剥 ``` 围栏、提取首个平衡 JSON）
  - 有限重试；仍失败就返回 None，让上层降级到 S4，AI 绝不是单点故障
  - 请求走该账号的代理（见 use_proxy），不让视觉调用暴露真实出口 IP；
    代理不通时自动改走直连，AI 依旧不是单点故障
  - 直连请求超时会进入 TIMEOUT_COOLDOWN 秒的全局避让：超时通常意味着模型侧过载
    或限流，立刻重发只会加重拥塞，所以让所有线程一起等一会儿再继续
  - 日志里 api_key 一律脱敏
"""

from __future__ import annotations

import base64
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Iterator, Optional, Tuple

from curl_cffi import requests as cffi

from .. import logger as log
from ..config import AIConfig
from ..utils import clamp, loose_json
from . import prompts

# AI 请求超时后的避让时长（秒）。避让是全局的：并行签到时所有账号共用同一个
# AI 端点，一个线程超时说明端点已经吃不消，其他线程也该一起让一让。
TIMEOUT_COOLDOWN = 10.0

# cfg.timeout 是「单次请求」上限。换代理 IP 不限次数，所以整个任务另有一个
# 墙钟上限兜底 = max(MIN_TASK_DEADLINE, cfg.timeout × TASK_DEADLINE_FACTOR)。
# 没有它的话，池子一直吐死代理就会在这里无限打转。
TASK_DEADLINE_FACTOR = 2.0
MIN_TASK_DEADLINE = 60.0

# 模型「拒答」的特征词。截图不是它以为的那种题时（最常见的是把带 Cloudflare
# 复选框的仪表盘当成点选题问过去），模型往往不按格式回 found=false，而是用自然
# 语言纠正前提：'The image isn't a tile-selection captcha — it's a dashboard...'。
# 这种回答重试多少次都是同一个结果，识别出来当场收手，别再烧视觉 token。
_REFUSAL_MARKERS = (
    "can't help", "cannot help", "can't assist", "cannot assist",
    "can't provide", "cannot provide", "can not help",
    "isn't a", "is not a", "not a tile", "no tile", "aren't any",
    "i'm sorry", "i am sorry", "unable to",
    "无法", "抱歉", "不是点选", "没有点选", "并非", "无需点选",
)


def _looks_refused(text: str) -> bool:
    """判断模型是不是在用自然语言拒答，而不是输出被截断了。

    两者的处置完全相反：拒答重试没有意义，截断（thinking 把 max_tokens 吃光）
    再来一次可能就成了。区分靠有没有 JSON 开括号 —— 只要模型动手写了对象，
    哪怕写残，也说明它接受了任务，那就留给正常重试路径。
    """
    if "{" in text:
        return False
    low = text.lower()
    return any(marker in low for marker in _REFUSAL_MARKERS)


try:  # curl_cffi 的超时异常层级在不同版本里略有差异，缺失时退回错误码/文案判断
    from curl_cffi.requests.exceptions import Timeout as _CurlTimeout
except (ImportError, AttributeError):  # pragma: no cover - 取决于依赖版本
    _CurlTimeout = ()

_TIMEOUT_CURL_CODE = 28          # CURLE_OPERATION_TIMEDOUT
_TIMEOUT_TEXTS = ("timed out", "timeout", "operation too slow")


def is_timeout(exc: BaseException) -> bool:
    """判断一个请求异常是不是超时。类型 -> curl 错误码 -> 文案，逐层兜底。"""
    if _CurlTimeout and isinstance(exc, _CurlTimeout):
        return True
    code = getattr(exc, "code", None)
    try:
        if code is not None and int(code) == _TIMEOUT_CURL_CODE:
            return True
    except (TypeError, ValueError):
        pass
    text = str(exc).lower()
    return any(marker in text for marker in _TIMEOUT_TEXTS)


@dataclass
class PageVerdict:
    state: str = prompts.UNKNOWN
    confidence: float = 0.0
    reason: str = ""

    def __str__(self) -> str:
        return f"{self.state}(conf={self.confidence:.2f}) {self.reason}"


class VisionClient:
    # cfg.timeout 被当作「整个任务」的预算而不是单次请求上限，避免重试把 60s
    # 放大成 180s；每个工作线程各自持有一个 curl session，避免并行时互相排队。
    def __init__(self, cfg: AIConfig):
        if not cfg.ready:
            raise ValueError("AI 配置不完整（base_url / api_key / model）")
        self.cfg = cfg
        self._json_mode = True
        # 超时避让：所有线程共享同一个解禁时刻
        self._cooldown_lock = threading.Lock()
        self._cooldown_until = 0.0
        # 每个工作线程各自绑定自己账号的代理（见 use_proxy）
        self._local = threading.local()
        # curl_cffi 的 Session 不保证线程安全。用「一线程一 session」而不是加全局锁：
        # 加锁会把几个账号的视觉调用串起来排队，单次预算 60s，排队几分钟很常见。
        #
        # 隔离靠 threading.local 而不是 threading.get_ident() 做字典 key —— 线程 id
        # 在线程退出后会被系统复用（Linux 上尤其快），复用一发生就有两个后果：新线程
        # 摸到上一个线程留下的 session（可能已经 close 过，或还绑着别的账号的代理），
        # 而被顶掉的那个 session 再也没人关，直接泄漏。
        #
        # 但 close() 要能收掉「所有线程创建过的」session，threading.local 只看得见
        # 当前线程，所以另外用一个只增列表登记，专供收尾时统一关闭。
        self._all_sessions: list = []
        self._sessions_lock = threading.Lock()
        self._session_override = None
        # 强制走代理时用来「再要一个 IP」的回调（由 Runner 注册）
        self._proxy_provider = None
        self._require_proxy = False
        # 「已放弃这个代理」的上报回调（由 Runner 注册，见 set_proxy_source）
        self._proxy_failed_cb = None

    def _new_session(self):
        return cffi.Session(
            timeout=self.cfg.timeout,
            headers={
                "Authorization": f"Bearer {self.cfg.api_key}",
                "Content-Type": "application/json",
                "Accept": "application/json",
            },
        )

    @property
    def _session(self):
        """当前线程专属的 session（测试可以整体覆盖）。"""
        if self._session_override is not None:
            return self._session_override
        session = getattr(self._local, "session", None)
        if session is None:
            session = self._new_session()
            self._local.session = session
            # 登记一份供 close() 收尾。线程退出后 threading.local 会丢掉它自己那份
            # 引用，只有这个列表还攥着，所以 session 不会在没关闭前被回收
            with self._sessions_lock:
                self._all_sessions.append(session)
        return session

    @_session.setter
    def _session(self, value) -> None:
        self._session_override = value

    # ------------------------------------------------------------------ #
    # 代理绑定：AI 请求跟着账号走同一个出口 IP
    # ------------------------------------------------------------------ #

    def current_proxy(self) -> Optional[str]:
        return getattr(self._local, "proxy", None)

    @contextmanager
    def use_proxy(self, proxy: Optional[str]) -> Iterator[None]:
        """在代码块内让**本线程**的 AI 请求走指定代理，退出时还原。

        正常情况下 AI 全程跟着账号的出口 IP（调用方在过盾流程里绑定），
        这里只是临时切换。绑定后的代理若连不上 AI 端点，_ask 的换 IP 分支
        仍会破例另选 —— 保视觉调用可用，不因一个端点故障拖垮整个过盾。
        """
        previous = self.current_proxy()
        self._local.proxy = proxy or None
        try:
            yield
        finally:
            self._local.proxy = previous

    def set_proxy_source(self, provider, require: bool = True,
                         on_failed=None) -> None:
        """注册「再要一个代理」的回调。

        provider() 每次返回一个可用代理地址（拿不到返回 None）。
        require=True 表示 AI 请求**必须**走代理：代理连不上就换下一个，
        不限次数，绝不退回直连暴露真实出口 IP。

        on_failed(proxy) 在**彻底放弃**某个代理时调用，让上层决定要不要
        拉黑。本类故意不直接 mark_bad：「连不上 AI 端点」不等于「连不上
        签到目标站点」，只有掌握全局分配情况的 Runner 才能判断拉黑会不会
        误伤正在签到的账号。
        """
        self._proxy_provider = provider
        self._require_proxy = bool(require)
        self._proxy_failed_cb = on_failed

    def _next_proxy(self) -> Optional[str]:
        provider = self._proxy_provider
        if provider is None:
            return None
        try:
            proxy = provider()
        except Exception as exc:  # noqa: BLE001 - 取代理失败不该炸掉视觉调用
            log.debug(f"获取新代理失败: {type(exc).__name__}: {exc}")
            return None
        return str(proxy).strip() or None if proxy else None

    def _report_bad_proxy(self, proxy: Optional[str]) -> None:
        """上报「这个代理连不上 AI 端点，已放弃」。回调抛错绝不能影响视觉调用。"""
        cb = self._proxy_failed_cb
        if cb is None or not proxy:
            return
        try:
            cb(proxy)
        except Exception as exc:  # noqa: BLE001
            log.debug(f"上报坏代理失败: {type(exc).__name__}: {exc}")

    @staticmethod
    def _proxies(proxy: Optional[str]) -> dict:
        return {"proxies": {"http": proxy, "https": proxy}} if proxy else {}

    # ------------------------------------------------------------------ #
    # 超时避让
    # ------------------------------------------------------------------ #

    def _enter_cooldown(self) -> float:
        """进入避让期，返回本次设定的解禁时刻。"""
        with self._cooldown_lock:
            self._cooldown_until = max(self._cooldown_until,
                                       time.monotonic() + TIMEOUT_COOLDOWN)
            return self._cooldown_until

    def _cooldown_left(self) -> float:
        with self._cooldown_lock:
            return max(0.0, self._cooldown_until - time.monotonic())

    def _wait_cooldown(self, max_wait: Optional[float] = None) -> float:
        """还在避让期内就先等，返回实际等待秒数（不计入请求预算）。

        max_wait 是本任务剩余墙钟预算：避让可以等，但不能等过任务预算，
        否则会把 AI 任务整体拖到超时。传 None 表示不限时（默认行为不变）。
        """
        waited = 0.0
        while True:
            left = self._cooldown_left()
            if left <= 0:
                return waited
            if max_wait is not None:
                remaining = max_wait - waited
                if remaining <= 0:
                    return waited
                left = min(left, remaining)
            log.warn(f"AI 处于超时避让期，等待 {left:.1f}s 后再请求")
            time.sleep(left)
            waited += left

    def close(self) -> None:
        """关闭所有线程创建过的 session。"""
        with self._sessions_lock:
            sessions = list(self._all_sessions)
            self._all_sessions.clear()
        # 当前线程那份也从 local 里摘掉，免得 close 之后又被复用
        if getattr(self._local, "session", None) is not None:
            self._local.session = None
        if self._session_override is not None:
            sessions.append(self._session_override)
        for session in sessions:
            try:
                session.close()
            except Exception:  # noqa: BLE001
                pass

    # ------------------------------------------------------------------ #
    # 底层调用
    # ------------------------------------------------------------------ #

    def _payload(self, prompt: str, images: list, detail: str, max_tokens: int) -> dict:
        content: list = [{"type": "text", "text": prompt}]
        for raw in images:
            b64 = base64.b64encode(raw).decode("ascii")
            content.append({
                "type": "image_url",
                "image_url": {"url": f"data:image/png;base64,{b64}", "detail": detail},
            })
        payload = {
            "model": self.cfg.model,
            "temperature": 0,
            "max_tokens": max_tokens,
            "messages": [{"role": "user", "content": content}],
        }
        if self._json_mode:
            payload["response_format"] = {"type": "json_object"}
        return payload

    def _ask(self, prompt: str, images: list, *, detail: str = "high",
             max_tokens: int = 300) -> Optional[dict]:
        if not images or not any(images):
            log.debug("AI 调用被跳过：没有可用截图")
            return None

        attempts = max(1, self.cfg.max_retries + 1)
        # cfg.timeout 是单次请求上限；换 IP 不限次数，整个任务另有墙钟上限兜底
        per_request = max(1.0, float(self.cfg.timeout))
        deadline = time.monotonic() + max(MIN_TASK_DEADLINE,
                                         per_request * TASK_DEADLINE_FACTOR)

        proxy = self.current_proxy()
        if self._require_proxy and not proxy:
            proxy = self._next_proxy()
            if not proxy:
                log.err("AI 强制走代理，但当前拿不到任何代理，跳过本次视觉调用")
                return None
            self._local.proxy = proxy

        used = 0        # 计次重试（不含换 IP）
        swaps = 0       # 换 IP 次数，不限
        while used < attempts:
            left = deadline - time.monotonic()
            if left <= 0.5:
                log.warn(f"AI 调用已用满 {max(MIN_TASK_DEADLINE, per_request * TASK_DEADLINE_FACTOR):.0f}s "
                         f"墙钟上限（第 {used + 1} 次尝试前，已换 {swaps} 次 IP）")
                break
            # 每次发请求前都尊重全局避让；等待不能超过任务剩余预算
            self._wait_cooldown(max_wait=left)
            left = deadline - time.monotonic()
            if left <= 0.5:
                log.warn(f"AI 调用已用满 {max(MIN_TASK_DEADLINE, per_request * TASK_DEADLINE_FACTOR):.0f}s "
                         f"墙钟上限（避让等待耗尽剩余预算，已换 {swaps} 次 IP）")
                break
            limit = min(per_request, left)
            payload = self._payload(prompt, images, detail, max_tokens)
            try:
                resp = self._session.post(self.cfg.chat_url, json=payload,
                                          timeout=limit, **self._proxies(proxy))
            except Exception as exc:  # noqa: BLE001
                if proxy:
                    # 经代理失败是代理的问题，不能据此判定 AI 端点过载，所以不进避让。
                    # 换下一个 IP 继续，不计入 attempts（换 IP 不限次数）。
                    # 顺序要紧：先拿到替代品再上报旧 IP 坏掉。万一池子已空、
                    # 只能沿用它重试，就不能把它拉黑，否则就成了「拿一个已拉黑
                    # 的代理继续用」。
                    nxt = self._next_proxy()
                    if nxt and nxt != proxy:
                        self._report_bad_proxy(proxy)
                        swaps += 1
                        proxy = nxt
                        # 换代理成功后同步线程本地代理，避免下一次视觉请求
                        # 又拿到已失败的旧代理
                        self._local.proxy = proxy
                        log.warn(f"AI 请求经代理失败，换第 {swaps} 个 IP 重试: "
                                 f"{type(exc).__name__}: {exc}"[:160])
                        continue
                    if self._require_proxy:
                        # 换不到新 IP：按配置绝不直连，只能沿用当前代理计次重试。
                        # 这里不上报坏代理——下一轮还要靠它。
                        used += 1
                        log.warn(f"AI 请求经代理失败且暂无其他可用 IP，"
                                 f"沿用当前代理重试({used}/{attempts})")
                        continue
                    self._report_bad_proxy(proxy)
                    proxy = None
                    # 降级直连同样同步线程本地代理，保证 current_proxy() 一致
                    self._local.proxy = None
                    log.warn(f"AI 请求经代理失败，改为直连重试: "
                             f"{type(exc).__name__}: {exc}"[:160])
                    continue
                used += 1
                if is_timeout(exc):
                    # 直连也超时基本等于模型侧过载/限流，立刻重发只会加重拥塞
                    self._enter_cooldown()
                    log.warn(f"AI 请求超时({used}/{attempts})，"
                             f"避让 {TIMEOUT_COOLDOWN:.0f}s 后再继续")
                    # 避让等待不得超过任务剩余预算
                    self._wait_cooldown(max_wait=max(0.0, deadline - time.monotonic()))
                else:
                    log.debug(f"AI 请求异常({used}/{attempts}): {type(exc).__name__}: {exc}")
                continue

            used += 1
            if resp.status_code in (400, 415, 422) and self._json_mode:
                # 模型/中转不支持 response_format，摘掉后重试（不算一次失败）
                log.debug(f"模型不支持 response_format（HTTP {resp.status_code}），改用 prompt 约束")
                self._json_mode = False
                used -= 1
                continue
            if resp.status_code >= 400:
                body = (resp.text or "")[:200]
                log.debug(f"AI HTTP {resp.status_code}({used}/{attempts}): {body}")
                continue

            text = self._content_of(resp)
            if not text:
                log.debug(f"AI 返回空内容({used}/{attempts})")
                continue
            parsed = loose_json(text)
            if parsed is None:
                if _looks_refused(text):
                    # 拒答不是故障，也不该重试：模型看清了图，只是图里没有它要找的
                    # 东西。返回 found=false 让各任务照「没找到」处理，另带一个内部
                    # 标记，好让点选流程知道「别再拿同一张图问了」。
                    log.debug(f"AI 判定图中没有该任务的目标，跳过重试: {text[:120]}")
                    return {"found": False, "_refused": True}
                log.debug(f"AI 输出无法解析为 JSON({used}/{attempts}): {text[:160]}")
                continue
            log.debug(f"AI 输出: {parsed}")
            return parsed

        log.debug(f"AI 调用全部失败（尝试 {used} 次、换 IP {swaps} 次），交由上层降级")
        return None

    @staticmethod
    def _content_of(resp) -> str:
        try:
            data = resp.json()
        except Exception:  # noqa: BLE001
            return ""
        if not isinstance(data, dict):
            return ""
        choices = data.get("choices")
        if not isinstance(choices, list) or not choices:
            log.debug(f"AI 响应缺少 choices: {str(data)[:200]}")
            return ""
        message = choices[0].get("message") if isinstance(choices[0], dict) else None
        if not isinstance(message, dict):
            return ""
        content = message.get("content")
        if isinstance(content, str):
            return content
        # 某些实现把 content 拆成分片数组
        if isinstance(content, list):
            return "".join(
                part.get("text", "") for part in content if isinstance(part, dict)
            )
        return ""

    # ------------------------------------------------------------------ #
    # 三个任务
    # ------------------------------------------------------------------ #

    def classify_page(self, png: bytes) -> PageVerdict:
        """页面状态分类：告诉流程「现在到哪一步了」，替代硬编码选择器。"""
        data = self._ask(prompts.STATE_PROMPT, [png], detail="low", max_tokens=200)
        if data is None:
            return PageVerdict()
        state = str(data.get("state") or "").strip().lower()
        if state not in prompts.ALL_STATES:
            log.debug(f"AI 返回未知状态 {state!r}，按 unknown 处理")
            state = prompts.UNKNOWN
        try:
            confidence = clamp(float(data.get("confidence", 0)))
        except (TypeError, ValueError):
            confidence = 0.0
        return PageVerdict(state, confidence, str(data.get("reason") or "")[:60])

    def locate(self, png: bytes, width: int, height: int,
               target: str) -> Optional[Tuple[float, float]]:
        """返回归一化坐标 (x, y)。传局部图比整页图精度高得多。"""
        prompt = prompts.LOCATE_PROMPT.format(width=int(width), height=int(height), target=target)
        data = self._ask(prompt, [png], detail="high", max_tokens=200)
        if not data or not data.get("found"):
            return None
        return self._point(data, width, height)

    def locate_grid(self, png: bytes, width: int, height: int, target: str) -> Optional[list]:
        """点选式验证码：返回多个归一化坐标。

        返回 None 有特殊含义 —— AI 看过图并明确表示里面没有点选题（例如只有一个
        Cloudflare 复选框）。调用方据此停手，别对着同一个画面反复问；空列表则只是
        这一次没拿到有效坐标，还可以再试。
        """
        prompt = prompts.GRID_PROMPT.format(width=int(width), height=int(height), target=target)
        data = self._ask(prompt, [png], detail="high", max_tokens=400)
        if not data:
            return []
        if data.get("_refused") or data.get("found") is False:
            return None
        if not data.get("found"):
            return []
        points = data.get("points")
        if not isinstance(points, list):
            return []
        out = []
        for item in points:
            if not isinstance(item, dict):
                continue
            point = self._point(item, width, height)
            if point is not None:
                out.append(point)
        return out

    def ocr(self, png: bytes) -> Optional[str]:
        """字符验证码识别。"""
        data = self._ask(prompts.OCR_PROMPT, [png], detail="high", max_tokens=60)
        if not data:
            return None
        text = str(data.get("text") or "").strip()
        return text or None

    def ping(self) -> Tuple[bool, str]:
        """doctor 用：验证接口连通与鉴权。"""
        try:
            resp = self._session.get(self.cfg.models_url,
                                     **self._proxies(self.current_proxy()))
        except Exception as exc:  # noqa: BLE001
            return False, f"{type(exc).__name__}: {exc}"
        if resp.status_code >= 400:
            return False, f"HTTP {resp.status_code}: {(resp.text or '')[:160]}"
        return True, f"HTTP {resp.status_code} 连通正常"

    # ------------------------------------------------------------------ #

    @classmethod
    def _point(cls, data: dict, width: int, height: int) -> Optional[Tuple[float, float]]:
        x = cls._norm(data.get("x"), width)
        y = cls._norm(data.get("y"), height)
        if x is None or y is None:
            return None
        return x, y

    @staticmethod
    def _norm(value, size) -> Optional[float]:
        """模型经常无视归一化要求直接给像素值，这里两种都兼容。"""
        try:
            number = float(value)
        except (TypeError, ValueError):
            return None
        if number > 1.0:
            number = number / max(1.0, float(size))
        return clamp(number)
