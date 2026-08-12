"""OpenAI 兼容视觉客户端。

设计要点：
  - temperature=0，先试 response_format=json_object，模型不支持就自动降级到 prompt 约束
  - 输出用 loose_json 容错解析（剥 ``` 围栏、提取首个平衡 JSON）
  - 有限重试；仍失败就返回 None，让上层降级到 S4，AI 绝不是单点故障
  - 日志里 api_key 一律脱敏
"""

from __future__ import annotations

import base64
from dataclasses import dataclass
from typing import Optional, Tuple

from curl_cffi import requests as cffi

from .. import logger as log
from ..config import AIConfig
from ..utils import clamp, loose_json
from . import prompts


@dataclass
class PageVerdict:
    state: str = prompts.UNKNOWN
    confidence: float = 0.0
    reason: str = ""

    def __str__(self) -> str:
        return f"{self.state}(conf={self.confidence:.2f}) {self.reason}"


class VisionClient:
    def __init__(self, cfg: AIConfig):
        if not cfg.ready:
            raise ValueError("AI 配置不完整（base_url / api_key / model）")
        self.cfg = cfg
        self._json_mode = True
        self._session = cffi.Session(
            timeout=cfg.timeout,
            headers={
                "Authorization": f"Bearer {cfg.api_key}",
                "Content-Type": "application/json",
                "Accept": "application/json",
            },
        )

    def close(self) -> None:
        try:
            self._session.close()
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
        for attempt in range(1, attempts + 1):
            payload = self._payload(prompt, images, detail, max_tokens)
            try:
                resp = self._session.post(self.cfg.chat_url, json=payload)
            except Exception as exc:  # noqa: BLE001
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
            resp = self._session.get(self.cfg.models_url)
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
