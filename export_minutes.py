"""会议纪要导出：① Obsidian Inbox ② WIKI meetings/

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


def export_to_wiki(minutes_text, title="", date=None, task_dir=""):
    """WIKI meetings/YYYY-MM-DD-<主题>-会议纪要.md + log.md 追加"""
    target = wiki_dir()
    if not target:
        print("[export] 未配置知识库目录，跳过 meetings/ 入库（纪要仍在 records/ 内）", flush=True)
        return None
    d = date or datetime.date.today()
    topic = _safe(title or extract_title(minutes_text))
    # WIKI 命名规范：YYYY-MM-DD-<主题>-会议纪要.md；主题若已带"纪要"后缀先剥掉，避免重复
    base = re.sub(r"(会议)?纪要$", "", topic).strip() or topic
    fname = f"{d.isoformat()}-{base}-会议纪要.md"
    path = os.path.join(str(target), fname)
    fm = (
        f"---\ntitle: \"{topic}会议纪要\"\n"
        f"created: \"{d.isoformat()}\"\nupdated: \"{d.isoformat()}\"\n"
        f"type: \"report\"\naudience: \"unclassified\"\nstatus: \"active\"\n"
        f"tags: \"[meeting, report]\"\n"
        f"frontmatter_inferred_by: \"meeting-recorder\"\n---\n\n"
    )
    os.makedirs(str(target), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(fm + minutes_text.strip() + "\n")
    # 更新知识库 log.md（可选）
    now = datetime.datetime.now().strftime("%Y-%m-%d %H:%M")
    src = task_dir or f"{settings.RECORDS_DIR}/{d.strftime('%Y%m%d')}"
    entry = (
        f"\n#### [{now}] [meeting-recorder] {topic}会议纪要入库\n"
        f"- source: 会议记录员语音转写（{src}）\n"
        f"- action: 新建 `meetings/{fname}` + Obsidian Inbox 同步\n"
        f"- detail: 语音转写→LLM纪要→自动导出双库\n"
    )
    log = wiki_log()
    if log:
        with open(str(log), "a", encoding="utf-8") as f:
            f.write(entry)
    return path


def export_all(minutes_text, title="", date=None, task_dir=""):
    """导出双库，返回 {obsidian, wiki} 路径（未配置的目标返回 None）"""
    ob = export_to_obsidian(minutes_text, title, date)
    wk = export_to_wiki(minutes_text, title, date, task_dir)
    return {"obsidian": ob, "wiki": wk}
