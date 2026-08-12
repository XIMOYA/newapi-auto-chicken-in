#!/usr/bin/env python3
"""Build a Windows EXE release containing only sanitized configuration data.

This script intentionally stages the release from PyInstaller output instead of
copying the live project directory. The live config.json and data/ tree are
never copied into the release.
"""

from __future__ import annotations

import hashlib
import json
import shutil
import struct
import subprocess
import sys
import tempfile
import zipfile
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parent.parent
SPEC = ROOT / "packaging" / "newapi_checkin_windows.spec"
DIST_ROOT = ROOT / "dist" / "windows-sanitized"
LIVE_CONFIG = ROOT / "config.json"
BUNDLED_CAMOUFOX_DIR = Path("browser") / "camoufox-x86_64"
BUNDLED_CAMOUFOX_EXE = BUNDLED_CAMOUFOX_DIR / "camoufox.exe"

SANITIZED_CONFIG: dict[str, Any] = {
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
        "headless": "virtual",
        "humanize": True,
        "timeout": 60,
        "keep_artifacts_on_fail": True,
        "locale": "zh-CN",
        "window": [1280, 800],
        "executable_path": BUNDLED_CAMOUFOX_EXE.as_posix(),
    },
    "http": {
        "impersonate": "chrome",
        "timeout": 20,
        "verify": True,
    },
    "defaults": {
        "retry": 5,
        "interval_seconds": [3, 8],
    },
    "accounts": [
        {
            "name": "示例账号（请填写）",
            "username": "",
            "site_api_key": "",
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


def run(command: list[str]) -> subprocess.CompletedProcess[str]:
    printable = " ".join(command)
    print(f"==> {printable}")
    return subprocess.run(
        command,
        cwd=ROOT,
        check=True,
        text=True,
    )


def write_sanitized_config(path: Path) -> None:
    path.write_text(
        json.dumps(SANITIZED_CONFIG, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def assert_live_config_unchanged(original: bytes | None) -> None:
    current = LIVE_CONFIG.read_bytes() if LIVE_CONFIG.is_file() else None
    if current != original:
        original_sha = hashlib.sha256(original).hexdigest() if original is not None else "<missing>"
        current_sha = file_sha256(LIVE_CONFIG) if current is not None else "<missing>"
        raise RuntimeError(
            "构建过程修改了项目根目录 config.json: "
            f"before={original_sha}, after={current_sha}"
        )


def pe_machine(path: Path) -> int:
    """Return the PE machine field without executing the binary."""
    with path.open("rb") as handle:
        dos_header = handle.read(0x40)
        if len(dos_header) < 0x40:
            raise RuntimeError(f"文件不是完整 PE: {path}")
        pe_offset = struct.unpack_from("<I", dos_header, 0x3C)[0]
        handle.seek(pe_offset + 4)
        machine_raw = handle.read(2)
        if len(machine_raw) != 2:
            raise RuntimeError(f"文件缺少 PE machine 字段: {path}")
        return struct.unpack("<H", machine_raw)[0]


def validate_packaged_language_data(root: Path) -> Path:
    """Require the language_tags JSON registry in the frozen release."""
    relative = Path("language_tags") / "data" / "json" / "index.json"
    candidates = (root / relative, root / "_internal" / relative)
    for candidate in candidates:
        if candidate.is_file():
            print(f"==> language_tags 数据校验通过: {candidate}")
            return candidate
    searched = ", ".join(str(candidate) for candidate in candidates)
    raise RuntimeError(f"发布包缺少 language_tags 数据文件: {searched}")


def find_local_camoufox_archive() -> Path:
    """Find the user-provided 64-bit Camoufox zip in the project root."""
    candidates = sorted(ROOT.glob("camoufox-*-win.x86_64.zip"))
    if not candidates:
        raise RuntimeError(
            "项目根目录缺少 Camoufox x86_64 压缩包。请放入: "
            "camoufox-152.0.4-beta.28-win.x86_64.zip"
        )
    if len(candidates) > 1:
        print(f"==> 检测到多个 x86_64 压缩包，使用: {candidates[0].name}")
    else:
        print(f"==> 使用根目录 Camoufox x86_64: {candidates[0].name}")
    return candidates[0]


def stage_bundled_camoufox(temp_root: Path) -> Path:
    """Stage the user-provided Windows x86_64 Camoufox runtime."""
    archive = find_local_camoufox_archive()
    unpacked = temp_root / "camoufox-x86_64-unpacked"
    with zipfile.ZipFile(archive) as package:
        package.extractall(unpacked)

    candidates = sorted(unpacked.rglob("camoufox.exe"), key=lambda path: len(path.parts))
    if not candidates:
        raise RuntimeError("Camoufox x86_64 压缩包中没有 camoufox.exe")

    source_root = candidates[0].parent
    for parent in (source_root, *source_root.parents):
        if (parent / "version.json").is_file():
            source_root = parent
            break

    target_root = DIST_ROOT / BUNDLED_CAMOUFOX_DIR
    shutil.rmtree(target_root, ignore_errors=True)
    target_root.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(source_root, target_root)

    # The standalone release zip does not include the package manager's
    # version.json. Add non-sensitive metadata so the runtime can infer the
    # Firefox major version without relying on a user-level Camoufox cache.
    version_token = archive.name.removeprefix("camoufox-").removesuffix(
        "-win.x86_64.zip"
    )
    if "-" not in version_token:
        raise RuntimeError(f"无法从 Camoufox 文件名解析版本: {archive.name}")
    browser_version, browser_build = version_token.split("-", 1)
    (target_root / "version.json").write_text(
        json.dumps(
            {
                "version": browser_version,
                "build": browser_build,
                "prerelease": "beta" in browser_build or "alpha" in browser_build,
            },
            ensure_ascii=False,
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )

    target_exe = DIST_ROOT / BUNDLED_CAMOUFOX_EXE
    if not target_exe.is_file():
        raise RuntimeError(f"内置 Camoufox x86_64 路径不完整: {target_exe}")
    machine = pe_machine(target_exe)
    if machine != 0x8664:
        raise RuntimeError(f"内置 Camoufox 不是 x86_64/64 位（machine=0x{machine:04x}）")
    print(f"==> 内置 Camoufox x86_64 校验通过: {target_exe}")
    return target_exe


def live_sensitive_values() -> list[str]:

    """Read only the live secrets for post-build scanning; never copy them."""
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
            if not isinstance(item, dict):
                continue
            for key in ("cookie", "site_api_key", "proxy"):
                value = item.get(key)
                if isinstance(value, str) and len(value.strip()) >= 8:
                    values.append(value.strip())
    return sorted(set(values), key=len, reverse=True)


def validate_sanitized_config(path: Path) -> None:
    raw = json.loads(path.read_text(encoding="utf-8-sig"))
    if raw.get("ai", {}).get("enabled") is not False:
        raise RuntimeError("脱敏配置必须关闭 ai.enabled")
    if raw.get("ai", {}).get("api_key") != "":
        raise RuntimeError("脱敏配置包含非空 ai.api_key")
    if raw.get("defaults", {}).get("retry") != 5:
        raise RuntimeError("脱敏配置的 defaults.retry 必须为 5")
    executable_path = raw.get("browser", {}).get("executable_path")
    if executable_path != BUNDLED_CAMOUFOX_EXE.as_posix():
        raise RuntimeError("脱敏配置必须指向内置 x86_64 Camoufox")
    bundled_exe = path.parent / Path(executable_path)
    if not bundled_exe.is_file() or pe_machine(bundled_exe) != 0x8664:
        raise RuntimeError("脱敏配置指向的 Camoufox 不是可用的 x86_64 浏览器")

    accounts = raw.get("accounts")
    if not isinstance(accounts, list) or not accounts:
        raise RuntimeError("脱敏配置必须保留一个可编辑的示例账号")
    for account in accounts:
        if not isinstance(account, dict):
            raise RuntimeError("脱敏配置的账号项必须是对象")
        for key in ("cookie", "site_api_key", "proxy", "username"):
            if account.get(key) not in (None, ""):
                raise RuntimeError(f"脱敏配置字段非空: accounts[].{key}")
        if account.get("user_id") is not None:
            raise RuntimeError("脱敏配置不能包含 user_id")
        if account.get("enabled") is not False:
            raise RuntimeError("示例账号必须默认禁用")


def scan_release(root: Path, require_empty_data: bool = False) -> None:
    forbidden = live_sensitive_values()
    for path in root.rglob("*"):
        if not path.is_file():
            continue
        data = path.read_bytes()
        for value in forbidden:
            if value.encode("utf-8") in data:
                raise RuntimeError(f"发布包疑似包含真实私密值: {path.name}")

    if require_empty_data:
        data_root = root / "data"
        if data_root.exists():
            unexpected_files = [path for path in data_root.rglob("*") if path.is_file()]
            if unexpected_files:
                names = ", ".join(str(path.relative_to(root)) for path in unexpected_files[:5])
                raise RuntimeError(f"发布包包含运行期数据文件: {names}")


def clean_runtime_files(root: Path) -> None:
    """Remove files created only by the post-build smoke test."""
    data_root = root / "data"
    if not data_root.exists():
        return
    for path in data_root.rglob("*"):
        if path.is_file() or path.is_symlink():
            path.unlink()


def verify_executable(exe: Path) -> None:
    if not exe.is_file():
        raise RuntimeError(f"PyInstaller 未生成 EXE: {exe}")

    for args in (("--version",), ("--help",)):
        result = subprocess.run(
            [str(exe), *args],
            cwd=exe.parent,
            capture_output=True,
            text=True,
            check=False,
        )
        if result.returncode != 0:
            detail = (result.stdout + result.stderr).strip()
            raise RuntimeError(f"EXE {args[0]} 启动失败（退出码 {result.returncode}）: {detail}")
        print(f"==> EXE {args[0]} 验证通过")

    # The sanitized account is intentionally disabled. This verifies config
    # loading without attempting any network call or browser launch.
    result = subprocess.run(
        [str(exe), "--dry-run", "--no-browser", "--no-ai"],
        cwd=exe.parent,
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 2 or "没有启用的账号" not in (result.stdout + result.stderr):
        detail = (result.stdout + result.stderr).strip()
        raise RuntimeError(f"空配置加载验证失败（退出码 {result.returncode}）: {detail}")
    print("==> 空配置加载验证通过")


def main() -> int:
    if sys.platform != "win32":
        raise SystemExit(f"此脚本用于 Windows 原生打包，当前平台为 {sys.platform}")
    if sys.version_info < (3, 10):
        raise SystemExit("需要 Python 3.10 或更高版本")
    if not SPEC.is_file():
        raise SystemExit(f"找不到 PyInstaller spec: {SPEC}")

    try:
        import PyInstaller  # noqa: F401
    except ImportError as exc:
        raise SystemExit(
            "未安装 PyInstaller，请使用当前系统 Python 执行: "
            f"{sys.executable} -m pip install pyinstaller"
        ) from exc

    # Snapshot the live configuration so the build can fail closed if any
    # packaging or smoke-test step writes to the project root by mistake.
    live_config_before = LIVE_CONFIG.read_bytes() if LIVE_CONFIG.is_file() else None

    DIST_ROOT.parent.mkdir(parents=True, exist_ok=True)
    shutil.rmtree(DIST_ROOT, ignore_errors=True)

    with tempfile.TemporaryDirectory(prefix="newapi-checkin-build-") as temp_dir:
        temp_root = Path(temp_dir)
        pyi_dist = temp_root / "pyinstaller-dist"
        pyi_work = temp_root / "pyinstaller-work"

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
            ]
        )

        pyi_app = pyi_dist / "newapi-checkin-sanitized"
        if not pyi_app.is_dir():
            raise RuntimeError(f"PyInstaller 未生成 onedir 目录: {pyi_app}")

        DIST_ROOT.mkdir(parents=True, exist_ok=True)
        for child in pyi_app.iterdir():
            target = DIST_ROOT / child.name
            if child.is_dir():
                shutil.copytree(child, target)
            else:
                shutil.copy2(child, target)

        stage_bundled_camoufox(temp_root)
        validate_packaged_language_data(DIST_ROOT)

    config_path = DIST_ROOT / "config.json"
    write_sanitized_config(config_path)
    validate_sanitized_config(config_path)

    for relative in ("data/profiles", "data/shots", "data/logs"):
        (DIST_ROOT / relative).mkdir(parents=True, exist_ok=True)

    # A previous smoke test may have left a generated log if a build was
    # interrupted. It is safe to remove only runtime files in this generated
    # release directory before the pristine-package check.
    clean_runtime_files(DIST_ROOT)
    scan_release(DIST_ROOT, require_empty_data=True)
    verify_executable(DIST_ROOT / "newapi-checkin-sanitized.exe")
    scan_release(DIST_ROOT)
    clean_runtime_files(DIST_ROOT)
    scan_release(DIST_ROOT, require_empty_data=True)
    assert_live_config_unchanged(live_config_before)

    print(f"==> 完成: {DIST_ROOT / 'newapi-checkin-sanitized.exe'}")
    print(f"==> 配置: {config_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
