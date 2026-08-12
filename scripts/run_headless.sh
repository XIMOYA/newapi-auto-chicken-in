#!/usr/bin/env bash
# Debian/ Linux VPS 无图形运行入口。
set -euo pipefail

ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
if [[ -n "${PYTHON_BIN:-}" ]]; then
    PYTHON="${PYTHON_BIN}"
elif [[ -x "${ROOT}/.venv/bin/python" ]]; then
    PYTHON="${ROOT}/.venv/bin/python"
else
    PYTHON="python3"
fi

cd "${ROOT}"
exec "${PYTHON}" main.py --headless "$@"
