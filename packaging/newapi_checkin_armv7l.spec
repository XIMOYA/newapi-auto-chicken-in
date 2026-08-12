# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller onedir spec for the native ARMv7l release."""

from pathlib import Path

from PyInstaller.utils.hooks import collect_all, collect_submodules

PROJECT_ROOT = Path.cwd().resolve()

all_datas = []
all_binaries = []
all_hiddenimports = []

for package in ("newapi_checkin", "curl_cffi", "rich", "patchright"):
    datas, binaries, hiddenimports = collect_all(package)
    all_datas.extend(datas)
    all_binaries.extend(binaries)
    all_hiddenimports.extend(hiddenimports)

# These imports are selected dynamically by the strategy chain.
all_hiddenimports.extend(
    [
        "newapi_checkin.cf.driver_patchright",
        "newapi_checkin.cf.driver_camoufox",
        "newapi_checkin.cf.detect",
        "newapi_checkin.cf.session_store",
        "newapi_checkin.ai.humanize",
        "newapi_checkin.ai.prompts",
        "newapi_checkin.ai.vision",
    ]
)
all_hiddenimports.extend(collect_submodules("curl_cffi"))
all_hiddenimports.extend(collect_submodules("patchright"))

# Do not include config.json, config.example.json, data/, or browser/ here.
# They are staged beside the executable by scripts/build_armv7l.sh.

a = Analysis(
    [str(PROJECT_ROOT / "main.py")],
    pathex=[str(PROJECT_ROOT)],
    binaries=all_binaries,
    datas=all_datas,
    hiddenimports=sorted(set(all_hiddenimports)),
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=["camoufox", "pytest", "tests"],
    noarchive=False,
)

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="newapi-checkin-armv7l",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=True,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    name="newapi-checkin-armv7l",
)
