#!/usr/bin/env bash
# scripts/build-config-platform.sh
# 构建「单二进制 = 前端 + 后端」的配置管理平台。
#
# 流程：npm 构建前端 -> 产物拷入 server/embed_dist -> go build 嵌入 -> 单文件
# 用法：
#   bash scripts/build-config-platform.sh            # 构建 linux/amd64（VPS 部署用）
#   GOOS=windows GOARCH=amd64 bash scripts/build-config-platform.sh   # 构建 Windows 版
#   GOOS=linux GOARCH=arm64 bash scripts/build-config-platform.sh     # ARM VPS
set -euo pipefail

ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
GOOS="${GOOS:-linux}"
GOARCH="${GOARCH:-amd64}"
OUT="${OUT:-newapi-config-server}"

echo "==> [1/4] 构建前端（web）"
cd "$ROOT/web"
npm install --no-audit --no-fund
npm run build

echo "==> [2/4] 拷贝产物到 server/embed_dist（供 go:embed 嵌入）"
mkdir -p "$ROOT/server/embed_dist"
find "$ROOT/server/embed_dist" -mindepth 1 ! -name '.gitkeep' -exec rm -rf {} + 2>/dev/null || true
cp -r "$ROOT/web/dist/." "$ROOT/server/embed_dist/"

echo "==> [3/4] 构建后端并嵌入前端（${GOOS}/${GOARCH}）"
cd "$ROOT/server"
CGO_ENABLED=0 GOOS="$GOOS" GOARCH="$GOARCH" go build -trimpath -o "$OUT" .

echo "==> [4/4] 完成"
ls -lh "$ROOT/server/$OUT"
echo
echo "部署时只需上传这一个文件: $ROOT/server/$OUT"
echo "（SQLite 数据库会在首次运行时自动创建于 ./data/config.db）"
