#!/usr/bin/env python3
"""构建带 PySide6 GUI、daemon 和本地 Camoufox 的 Windows x64 onedir 包。"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import struct
import subprocess
import sys
import tempfile
import zipfile
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
SPEC = ROOT / "packaging" / "newapi_checkin_gui_windows.spec"
DIST_ROOT = ROOT / "dist" / "windows-gui"
LIVE_CONFIG = ROOT / "config.json"
BROWSER_DIR = Path("browser") / "camoufox-x86_64"
BROWSER_EXE = BROWSER_DIR / "camoufox.exe"

SANITIZED_CONFIG: dict[str, Any] = {
    "security": {
        "encryption_enabled": False,
        "config_key": "",
        "encrypted_file": "data/config.encrypted.json",
    },
    "config_sync": {
        "enabled": False,
        "url": "",
        "method": "GET",
        "token": "",
        "token_header": "Authorization",
        "token_prefix": "Bearer",
        "headers": {},
        "body": None,
        "response_field": "",
        "timeout": 20,
        "auto_before_checkin": True,
    },
    "ai": {
        "enabled": False,
        "base_url": "",
        "api_key": "",
        "model": "gpt-4o-mini",
        "timeout": 60,
        "max_retries": 2,
    },
    "browser": {
        "driver": "camoufox",
        "headless": True,
        "humanize": True,
        "timeout": 60,
        "keep_artifacts_on_fail": True,
        "locale": "zh-CN",
        "window": [1280, 800],
        "executable_path": BROWSER_EXE.as_posix(),
    },
    "http": {"impersonate": "chrome", "timeout": 20, "verify": True},
    "defaults": {"retry": 5, "interval_seconds": [3, 8]},
    "accounts": [
        {
            "name": "示例账号（请填写）",
            "url": "https://example.com",
            "cookie": "",
            "proxy": None,
            "user_id": None,
            "checkin_path": None,
            "browser_path": "/dashboard",
            "enabled": False,
        }
    ],
}

DEFAULT_SCHEDULER = {
    "enabled": True,
    "times": ["09:00"],
    "account_names": [],
    "run_on_start": False,
    "headless": True,
    "parallelism": 2,
}


def run(command: list[str], *, env: dict[str, str] | None = None, timeout: int = 600) -> subprocess.CompletedProcess[str]:
    print("==>", " ".join(command))
    return subprocess.run(command, cwd=ROOT, check=True, text=True, env=env, timeout=timeout)


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def assert_live_config_unchanged(original: bytes | None) -> None:
    current = LIVE_CONFIG.read_bytes() if LIVE_CONFIG.is_file() else None
    if current != original:
        before = hashlib.sha256(original).hexdigest() if original is not None else "<missing>"
        after = file_sha256(LIVE_CONFIG) if current is not None else "<missing>"
        raise RuntimeError(f"构建过程修改了根 config.json: before={before}, after={after}")


def pe_machine(path: Path) -> int:
    with path.open("rb") as handle:
        dos = handle.read(0x40)
        if len(dos) < 0x40:
            raise RuntimeError(f"不是完整 PE: {path}")
        offset = struct.unpack_from("<I", dos, 0x3C)[0]
        handle.seek(offset + 4)
        raw = handle.read(2)
        if len(raw) != 2:
            raise RuntimeError(f"PE 缺少 machine: {path}")
        return struct.unpack("<H", raw)[0]


def find_local_camoufox_archive() -> Path:
    candidates = sorted(ROOT.glob("camoufox-*-win.x86_64.zip"))
    if not candidates:
        raise RuntimeError("根目录缺少 camoufox-*-win.x86_64.zip")
    print(f"==> 使用 Camoufox: {candidates[0].name}")
    return candidates[0]


def stage_camoufox(temp_root: Path) -> Path:
    archive = find_local_camoufox_archive()
    unpacked = temp_root / "camoufox-unpacked"
    with zipfile.ZipFile(archive) as package:
        package.extractall(unpacked)
    candidates = sorted(unpacked.rglob("camoufox.exe"), key=lambda item: len(item.parts))
    if not candidates:
        raise RuntimeError("Camoufox 压缩包内没有 camoufox.exe")
    source_root = candidates[0].parent
    for parent in (source_root, *source_root.parents):
        if (parent / "version.json").is_file():
            source_root = parent
            break
    target = DIST_ROOT / BROWSER_DIR
    shutil.rmtree(target, ignore_errors=True)
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(source_root, target)
    version_token = archive.name.removeprefix("camoufox-").removesuffix("-win.x86_64.zip")
    if "-" in version_token:
        version, build = version_token.split("-", 1)
        (target / "version.json").write_text(
            json.dumps({"version": version, "build": build, "prerelease": "beta" in build or "alpha" in build}, indent=2)
            + "\n",
            encoding="utf-8",
        )
    target_exe = DIST_ROOT / BROWSER_EXE
    if not target_exe.is_file() or pe_machine(target_exe) != 0x8664:
        raise RuntimeError(f"内置 Camoufox 不是 x86_64: {target_exe}")
    print(f"==> Camoufox x86_64 校验通过: {target_exe}")
    return target_exe


def write_json(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def write_sanitized_files() -> None:
    write_json(DIST_ROOT / "config.json", SANITIZED_CONFIG)
    write_json(DIST_ROOT / "data" / "scheduler.json", DEFAULT_SCHEDULER)
    for relative in ("data/profiles", "data/shots", "data/logs"):
        (DIST_ROOT / relative).mkdir(parents=True, exist_ok=True)


def validate_config() -> None:
    path = DIST_ROOT / "config.json"
    raw = json.loads(path.read_text(encoding="utf-8"))
    if raw.get("ai", {}).get("enabled") is not False or raw.get("ai", {}).get("api_key") != "":
        raise RuntimeError("发布包 AI 配置未脱敏")
    sync = raw.get("config_sync") or {}
    if sync.get("enabled") is not False or sync.get("token") not in (None, "") or sync.get("url") not in (None, ""):
        raise RuntimeError("发布包远程配置同步未脱敏")
    if raw.get("browser", {}).get("executable_path") != BROWSER_EXE.as_posix():
        raise RuntimeError("发布包浏览器路径不正确")
    accounts = raw.get("accounts")
    if not isinstance(accounts, list) or not accounts:
        raise RuntimeError("发布包需要保留一个示例账号")
    for account in accounts:
        for key in ("cookie", "proxy"):
            if account.get(key) not in (None, ""):
                raise RuntimeError(f"发布包含有账号私密字段: {key}")
        if account.get("user_id") is not None or account.get("enabled") is not False:
            raise RuntimeError("发布包示例账号必须清空 user_id 并禁用")


def validate_language_data() -> Path:
    relative = Path("language_tags") / "data" / "json" / "index.json"
    for candidate in (DIST_ROOT / relative, DIST_ROOT / "_internal" / relative):
        if candidate.is_file():
            print(f"==> language_tags 数据校验通过: {candidate}")
            return candidate
    raise RuntimeError("发布包缺少 language_tags/data/json/index.json")


def live_sensitive_values() -> list[str]:
    if not LIVE_CONFIG.is_file():
        return []
    try:
        raw = json.loads(LIVE_CONFIG.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError):
        return []
    values: list[str] = []
    if isinstance(raw, dict):
        ai = raw.get("ai") or {}
        for key in ("api_key", "base_url"):
            value = ai.get(key)
            if isinstance(value, str) and len(value.strip()) >= 8:
                values.append(value.strip())
        for item in raw.get("accounts") or []:
            if isinstance(item, dict):
                for key in ("cookie", "site_api_key", "proxy"):
                    value = item.get(key)
                    if isinstance(value, str) and len(value.strip()) >= 8:
                        values.append(value.strip())
    return sorted(set(values), key=len, reverse=True)


def scan_release() -> None:
    for path in DIST_ROOT.rglob("*"):
        if path.is_file():
            data = path.read_bytes()
            for secret in live_sensitive_values():
                if secret.encode("utf-8") in data:
                    raise RuntimeError(f"发布包疑似包含真实私密值: {path}")
    forbidden_runtime = [
        DIST_ROOT / "data" / "daemon.json",
        DIST_ROOT / "data" / "sessions.json",
    ]
    for path in forbidden_runtime:
        if path.exists():
            raise RuntimeError(f"发布包包含运行期状态: {path}")


def verify_exe(exe: Path) -> None:
    if not exe.is_file():
        raise RuntimeError(f"未生成 GUI EXE: {exe}")
    if pe_machine(exe) != 0x8664:
        raise RuntimeError(f"GUI EXE 不是 x86_64: machine=0x{pe_machine(exe):04x}")
    for args in (("--version",), ("--smoke-test",)):
        result = subprocess.run(
            [str(exe), *args], cwd=exe.parent, capture_output=True, text=True, check=False, timeout=90
        )
        if result.returncode != 0:
            detail = (result.stdout + result.stderr).strip()
            raise RuntimeError(f"GUI EXE {args[0]} 烟测失败 code={result.returncode}: {detail}")
        print(f"==> GUI EXE {args[0]} 验证通过")


def clean_runtime_files() -> None:
    """清理烟测产生的运行数据，但保留安全的默认 scheduler.json 模板。"""
    data_root = DIST_ROOT / "data"
    if not data_root.exists():
        return
    for path in data_root.rglob("*"):
        if (path.is_file() or path.is_symlink()) and path.name != "scheduler.json":
            path.unlink()


def main() -> int:
    if sys.platform != "win32":
        raise SystemExit(f"此脚本需要 Windows 原生环境，当前为 {sys.platform}")
    if not SPEC.is_file():
        raise SystemExit(f"找不到 spec: {SPEC}")
    try:
        import PyInstaller  # noqa: F401
    except ImportError as exc:
        raise SystemExit(f"未安装 PyInstaller: {sys.executable} -m pip install pyinstaller") from exc

    live_config_before = LIVE_CONFIG.read_bytes() if LIVE_CONFIG.is_file() else None
    shutil.rmtree(DIST_ROOT, ignore_errors=True)
    DIST_ROOT.parent.mkdir(parents=True, exist_ok=True)

    with tempfile.TemporaryDirectory(prefix="newapi-checkin-gui-") as temporary:
        temp_root = Path(temporary)
        pyi_dist = temp_root / "dist"
        pyi_work = temp_root / "work"
        run(
            [
                sys.executable,
                "-m",
                "PyInstaller",
                "--clean",
                "--noconfirm",
                "--distpath",
                str(pyi_dist),
                "--workpath",
                str(pyi_work),
                str(SPEC),
            ],
            timeout=1800,
        )
        pyi_app = pyi_dist / "newapi-checkin"
        if not pyi_app.is_dir():
            raise RuntimeError(f"PyInstaller 未生成目录: {pyi_app}")
        DIST_ROOT.mkdir(parents=True, exist_ok=True)
        for child in pyi_app.iterdir():
            target = DIST_ROOT / child.name
            if child.is_dir():
                shutil.copytree(child, target)
            else:
                shutil.copy2(child, target)
        stage_camoufox(temp_root)
        validate_language_data()

    write_sanitized_files()
    validate_config()
    clean_runtime_files()
    scan_release()
    verify_exe(DIST_ROOT / "newapi-checkin.exe")
    clean_runtime_files()
    scan_release()
    assert_live_config_unchanged(live_config_before)
    print(f"==> 完成: {DIST_ROOT / 'newapi-checkin.exe'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
