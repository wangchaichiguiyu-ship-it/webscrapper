#!/usr/bin/env bash
# One-time local setup for BiliSonic (macOS/Linux).
set -euo pipefail
cd "$(dirname "$0")"

if ! command -v python3 >/dev/null 2>&1; then
  echo "需要 Python 3.10+，请先安装：https://www.python.org/downloads/"
  exit 1
fi
if ! command -v ffmpeg >/dev/null 2>&1; then
  echo "警告：未找到 ffmpeg。macOS 可运行：brew install ffmpeg"
fi

python3 -m venv .venv
.venv/bin/pip install --upgrade pip
# In mainland China the official PyPI index can be slow; set PIP_INDEX_URL to a
# mirror before running, e.g.:
#   PIP_INDEX_URL=https://pypi.tuna.tsinghua.edu.cn/simple ./setup.sh
.venv/bin/pip install -e '.[dev]'

echo
echo "安装完成。启动：./run.sh"
echo "体检：./run.sh doctor"
