"""NewAPI 后台守护进程与本地 IPC 客户端。"""

from __future__ import annotations

import argparse
import json
import os
import secrets
import socket
import subprocess
import sys
import threading
import time
from dataclasses import asdict, dataclass
from datetime import datetime
from multiprocessing.connection import Client, Listener
from pathlib import Path
from typing import Any, Optional

from . import __version__
from . import logger as log
from . import daemon_control
from .config import DATA_DIR, LOGS_DIR, ConfigError, load_config
from .config_store import load_document, save_document
from .remote_sync import sync_remote_config
from .runner import RunOptions, Runner
from .secure_config import ConfigEncryptionError
from .scheduler import ScheduleError, SchedulerService

STATE_FILE = DATA_DIR / "daemon.json"
HOST = "127.0.0.1"
AUTHKEY_BYTES = 32


@dataclass
class DaemonInfo:
    host: str
    port: int
    authkey: str
    pid: int
    started_at: str
    version: str = __version__

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, raw: dict) -> "DaemonInfo":
        return cls(
            host=str(raw["host"]),
            port=int(raw["port"]),
            authkey=str(raw["authkey"]),
            pid=int(raw.get("pid") or 0),
            started_at=str(raw.get("started_at") or ""),
            version=str(raw.get("version") or ""),
        )


def state_file(path: Optional[Path] = None) -> Path:
    return path or STATE_FILE


def read_info(path: Optional[Path] = None) -> Optional[DaemonInfo]:
    target = state_file(path)
    try:
        raw = json.loads(target.read_text(encoding="utf-8"))
        return DaemonInfo.from_dict(raw)
    except (OSError, json.JSONDecodeError, KeyError, TypeError, ValueError):
        return None


def _write_info(info: DaemonInfo, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(json.dumps(info.to_dict(), ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temporary.replace(path)


def _remove_info(info: DaemonInfo, path: Path) -> None:
    current = read_info(path)
    if current and current.pid == info.pid and current.port == info.port:
        try:
            path.unlink()
        except OSError:
            pass


def _authkey(info: DaemonInfo) -> bytes:
    return bytes.fromhex(info.authkey)


def _connect(info: DaemonInfo, timeout: float = 2.0):
    # multiprocessing.connection.Client 没有 timeout 参数，临时设置 socket 默认超时。
    previous = socket.getdefaulttimeout()
    socket.setdefaulttimeout(timeout)
    try:
        return Client((info.host, info.port), authkey=_authkey(info))
    finally:
        socket.setdefaulttimeout(previous)


class DaemonClient:
    """GUI/CLI 使用的轻量 IPC 客户端。"""

    def __init__(self, info: Optional[DaemonInfo] = None, path: Optional[Path] = None) -> None:
        self.path = state_file(path)
        self.info = info or read_info(self.path)

    @property
    def available(self) -> bool:
        return self.info is not None

    def request(self, command: str, **payload: Any) -> dict:
        if self.info is None:
            self.info = read_info(self.path)
        if self.info is None:
            raise ConnectionError("daemon 未运行")
        conn = _connect(self.info)
        try:
            conn.send({"command": command, **payload})
            response = conn.recv()
        finally:
            conn.close()
        if not isinstance(response, dict):
            raise ConnectionError("daemon 返回了无效响应")
        return response

    def ping(self) -> bool:
        try:
            return bool(self.request("ping").get("ok"))
        except (ConnectionError, OSError, EOFError, socket.timeout):
            return False

    def status(self) -> dict:
        return self.request("status")


def daemon_is_running(path: Optional[Path] = None) -> bool:
    return DaemonClient(path=path).ping()


def daemon_process_command() -> list[str]:
    """返回当前环境启动 daemon 的命令，冻结后复用桌面 EXE。"""
    if getattr(sys, "frozen", False):
        return [sys.executable, "--daemon"]
    return [sys.executable, str(Path(__file__).resolve().parent.parent / "desktop.py"), "--daemon"]


def start_daemon_process() -> subprocess.Popen:
    command = daemon_process_command()
    kwargs: dict[str, Any] = {
        "stdin": subprocess.DEVNULL,
        "stdout": subprocess.DEVNULL,
        "stderr": subprocess.DEVNULL,
        "close_fds": True,
    }
    if os.name == "nt":
        kwargs["creationflags"] = getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)
        kwargs["cwd"] = str(Path(sys.executable).resolve().parent if getattr(sys, "frozen", False) else DATA_DIR.parent)
    return subprocess.Popen(command, **kwargs)


class DaemonServer:
    def __init__(self, data_dir: Optional[Path] = None) -> None:
        self.data_dir = data_dir or DATA_DIR
        self.state_path = self.data_dir / "daemon.json"
        self.scheduler_path = self.data_dir / "scheduler.json"
        self._stop_event = threading.Event()
        self._stopped_event = threading.Event()
        self._stop_lock = threading.Lock()
        self._process_mode = False
        self._listener: Optional[Listener] = None
        self._info: Optional[DaemonInfo] = None
        self._scheduler: Optional[SchedulerService] = None
        self._connections: set[Any] = set()
        self._connections_lock = threading.Lock()

    def run(self) -> int:
        self._process_mode = threading.current_thread() is threading.main_thread()
        self.data_dir.mkdir(parents=True, exist_ok=True)
        existing = read_info(self.state_path)
        if existing and DaemonClient(existing, self.state_path).ping():
            log.info("daemon 已在运行，复用现有进程")
            return 0
        if existing:
            try:
                self.state_path.unlink()
            except OSError:
                pass

        log.setup(verbose=False, log_dir=self.data_dir / "logs")
        try:
            self._scheduler = SchedulerService(
                config_path=self.scheduler_path,
                run_callback=self._run_checkin,
                log_callback=lambda text: log.info(text),
            )
            authkey = secrets.token_bytes(AUTHKEY_BYTES)
            self._listener = Listener((HOST, 0), authkey=authkey)
            # multiprocessing.connection 没有公开 accept timeout；设置底层
            # loopback socket 的短超时，让 stop_event 能在 Windows 上及时生效。
            raw_listener = getattr(getattr(self._listener, "_listener", None), "_socket", None)
            if raw_listener is not None:
                raw_listener.settimeout(1.0)
            host, port = self._listener.address
            self._info = DaemonInfo(
                host=host,
                port=int(port),
                authkey=authkey.hex(),
                pid=os.getpid(),
                started_at=datetime.now().isoformat(timespec="seconds"),
            )
            _write_info(self._info, self.state_path)
            self._scheduler.start()
            log.info(f"daemon 已启动 pid={os.getpid()} 监听 {HOST}:{port}")
            self._accept_loop()
            return 0
        except KeyboardInterrupt:
            return 130
        except Exception as exc:  # noqa: BLE001 - 守护进程需给出日志而非静默退出
            log.err(f"daemon 启动失败: {type(exc).__name__}: {exc}")
            return 1
        finally:
            self.stop()

    def stop(self) -> None:
        if self._stopped_event.is_set():
            return
        if not self._stop_lock.acquire(blocking=False):
            self._stopped_event.wait(timeout=5.0)
            return
        try:
            listener = self._listener
            info = self._info
            self._stop_event.set()
            if self._scheduler:
                self._scheduler.stop()
            # Windows 上 Listener.accept() 可能不会因 close() 立即返回，先建立
            # 一个 loopback 连接唤醒 accept，再关闭 listener，确保 --stop 能退出。
            if listener is not None and info is not None:
                try:
                    wake = _connect(info, timeout=0.5)
                    wake.close()
                except (ConnectionError, OSError, socket.timeout):
                    pass
            if listener:
                try:
                    listener.close()
                except OSError:
                    pass
                self._listener = None
            with self._connections_lock:
                connections = list(self._connections)
                self._connections.clear()
            for conn in connections:
                try:
                    conn.close()
                except OSError:
                    pass
            if self._info:
                _remove_info(self._info, self.state_path)
            log.info("daemon 已停止")
        finally:
            self._stopped_event.set()
            self._stop_lock.release()

    def _accept_loop(self) -> None:
        assert self._listener is not None
        while not self._stop_event.is_set():
            try:
                conn = self._listener.accept()
            except (OSError, EOFError):
                if self._stop_event.is_set():
                    break
                continue
            with self._connections_lock:
                self._connections.add(conn)
            threading.Thread(target=self._handle, args=(conn,), daemon=True).start()

    def _handle(self, conn: Any) -> None:
        try:
            request = conn.recv()
            response = self._dispatch(request if isinstance(request, dict) else {})
            shutdown_process = bool(response.pop("_shutdown", False))
            conn.send(response)
            if shutdown_process:
                # 独立 daemon 进程收到 stop 后直接结束自身，避免 Windows
                # 后台线程/accept 阻塞让 GUI 看到“已停止”但进程仍残留。
                self._stop_event.set()
                if self._info:
                    _remove_info(self._info, self.state_path)
                os._exit(0)
        except (EOFError, OSError) as exc:
            log.debug(f"IPC 连接关闭: {exc}")
        except Exception as exc:  # noqa: BLE001
            try:
                conn.send({"ok": False, "error": f"{type(exc).__name__}: {exc}"})
            except OSError:
                pass
        finally:
            with self._connections_lock:
                self._connections.discard(conn)
            try:
                conn.close()
            except OSError:
                pass

    def _dispatch(self, request: dict) -> dict:
        command = str(request.get("command") or "").strip().lower()
        if command == "ping":
            return {"ok": True, "pid": os.getpid(), "version": __version__}
        if command == "status":
            return {"ok": True, "status": self._scheduler.snapshot() if self._scheduler else {}}
        if command == "get_schedule":
            assert self._scheduler is not None
            return {"ok": True, "schedule": self._scheduler.config.to_dict(), "status": self._scheduler.snapshot()}
        if command == "set_schedule":
            assert self._scheduler is not None
            try:
                schedule = self._scheduler.set_config(request.get("schedule") or {})
            except ScheduleError as exc:
                return {"ok": False, "error": str(exc)}
            return {"ok": True, "schedule": schedule, "status": self._scheduler.snapshot()}
        if command == "get_config_meta":
            try:
                return {"ok": True, "meta": load_document().safe_meta()}
            except (ConfigError, OSError, ValueError) as exc:
                return {"ok": False, "error": str(exc)}
        if command == "save_config":
            raw = request.get("config")
            if not isinstance(raw, dict):
                return {"ok": False, "error": "config 必须是对象"}
            try:
                encryption_enabled = request.get("encryption_enabled")
                document = save_document(
                    raw,
                    encryption_enabled=None if encryption_enabled is None else bool(encryption_enabled),
                    key=request.get("key") if isinstance(request.get("key"), str) else None,
                )
                return {"ok": True, "meta": document.safe_meta()}
            except (ConfigError, ConfigEncryptionError, OSError, ValueError) as exc:
                return {"ok": False, "error": str(exc)}
        if command == "reload_config":
            try:
                document = load_document()
                cfg = load_config()
                return {
                    "ok": True,
                    "meta": document.safe_meta(),
                    "account_count": len(cfg.accounts),
                }
            except (ConfigError, OSError, ValueError) as exc:
                return {"ok": False, "error": str(exc)}
        if command == "sync_config":
            return sync_remote_config(force=True)
        if command in {"run_now", "run_manual"}:
            assert self._scheduler is not None
            names = request.get("account_names")
            if names is not None and not isinstance(names, list):
                return {"ok": False, "error": "account_names 必须是数组"}
            result = self._scheduler.run_now(names, manual=command == "run_manual")
            return result
        if command == "logs":
            limit = max(1, min(int(request.get("limit") or 200), 1000))
            return {"ok": True, "logs": log.recent_logs(limit)}
        if command == "stop":
            if self._process_mode:
                return {"ok": True, "_shutdown": True}
            threading.Thread(target=self.stop, name="daemon-stop", daemon=True).start()
            return {"ok": True}
        return {"ok": False, "error": f"未知 IPC 命令: {command or '<empty>'}"}

    def _run_checkin(self, account_names: Optional[list[str]], manual: bool) -> dict:
        log.step("daemon 开始执行签到" + ("（手动验证）" if manual else "（定时/立即）"))
        try:
            sync_result = sync_remote_config(auto_only=True)
            if sync_result.get("ok") and not sync_result.get("skipped"):
                log.info(sync_result.get("message") or "远程配置已同步")
            elif not sync_result.get("ok"):
                log.warn(f"远程配置自动同步失败，继续使用本地配置: {sync_result.get('error')}")
            cfg = load_config()
            if manual:
                cfg.browser.headless = False
            else:
                # 定时/立即签到永远不弹浏览器窗口；手动验证是唯一有头例外。
                cfg.browser.headless = True
            # 手动验证只有一台浏览器，强制串行；定时/立即签到用调度配置的并行度
            scheduler = self._scheduler
            scheduled_parallelism = 1
            if scheduler is not None:
                scheduled_parallelism = getattr(scheduler.config, "parallelism", 1)
            options = RunOptions(
                account_names=account_names,
                headful=manual,
                manual=manual,
                use_ai=True,
                use_browser=True,
                verbose=False,
                parallelism=1 if manual else scheduled_parallelism,
                # 调度里配的并发数是用户的明确意愿，包括「就要串行 1」
                parallelism_explicit=not manual,
            )
            runner = Runner(cfg, options)
            exit_code = runner.run()
            rows = [
                {
                    "name": row.name,
                    "status": row.status,
                    "strategy": row.strategy,
                    "detail": row.detail,
                    "quota": row.quota,
                }
                for row in runner.summary.rows
            ]
            result = {
                "ok": exit_code == 0,
                "exit_code": exit_code,
                "finished_at": datetime.now().isoformat(timespec="seconds"),
                "rows": rows,
            }
            log.info(f"daemon 签到完成 exit_code={exit_code}")
            return result
        except ConfigError as exc:
            log.err(str(exc))
            return {"ok": False, "exit_code": 2, "error": str(exc), "rows": []}
        except Exception as exc:  # noqa: BLE001
            log.err(f"daemon 执行签到失败: {type(exc).__name__}: {exc}")
            return {"ok": False, "exit_code": 1, "error": f"{type(exc).__name__}: {exc}", "rows": []}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="NewAPI 签到后台守护进程")
    parser.add_argument("--daemon", action="store_true", help="运行后台 daemon")
    parser.add_argument("--autostart", action="store_true", help="标记为开机启动进入（保留参数）")
    parser.add_argument("--stop", action="store_true", help="请求正在运行的 daemon 停止")
    parser.add_argument("--status", action="store_true", help="查询 daemon 状态")
    parser.add_argument("--version", action="version", version=f"newapi-checkin {__version__}")
    return parser


def main(argv: Optional[list[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    if args.stop:
        try:
            print(json.dumps(DaemonClient().request("stop"), ensure_ascii=False))
            return 0
        except (ConnectionError, OSError) as exc:
            print(f"daemon 未运行: {exc}")
            return 1
    if args.status:
        try:
            print(json.dumps(DaemonClient().status(), ensure_ascii=False, indent=2))
            return 0
        except (ConnectionError, OSError) as exc:
            print(f"daemon 未运行: {exc}")
            return 1
    if args.daemon:
        if args.autostart and not daemon_control.is_enabled():
            return 0
        return DaemonServer().run()
    build_parser().print_help()
    return 0


if __name__ == "__main__":
    sys.exit(main())
