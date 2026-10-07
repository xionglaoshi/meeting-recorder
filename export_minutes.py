"""会议纪要本地归档与可选外部同步：默认本地保存；WIKI 同步须显式启用。

命名规则（用户 Obsidian 规范）：
- 专项会议：`YYYYMMDD <主题>纪要.md`（空格分隔，参考 20260715 ××业务研讨会纪要.md）
WIKI 规范：`YYYY-MM-DD-<主题>-会议纪要.md` + frontmatter(type=report) + log.md 追加
目录路由（WRITING-SPEC §一）：会议纪要一律落 `LLM-WIKI/meetings/`。
  · 2026-08-20 原定"WIKI 落 reports/"，早于 WRITING-SPEC；
  · 2026-09-03 WRITING-SPEC 建立"会议纪要→meetings/"路由后，本脚本硬编码路径未同步，
    导致 09-05 / 09-08 / 09-10 三份纪要又落回 reports/；
  · 2026-09-11 修正为 meetings/（用户指示），并同步 server.py / 文档 / 前端文案。
"""
import datetime
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import settings

# 归档目标属**可选集成**：未配置（settings 探测不到 Obsidian 收件箱 / 知识库）时
# 本模块整体降级为「不导出」，纪要仍留在 records/ 目录里，不影响核心流程。
# 覆盖方式见 SKILL.md「运维要点 §配置」：$MEETING_OBSIDIAN_INBOX / $MEETING_KNOWLEDGE_BASE


def inbox_dir():
    return settings.obsidian_inbox()


def wiki_dir():
    return settings.wiki_meetings_dir()


def wiki_log():
    return settings.wiki_log_file()


def extract_title(minutes_text, fallback="会议纪要"):
    """从纪要提取会议主题。

    ⚠️ 2026-09-13 修复：原实现只认旧模板的「## 一、会议主题」小节，而现在的纪要
    按《会议纪要质量标准》用的是**六要素一行式**（`会议主题：xxx`），于是永远匹配不到
    → 回落 fallback="会议纪要" → 导出文件名变成 "20260913 会议纪要纪要.md"（双"纪要"）。
    现按优先级依次尝试：六要素行 → 旧模板小节 → 首行标题（去日期、去"纪要"）。
    """
    text = minutes_text or ""

    def _clean(t):
        t = (t or "").strip().strip("*#").strip()
        t = re.sub(r"[（(][^）)]*[）)]", "", t).strip()   # 去括注（如"（进度汇报及压力测试部署）"）
        return t

    # ① 六要素一行式：会议主题：xxx
    m = re.search(r"^会议主题[：:]\s*(.+)$", text, re.M)
    if m:
        t = _clean(m.group(1))
        if t:
            return t
    # ② 旧模板：## 一、会议主题 下第一行
    m = re.search(r"##\s*一、会议主题\s*\n\s*(.+)", text)
    if m:
        t = _clean(m.group(1))
        if t and not t.startswith("（"):
            return t
    # ③ 首行标题：YYYY年M月D日<主体><会议性质>纪要 → 取日期后、末尾"纪要"前的部分
    first = next((l.strip() for l in text.splitlines() if l.strip()), "")
    m = re.match(r"^\d{4}年\d{1,2}月\d{1,2}日\s*(.+?)纪要\s*$", first)
    if m:
        t = _clean(m.group(1))
        if t:
            return t
    return fallback


def _safe(s, max_len=40):
    s = re.sub(r'[\\/:*?"<>|\n\r]', "", s).strip()
    return s[:max_len] or "会议"


def _with_suffix(topic, suffix="纪要"):
    """拼文件名后缀时避免"会议纪要纪要"这类重复（topic 已是"XX纪要"就不再追加）。"""
    t = (topic or "").rstrip()
    return t if t.endswith(suffix) else t + suffix


def export_to_obsidian(minutes_text, title="", date=None):
    """Obsidian Inbox/YYYYMMDD <主题>纪要.md"""
    target = inbox_dir()
    if not target:
        print("[export] 未配置 Obsidian 收件箱，跳过同步（纪要仍在 records/ 内）", flush=True)
        return None
    d = date or datetime.date.today()
    topic = _safe(title or extract_title(minutes_text))
    fname = f"{d.strftime('%Y%m%d')} {_with_suffix(topic)}.md"
    path = os.path.join(str(target), fname)
    content = (
        f"---\n创建日期: {d.isoformat()}\n来源: 会议记录员（实时语音转写）\n---\n\n"
        f"{minutes_text.strip()}\n"
    )
    os.makedirs(str(target), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(content)
    return path


def export_to_wiki(minutes_text, title="", date=None, task_dir="", *, confirmed=False):
    """仅由用户确认的保存动作调用；重复保存相同正文不重复写入。"""
    if not confirmed:
        return None
    from pathlib import Path
    import subprocess
    import tempfile
    import yaml
    target = wiki_dir()
    if not target or not Path(target).parent.is_dir():
        raise RuntimeError("WIKI 目录不存在，请检查 MEETING_KNOWLEDGE_BASE")
    # 专用日志工具固定服务于本机正式 WIKI，拒绝把正文与日志写到不同库。
    if Path(target).parent.resolve() != settings.WIKI_ROOT.resolve():
        raise RuntimeError("WIKI 保存目标须为 ~/wps/WIKI；请移除错误的 MEETING_KNOWLEDGE_BASE 覆盖")
    if not minutes_text.strip():
        raise ValueError("正式纪要为空")
    d = date or datetime.date.today()
    topic = _safe(title or extract_title(minutes_text))
    base = re.sub(r"(会议)?纪要$", "", topic).strip() or topic
    target = Path(target)
    target.mkdir(parents=True, exist_ok=True)
    # 对同日同主题的既有页面先查重；不同正文不静默覆盖用户维护稿。
    path = target / f"{d.isoformat()}-{base}-会议纪要.md"
    body = minutes_text.strip() + "\n"
    if path.exists():
        old = path.read_text(encoding="utf-8")
        old_body = old.split("---", 2)[-1].strip() if old.startswith("---\n") else old.strip()
        if old_body == body.strip():
            return str(path)
        raise FileExistsError("WIKI 已有同日同主题纪要且内容不同，请先核对现有页面")
    fm = yaml.safe_dump(dict(title=_with_suffix(topic), created=d.isoformat(),
        updated=d.isoformat(), type="report", audience="unclassified", status="active",
        tags=["meeting", "report"], sources=[str(Path(settings.RECORDS_DIR) / task_dir / "会议纪要.md")]),
        allow_unicode=True, sort_keys=False)
    content = "---\n" + fm + "---\n\n" + body
    now = datetime.datetime.now().strftime("%Y-%m-%d %H:%M")
    entry = (f"## [{now}] Codex：会议纪要确认入库\n\n"
             f"- source: 用户在会议记录员中确认保存；任务 {task_dir}\n"
             f"- action: 新建 [[meetings/{path.stem}]]\n"
             "- verify: 正式纪要文件写后逐字读回一致\n"
             f"- 来源：Codex · {datetime.date.today().isoformat()}\n")
    script = Path.home() / ".codex/scripts/wiki_log_append.py"
    with tempfile.NamedTemporaryFile(mode="w", suffix=".md", encoding="utf-8") as tmp:
        tmp.write(entry); tmp.flush()
        cmd = [sys.executable, str(script), "--entry-file", tmp.name]
        check = subprocess.run(cmd + ["--dry-run"], capture_output=True, text=True)
        if check.returncode:
            raise RuntimeError("WIKI 日志预检失败：" + check.stderr + check.stdout)
        # 排他创建避免并发覆盖；日志失败回滚本次新建正文。
        with path.open("x", encoding="utf-8") as out:
            out.write(content)
        try:
            if path.read_text(encoding="utf-8") != content:
                raise RuntimeError("WIKI 写后校验失败")
            logged = subprocess.run(cmd, capture_output=True, text=True)
            if logged.returncode:
                raise RuntimeError("WIKI 日志写入失败：" + logged.stderr + logged.stdout)
        except Exception:
            path.unlink()
            raise
    return str(path)


def export_all(minutes_text, title="", date=None, task_dir=""):
    """生成流程仅保存本地及已配置 Obsidian；WIKI 由确认接口单独保存。"""
    ob = export_to_obsidian(minutes_text, title, date)
    wk = None  # 生成流程只落本地，环境变量也不能开启自动入库
    return {"obsidian": ob, "wiki": wk}
