#!/usr/bin/env bash
# VPS 一键安装（Debian / Ubuntu）。需要 root 或 sudo。
#   bash scripts/install_vps.sh
set -euo pipefail

cd "$(dirname "$0")/.."
ROOT="$(pwd)"
echo "==> 项目目录: $ROOT"

SUDO=""
if [ "$(id -u)" -ne 0 ]; then
  SUDO="sudo"
fi

echo "==> 安装系统依赖（Xvfb、浏览器运行库、中文字体）"
$SUDO apt-get update -y
$SUDO apt-get install -y \
  python3 python3-venv python3-pip \
  xvfb \
  libgtk-3-0 libasound2t64 libdbus-glib-1-2 libx11-xcb1 libxcb1 \
  libxcomposite1 libxdamage1 libxfixes3 libxrandr2 libgbm1 libnss3 \
  fonts-liberation fonts-noto-cjk ca-certificates \
  || $SUDO apt-get install -y libasound2   # 旧发行版包名回退

echo "==> 创建 venv 并安装 Python 依赖"
python3 -m venv .venv
./.venv/bin/python -m pip install --upgrade pip
./.venv/bin/python -m pip install -r requirements.txt

echo "==> 下载 Camoufox 浏览器与 GeoIP 库（约几百 MB，耐心等）"
./.venv/bin/python -m camoufox fetch

echo "==> 可选：安装 Patchright 的 Chromium 作为备选驱动"
./.venv/bin/patchright install chromium || echo "    (跳过，Camoufox 已可用)"

if [ ! -f config.json ]; then
  cp config.example.json config.json
  echo "==> 已生成 config.json，请填写 accounts 与 ai 段落"
fi

mkdir -p data/profiles data/shots data/logs

echo
echo "==> 安装完成。接下来："
echo "    1) 编辑 config.json，填 accounts[].url / cookie 和 ai.base_url / api_key / model"
echo "    2) 自检:      ./.venv/bin/python scripts/doctor.py"
echo "    3) 连通性:    ./.venv/bin/python main.py --dry-run -v"
echo "    4) 正式签到:  ./.venv/bin/python main.py"
echo "    5) 定时任务:  crontab -e 追加"
echo "       0 9 * * * cd $ROOT && ./.venv/bin/python main.py >> data/logs/cron.log 2>&1"
