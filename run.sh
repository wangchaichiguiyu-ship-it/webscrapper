#!/usr/bin/env bash
# Start BiliSonic from the repository root using the project virtualenv.
set -euo pipefail
cd "$(dirname "$0")"

if [[ ! -x .venv/bin/python ]]; then
  echo "没有找到 .venv。请先运行："
  echo "  python3 -m venv .venv && .venv/bin/pip install -e '.[dev]'"
  echo "或运行 ./setup.sh"
  exit 1
fi

exec .venv/bin/python -m bili_audio "$@"
