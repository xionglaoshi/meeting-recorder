#!/bin/bash
# meeting-server 启动脚本（手动按需启动；也兼容 launchd 托管）
#
# 解释器不写死：由 runtime.py 探测决定——
#   项目本地 .venv → 用户已有的 AI Agent 环境 → 系统 Python，谁依赖齐全用谁。
# 强制指定：MEETING_SERVER_PYTHON=/path/to/python bash start.sh
#
# 端口：$MEETING_PORT（默认 8789）
# 停止：bash stop.sh
set -euo pipefail

cd "$(dirname "$0")"

PORT="${MEETING_PORT:-8789}"
export MEETING_PORT="$PORT"

# ── 1. 解析解释器 ──
# runtime.py 只用标准库，任何 python3 都能跑它；它自己负责挑一个依赖齐全的解释器。
BOOT_PY="${MEETING_SERVER_PYTHON:-python3}"
if ! PY="$("$BOOT_PY" runtime.py 2>/dev/null)"; then
  echo "❌ 没能找到可用的 Python 解释器。" >&2
  echo "   安装依赖后重试：python3 -m pip install -r requirements.txt" >&2
  exit 1
fi
echo "→ 解释器：$PY"

# ── 2. 依赖体检：缺依赖时立刻给修复指引，而不是启动几秒后抛 ImportError ──
if ! "$PY" -c "import fastapi, uvicorn, dashscope, websocket, docx, openpyxl, pptx, fitz, markdown, yaml" 2>/dev/null; then
  echo "❌ 解释器 $PY 缺依赖。安装：" >&2
  echo "   \"$PY\" -m pip install -r requirements.txt" >&2
  echo "   （或 bash setup.sh 新建一个专用环境）" >&2
  exit 1
fi

# ── 3. 端口已被占用：先优雅停任务，再接管 ──
if lsof -i :"$PORT" -sTCP:LISTEN >/dev/null 2>&1; then
  STATE=$(curl -s --max-time 3 "http://127.0.0.1:$PORT/api/state" 2>/dev/null \
          | "$PY" -c "import json,sys; print(json.load(sys.stdin).get('state',''))" 2>/dev/null || true)
  case "$STATE" in
    recording|paused|processing)
      echo "检测到正在录音（state=$STATE），先优雅停任务…"
      # /api/stop 是异步的（立即返回），所以下面要等 state 回到 idle 再杀进程，
      # 否则会在纪要还没写盘时把服务杀掉（流水/清洗稿不丢，纪要会丢）。
      curl -s --max-time 30 -X POST "http://127.0.0.1:$PORT/api/stop" \
           -H 'Content-Type: application/json' -d '{}' >/dev/null 2>&1 || true
      WAIT_MAX="${MEETING_STOP_WAIT:-900}"
      i=0
      while [ "$i" -lt "$WAIT_MAX" ]; do
        sleep 3
        i=$((i + 3))
        s=$(curl -s --max-time 3 "http://127.0.0.1:$PORT/api/state" 2>/dev/null \
            | "$PY" -c "import json,sys; print(json.load(sys.stdin).get('state',''))" 2>/dev/null || echo "")
        [ "$s" = "idle" ] && break
        if [ $((i % 30)) -eq 0 ]; then
          echo "  仍在整理（state=${s:-未知}，已等 ${i}s）…"
        fi
      done
      if [ "$i" -ge "$WAIT_MAX" ]; then
        echo "等待超过 ${WAIT_MAX}s 仍未完成（流水/清洗稿已落盘，纪要可能未生成）" >&2
      else
        echo "任务已停止（产物已归档，用时约 ${i}s）"
      fi
      ;;
  esac
  # 杀掉旧进程：按端口 + 按脚本名兜底（服务端 atexit 钩子会清理 ffmpeg/swift 子进程）
  OLD_PIDS=$(lsof -ti :"$PORT" -sTCP:LISTEN 2>/dev/null || true)
  if [ -n "$OLD_PIDS" ]; then
    # shellcheck disable=SC2086
    kill $OLD_PIDS 2>/dev/null || true
  fi
  pkill -f "$(basename "$PWD")/server.py" 2>/dev/null || true
  sleep 1
fi

# ── 4. launchd 托管模式：让它自己把服务拉起来，别抢端口 ──
# 若把本服务注册成了 launchd 服务（KeepAlive），上面的 kill 会触发自动重启，
# 这时自己 exec 会和 launchd 抢端口，所以只等它起来。
#   指定 label：MEETING_LAUNCHD_LABEL=com.example.meeting-server
#   不指定则自动找 label 里带 meeting-server 的已加载服务。
LABEL="${MEETING_LAUNCHD_LABEL:-}"
if [ -z "$LABEL" ]; then
  LABEL=$(launchctl list 2>/dev/null | awk '$3 ~ /meeting-server/ {print $3; exit}' || true)
fi
if [ -n "$LABEL" ] && launchctl print "gui/$(id -u)/$LABEL" >/dev/null 2>&1; then
  echo "检测到 launchd 托管（$LABEL），等待 launchd 拉起服务…"
  for _ in $(seq 1 20); do
    if curl -s --max-time 1 "http://127.0.0.1:$PORT/api/state" >/dev/null 2>&1; then
      echo "服务已恢复 → http://127.0.0.1:$PORT/"
      exit 0
    fi
    sleep 1
  done
  echo "等待超时（>20s），检查：launchctl print gui/$(id -u)/$LABEL" >&2
  exit 1
fi

# ── 5. 非托管模式：前台运行 ──
echo "启动会议记录服务 → http://127.0.0.1:$PORT/"
echo "（Ctrl+C 停止；后台运行用：nohup bash start.sh > logs/start.log 2>&1 &）"

mkdir -p logs
exec "$PY" server.py "$@"
