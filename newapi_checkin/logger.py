"""统一日志：彩色输出、敏感信息脱敏、末尾汇总表。"""

from __future__ import annotations

import os
import threading
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Callable, Optional

from rich.console import Console
from rich.table import Table
from rich.theme import Theme

_THEME = Theme(
    {
        "ok": "bold green",
        "warn": "bold yellow",
        "err": "bold red",
        "step": "bold cyan",
        "dim2": "grey58",
    }
)

console = Console(theme=_THEME, highlight=False, soft_wrap=False)

_VERBOSE = False
_LOG_FILE: Optional[Path] = None
_LOG_LOCK = threading.RLock()
_RECENT_LOGS = deque(maxlen=1000)
_LISTENERS: list[Callable[[str], None]] = []


def setup(verbose: bool = False, log_dir: Optional[Path] = None) -> None:
    """初始化日志。log_dir 非空时同时把纯文本日志落盘（便于 cron 排查）。"""
    global _VERBOSE, _LOG_FILE
    _VERBOSE = verbose
    if log_dir is not None:
        log_dir.mkdir(parents=True, exist_ok=True)
        _LOG_FILE = log_dir / f"{datetime.now():%Y-%m-%d}.log"


def subscribe(listener: Callable[[str], None]) -> Callable[[], None]:
    """订阅纯文本日志事件，返回取消订阅函数。"""
    with _LOG_LOCK:
        _LISTENERS.append(listener)

    def unsubscribe() -> None:
        with _LOG_LOCK:
            if listener in _LISTENERS:
                _LISTENERS.remove(listener)

    return unsubscribe


def recent_logs(limit: int = 200) -> list[str]:
    with _LOG_LOCK:
        return list(_RECENT_LOGS)[-max(1, int(limit)):]


def mask(value: object, keep: int = 4) -> str:
    """脱敏：只保留首尾若干字符。cookie / api_key 打印必须走这里。"""
    if value is None:
        return "<none>"
    text = str(value)
    if not text:
        return "<empty>"
    if len(text) <= keep * 2:
        return "*" * len(text)
    return f"{text[:keep]}...{text[-keep:]}(len={len(text)})"


def _emit(markup: str, plain: str) -> None:
    console.print(markup)
    timestamped = f"[{datetime.now():%H:%M:%S}] {plain}"
    with _LOG_LOCK:
        _RECENT_LOGS.append(timestamped)
        listeners = list(_LISTENERS)
    for listener in listeners:
        try:
            listener(timestamped)
        except Exception:
            pass
    if _LOG_FILE is not None:
        try:
            with _LOG_FILE.open("a", encoding="utf-8") as fh:
                fh.write(timestamped + "\n")
        except OSError:
            pass


def _esc(text: str) -> str:
    return text.replace("[", "\\[")


def step(text: str) -> None:
    _emit(f"[step]==>[/step] {_esc(text)}", f"==> {text}")


def info(text: str) -> None:
    _emit(f"    {_esc(text)}", f"    {text}")


def ok(text: str) -> None:
    _emit(f"    [ok]OK[/ok]   {_esc(text)}", f"    OK   {text}")


def warn(text: str) -> None:
    _emit(f"    [warn]WARN[/warn] {_esc(text)}", f"    WARN {text}")


def err(text: str) -> None:
    _emit(f"    [err]FAIL[/err] {_esc(text)}", f"    FAIL {text}")


def debug(text: str) -> None:
    if _VERBOSE:
        _emit(f"    [dim2]dbg  {_esc(text)}[/dim2]", f"    dbg  {text}")


def is_verbose() -> bool:
    return _VERBOSE


# --------------------------------------------------------------------------- #
# 汇总
# --------------------------------------------------------------------------- #

STATUS_STYLE = {
    "success": "ok",
    "already_done": "ok",
    "skipped": "dim2",
    "failed": "err",
    "auth_failed": "err",
    "login_required": "err",
    "cf_blocked": "err",
    "waf_block": "err",
    "turnstile_required": "warn",
    "network_error": "err",
    "config_error": "err",
    "unknown": "warn",
}

STATUS_LABEL = {
    "success": "签到成功",
    "already_done": "今日已签",
    "skipped": "已跳过",
    "failed": "签到失败",
    "auth_failed": "认证失败",
    "login_required": "浏览器未登录",
    "cf_blocked": "被盾拦截",
    "waf_block": "WAF 封禁",
    "turnstile_required": "需要 Turnstile",
    "network_error": "网络异常",
    "config_error": "配置错误",
    "unknown": "结果未知",
}

OK_STATUSES = {"success", "already_done"}


@dataclass
class SummaryRow:
    name: str
    status: str
    strategy: str = "-"
    detail: str = ""
    quota: object = None


@dataclass
class Summary:
    rows: list = field(default_factory=list)

    def add(self, row: SummaryRow) -> None:
        self.rows.append(row)

    @property
    def failed(self) -> int:
        return sum(1 for r in self.rows if r.status not in OK_STATUSES and r.status != "skipped")

    def render(self) -> None:
        if not self.rows:
            return
        table = Table(title="签到结果汇总", title_style="bold", header_style="bold")
        table.add_column("账号", overflow="fold")
        table.add_column("结果")
        table.add_column("命中策略")
        table.add_column("额度", justify="right")
        table.add_column("说明", overflow="fold")
        for r in self.rows:
            style = STATUS_STYLE.get(r.status, "warn")
            label = STATUS_LABEL.get(r.status, r.status)
            quota = "-" if r.quota in (None, "", 0) else str(r.quota)
            table.add_row(r.name, f"[{style}]{label}[/{style}]", r.strategy, quota, _esc(r.detail))
        console.print()
        console.print(table)


def supports_color() -> bool:
    return console.is_terminal and not os.environ.get("NO_COLOR")
