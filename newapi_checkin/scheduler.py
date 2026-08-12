"""后台定时调度：无第三方 scheduler 依赖，可被 GUI 和 daemon 复用。"""

from __future__ import annotations

import json
import threading
import time
from dataclasses import asdict, dataclass, field, replace
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Callable, Optional


DEFAULT_SCHEDULE = {
    "enabled": True,
    "times": ["09:00"],
    "account_names": [],
    "run_on_start": False,
    "headless": True,
    "parallelism": 2,
}


class ScheduleError(ValueError):
    """定时配置不合法。"""


def normalize_time(value: Any) -> str:
    text = str(value or "").strip()
    parts = text.split(":")
    if len(parts) != 2:
        raise ScheduleError(f"时间必须是 HH:MM 格式: {value!r}")
    try:
        hour, minute = int(parts[0]), int(parts[1])
    except (TypeError, ValueError) as exc:
        raise ScheduleError(f"时间必须是 HH:MM 格式: {value!r}") from exc
    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        raise ScheduleError(f"时间超出范围: {value!r}")
    return f"{hour:02d}:{minute:02d}"


def _bool(value: Any, default: bool) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


@dataclass
class ScheduleConfig:
    enabled: bool = True
    times: tuple[str, ...] = ("09:00",)
    account_names: tuple[str, ...] = ()
    run_on_start: bool = False
    headless: bool = True
    parallelism: int = 2

    @classmethod
    def from_dict(cls, raw: Optional[dict]) -> "ScheduleConfig":
        raw = raw if isinstance(raw, dict) else {}
        raw_times = raw.get("times", DEFAULT_SCHEDULE["times"])
        if isinstance(raw_times, str):
            raw_times = [raw_times]
        if not isinstance(raw_times, (list, tuple)):
            raise ScheduleError("times 必须是数组")
        times = tuple(sorted({normalize_time(value) for value in raw_times}))
        if not times:
            raise ScheduleError("至少需要一个签到时间")

        raw_accounts = raw.get("account_names", [])
        if isinstance(raw_accounts, str):
            raw_accounts = [raw_accounts]
        if not isinstance(raw_accounts, (list, tuple)):
            raise ScheduleError("account_names 必须是数组")
        accounts = tuple(dict.fromkeys(str(item).strip() for item in raw_accounts if str(item).strip()))

        # 并行度支持 max_workers 旧别名，钳制到 [1, 8]
        raw_workers = raw.get("parallelism", raw.get("max_workers",
                                                     DEFAULT_SCHEDULE["parallelism"]))
        try:
            workers = int(raw_workers)
        except (TypeError, ValueError):
            workers = DEFAULT_SCHEDULE["parallelism"]
        parallelism = max(1, min(8, workers))

        return cls(
            enabled=_bool(raw.get("enabled"), True),
            times=times,
            account_names=accounts,
            run_on_start=_bool(raw.get("run_on_start"), False),
            headless=_bool(raw.get("headless"), True),
            parallelism=parallelism,
        )

    def to_dict(self) -> dict:
        return {
            "enabled": self.enabled,
            "times": list(self.times),
            "account_names": list(self.account_names),
            "run_on_start": self.run_on_start,
            "headless": self.headless,
            "parallelism": self.parallelism,
        }

    def next_run(self, now: Optional[datetime] = None) -> Optional[datetime]:
        if not self.enabled:
            return None
        now = now or datetime.now()
        candidates: list[datetime] = []
        for day_offset in range(0, 8):
            day = (now + timedelta(days=day_offset)).date()
            for value in self.times:
                hour, minute = (int(part) for part in value.split(":"))
                candidate = datetime.combine(day, datetime.min.time()).replace(
                    hour=hour, minute=minute
                )
                if candidate > now:
                    candidates.append(candidate)
        return min(candidates) if candidates else None


@dataclass
class ServiceSnapshot:
    running: bool = False
    enabled: bool = True
    times: list[str] = field(default_factory=list)
    account_names: list[str] = field(default_factory=list)
    next_run: Optional[str] = None
    last_started: Optional[str] = None
    last_finished: Optional[str] = None
    last_result: Optional[dict] = None
    last_error: str = ""
    pid: int = 0

    def to_dict(self) -> dict:
        return asdict(self)


RunCallback = Callable[[Optional[list[str]], bool], dict]


class SchedulerService:
    """进程内调度服务；签到本身在独立线程执行，防止阻塞 IPC。"""

    def __init__(
        self,
        config_path: Path,
        run_callback: RunCallback,
        log_callback: Optional[Callable[[str], None]] = None,
    ) -> None:
        self.config_path = config_path
        self.run_callback = run_callback
        self.log_callback = log_callback
        self._config = self.load_config(config_path)
        if self._config.account_names:
            # 旧版本允许在定时配置中选择账号；现在定时任务统一覆盖所有启用账号。
            self._config = replace(self._config, account_names=())
            self.save_config(self._config)
        self._stop_event = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._run_lock = threading.Lock()
        self._config_lock = threading.RLock()
        self._pending_requests: dict[str, dict] = {}
        self._snapshot = ServiceSnapshot(pid=__import__("os").getpid())
        self._last_slot = ""
        self._startup_pending = self._config.run_on_start
        self._sync_snapshot()

    @staticmethod
    def load_config(path: Path) -> ScheduleConfig:
        if not path.exists():
            return ScheduleConfig.from_dict(DEFAULT_SCHEDULE)
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise ScheduleError(f"读取调度配置失败: {path}: {exc}") from exc
        return ScheduleConfig.from_dict(raw)

    def save_config(self, config: ScheduleConfig) -> None:
        self.config_path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.config_path.with_name(self.config_path.name + ".tmp")
        temporary.write_text(
            json.dumps(config.to_dict(), ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        temporary.replace(self.config_path)

    @property
    def config(self) -> ScheduleConfig:
        with self._config_lock:
            return self._config

    def set_config(self, raw: dict) -> dict:
        config = ScheduleConfig.from_dict(raw)
        self.save_config(config)
        with self._config_lock:
            self._config = config
            self._startup_pending = False
            self._sync_snapshot()
        return config.to_dict()

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop_event.clear()
        self._thread = threading.Thread(target=self._loop, name="checkin-scheduler", daemon=True)
        self._thread.start()

    def stop(self, timeout: float = 5.0) -> None:
        self._stop_event.set()
        thread = self._thread
        if thread and thread.is_alive() and thread is not threading.current_thread():
            thread.join(timeout=timeout)

    def run_now(self, account_names: Optional[list[str]] = None, manual: bool = False,
                request_id: Optional[str] = None) -> dict:
        if request_id is not None:
            with self._config_lock:
                pending = self._pending_requests.get(request_id)
            if pending is not None:
                return pending
        if not self._run_lock.acquire(blocking=False):
            return {"ok": False, "running": True, "message": "已有签到任务正在运行"}
        started = datetime.now().isoformat(timespec="seconds")
        result = {"ok": True, "running": True, "started": started}
        if request_id is not None:
            with self._config_lock:
                self._pending_requests[request_id] = result
        with self._config_lock:
            self._snapshot.running = True
            self._snapshot.last_started = started
            self._snapshot.last_error = ""
        thread = threading.Thread(
            target=self._run_worker,
            args=(account_names, manual, request_id),
            name="checkin-runner",
            daemon=True,
        )
        thread.start()
        return result

    def snapshot(self) -> dict:
        with self._config_lock:
            self._sync_snapshot()
            return self._snapshot.to_dict()

    def _sync_snapshot(self) -> None:
        config = self._config
        self._snapshot.enabled = config.enabled
        self._snapshot.times = list(config.times)
        self._snapshot.account_names = list(config.account_names)
        next_run = config.next_run()
        self._snapshot.next_run = next_run.isoformat(timespec="seconds") if next_run else None

    def _run_worker(self, account_names: Optional[list[str]], manual: bool,
                    request_id: Optional[str] = None) -> None:
        try:
            names = account_names
            if names is None:
                names = list(self.config.account_names) or None
            result = self.run_callback(names, manual)
            with self._config_lock:
                self._snapshot.last_result = result if isinstance(result, dict) else {"value": result}
                self._snapshot.last_finished = datetime.now().isoformat(timespec="seconds")
        except Exception as exc:  # noqa: BLE001 - daemon 不能因单次签到退出
            with self._config_lock:
                self._snapshot.last_error = f"{type(exc).__name__}: {exc}"
                self._snapshot.last_finished = datetime.now().isoformat(timespec="seconds")
            self._emit(f"签到线程异常: {type(exc).__name__}: {exc}")
        finally:
            with self._config_lock:
                self._snapshot.running = False
                if request_id is not None:
                    self._pending_requests.pop(request_id, None)
            self._run_lock.release()

    def _loop(self) -> None:
        while not self._stop_event.is_set():
            config = self.config
            if self._startup_pending:
                self._startup_pending = False
                self.run_now()
            elif config.enabled:
                now = datetime.now()
                slot = now.strftime("%Y-%m-%d %H:%M")
                if now.strftime("%H:%M") in config.times and slot != self._last_slot:
                    self._last_slot = slot
                    self.run_now()
            self._stop_event.wait(10.0)

    def _emit(self, text: str) -> None:
        if self.log_callback:
            self.log_callback(text)
        else:
            time.sleep(0)
