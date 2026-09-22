#!/usr/bin/env bash
# ============================================================================
#  meeting-recorder · 安装脚本
#
#  装依赖、把模板文件铺出来（.env / vocab_seed.py / hr_sources.py）、建好目录。
#  不改动你自己的数据。想先看它要做什么：bash install.sh --dry-run
# ============================================================================
set -euo pipefail
cd "$(dirname "$0")"
DRY=0
[ "${1:-}" = "--dry-run" ] && DRY=1
run() { if [ "$DRY" = "1" ]; then echo "  [dry-run] $*"; else eval "$@"; fi; }

echo "📁 技能目录：$PWD"
echo

echo "── 1/4 检查 Python ──"
PY="${MEETING_SERVER_PYTHON:-python3}"
if command -v uv >/dev/null 2>&1; then echo "  ✓ 发现 uv（安装会快一些）"; fi
"$PY" -c 'import sys; sys.exit(0 if sys.version_info >= (3,9) else 1)' 2>/dev/null \
  && echo "  ✓ $("$PY" -V 2>&1)" \
  || echo "  ⚠ 没找到可用的 python3（≥3.9）；装好再来，或用 MEETING_SERVER_PYTHON 指定"

echo "── 2/4 建目录 ──"
run "mkdir -p roster records logs"
echo "  ✓ roster/（放你的花名册）records/（会议记录落这里）logs/"

echo "── 3/4 铺模板文件（已存在就跳过，绝不覆盖）──"
for pair in ".env.example:.env" "vocab_seed.example.py:vocab_seed.py" "hr_sources.example.py:hr_sources.py"; do
  src="${pair%%:*}"; dst="${pair##*:}"
  if [ -e "$dst" ]; then echo "  = $dst 已存在，跳过"
  else run "cp '$src' '$dst'"; echo "  + $dst（照 $src 里的说明填）"; fi
done

echo "── 4/4 装 Python 依赖 ──"
if [ "$DRY" = "1" ]; then echo "  [dry-run] bash setup.sh"
else bash setup.sh; fi

echo
echo "✅ 准备完成。接下来："
echo "   1) 编辑 .env 填你的凭证（阿里百炼 DASHSCOPE_API_KEY 等；也可换自己的模型，见 README）"
echo "   2) 编辑 vocab_seed.py / hr_sources.py 填你自己的人名、专有词、花名册"
echo "   3) bash start.sh          启动服务（默认 http://127.0.0.1:8789）"
echo "   4) \"\$(pwd)/.venv/bin/python\" runtime.py --doctor    自检"
