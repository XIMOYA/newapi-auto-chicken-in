"""统一日志：彩色输出、敏感信息脱敏、末尾汇总表。

并行签到时多个账号的输出会交错在一起，所以每条日志都带一个线程局部的账号标签
（见 context()）：`Runner._run_account` 进入账号时打上，退出时还原。标签插在
等级标记之后，不影响原有缩进对齐。
"""

from __future__ import annotations

import os
import threading
from collections import deque
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Callable, Iterator, Optional

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
_LOG_HANDLE = None
_LOG_LOCK = threading.RLock()
_RECENT_LOGS = deque(maxlen=1000)
_LISTENERS: list[Callable[[str], None]] = []
# 账号标签是线程局部的：并行时每个工作线程各自持有自己那个账号的名字
_CONTEXT = threading.local()


def set_context(label: Optional[str]) -> None:
    """设置当前线程的日志标签（账号名）。传空即清除。"""
    _CONTEXT.label = str(label or "")


def get_context() -> str:
    return getattr(_CONTEXT, "label", "") or ""


@contextmanager
def context(label: Optional[str]) -> Iterator[None]:
    """在代码块内给本线程的所有日志加上账号标签，退出时还原上一层标签。"""
    previous = get_context()
    set_context(label)
    try:
        yield
    finally:
        set_context(previous)


def _tag(text: str) -> str:
    label = get_context()
    return f"[{label}] {text}" if label else text


def setup(verbose: bool = False, log_dir: Optional[Path] = None) -> None:
    """初始化日志。log_dir 非空时同时把纯文本日志落盘（便于 cron 排查）。"""
    global _VERBOSE, _LOG_FILE, _LOG_HANDLE
    _VERBOSE = verbose
    if log_dir is None:
        return
    log_dir.mkdir(parents=True, exist_ok=True)
    with _LOG_LOCK:
        _close_handle()
        _LOG_FILE = log_dir / f"{datetime.now():%Y-%m-%d}.log"
        try:
            # 常开句柄：并行签到时每条日志都 open/close 会放大成上千次 syscall
            _LOG_HANDLE = _LOG_FILE.open("a", encoding="utf-8", buffering=1)
        except OSError:
            _LOG_HANDLE = None


def _close_handle() -> None:
    global _LOG_HANDLE
    if _LOG_HANDLE is not None:
        try:
            _LOG_HANDLE.close()
        except OSError:
            pass
        _LOG_HANDLE = None


def _roll_log_if_needed(now: datetime) -> None:
    """daemon 跨日运行时自动切换到新日期的日志文件。"""
    global _LOG_FILE, _LOG_HANDLE
    if _LOG_FILE is None:
        return
    expected = f"{now:%Y-%m-%d}.log"
    if _LOG_FILE.name == expected:
        return
    _close_handle()
    _LOG_FILE = _LOG_FILE.with_name(expected)
    try:
        _LOG_HANDLE = _LOG_FILE.open("a", encoding="utf-8", buffering=1)
    except OSError:
        _LOG_HANDLE = None


def flush() -> None:
    """把日志缓冲刷到磁盘，不关闭句柄（daemon stop 前调用）。"""
    with _LOG_LOCK:
        handle = _LOG_HANDLE
        if handle is not None:
            try:
                handle.flush()
            except (OSError, ValueError):
                _close_handle()


def shutdown() -> None:
    """关闭日志文件句柄（进程退出或切换日志目录前调用）。"""
    with _LOG_LOCK:
        _close_handle()


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
    now = datetime.now()
    timestamped = f"[{now:%H:%M:%S}] {plain}"
    with _LOG_LOCK:
        _RECENT_LOGS.append(timestamped)
        listeners = list(_LISTENERS)
        _roll_log_if_needed(now)
        handle = _LOG_HANDLE
        if handle is not None:
            try:
                handle.write(timestamped + "\n")
            except (OSError, ValueError):
                _close_handle()
        elif _LOG_FILE is not None:
            # setup() 没能拿到常开句柄时退回逐条追加，保证日志不丢
            try:
                with _LOG_FILE.open("a", encoding="utf-8") as fh:
                    fh.write(timestamped + "\n")
            except OSError:
                pass
    for listener in listeners:
        try:
            listener(timestamped)
        except Exception:
            pass


def _esc(text: str) -> str:
    return text.replace("[", "\\[")


def step(text: str) -> None:
    tagged = _tag(text)
    _emit(f"[step]==>[/step] {_esc(tagged)}", f"==> {tagged}")


def info(text: str) -> None:
    tagged = _tag(text)
    _emit(f"    {_esc(tagged)}", f"    {tagged}")


def ok(text: str) -> None:
    tagged = _tag(text)
    _emit(f"    [ok]OK[/ok]   {_esc(tagged)}", f"    OK   {tagged}")


def warn(text: str) -> None:
    tagged = _tag(text)
    _emit(f"    [warn]WARN[/warn] {_esc(tagged)}", f"    WARN {tagged}")


def err(text: str) -> None:
    tagged = _tag(text)
    _emit(f"    [err]FAIL[/err] {_esc(tagged)}", f"    FAIL {tagged}")


def debug(text: str) -> None:
    if _VERBOSE:
        tagged = _tag(text)
        _emit(f"    [dim2]dbg  {_esc(tagged)}[/dim2]", f"    dbg  {tagged}")


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
    # 本次签到奖励的额度（原始 quota 单位）。今日已签、或站点不返回时为 None
    quota: object = None
    # 账户剩余额度（原始 quota 单位）。None = 没查到，0 = 真的没钱了，两者别混
    balance: object = None
    # 站点的额度换算率。None 表示按默认值算（不同 fork 的 quota_per_unit 不一定相同）
    quota_per_unit: object = None


def _as_money(raw, unit: int) -> Optional[str]:
    """把原始 quota 数值按换算率渲染成 $ 金额。认不出数字返回 None。"""
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return None
    return f"${value / unit:.2f}"


def format_balance(balance, quota_per_unit=None, award=None) -> str:
    """额度列的文案：余额为主，本次奖励跟在括号里。

    形如 `$12.34（+$5.20）`；没有奖励值（今日已签、站点不返回）时只显示余额。
    余额查不到才是 `-` —— 余额真的是 0 要显示 $0.00，那是「账户没钱了」，
    和「没查到」是两件事，混在一起会让人误判。
    """
    from .config import DEFAULT_QUOTA_PER_UNIT

    unit = int(quota_per_unit) if _positive(quota_per_unit) else DEFAULT_QUOTA_PER_UNIT
    text = _as_money(balance, unit)
    if text is None:
        # 余额没拿到时退一步显示本次奖励，总比整列空着有用
        award_only = _as_money(award, unit) if award not in (None, "", 0) else None
        return f"+{award_only}" if award_only else "-"
    if award in (None, "", 0):
        return text
    award_text = _as_money(award, unit)
    return f"{text}（+{award_text}）" if award_text else text


def _positive(raw) -> bool:
    try:
        return int(raw) > 0
    except (TypeError, ValueError):
        return False


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
        table.add_column("剩余额度", justify="right")
        table.add_column("说明", overflow="fold")
        for r in self.rows:
            style = STATUS_STYLE.get(r.status, "warn")
            label = STATUS_LABEL.get(r.status, r.status)
            quota = format_balance(r.balance, r.quota_per_unit, r.quota)
            table.add_row(r.name, f"[{style}]{label}[/{style}]", r.strategy, quota, _esc(r.detail))
        console.print()
        console.print(table)


def supports_color() -> bool:
    return console.is_terminal and not os.environ.get("NO_COLOR")
