#!/usr/bin/env python3
"""腾讯会议集成后端能力：用户一句话 → 找到会议 → 拉转写 → 正式纪要 → 同步双库

用法：
  python3 tx_meeting.py --keyword 产品评审              # 按关键词找最近会议
  python3 tx_meeting.py --code 259932425             # 按会议号
  python3 tx_meeting.py --days 30 --keyword 评审     # 指定时间范围+关键词
  python3 tx_meeting.py --list                        # 列出最近会议记录

流程（全自动）：
  search_records → 找到会议 → 拉完整转写 → 存原始稿(Downloads) →
  调 /api/import 全链路优化（L2校对+实体核验+质量标准纪要）→
  自动同步 WIKI meetings/ + Obsidian Inbox/
"""
import argparse
import json
import os
import re
import subprocess
import sys
import urllib.request
from datetime import datetime, timedelta

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from runtime import ensure_runtime
ensure_runtime()

import settings

# ── 配置 ─────────────────────────────────────────────
# 腾讯会议工具目录（可选集成）：历史上指 Agent 的技能目录，现改为可配置 + 自动探测。
# 未配置时本脚本给出明确提示并退出，不影响 meeting-server 自身运行。
SKILL_DIR = settings.tencent_cli_dir()
IMPORT_URL = "http://127.0.0.1:8789/api/import"
STATE_URL = "http://127.0.0.1:8789/api/state"
DOWNLOADS = os.path.expanduser(os.environ.get("MEETING_DOWNLOADS_DIR") or "~/Downloads")
CLIENT_INFO = {
    "os": "macos-26",
    "agent": os.environ.get("MEETING_CLIENT_AGENT", "meeting-server"),
    "model": os.environ.get("MEETING_CLIENT_MODEL", "deepseek-flash"),
}


def load_token():
    """腾讯会议令牌：环境变量优先，其次 settings 的 .env 链。"""
    return settings.get("TENCENT_MEETING_TOKEN", "") or ""


def call_tool(name, arguments):
    """调用腾讯会议 MCP 工具（CLI 方式）"""
    token = load_token()
    if not token:
        raise RuntimeError("TENCENT_MEETING_TOKEN 未配置")
    if not SKILL_DIR or not os.path.isdir(str(SKILL_DIR)):
        raise RuntimeError(
            "未找到腾讯会议工具目录（tencent_meeting.py）。\n"
            "  这是可选集成：本机现行入口是命令行 `tmeet`（/opt/homebrew/bin/tmeet）。\n"
            "  如需沿用 MCP 脚本方式，请用 $MEETING_TENCENT_CLI_DIR 指向它的 scripts 目录。")
    args = {**arguments, "_client_info": CLIENT_INFO}
    payload = json.dumps({"name": name, "arguments": args}, ensure_ascii=False)
    env = {**os.environ, "TENCENT_MEETING_TOKEN": token}
    r = subprocess.run(
        [sys.executable, "tencent_meeting.py", "tools/call", payload],
        cwd=str(SKILL_DIR), capture_output=True, text=True, env=env, timeout=120)
    if r.returncode != 0:
        raise RuntimeError(f"CLI 失败: {r.stderr[:200]}")
    try:
        d = json.loads(r.stdout)
    except json.JSONDecodeError:
        # CLI 直接输出错误文本
        raise RuntimeError(f"工具返回异常: {r.stdout[:200]}")
    if "[错误]" in r.stdout and "data" not in d:
        raise RuntimeError(f"腾讯会议错误: {r.stdout[:200]}")
    body = d.get("data", {}).get("body") or d.get("body", "")
    if isinstance(body, str):
        try:
            body = json.loads(body)
        except json.JSONDecodeError:
            pass
    return body if isinstance(body, dict) else {}


def fmt_time(ms):
    s = ms // 1000
    return f"{s // 60:02d}:{s % 60:02d}"


def parse_transcript(body):
    """转写 body → 带时间戳/说话人文本"""
    paras = body.get("minutes", {}).get("paragraphs", [])
    lines = []
    for p in paras:
        sp = p.get("speaker")
        spk = ""
        if isinstance(sp, dict):
            spk = (sp.get("nick_name") or sp.get("name") or "").strip()
        elif isinstance(sp, str):
            spk = sp.strip()
        for s in p.get("sentences", []):
            text = "".join(w.get("text", "") for w in s.get("words", []))
            if text.strip():
                ts = f"{fmt_time(s.get('start_time', 0))}-{fmt_time(s.get('end_time', 0))}"
                lines.append(f"[{ts}] {spk}：{text.strip()}" if spk else f"[{ts}] {text.strip()}")
    return "\n".join(lines)


def find_meeting(keyword="", code="", days=60, file_type="all"):
    """找会议 → 返回记录 dict 或 None"""
    now = datetime.now()
    start = (now - timedelta(days=days)).strftime("%Y-%m-%dT%H:%M:%S+08:00")
    end = now.strftime("%Y-%m-%dT%H:%M:%S+08:00")
    args = {"file_type": file_type, "from": start, "to": end}
    if keyword:
        args["q"] = keyword
    if code:
        args["meeting_code"] = code
    body = call_tool("search_records", args)
    recs = body.get("records", [])
    if not recs:
        return None
    # 关键词优先精确匹配 subject
    if keyword:
        kw = keyword.lower()
        exact = [r for r in recs if kw in (r.get("subject") or "").lower()]
        if exact:
            return exact[0]
    return recs[0]


def fetch_transcript(rec):
    """拉完整转写 → 原始稿文本"""
    meeting_id = rec.get("meeting_id")
    file_id = rec.get("record_file_id")
    body = call_tool("get_transcripts_details", {
        "meeting_id": meeting_id, "record_file_id": file_id})
    return parse_transcript(body)


def fetch_smart_minutes(rec):
    """拉官方智能纪要（对比用）"""
    meeting_id = rec.get("meeting_id")
    file_id = rec.get("record_file_id")
    body = call_tool("get_smart_minutes", {
        "meeting_id": meeting_id, "record_file_id": file_id})
    mm = body.get("meeting_minute", {})
    if isinstance(mm, dict):
        return mm.get("minute", "")
    return str(mm)


def ensure_server():
    """确认 8789 后端在跑。**不自动启动**（2026-09-17 用户定：服务平时静默，由用户手动按需拉起）。"""
    try:
        urllib.request.urlopen(STATE_URL, timeout=3)
        return
    except Exception:
        pass
    start_sh = os.path.join(os.path.dirname(os.path.abspath(__file__)), "start.sh")
    raise RuntimeError(
        "会议纪要服务未运行（127.0.0.1:8789）。\n"
        "  该服务按需手动拉起，请先在终端执行：\n"
        f"      bash {start_sh}\n"
        "  启动后再重跑本脚本。")


def import_transcript(text, title, attendees="", location="腾讯会议（线上）"):
    """调 /api/import 全链路优化"""
    payload = json.dumps({
        "text": text, "title": title, "attendees": attendees,
        "location": location}, ensure_ascii=False).encode()
    req = urllib.request.Request(IMPORT_URL, data=payload,
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=300) as r:
            return json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        body = e.read().decode()
        try:
            return json.loads(body)
        except Exception:
            return {"ok": False, "detail": body[:200]}


def main():
    ap = argparse.ArgumentParser(description="腾讯会议 → 正式纪要（全自动）")
    ap.add_argument("--keyword", default="", help="关键词（会议主题）")
    ap.add_argument("--code", default="", help="会议号（9位）")
    ap.add_argument("--days", type=int, default=60, help="回溯天数")
    ap.add_argument("--list", action="store_true", help="列出最近会议")
    ap.add_argument("--title", default="", help="纪要标题（覆盖）")
    ap.add_argument("--attendees", default="", help="参会人员")
    args = ap.parse_args()

    if args.list:
        now = datetime.now()
        body = call_tool("search_records", {
            "file_type": "all",
            "from": (now - timedelta(days=args.days)).strftime("%Y-%m-%dT%H:%M:%S+08:00"),
            "to": now.strftime("%Y-%m-%dT%H:%M:%S+08:00")})
        recs = body.get("records", [])
        print(f"最近 {args.days} 天会议记录: {len(recs)} 条")
        for r in recs:
            print(f"  {r.get('record_start_time','')[:10]} | {r.get('subject','')} "
                  f"| 会议号:{r.get('meeting_code','')} | 转写:{r.get('has_transcript_content')}")
        return

    print("① 找会议...")
    rec = find_meeting(args.keyword, args.code, args.days)
    if not rec:
        print(f"✗ 未找到会议（关键词: {args.keyword or '无'}，回溯 {args.days} 天）")
        sys.exit(1)
    subject = rec.get("subject", "会议")
    print(f"✓ 找到: {subject} | 会议号 {rec.get('meeting_code')} "
          f"| {rec.get('record_start_time','')[:16]}")
    if not rec.get("has_transcript_content"):
        print("✗ 该会议没有转写内容（未开云录制/转写）")
        sys.exit(1)

    print("② 拉取完整转写...")
    text = fetch_transcript(rec)
    if len(text) < 50:
        print(f"✗ 转写内容过少（{len(text)} 字符）")
        sys.exit(1)
    print(f"✓ 转写 {len(text)} 字符")

    # 存原始稿（未优化）
    raw_path = os.path.join(DOWNLOADS, f"{subject}-腾讯会议原始转写.txt")
    with open(raw_path, "w", encoding="utf-8") as f:
        f.write(f"# {subject}（腾讯会议原始转写）\n# 会议号: {rec.get('meeting_code')} "
                f"| {rec.get('record_start_time','')[:16]} | 创建: {rec.get('creator_name','')}\n\n{text}\n")
    print(f"✓ 原始稿已存: {raw_path}")

    print("③ 全链路优化（L2校对+实体核验+质量标准纪要）...")
    ensure_server()
    title = args.title or subject
    # 会议日期从腾讯会议元数据提取，注入标题（纪要标题三要素需要真实日期）
    start_iso = (rec.get("record_start_time") or "")[:10]
    if start_iso:
        try:
            md = datetime.strptime(start_iso, "%Y-%m-%d")
            title = f"{md.year}年{md.month}月{md.day}日{title}"
        except ValueError:
            pass
    result = import_transcript(text, title, args.attendees)
    if not result.get("ok"):
        print(f"✗ 优化失败: {result.get('detail', result)}")
        sys.exit(1)
    task_dir = result.get("task_dir", "")
    print(f"✓ 正式纪要已生成: {task_dir}")

    # 优化稿复制到 Downloads
    opt_path = os.path.join(DOWNLOADS, f"{subject}-会议记录员优化版.md")
    minutes_path = os.path.join(task_dir, "会议纪要.md")
    if os.path.exists(minutes_path):
        import shutil
        shutil.copy2(minutes_path, opt_path)
        print(f"✓ 优化稿已存: {opt_path}")

    print("④ 已自动同步: WIKI meetings/ + Obsidian Inbox/（import 链路内完成）")
    print("\n✅ 完成！检查: ")
    print(f"  原始转写: {raw_path}")
    print(f"  优化纪要: {opt_path}")
    print(f"  主存档:   {task_dir}")


if __name__ == "__main__":
    main()
