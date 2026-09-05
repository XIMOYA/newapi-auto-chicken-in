#!/usr/bin/env bash
# ============================================================================ #
# scripts/install-xray.sh
# 在 CI runner 上装好 xray 可执行文件（VLESS 节点靠它转成本地 socks5 入口）
#
# 用法： XRAY_VERSION=v26.3.27 XRAY_ASSET=Xray-linux-64.zip scripts/install-xray.sh
#
# 可覆盖的环境变量：
#   XRAY_VERSION  必填，release tag
#   XRAY_ASSET    资产名，默认 Xray-linux-64.zip
#   XRAY_DEST_DIR 安装目录，默认 bin
#   XRAY_URL      直接指定下载地址（走内网镜像、或本机拿本地 zip 自测时用）
#
# 刻意的设计：
#   - 版本由调用方 pin，不追 latest —— latest 会某天静默换掉二进制，出问题无从对比
#   - 装到仓库内 bin/xray，正好是 newapi_checkin/xray.py:find_binary 的查找位置之一
#   - 压缩包内的文件名不写死：解压后没找到就把清单打出来再退出，不猜也不静默跳过
#   - 已经装好就直接退出，让 actions/cache 命中时这一步是空转
# ============================================================================ #
set -euo pipefail

VERSION="${XRAY_VERSION:?需要 XRAY_VERSION，例如 v26.3.27}"
ASSET="${XRAY_ASSET:-Xray-linux-64.zip}"
DEST_DIR="${XRAY_DEST_DIR:-bin}"
DEST="${DEST_DIR}/xray"

if [ -x "$DEST" ]; then
  echo "xray 已存在：$DEST（缓存命中，跳过下载）"
  "$DEST" version 2>/dev/null || "$DEST" -version 2>/dev/null || true
  exit 0
fi

URL="${XRAY_URL:-https://github.com/XTLS/Xray-core/releases/download/${VERSION}/${ASSET}}"
TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT

echo "下载 xray ${VERSION}：${URL}"
# --fail 让 404 直接失败而不是把错误页当 zip 存下来（那种失败会拖到 unzip 才暴露）
curl --fail --location --silent --show-error --retry 3 --retry-delay 2 \
     --output "${TMP}/xray.zip" "$URL"

unzip -o -q "${TMP}/xray.zip" -d "${TMP}/unpack"

# 压缩包布局不写死：先找同名可执行文件，找不到就报清单。官方包目前是平铺的，
# 但这里不赌它以后不变 —— 猜错的后果是「装了个空壳，跑起来才 404」
BIN="$(find "${TMP}/unpack" -type f -name 'xray' -print -quit)"
if [ -z "$BIN" ]; then
  echo "::error::压缩包里找不到 xray 可执行文件。实际内容如下："
  find "${TMP}/unpack" -type f -printf '  %P (%s bytes)\n'
  exit 1
fi

mkdir -p "$DEST_DIR"
install -m 755 "$BIN" "$DEST"

# geoip/geosite 一并带上（存在才拷）。当前配置只用直连规则不需要它们，
# 但缺了它们某些 xray 版本启动会 WARN，留着省得日志里一堆噪音
for data in geoip.dat geosite.dat; do
  found="$(find "${TMP}/unpack" -type f -name "$data" -print -quit)"
  if [ -n "$found" ]; then
    install -m 644 "$found" "${DEST_DIR}/${data}"
  fi
done

echo "已安装：$DEST"
# 版本子命令两种写法都试：不同大版本有过差异，这里只是要一个「能跑起来」的证据
"$DEST" version || "$DEST" -version
