#!/bin/bash
# meeting-server 一键准备运行环境
#
# 做的事：
#   1. 先用 runtime.py 找找本机有没有「依赖已经齐全」的解释器——有就直接用，不重复装
#   2. 没有就建一个项目本地 .venv（优先 uv，回退 python3 -m venv）并装 requirements.txt
#
# 用法：
#   bash setup.sh            # 缺环境才建
#   bash setup.sh --force    # 强制重建 .venv
set -euo pipefail
cd "$(dirname "$0")"

FORCE=0
[ "${1:-}" = "--force" ] && FORCE=1

echo "── 1/3 探测已有解释器 ──"
if [ "$FORCE" = 0 ]; then
  if PY="$(python3 runtime.py 2>/dev/null)" && \
     "$PY" -c "import fastapi, uvicorn, dashscope, websocket, docx, openpyxl, pptx, fitz, markdown, yaml" 2>/dev/null; then
    echo "✅ 已有可用环境，无需安装：$PY"
    echo "   （要强制新建本地 .venv，请跑：bash setup.sh --force）"
    exit 0
  fi
  echo "→ 本机没有依赖齐全的解释器，开始新建本地环境"
fi

echo "── 2/3 创建 .venv ──"
if [ -d .venv ] && [ "$FORCE" = 1 ]; then
  echo "→ --force：移除旧的 .venv"
  rm -rf .venv
fi

if command -v uv >/dev/null 2>&1; then
  echo "→ 使用 uv 创建（快）"
  uv venv --python 3.11 .venv 2>/dev/null || uv venv .venv
else
  echo "→ 使用 python3 -m venv"
  python3 -m venv .venv
fi
PY="$PWD/.venv/bin/python"
[ -x "$PY" ] || { echo "❌ .venv 创建失败" >&2; exit 1; }

echo "── 3/3 安装依赖 ──"
if command -v uv >/dev/null 2>&1; then
  VIRTUAL_ENV="$PWD/.venv" uv pip install -r requirements.txt
else
  "$PY" -m pip install --upgrade pip
  "$PY" -m pip install -r requirements.txt
fi

echo
echo "✅ 完成。启动：bash start.sh"
echo "   自检：\"$PY\" runtime.py --doctor"
