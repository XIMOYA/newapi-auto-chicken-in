#!/usr/bin/env bash
# 在原生 Debian/Ubuntu ARMv7l/armhf 构建机上生成自包含发布包。
# 必须提供：
#   ARMHF_CHROMIUM_ROOT=/opt/chromium-armhf
#   ARMHF_SYSROOT=/opt/armhf-sysroot
set -Eeuo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
BUILD_ROOT="${BUILD_ROOT:-$ROOT/.build-armv7l}"
DIST_ROOT="${DIST_ROOT:-$ROOT/dist/armv7l}"
PACKAGE_ROOT="$DIST_ROOT/newapi-checkin-armv7l"
ARCHIVE="$DIST_ROOT/newapi-checkin-armv7l.tar.gz"
VENV="$BUILD_ROOT/venv"
PYI_DIST="$BUILD_ROOT/pyinstaller-dist"
PYI_WORK="$BUILD_ROOT/pyinstaller-work"
CHROMIUM_ROOT="${ARMHF_CHROMIUM_ROOT:-}"
SYSROOT="${ARMHF_SYSROOT:-}"

fail() {
  printf 'ERROR: %s\n' "$*" >&2
  exit 1
}

need_cmd() {
  command -v "$1" >/dev/null 2>&1 || fail "缺少构建工具: $1"
}

[[ -n "$CHROMIUM_ROOT" ]] || fail "必须设置 ARMHF_CHROMIUM_ROOT（已验证可运行的完整 ARMhf Chromium 目录）"
[[ -n "$SYSROOT" ]] || fail "必须设置 ARMHF_SYSROOT（同架构用户态库根目录）"
[[ -d "$CHROMIUM_ROOT" ]] || fail "ARMHF_CHROMIUM_ROOT 不是目录: $CHROMIUM_ROOT"
[[ -d "$SYSROOT" ]] || fail "ARMHF_SYSROOT 不是目录: $SYSROOT"

MACHINE="$(uname -m)"
[[ "$MACHINE" == "armv7l" || "$MACHINE" == armv7* ]] \
  || fail "必须在原生 ARMv7l 构建，当前架构: $MACHINE"

need_cmd dpkg
DPKG_ARCH="$(dpkg --print-architecture)"
[[ "$DPKG_ARCH" == "armhf" ]] || fail "必须是 Debian armhf，当前架构: $DPKG_ARCH"

for cmd in python3 gcc patchelf readelf file lddtree sha256sum tar timeout grep awk cmp; do
  need_cmd "$cmd"
done

python3 - <<'PY'
import sys
if sys.version_info < (3, 10):
    raise SystemExit(f"需要 Python 3.10+，当前是 {sys.version_info.major}.{sys.version_info.minor}")
PY

find_chromium() {
  local root="$1"
  local name
  for name in chromium chromium-browser chromium-headless-shell chrome; do
    if [[ -x "$root/$name" ]]; then
      printf '%s\n' "$root/$name"
      return 0
    fi
  done
  return 1
}

CHROMIUM_BIN="$(find_chromium "$CHROMIUM_ROOT")" \
  || fail "ARMHF_CHROMIUM_ROOT 根目录必须直接包含 chromium/chromium-browser/chromium-headless-shell/chrome 可执行文件"

CHROMIUM_FILE="$(file -L "$CHROMIUM_BIN")"
printf '==> Chromium: %s\n' "$CHROMIUM_FILE"
printf '%s\n' "$CHROMIUM_FILE" | grep -Eq 'ELF 32-bit.*ARM' \
  || fail "Chromium 不是 32 位 ARM ELF: $CHROMIUM_BIN"

LOADER_REL="$(find "$SYSROOT" -type f -name 'ld-linux-armhf.so.3' -printf '%P\n' -quit 2>/dev/null || true)"
[[ -n "$LOADER_REL" ]] || fail "ARMHF_SYSROOT 缺少 ld-linux-armhf.so.3"

rm -rf "$BUILD_ROOT" "$DIST_ROOT"
mkdir -p "$BUILD_ROOT" "$DIST_ROOT" "$PACKAGE_ROOT" "$PACKAGE_ROOT/data/profiles" \
  "$PACKAGE_ROOT/data/shots" "$PACKAGE_ROOT/data/logs"

PROBE_PROFILE="$BUILD_ROOT/chromium-probe-profile"
mkdir -p "$PROBE_PROFILE"
printf '==> 验证 ARMhf Chromium 可以启动\n'
timeout 45s "$CHROMIUM_BIN" \
  --headless --no-sandbox --disable-gpu --disable-dev-shm-usage \
  --user-data-dir="$PROBE_PROFILE" --version >/dev/null \
  || fail "ARMhf Chromium 启动验证失败"

printf '==> 创建 ARMv7l 构建环境\n'
python3 -m venv "$VENV"
"$VENV/bin/python" -m pip install --upgrade pip
"$VENV/bin/pip" install -r "$ROOT/requirements-armv7l.txt"

printf '==> PyInstaller 构建主程序\n'
"$VENV/bin/pyinstaller" --clean --noconfirm \
  --distpath "$PYI_DIST" \
  --workpath "$PYI_WORK" \
  "$ROOT/packaging/newapi_checkin_armv7l.spec"

PYI_APP="$PYI_DIST/newapi-checkin-armv7l"
[[ -d "$PYI_APP" ]] || fail "PyInstaller 未生成 onedir 目录: $PYI_APP"
cp -a "$PYI_APP/." "$PACKAGE_ROOT/"

printf '==> 收集 ARMhf Chromium 及资源\n'
mkdir -p "$PACKAGE_ROOT/browser"
cp -a "$CHROMIUM_ROOT/." "$PACKAGE_ROOT/browser/"
STAGED_CHROMIUM="$PACKAGE_ROOT/browser/$(basename "$CHROMIUM_BIN")"
if [[ "$(basename "$CHROMIUM_BIN")" != "chromium" ]]; then
  ln -s "$(basename "$CHROMIUM_BIN")" "$PACKAGE_ROOT/browser/chromium"
  STAGED_CHROMIUM="$PACKAGE_ROOT/browser/chromium"
fi
[[ -x "$STAGED_CHROMIUM" ]] || fail "发布包内没有可执行 Chromium"

map_sysroot_path() {
  local absolute="$1"
  if [[ "$SYSROOT" == "/" ]]; then
    printf '%s\n' "$absolute"
  else
    printf '%s%s\n' "${SYSROOT%/}" "$absolute"
  fi
}

copy_dependency() {
  local absolute="$1"
  [[ "$absolute" == /* ]] || return 0
  [[ "$absolute" == /linux-vdso* ]] && return 0
  local source
  source="$(map_sysroot_path "$absolute")"
  [[ -e "$source" ]] || fail "sysroot 缺少动态库: $absolute（查找: $source）"
  local target="$PACKAGE_ROOT/runtime/lib$absolute"
  mkdir -p "$(dirname "$target")"
  cp -a "$source" "$target"
}

collect_dependencies() {
  local elf="$1"
  local tree
  tree="$(LD_LIBRARY_PATH="$PACKAGE_ROOT/runtime/lib" lddtree -l "$elf" 2>&1)" \
    || fail "lddtree 解析失败: $elf\n$tree"
  if printf '%s\n' "$tree" | grep -q 'not found'; then
    fail "动态库未闭合: $elf\n$tree"
  fi
  while IFS= read -r absolute; do
    [[ -n "$absolute" ]] || continue
    [[ "$absolute" == /* ]] || continue
    copy_dependency "$absolute"
  done < <(printf '%s\n' "$tree" | awk '/^\// {print $1}')
}

is_elf() {
  file -L "$1" 2>/dev/null | grep -q 'ELF'
}

printf '==> 收集主程序、Python 扩展和 Chromium 动态库\n'
while IFS= read -r -d '' candidate; do
  if is_elf "$candidate"; then
    collect_dependencies "$candidate"
  fi
done < <(find "$PACKAGE_ROOT" -path "$PACKAGE_ROOT/runtime" -prune -o -type f -print0)

printf '==> 设置发布包 RPATH\n'
while IFS= read -r -d '' candidate; do
  if is_elf "$candidate"; then
    patchelf --set-rpath '$ORIGIN:$ORIGIN/runtime/lib:$ORIGIN/../runtime/lib:$ORIGIN/../../runtime/lib:$ORIGIN/../../../runtime/lib' "$candidate"
  fi
done < <(find "$PACKAGE_ROOT" -path "$PACKAGE_ROOT/runtime" -prune -o -type f -print0)

printf '==> 复制外置配置（不嵌入、不覆盖）\n'
if [[ -f "$ROOT/config.json" ]]; then
  cp -p "$ROOT/config.json" "$PACKAGE_ROOT/config.json"
else
  cp -p "$ROOT/config.example.json" "$PACKAGE_ROOT/config.json"
fi
[[ -f "$PACKAGE_ROOT/config.json" ]] || fail "发布包缺少 config.json"
if [[ -f "$ROOT/config.json" ]]; then
  cmp -s "$ROOT/config.json" "$PACKAGE_ROOT/config.json" || fail "config.json 复制后内容不一致"
fi

printf '==> 验证发布包架构和动态库闭合\n'
APP="$PACKAGE_ROOT/newapi-checkin-armv7l"
APP_FILE="$(file -L "$APP")"
printf '%s\n' "$APP_FILE" | grep -Eq 'ELF 32-bit.*ARM' \
  || fail "主程序不是 ARMv7l ELF: $APP_FILE"
STAGED_FILE="$(file -L "$STAGED_CHROMIUM")"
printf '%s\n' "$STAGED_FILE" | grep -Eq 'ELF 32-bit.*ARM' \
  || fail "发布包 Chromium 不是 ARMv7l ELF: $STAGED_FILE"
collect_dependencies "$APP"
collect_dependencies "$STAGED_CHROMIUM"

printf '==> 验证主程序启动\n'
"$APP" --version >/dev/null || fail "主程序 --version 启动失败"

printf '==> 生成发布归档\n'
tar -C "$DIST_ROOT" -czf "$ARCHIVE" "newapi-checkin-armv7l"
sha256sum "$ARCHIVE"
printf '==> 完成: %s\n' "$ARCHIVE"
