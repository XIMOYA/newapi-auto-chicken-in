# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller onedir spec for the sanitized Windows x64 release."""

from pathlib import Path

from PyInstaller.utils.hooks import collect_all, collect_submodules

# The build script invokes PyInstaller from the project root intentionally.
PROJECT_ROOT = Path.cwd().resolve()

all_datas = []
all_binaries = []
all_hiddenimports = []

# These packages contain native extensions, package data, or dynamically selected
# browser/strategy modules that PyInstaller cannot always discover statically.
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
):
    datas, binaries, hiddenimports = collect_all(package)
    all_datas.extend(datas)
    all_binaries.extend(binaries)
    if package == "camoufox":
        # camoufox.__main__ imports its optional PySide6 GUI. The check-in
        # runtime only needs sync_api and the browser helpers, so omit the GUI
        # to avoid shipping hundreds of megabytes of unused Qt libraries.
        hiddenimports = [
            name
            for name in hiddenimports
            if name != "camoufox.__main__" and not name.startswith("camoufox.gui")
        ]
    all_hiddenimports.extend(hiddenimports)

all_hiddenimports.extend(
    [
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
    ]
)

for package in ("curl_cffi", "patchright", "playwright"):
    all_hiddenimports.extend(collect_submodules(package))

# config.json, config.example.json, data/, and browser caches are deliberately
# not included here. The build script stages only a sanitized config.json beside
# the executable, while runtime data is created on first launch.

a = Analysis(
    [str(PROJECT_ROOT / "main.py")],
    pathex=[str(PROJECT_ROOT)],
    binaries=all_binaries,
    datas=all_datas,
    hiddenimports=sorted(set(all_hiddenimports)),
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=["pytest", "tests", "PySide6", "shiboken6", "camoufox.__main__", "camoufox.gui"],
    noarchive=False,
)

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="newapi-checkin-sanitized",
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
    name="newapi-checkin-sanitized",
)
