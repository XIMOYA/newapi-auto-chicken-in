"""会话缓存：cf_clearance + UA + 出口 IP 绑定关系的持久化。

cf_clearance 同时绑定出口 IP 和 User-Agent，任一不一致 cookie 立刻失效。
所以这里存的不只是 cookie，而是「cookie + 当时的 UA + 当时的出口 IP + 当时的代理」
这一整组上下文，复用前逐项比对。

checkin_path / user_id 不受 IP 绑定影响，单独存放，cookie 失效时不清掉。
"""

from __future__ import annotations

import hashlib
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
    # TaBiAI 的 new_api_refresh 会按代次轮转，必须逐轮持久化，否则下次用旧代会被判重放
    refresh_cookie: Optional[str] = None
    # 「当前这一代第一次被送进 refresh」的时刻，以及它绑定的代次指纹。
    #
    # 实测（2026-08 对 tabitoken.cc）旧代重放的宽限窗口只有 20~45 秒：放 20 秒重放还是
    # 幂等成功，放 45 秒直接 AUTH_SESSION_REVOKED，中间没有温和的 AUTH_UNAUTHORIZED 过渡。
    # 而 refresh 一旦超时（http.timeout 默认 20 秒）就已经烧掉整个安全窗口，此时换 IP 重试
    # 等于拿整条会话赌运气。所以「这一代悬空多久了」必须跨进程记住 —— Actions 跑超时被平台
    # 强杀是常态，纯内存计时的方案挡不住「进程死了、下一轮捡起旧代接着刷」。
    refresh_inflight_at: Optional[float] = None
    refresh_inflight_gen: Optional[str] = None
    # 站点的额度换算率（quota_per_unit）。站点级属性，探到一次就能一直用，
    # 缓存下来免得每轮都去打 /api/status
    quota_per_unit: Optional[int] = None

    def to_dict(self) -> dict:
        out: dict = {}
        if self.checkin_path:
            out["checkin_path"] = self.checkin_path
        if self.user_id is not None:
            out["user_id"] = self.user_id
        if self.refresh_cookie:
            out["refresh_cookie"] = self.refresh_cookie
        if self.refresh_inflight_at and self.refresh_inflight_gen:
            # 两个字段是一对，缺一个就没有意义，所以一起写、一起读
            out["refresh_inflight_at"] = self.refresh_inflight_at
            out["refresh_inflight_gen"] = self.refresh_inflight_gen
        if self.quota_per_unit:
            out["quota_per_unit"] = self.quota_per_unit
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
        # 换算率是除数，坏值（0/负数/非数字）必须当成没有，否则展示层要除零
        try:
            unit = int(raw.get("quota_per_unit"))
            unit = unit if unit > 0 else None
        except (TypeError, ValueError):
            unit = None
        cf_raw = raw.get("cf")
        # 悬空时间戳必须是能比较的数字；脏值当成没有标记（宁可多等一轮，不能拿坏值去判安全）
        try:
            inflight_at = float(raw.get("refresh_inflight_at"))
            inflight_at = inflight_at if inflight_at > 0 else None
        except (TypeError, ValueError):
            inflight_at = None
        inflight_gen = str(raw.get("refresh_inflight_gen") or "").strip() or None
        if inflight_at is None or inflight_gen is None:
            inflight_at, inflight_gen = None, None
        return cls(
            checkin_path=raw.get("checkin_path") or None,
            user_id=uid,
            cf=CFSession.from_dict(cf_raw) if isinstance(cf_raw, dict) else None,
            refresh_cookie=raw.get("refresh_cookie") or None,
            refresh_inflight_at=inflight_at,
            refresh_inflight_gen=inflight_gen,
            quota_per_unit=unit,
        )


def generation_fingerprint(cookie: str) -> str:
    """代次指纹：sid.secret 的短哈希，用来判断悬空标记还属不属于手上这一代。

    存哈希不存原值 —— refresh_cookie 字段已经有完整凭据了，没必要在库里留第二份。
    同时兼容 `new_api_refresh=sid.secret` 和裸 `sid.secret` 两种写法。
    """
    value = (cookie or "").strip()
    if "=" in value:
        value = value.split("=", 1)[1]
    value = value.split(";")[0].strip()
    if not value:
        return ""
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:12]


class SessionStore:
    """整个 sessions.json 的读写。原子落盘，避免 cron 中断写坏文件。"""

    # 并行签到时每个账号结束都落盘会退化成 O(账号数 × 会话数) 的全量重写，
    # 这里给「顺手落盘」加一个最小间隔；收尾的 flush() 仍然强制写。
    THROTTLE_SECONDS = 2.0

    def __init__(self, path: Path):
        self.path = Path(path)
        self._data: dict = {}
        self._dirty = False
        self._lock = threading.RLock()
        self._last_flush = 0.0
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
                 user_id: Optional[int] = None,
                 quota_per_unit: Optional[int] = None) -> None:
        with self._lock:
            record = self.get(slug)
            if checkin_path and record.checkin_path != checkin_path:
                record.checkin_path = checkin_path
                self._dirty = True
            if user_id is not None and record.user_id != user_id:
                record.user_id = user_id
                self._dirty = True
            # 只认正数：0 会让金额换算除零，负数是脏数据
            if quota_per_unit and quota_per_unit > 0 and record.quota_per_unit != quota_per_unit:
                record.quota_per_unit = quota_per_unit
                self._dirty = True

    def remember_refresh_cookie(self, slug: str, cookie: str) -> None:
        """记住 TaBiAI 轮转后的新凭据并**立即落盘**。

        不走 flush_throttled：refresh 的旧代次一旦被判重放会撤销整条会话，
        节流期间进程被杀就等于丢了一代，代价远大于一次多余的写盘。
        """
        value = (cookie or "").strip()
        if not value:
            return
        with self._lock:
            record = self.get(slug)
            if record.refresh_cookie == value:
                return
            record.refresh_cookie = value
            # 换代了，上一代的悬空账就此清零：它已经被站点取代，不会再被送去 refresh
            record.refresh_inflight_at = None
            record.refresh_inflight_gen = None
            self._dirty = True
        self.flush()

    def mark_refresh_inflight(self, slug: str, cookie: str) -> None:
        """记下「这一代开始被送进 refresh」的时刻，**立即落盘**。

        必须在请求发出**之前**写。写晚了就没意义：超时的那次请求恰恰是最需要记账的
        —— 站点可能已经推进了代次，而我们没收到响应，此时手上这一代已经是废纸。

        同一代重复调用只认第一次：悬空时长要从「第一次送出」算起，否则每次重试都
        重置计时，预算就永远用不完。
        """
        gen = generation_fingerprint(cookie)
        if not gen:
            return
        with self._lock:
            record = self.get(slug)
            if record.refresh_inflight_gen == gen and record.refresh_inflight_at:
                return
            record.refresh_inflight_at = now()
            record.refresh_inflight_gen = gen
            self._dirty = True
        self.flush()

    def refresh_inflight_age(self, slug: str, cookie: str) -> Optional[float]:
        """这一代已经悬空多少秒。没标记、指纹不匹配、或时钟倒流都返回 None。

        指纹不匹配是正常情况：平台回写、人工重新签发都会换掉凭据，那一代的悬空账
        自然作废。返回 None 表示「这一代没有悬空历史」，可以放心用。
        """
        gen = generation_fingerprint(cookie)
        if not gen:
            return None
        with self._lock:
            record = self._data.get(slug)
            if record is None or not record.refresh_inflight_at:
                return None
            if record.refresh_inflight_gen != gen:
                return None
            age = now() - record.refresh_inflight_at
        # 时钟被往回调过（容器里不罕见）会算出负数，当成没有标记而不是「刚刚才用」
        return age if age >= 0 else None

    def clear_refresh_inflight(self, slug: str) -> None:
        """确认这一代已经安全落地（拿到响应）后销账。"""
        with self._lock:
            record = self._data.get(slug)
            if record is None or not (record.refresh_inflight_at or record.refresh_inflight_gen):
                return
            record.refresh_inflight_at = None
            record.refresh_inflight_gen = None
            self._dirty = True

    def flush_throttled(self) -> bool:
        """节流落盘：距上次成功写盘不足 THROTTLE_SECONDS 就先攒着。

        返回是否真的写了盘。收尾必须再调一次 flush() 兜底。
        """
        with self._lock:
            if not self._dirty:
                return False
            if now() - self._last_flush < self.THROTTLE_SECONDS:
                return False
        self.flush()
        return True

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
                self._last_flush = now()
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
