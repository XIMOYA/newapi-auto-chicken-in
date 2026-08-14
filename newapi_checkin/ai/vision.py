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
        self._sessions: dict[int, object] = {}
        self._sessions_lock = threading.Lock()
        self._session_override = None

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
        ident = threading.get_ident()
        with self._sessions_lock:
            session = self._sessions.get(ident)
            if session is None:
                session = self._new_session()
                self._sessions[ident] = session
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
        """在代码块内让**本线程**的 AI 请求走指定代理，退出时还原。"""
        previous = self.current_proxy()
        self._local.proxy = proxy or None
        try:
            yield
        finally:
            self._local.proxy = previous

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

    def _wait_cooldown(self) -> float:
        """还在避让期内就先等满，返回实际等待秒数（不计入请求预算）。"""
        waited = 0.0
        while True:
            left = self._cooldown_left()
            if left <= 0:
                return waited
            log.warn(f"AI 处于超时避让期，等待 {left:.1f}s 后再请求")
            time.sleep(left)
            waited += left

    def close(self) -> None:
        """关闭所有线程创建过的 session。"""
        with self._sessions_lock:
            sessions = list(self._sessions.values())
            self._sessions.clear()
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
        # 整个任务共享 cfg.timeout 这一份预算：重试不再线性放大等待时间
        budget = max(1.0, float(self.cfg.timeout))
        started = time.monotonic()
        # 避让休眠是主动让路，不该算进请求预算里
        paused = 0.0
        proxy = self.current_proxy()
        proxy_dead = False
        for attempt in range(1, attempts + 1):
            # 每次发请求前都尊重全局避让：别人刚超时，本线程也一起等
            paused += self._wait_cooldown()
            remaining = budget - (time.monotonic() - started - paused)
            if remaining <= 0.5:
                log.debug(f"AI 调用预算 {budget:.0f}s 已用尽（第 {attempt}/{attempts} 次前）")
                break
            use_proxy = None if proxy_dead else proxy
            # 代理不通时会一直挂到超时，所以只给它一半预算，留出直连兜底的时间
            limit = min(remaining, max(5.0, budget * 0.5)) if use_proxy else remaining
            payload = self._payload(prompt, images, detail, max_tokens)
            try:
                resp = self._session.post(self.cfg.chat_url, json=payload,
                                          timeout=limit, **self._proxies(use_proxy))
            except Exception as exc:  # noqa: BLE001
                if use_proxy:
                    # 经代理失败说明代理有问题，不能据此判定 AI 端点过载，
                    # 所以不进避让，直接改走直连
                    proxy_dead = True
                    log.warn(f"AI 请求经代理失败({attempt}/{attempts})，改为直连重试: "
                             f"{type(exc).__name__}: {exc}"[:160])
                elif is_timeout(exc):
                    # 直连也超时基本等于模型侧过载/限流，立刻重发只会加重拥塞
                    self._enter_cooldown()
                    log.warn(f"AI 请求超时({attempt}/{attempts})，"
                             f"避让 {TIMEOUT_COOLDOWN:.0f}s 后再继续")
                    paused += self._wait_cooldown()
                else:
                    log.debug(f"AI 请求异常({attempt}/{attempts}): {type(exc).__name__}: {exc}")
                continue

            if resp.status_code in (400, 415, 422) and self._json_mode:
                # 模型/中转不支持 response_format，摘掉后重试
                log.debug(f"模型不支持 response_format（HTTP {resp.status_code}），改用 prompt 约束")
                self._json_mode = False
                continue
            if resp.status_code >= 400:
                body = (resp.text or "")[:200]
                log.debug(f"AI HTTP {resp.status_code}({attempt}/{attempts}): {body}")
                continue

            text = self._content_of(resp)
            if not text:
                log.debug(f"AI 返回空内容({attempt}/{attempts})")
                continue
            parsed = loose_json(text)
            if parsed is None:
                log.debug(f"AI 输出无法解析为 JSON({attempt}/{attempts}): {text[:160]}")
                continue
            log.debug(f"AI 输出: {parsed}")
            return parsed

        log.debug("AI 调用全部失败，交由上层降级")
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

    def locate_grid(self, png: bytes, width: int, height: int, target: str) -> list:
        """点选式验证码：返回多个归一化坐标。"""
        prompt = prompts.GRID_PROMPT.format(width=int(width), height=int(height), target=target)
        data = self._ask(prompt, [png], detail="high", max_tokens=400)
        if not data or not data.get("found"):
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
