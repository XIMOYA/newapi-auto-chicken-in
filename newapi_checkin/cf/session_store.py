"""会话缓存：cf_clearance + UA + 出口 IP 绑定关系的持久化。

cf_clearance 同时绑定出口 IP 和 User-Agent，任一不一致 cookie 立刻失效。
所以这里存的不只是 cookie，而是「cookie + 当时的 UA + 当时的出口 IP + 当时的代理」
这一整组上下文，复用前逐项比对。

checkin_path / user_id 不受 IP 绑定影响，单独存放，cookie 失效时不清掉。
"""

from __future__ import annotations

import json
import os
import tempfile
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional, Tuple

from ..utils import now

# cf_clearance 实际有效期由站点配置决定，cookie 没带 expires 时按这个兜底
DEFAULT_TTL = 25 * 60
# 距过期不足这个时长就当作已过期，避免请求发出去正好赶上失效
EXPIRY_MARGIN = 60


@dataclass
class CFSession:
    cookies: dict = field(default_factory=dict)
    user_agent: str = ""
    accept_language: str = ""
    exit_ip: Optional[str] = None
    proxy: Optional[str] = None
    expires_at: float = 0.0
    saved_at: float = 0.0

    def to_dict(self) -> dict:
        return {
            "cookies": self.cookies,
            "user_agent": self.user_agent,
            "accept_language": self.accept_language,
            "exit_ip": self.exit_ip,
            "proxy": self.proxy,
            "expires_at": self.expires_at,
            "saved_at": self.saved_at,
        }

    @classmethod
    def from_dict(cls, raw: dict) -> "CFSession":
        return cls(
            cookies=dict(raw.get("cookies") or {}),
            user_agent=str(raw.get("user_agent") or ""),
            accept_language=str(raw.get("accept_language") or ""),
            exit_ip=raw.get("exit_ip") or None,
            proxy=raw.get("proxy") or None,
            expires_at=float(raw.get("expires_at") or 0.0),
            saved_at=float(raw.get("saved_at") or 0.0),
        )

    def check(self, current_ip: Optional[str], proxy: Optional[str]) -> Tuple[bool, str]:
        """返回 (是否可用, 原因)。原因用于 -v 日志，说清为什么没命中缓存。"""
        if not self.cookies:
            return False, "无缓存 cookie"
        if not self.user_agent:
            return False, "缓存缺少 UA（UA 不一致 cf_clearance 必失效）"
        if self.expires_at and self.expires_at - EXPIRY_MARGIN <= now():
            return False, "cf_clearance 已过期"
        if (self.proxy or None) != (proxy or None):
            return False, f"代理已变更（缓存 {self.proxy or '直连'} -> 当前 {proxy or '直连'}）"
        if current_ip and self.exit_ip and current_ip != self.exit_ip:
            return False, f"出口 IP 已变更（{self.exit_ip} -> {current_ip}）"
        return True, "缓存有效"


@dataclass
class AccountSession:
    checkin_path: Optional[str] = None
    user_id: Optional[int] = None
    cf: Optional[CFSession] = None

    def to_dict(self) -> dict:
        out: dict = {}
        if self.checkin_path:
            out["checkin_path"] = self.checkin_path
        if self.user_id is not None:
            out["user_id"] = self.user_id
        if self.cf is not None:
            out["cf"] = self.cf.to_dict()
        return out

    @classmethod
    def from_dict(cls, raw: dict) -> "AccountSession":
        uid = raw.get("user_id")
        try:
            uid = int(uid) if uid not in (None, "") else None
        except (TypeError, ValueError):
            uid = None
        cf_raw = raw.get("cf")
        return cls(
            checkin_path=raw.get("checkin_path") or None,
            user_id=uid,
            cf=CFSession.from_dict(cf_raw) if isinstance(cf_raw, dict) else None,
        )


class SessionStore:
    """整个 sessions.json 的读写。原子落盘，避免 cron 中断写坏文件。"""

    def __init__(self, path: Path):
        self.path = Path(path)
        self._data: dict = {}
        self._dirty = False
        self._lock = threading.RLock()
        self._load()

    def _load(self) -> None:
        if not self.path.exists():
            return
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            # 缓存坏了不是致命问题，重新过一次盾就好
            self._data = {}
            return
        if isinstance(raw, dict):
            self._data = {
                str(k): AccountSession.from_dict(v)
                for k, v in raw.items()
                if isinstance(v, dict)
            }

    def get(self, slug: str) -> AccountSession:
        with self._lock:
            record = self._data.get(slug)
            if record is None:
                record = AccountSession()
                self._data[slug] = record
            return record

    def mark_dirty(self) -> None:
        with self._lock:
            self._dirty = True

    def update_cf(self, slug: str, session: CFSession) -> None:
        with self._lock:
            self.get(slug).cf = session
            self._dirty = True

    def clear_cf(self, slug: str) -> None:
        with self._lock:
            if self._data.get(slug) and self._data[slug].cf is not None:
                self._data[slug].cf = None
                self._dirty = True

    def remember(self, slug: str, *, checkin_path: Optional[str] = None,
                 user_id: Optional[int] = None) -> None:
        with self._lock:
            record = self.get(slug)
            if checkin_path and record.checkin_path != checkin_path:
                record.checkin_path = checkin_path
                self._dirty = True
            if user_id is not None and record.user_id != user_id:
                record.user_id = user_id
                self._dirty = True

    def flush(self) -> None:
        with self._lock:
            if not self._dirty:
                return
            payload = {k: v.to_dict() for k, v in self._data.items() if v.to_dict()}
            self.path.parent.mkdir(parents=True, exist_ok=True)
            fd, tmp = tempfile.mkstemp(dir=str(self.path.parent), prefix=".sessions-",
                                       suffix=".tmp")
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as fh:
                    json.dump(payload, fh, ensure_ascii=False, indent=2)
                os.replace(tmp, self.path)
                self._dirty = False
            except OSError:
                try:
                    os.unlink(tmp)
                except OSError:
                    pass


def cookie_expiry(cookies: list) -> float:
    """从浏览器 cookie 列表里取 cf_clearance 的到期时间；没有则按默认 TTL 兜底。"""
    for item in cookies or []:
        if item.get("name") == "cf_clearance":
            expires = item.get("expires")
            try:
                expires = float(expires)
            except (TypeError, ValueError):
                expires = -1
            if expires and expires > 0:
                return expires
            break
    return now() + DEFAULT_TTL
