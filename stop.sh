#!/bin/bash
# 停止 meeting-recorder（优雅：先停任务并等整理跑完，再结束进程）
#
# 2026-09-13 起 /api/stop 是异步的：POST 立即返回，纪要生成在后台线程跑。
# 所以这里必须轮询等到 state 回到 idle 再杀进程，否则会在纪要还没写盘时把服务杀掉
# （流水/清洗稿已落盘不丢，丢的是刚生成的纪要）。
set -euo pipefail
cd "$(dirname "$0")"

PORT="${MEETING_PORT:-8789}"
BOOT_PY="${MEETING_SERVER_PYTHON:-python3}"

state_of() {
  curl -s --max-time 3 "http://127.0.0.1:$PORT/api/state" 2>/dev/null \
    | "$BOOT_PY" -c "import json,sys; print(json.load(sys.stdin).get('state',''))" 2>/dev/null \
    || echo ""
}

if ! lsof -i :"$PORT" -sTCP:LISTEN >/dev/null 2>&1; then
  echo "服务未在运行（端口 $PORT 空闲）"
  exit 0
fi

STATE=$(state_of)
NEED_WAIT=0

if [ "$STATE" = "recording" ] || [ "$STATE" = "paused" ]; then
  echo "正在录音（state=$STATE），先优雅停任务…"
  curl -s --max-time 30 -X POST "http://127.0.0.1:$PORT/api/stop" \
       -H 'Content-Type: application/json' -d '{}' >/dev/null 2>&1 || true
  NEED_WAIT=1
elif [ "$STATE" = "processing" ]; then
  echo "已有整理任务在跑，等它完成…"
  NEED_WAIT=1
fi

if [ "$NEED_WAIT" = "1" ]; then
  WAIT_MAX="${MEETING_STOP_WAIT:-900}"
  i=0
  while [ "$i" -lt "$WAIT_MAX" ]; do
    sleep 3
    i=$((i + 3))
    s=$(state_of)
    if [ "$s" = "idle" ]; then
      echo "整理完成（产物已归档），用时约 ${i}s"
      break
    fi
    if [ $((i % 30)) -eq 0 ]; then
      echo "  仍在整理（state=${s:-未知}，已等 ${i}s）…"
    fi
  done
  if [ "$i" -ge "$WAIT_MAX" ]; then
    echo "等待超过 ${WAIT_MAX}s 仍未完成（流水/清洗稿已落盘，纪要可能未生成）" >&2
    echo "  可先看 logs/ 再决定是否强停" >&2
  fi
fi

PIDS=$(lsof -ti :"$PORT" -sTCP:LISTEN 2>/dev/null || true)
if [ -n "$PIDS" ]; then
  kill $PIDS 2>/dev/null || true
  echo "已停止：$PIDS"
fi
