# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller onedir spec for the Windows PySide6 desktop release."""

from pathlib import Path

from PyInstaller.utils.hooks import collect_all, collect_submodules

PROJECT_ROOT = Path.cwd().resolve()

all_datas = [
    (str(PROJECT_ROOT / "assets" / "newapi_checkin.ico"), "assets"),
    (str(PROJECT_ROOT / "assets" / "newapi_checkin.svg"), "assets"),
]
all_binaries = []
all_hiddenimports = []

for package in (
    "newapi_checkin",
    "curl_cffi",
    "rich",
    "patchright",
    "camoufox",
    "language_tags",
    "playwright",
    "browserforge",
    "apify_fingerprint_datapoints",
    "cryptography",
):
    datas, binaries, hiddenimports = collect_all(package)
    all_datas.extend(datas)
    all_binaries.extend(binaries)
    if package == "camoufox":
        hiddenimports = [
            name
            for name in hiddenimports
            if name != "camoufox.__main__" and not name.startswith("camoufox.gui")
        ]
    all_hiddenimports.extend(hiddenimports)

all_hiddenimports.extend(
    [
        "newapi_checkin.autostart",
        "newapi_checkin.daemon",
        "newapi_checkin.config_store",
        "newapi_checkin.remote_sync",
        "newapi_checkin.gui",
        "newapi_checkin.scheduler",
        "newapi_checkin.secure_config",
        "newapi_checkin.cf.driver_patchright",
        "newapi_checkin.cf.driver_camoufox",
        "newapi_checkin.cf.detect",
        "newapi_checkin.cf.session_store",
        "newapi_checkin.ai.humanize",
        "newapi_checkin.ai.prompts",
        "newapi_checkin.ai.vision",
        "camoufox.sync_api",
        "camoufox.addons",
        "camoufox.virtdisplay",
        "camoufox.pkgman",
        "camoufox.geolocation",
        "camoufox.fingerprints",
        "patchright.sync_api",
        "playwright.sync_api",
        "PySide6.QtCore",
        "PySide6.QtGui",
        "PySide6.QtWidgets",
    ]
)

for package in ("curl_cffi", "patchright", "playwright"):
    all_hiddenimports.extend(collect_submodules(package))


a = Analysis(
    [str(PROJECT_ROOT / "desktop.py")],
    pathex=[str(PROJECT_ROOT)],
    binaries=all_binaries,
    datas=all_datas,
    hiddenimports=sorted(set(all_hiddenimports)),
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=["pytest", "tests", "camoufox.__main__", "camoufox.gui"],
    noarchive=False,
)

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="newapi-checkin",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,
    icon=str(PROJECT_ROOT / "assets" / "newapi_checkin.ico"),
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    name="newapi-checkin",
)
