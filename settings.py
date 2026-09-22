#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""settings.py — 路径 / 凭证 / 可选数据源的统一解析（纯标准库）

把「去哪读密钥」「产物写哪」「要不要写回知识库」这类环境相关的事收敛到一处，
让服务本身不绑任何特定平台的目录。历史代码里散落的 ~/.hermes/... 就是这么来的。

⚠️ 2026-09-20：本技能已迁为**公用件**（正本 `~/.agents/skills/meeting-recorder/`，四家共用一份），
   故凭证链**同时认三家**（`~/.codex` → `~/.hermes` → `~/.dsh`）+ 公用兜底 `~/.agents/.env`。

三类配置，优先级都是「显式环境变量 > 项目内约定 > 用户级约定 > 自动探测」：

1. 目录
   $MEETING_SERVER_HOME     服务根目录（默认 = 本文件所在目录）
   $MEETING_RECORDS_DIR     会议产物根（默认 $HOME/records）
   $MEETING_REFERENCES_DIR  模板目录（默认 $HOME/references）

2. 凭证（DASHSCOPE_API_KEY / DEEPSEEK_API_KEY / TENCENT_MEETING_TOKEN …）
   先查环境变量，再按顺序**合并**下列 .env 文件（靠前的优先，同名键不覆盖）：
     $MEETING_ENV_FILE → <服务根>/.env → ~/.config/meeting-server/.env
     → ~/.codex/.env → ~/.hermes/.env → ~/.dsh/.env → ~/.agents/.env（公用兜底，§6.4 第三跳）
   —— 合并而非"取第一个存在的文件"，所以项目内 .env 只写要覆盖的几项即可，
     不会把后面的凭证文件整个遮住。

3. 可选集成（都没有就自动降级，不影响核心录音转写功能）
   $MEETING_KNOWLEDGE_BASE     知识库根（LLM-WIKI）
   $MEETING_OBSIDIAN_INBOX     纪要同步到的 Obsidian 收件箱
   $MEETING_HR_ETL             人事库重建脚本（默认用本目录自持的 hr_etl.py）
   $MEETING_EMPLOYER_ENTITIES  单位实体底稿（employer-entities.md）
   $MEETING_TENCENT_CLI_DIR    腾讯会议 CLI 目录
   $MEETING_HR_DB              人事库 sqlite 路径（默认 /tmp/hr_data.db）

自查：python3 runtime.py --doctor
"""
import os
import shutil
from pathlib import Path

PROJECT = "meeting-server"

# ── 1. 目录 ──────────────────────────────────────────────
HOME = Path(os.environ.get("MEETING_SERVER_HOME")
            or Path(__file__).resolve().parent).expanduser()
RECORDS_DIR = Path(os.environ.get("MEETING_RECORDS_DIR")
                   or HOME / "records").expanduser()
STATIC_DIR = HOME / "static"
REFERENCES_DIR = Path(os.environ.get("MEETING_REFERENCES_DIR")
                      or HOME / "references").expanduser()
LOGS_DIR = HOME / "logs"

# ── 2. 凭证 ──────────────────────────────────────────────
def env_files():
    """凭据文件候选链（含不存在的，便于诊断）。第一个存在的生效。"""
    chain = []
    explicit = os.environ.get("MEETING_ENV_FILE")
    if explicit:
        chain.append(Path(explicit).expanduser())
    chain += [
        HOME / ".env",
        Path.home() / ".config" / PROJECT / ".env",
        Path.home() / ".codex" / ".env",
        Path.home() / ".hermes" / ".env",      # 公用副本：三家 .env 都认
        Path.home() / ".dsh" / ".env",
        Path.home() / ".agents" / ".env",      # 公用兜底（§6.4 第三跳）
    ]
    return chain


def env_file():
    """返回第一个存在的 .env 文件；都没有返回 None。"""
    for p in env_files():
        if p.is_file():
            return p
    return None


_env_cache = None


def _env_from_file():
    """按序合并整条 .env 链：靠前的文件优先，同名键不覆盖。"""
    global _env_cache
    if _env_cache is not None:
        return _env_cache
    data = {}
    for p in env_files():
        if not p.is_file():
            continue
        try:
            for line in p.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, v = line.split("=", 1)
                data.setdefault(k.strip(), v.strip().strip('"').strip("'"))
        except Exception:
            continue
    _env_cache = data
    return data


def get(name, default=None):
    """取**普通配置**（端口/目录/开关）：环境变量优先，其次 .env 文件链。

    注意：凭证与"账号专属接入点"不要用这个，用下面的 `secret()`。
    """
    v = os.environ.get(name)
    if v:
        return v
    return _env_from_file().get(name, default)


def secret(name, default=None):
    """取**凭证 / 账号专属设置**：**.env 文件优先，环境变量兜底**。

    为什么反过来：宿主应用（如 Codex）会把会话启动那一刻的 .env 导出进 shell 环境，
    那份导出值是**历史快照**——用户后来改了 .env（换端点、轮换令牌），环境里还是旧的，
    而且它优先级更高就会静默盖住新值。本项目约定"文件是真身"，
    与 `~/.codex/scripts/gitee.sh` 的处理一致。
    """
    v = _env_from_file().get(name)
    if v:
        return v
    return os.environ.get(name, default)


def require(name):
    v = secret(name)
    if not v:
        raise RuntimeError(
            "未找到 " + name + "。请设置环境变量，或写进 .env：\n  候选位置：\n    "
            + "\n    ".join(str(p) for p in env_files()))
    return v


def reload():
    """清空缓存（改过 .env 后调用）。"""
    global _env_cache
    _env_cache = None

# ── 3. 可选集成 ───────────────────────────────────────────
# 知识库根：默认 ~/WIKI，可用 $MEETING_KNOWLEDGE_BASE 指向任意目录
WIKI_ROOT = Path.home() / "WIKI"


def _first_dir(cands):
    for c in cands:
        if c:
            p = Path(c).expanduser()
            if p.is_dir():
                return p
    return None


def _first_file(cands):
    for c in cands:
        if c:
            p = Path(c).expanduser()
            if p.is_file():
                return p
    return None


def knowledge_base():
    """知识库根。显式变量优先（$MEETING_KNOWLEDGE_BASE），默认 ~/WIKI。"""
    explicit = os.environ.get("MEETING_KNOWLEDGE_BASE")
    if explicit:
        return _first_dir([explicit])
    return _first_dir([WIKI_ROOT])


def obsidian_inbox():
    """已废弃（2026-09-17）：Obsidian Inbox 通道停用，纪要统一落 WIKI/meetings/。

    保留函数签名以免调用方（export_minutes / server）报错；除非显式设置
    MEETING_OBSIDIAN_INBOX，否则一律返回 None。
    """
    explicit = os.environ.get("MEETING_OBSIDIAN_INBOX")
    if explicit:
        return _first_dir([explicit])
    return None


def wiki_meetings_dir():
    kb = knowledge_base()
    return (kb / "meetings") if kb else None


def wiki_log_file():
    kb = knowledge_base()
    return (kb / "log.md") if kb else None


def hr_etl_script():
    """人事库重建脚本（可选）。默认用本目录自持的 hr_etl.py——不再依赖 Agent 脚本目录。"""
    return _first_file([
        os.environ.get("MEETING_HR_ETL"),
        HOME / "hr_etl.py",
    ])


def employer_entities():
    """单位实体底稿（可选，属个人数据，未配置即跳过）。"""
    kb = knowledge_base()
    return _first_file([
        os.environ.get("MEETING_EMPLOYER_ENTITIES"),
        (kb / "references" / "employer-entities.md") if kb else None,
    ])


def hr_db_path():
    return Path(os.environ.get("MEETING_HR_DB") or "/tmp/hr_data.db")


def model_api_key_names():
    """返回本项目用到的凭证字段名（供文档/自检引用，避免各处硬编码字符串）。"""
    return ("DASHSCOPE_API_KEY", "DEEPSEEK_API_KEY", "TENCENT_MEETING_TOKEN")


# ── 百炼（DashScope）接入点 ──────────────────────────────
# 有人用的是官方公共端点，有人用"专属部署"（专属域名形如 ws-xxxx.<region>.maas.aliyuncs.com）。
# 因此这里不写死域名：统一从 $DASHSCOPE_BASE_URL 推导，没配就用官方公共端点。
DEFAULT_DASHSCOPE_HOST = "dashscope.aliyuncs.com"


def dashscope_host():
    """从 $DASHSCOPE_BASE_URL 取主机名；未配置/解析不出时用官方域名。"""
    raw = (secret("DASHSCOPE_BASE_URL") or "").strip()
    if raw:
        from urllib.parse import urlparse
        host = urlparse(raw if "://" in raw else "https://" + raw).hostname
        if host:
            return host
    return DEFAULT_DASHSCOPE_HOST


def dashscope_ws_url():
    """流式 ASR 的 WebSocket 端点（专属部署与公共端点同路径，仅域名不同）。"""
    return (secret("DASHSCOPE_WS_URL")
            or f"wss://{dashscope_host()}/api-ws/v1/inference")


def dashscope_api_base():
    """百炼 REST 根（热词表 VocabularyService 用）。"""
    return (secret("DASHSCOPE_API_BASE")
            or f"https://{dashscope_host()}/api/v1")


def dashscope_compatible_url():
    """OpenAI 兼容端点（列模型、部分模型对话用）。"""
    return (secret("DASHSCOPE_BASE_URL")
            or f"https://{dashscope_host()}/compatible-mode/v1")


def vocab_prefix():
    """ASR 热词表前缀（百炼侧按前缀做幂等更新的标识）。换前缀会新建一张热词表。"""
    return get("MEETING_VOCAB_PREFIX", "meeting")


def speaker_index():
    """说话人消歧依据（可选，属个人数据）。

    优先本地未入库版本，其次随仓库分发的模板：
      $MEETING_SPEAKER_INDEX → references/人名职责索引.local.md → references/人名职责索引.md
    真实人名表请放 .local.md（已在 .gitignore 中），避免随仓库外发。
    """
    return _first_file([
        os.environ.get("MEETING_SPEAKER_INDEX"),
        REFERENCES_DIR / "人名职责索引.local.md",
        REFERENCES_DIR / "人名职责索引.md",
    ])


def tencent_cli_dir():
    """腾讯会议 CLI 目录（可选）。

    正主已随服务自持：tools/tencent_meeting/scripts（2026-09-13 从 Hermes 备份迁入）。
    仅保留 $MEETING_TENCENT_CLI_DIR 一个覆盖入口（2026-09-19 去掉 ~/.hermes 跨家引用）。
    """
    return _first_dir([
        os.environ.get("MEETING_TENCENT_CLI_DIR"),
        HOME / "tools" / "tencent_meeting" / "scripts",
    ])


def tencent_cli_command():
    """腾讯会议命令行入口：优先 PATH 上的 tmeet（现行），否则返回 None。"""
    return shutil.which("tmeet")

# ── 自查 ─────────────────────────────────────────────────
def _mark(v):
    return "✅" if v else "—"


def describe():
    chosen_env = env_file()
    lines = ["── settings（meeting-server）──",
             "服务根 HOME      : " + str(HOME),
             "产物根 RECORDS   : " + str(RECORDS_DIR),
             "模板 REFERENCES  : " + str(REFERENCES_DIR),
             "生效 .env        : " + (str(chosen_env) if chosen_env else "（无，仅用环境变量）"),
             "  候选链："]
    for p in env_files():
        lines.append("    " + ("✅ " if p.is_file() else "   ") + str(p))

    lines += ["", "凭证："]
    file_env = _env_from_file()
    for name in ("DASHSCOPE_API_KEY", "DEEPSEEK_API_KEY",
                 "TENCENT_MEETING_TOKEN", "TENCENT_SECRET_ID"):
        if os.environ.get(name):
            src = "环境变量"
        elif name in file_env:
            src = chosen_env.name if chosen_env else "文件"
        else:
            src = None
        lines.append("  " + _mark(src) + " " + name.ljust(24)
                     + (("来自 " + str(src)) if src else "未配置"))

    lines += ["", "可选集成（缺失即自动降级，不影响核心功能）："]
    items = [("知识库 LLM-WIKI", knowledge_base()),
             ("Obsidian 收件箱", obsidian_inbox()),
             ("纪要归档目录", wiki_meetings_dir()),
             ("知识库 log.md", wiki_log_file()),
             ("人事库脚本 hr_etl.py", hr_etl_script()),
             ("单位实体底稿", employer_entities()),
             ("腾讯会议 CLI 目录", tencent_cli_dir())]
    for label, p in items:
        lines.append("  " + _mark(p) + " " + label.ljust(22) + str(p or "未找到"))
    tmeet = tencent_cli_command()
    lines.append("  " + _mark(tmeet) + " " + "tmeet 命令".ljust(22) + str(tmeet or "未找到"))
    return "\n".join(lines)


if __name__ == "__main__":
    print(describe())
