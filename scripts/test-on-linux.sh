#!/usr/bin/env bash
# scripts/test-on-linux.sh
# 在 Linux（WSL 或真机）上按 CI 的方式跑一遍 pytest。
#
# 为什么需要它：开发机是 Windows，但 CI 跑在 ubuntu-latest。有些缺陷只在 Linux 上露头，
# 本地全绿照样能推出一个红的 CI —— 已经踩过一次：VisionClient 用 threading.get_ident()
# 做 session 字典的 key，而线程 id 在线程退出后会被系统复用（Linux 上尤其快），于是
# 「一线程一 session」在 Linux 上退化成几个线程共用一个，两个并发测试直接失败。
#
# 与 .github/workflows/test.yml 的对齐点：
#   - 依赖用 requirements-ci.txt（不含 PySide6，验证 importorskip 保护真的生效）
#   - pytest 版本取自 requirements.txt 的锁定值，不装最新版
# 有意的差异：
#   - camoufox / patchright 不装。测试里对它们是函数内延迟导入，剔掉能省几百 MB
#     下载（checkin.yml 的 plan job 也是这么做的）
#   - Python 用发行版自带的版本，可能与 CI 指定的不完全一致
#
# 不污染 Windows 工作区：venv 建在 /tmp、禁止写 .pyc、关掉 pytest 缓存目录。
#
# 用法（在项目根目录）：
#   wsl -- bash scripts/test-on-linux.sh              # 跑全量
#   wsl -- bash scripts/test-on-linux.sh tests/test_ai_cooldown.py -x
#   wsl -- bash scripts/test-on-linux.sh --rebuild    # 依赖变了就重建 venv
set -uo pipefail

VENV=${LINUX_TEST_VENV:-/tmp/newapi-ci-venv}
PROJECT=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
cd "$PROJECT" || { echo "进不去项目目录: $PROJECT"; exit 1; }

if [ "${1:-}" = "--rebuild" ]; then
  echo "== 删掉旧 venv：$VENV =="
  rm -rf "$VENV"
  shift
fi

if [ ! -x "$VENV/bin/python" ]; then
  echo "== 建 venv：$VENV =="
  python3 -m venv "$VENV" || exit 1
  "$VENV/bin/pip" install -q --upgrade pip

  echo "== 装依赖（剔掉 camoufox / patchright）=="
  grep -vE '^(camoufox|patchright)' requirements-ci.txt > "$VENV/req-ci.txt"
  "$VENV/bin/pip" install -q -r "$VENV/req-ci.txt" || exit 1

  PYTEST_PIN=$(grep -E '^pytest==' requirements.txt)
  if [ -z "$PYTEST_PIN" ]; then
    echo "requirements.txt 里找不到锁定的 pytest 版本"; exit 1
  fi
  echo "== 装 $PYTEST_PIN（与 CI 同版本）=="
  "$VENV/bin/pip" install -q "$PYTEST_PIN" || exit 1
fi

echo "== 环境 =="
"$VENV/bin/python" -V
"$VENV/bin/python" -m pytest --version 2>&1 | head -1
"$VENV/bin/python" -c 'import importlib.util as u; print("PySide6:", "装了（CI 上没有）" if u.find_spec("PySide6") else "未装（与 CI 一致）")'

echo "== 跑 pytest =="
export PYTHONDONTWRITEBYTECODE=1      # 别把 Linux 的 .pyc 混进 Windows 工作区
export PYTHONIOENCODING=utf-8
"$VENV/bin/python" -m pytest "${@:-tests/}" -q -p no:cacheprovider
code=$?
echo "== 退出码 $code =="
exit $code
