#!/bin/bash
# meeting-server 常驻入口（给 launchd / 前台常驻用）
#
# 和 start.sh 的区别：这里**不做"等 launchd 拉起"的判断**，直接 exec 到服务进程。
#   · start.sh  是给人用的「重启」脚本：先优雅停任务 → 杀旧进程 → 交给 launchd 拉起
#   · serve.sh  是给 launchd 用的「被拉起」脚本：解析解释器后直接 exec
# 如果 plist 指向 start.sh，start.sh 会进入"等 launchd 拉起"分支，而它自己就是被
# launchd 拉起的那个进程 —— 自己等自己，20 秒超时后退出，KeepAlive 再拉起，死循环。
set -euo pipefail
cd "$(dirname "$0")"

PY="$(python3 runtime.py)"
export MEETING_PORT="${MEETING_PORT:-8789}"
exec "$PY" server.py "$@"
