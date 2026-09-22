#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""macOS Speech 兜底转写 — qwen（百炼）不可用时的离线/在线降级

qwen 是主力；本脚本是 macOS Speech（SFSpeechRecognizer）兜底：
免费、不依赖 DASHSCOPE_API_KEY、断网时若已下载语言模型可纯本地识别。

用法:
  python3 macos_speech.py <音频.pcm|.wav|.m4a|.mp3> -o <流水.md>
        [--start-time HH:MM:SS] [--on-device] [--locale zh-CN]

输出: 与 transcribe_stream / transcribe 同构的流水格式（HH:MM:SS-HH:MM:SS 文字），
      可直接进 proofread → verify → summarize 流水线。
首次运行会触发 macOS「语音识别」授权弹窗；未授权时按提示去系统设置开启。
"""
import argparse
import json
import os
import subprocess
import sys
import tempfile

SCRIPTS = os.path.dirname(os.path.abspath(__file__))
SWIFT_SRC = os.path.join(SCRIPTS, "asr_macos.swift")
SWIFT_BIN = os.path.join(SCRIPTS, "asr_macos_bin")
INFO_PLIST = os.path.join(SCRIPTS, "Info.plist")


def ensure_binary():
    """编译 asr_macos_bin（源码或 Info.plist 更新则重编）；失败给出明确错误"""
    if os.path.isfile(SWIFT_BIN):
        bin_m = os.path.getmtime(SWIFT_BIN)
        if os.path.getmtime(SWIFT_SRC) <= bin_m and os.path.getmtime(INFO_PLIST) <= bin_m:
            return True
    cmd = ["swiftc", "-O", "-swift-version", "5",
           "-Xlinker", "-sectcreate", "-Xlinker", "__TEXT",
           "-Xlinker", "__info_plist", "-Xlinker", INFO_PLIST,
           SWIFT_SRC, "-o", SWIFT_BIN]
    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode != 0:
        print(f"❌ 编译 macOS 语音识别工具失败：\n{(r.stderr or '')[-800:]}", file=sys.stderr)
        return False
    return True


def to_wav(audio):
    """任意输入 → 16k mono wav（SFSpeechURLRecognitionRequest 需带容器；裸 pcm 无头需显式格式）"""
    tmp = tempfile.NamedTemporaryFile(suffix=".wav", delete=False)
    tmp.close()
    if audio.lower().endswith(".pcm"):
        cmd = ["ffmpeg", "-y", "-loglevel", "error", "-f", "s16le", "-ar", "16000", "-ac", "1",
               "-i", audio, "-ar", "16000", "-ac", "1", tmp.name]
    else:
        cmd = ["ffmpeg", "-y", "-loglevel", "error", "-i", audio, "-ar", "16000", "-ac", "1", tmp.name]
    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode != 0:
        try:
            os.unlink(tmp.name)
        except Exception:
            pass
        return None
    return tmp.name


def hhmmss(sec):
    sec = max(0, int(sec))
    h, rem = divmod(sec, 3600)
    m, s = divmod(rem, 60)
    return f"{h:02d}:{m:02d}:{s:02d}"


def main():
    ap = argparse.ArgumentParser(description="macOS Speech 兜底转写（qwen 不可用时离线识别）")
    ap.add_argument("audio", help="音频文件（pcm/wav/m4a/mp3）")
    ap.add_argument("-o", "--output", default=None, help="流水输出（HH:MM:SS-HH:MM:SS 文字）")
    ap.add_argument("--start-time", default=None, help="录音开始时间 HH:MM:SS（流水时间戳基准，默认 00:00:00）")
    ap.add_argument("--on-device", action="store_true", help="强制本地识别（不联网）")
    ap.add_argument("--locale", default="zh-CN")
    args = ap.parse_args()

    if not os.path.isfile(args.audio):
        print(f"❌ 音频文件不存在：{args.audio}", file=sys.stderr)
        sys.exit(2)
    if not ensure_binary():
        sys.exit(3)

    wav = to_wav(args.audio)
    if not wav:
        print(f"❌ 音频转 wav 失败：{args.audio}", file=sys.stderr)
        sys.exit(4)
    try:
        cmd = [SWIFT_BIN, wav, "--locale", args.locale]
        if args.on_device:
            cmd.append("--on-device")
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=1800)
    finally:
        try:
            os.unlink(wav)
        except Exception:
            pass
    if r.returncode != 0:
        err = (r.stderr or '').strip()[-400:]
        # 区分"静音录音"（预期行为，给出清晰指引）与"真失败"（权限/服务异常）
        if r.returncode == 3 or "No speech" in err or "结果为空" in err or "未检测到" in err:
            print(f"❌ macOS 语音识别未检测到语音（音频中可能无人说话或声音过小/麦克风未拾音）：{err}", file=sys.stderr)
        else:
            print(f"❌ macOS 语音识别失败（exit {r.returncode}）：{err}", file=sys.stderr)
        sys.exit(5)

    base = 0
    if args.start_time:
        try:
            hh, mm, ss = (int(x) for x in args.start_time.split(":"))
            base = hh * 3600 + mm * 60 + ss
        except Exception:
            base = 0

    lines = []
    for ln in r.stdout.splitlines():
        ln = ln.strip()
        if not ln:
            continue
        try:
            obj = json.loads(ln)
        except Exception:
            continue
        ts = int(obj.get("ts") or 0)
        end = int(obj.get("end") or (ts + 10))
        text = (obj.get("text") or "").strip()
        if not text:
            continue
        lines.append(f"{hhmmss(base + ts)}-{hhmmss(base + end)}  {text}")

    if not lines:
        print("❌ macOS 语音识别结果为空（音频可能无声或无人声）", file=sys.stderr)
        sys.exit(6)

    out = args.output or (os.path.splitext(args.audio)[0] + "-流水.md")
    os.makedirs(os.path.dirname(os.path.abspath(out)) or ".", exist_ok=True)
    with open(out, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")
    print(f"✅ macOS Speech 兜底转写完成：{len(lines)} 句 → {out}", file=sys.stderr)


if __name__ == "__main__":
    main()
