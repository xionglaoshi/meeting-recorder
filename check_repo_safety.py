#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""check_repo_safety.py — 提交前体检：确认仓库里**没有**个性化/敏感数据

为什么需要它
------------
本程序有大量"本机个性化数据"：单位与子公司名、人名与称呼、项目名、专有词、
大模型凭证……这些**必须留在本机**（程序自己还要用），但**绝不能进代码仓库**。
人肉记忆靠不住，所以做成一键体检。

它检查什么
----------
1. **会被提交的文件清单**——用临时 GIT_DIR 干跑 `git add -A`，
   拿到"真正会入库"的文件（遵守 .gitignore），不污染工作区、不建 .git。
2. **通用敏感模式**——密钥/私钥/手机号/身份证/带值的 password 等。
3. **个性化词表**——自动从本机数据源派生，无需手工维护：
     · `vocab.json`       的 person / company / project / partner 词与变体
     · `vocab_seed.py`    的种子词
     · `references/*.local.md` 的人名
   另可手写 `denylist.local.txt`（每行一个词或正则，入库不了、且只在本地生效）

用法
----
  python3 check_repo_safety.py            # 体检（有问题退出码 1）
  python3 check_repo_safety.py -v         # 连同"扫了哪些文件、用了多少词"一起打印

建议：把 `python3 check_repo_safety.py && git push` 当成固定动作——
体检不过就别推。
"""
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent
VERBOSE = "-v" in sys.argv or "--verbose" in sys.argv

# 一些"看起来像但其实是通用词"的词，避免误报（可按需追加）
GENERIC_OK = {
    "公司", "集团", "项目", "会议", "纪要", "报告", "方案", "合同", "协议",
    "投资", "合作", "发展", "管理", "经营", "财务", "法务", "人事", "行政",
    "供应链", "产业园", "物流", "贸易", "科技", "建设", "工程", "园区",
    "土地", "规划", "招标", "结算", "汇总", "统筹", "协调", "码头", "港区",
    "砂石", "开采", "选矿", "尾矿", "货运", "仓储", "航运", "大宗",
    # 称谓 / 职务 / 通用名词（这些不该被当成"敏感词"，否则满屏误报）
    "姓名", "名称", "简称", "全称", "称呼", "口语", "规范", "示例", "模板",
    "老板", "董事长", "总经理", "副总", "总监", "经理", "主任", "秘书",
    "主持", "参会", "地点", "主题", "时间", "单位", "主体", "性质", "附件",
    "岗位", "职责", "分工", "领域", "业务", "产品", "服务", "客户", "历史",
    "更新", "匹配", "指向", "索引", "权威", "工作", "平台", "用途", "外部",
    "内部", "内容", "信息", "数据", "系统", "工具", "流程", "标准", "质量",
}

# ── 通用敏感模式（与个人无关，任何仓库都该拦）──
GENERIC_PATTERNS = [
    ("大模型/云厂商密钥", re.compile(r"\bsk-[A-Za-z0-9_\-]{16,}")),
    ("阿里云 AccessKey", re.compile(r"\b(?:LTAI|AKID)[A-Za-z0-9]{12,}")),
    ("GitHub 令牌", re.compile(r"\bgh[pousr]_[A-Za-z0-9]{20,}")),
    ("Gitee 令牌", re.compile(r"(?:gitee[_\-]?token\s*[:=]\s*\S{16,})|"
                           r"(?:gitee[^\n]{0,40}\b[0-9a-f]{32}\b)",
                           re.IGNORECASE)),
    ("私钥文件头", re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----")),
    ("带值的口令字段", re.compile(
        r"(?i)\b(?:api[_\-]?key|apikey|secret|token|password|passwd|pwd)\b\s*[:=]\s*"
        r"['\"][^'\"]{12,}['\"]")),
    ("中国大陆手机号", re.compile(r"(?<!\d)1[3-9]\d{9}(?!\d)")),
    ("身份证号", re.compile(r"(?<!\d)\d{17}[\dXx](?!\d)")),
    ("疑似本机绝对路径", re.compile(r"/Users/[A-Za-z][\w.\-]*/(?:\.codex|\.hermes|\.trae-cn)/")),
]


def run(argv, **kw):
    return subprocess.run(argv, capture_output=True, text=True, **kw)


def committable_files():
    """返回"遵循 .gitignore 后会被提交"的相对路径列表（不污染工作区）。"""
    tmp = tempfile.mkdtemp(prefix="repo-safety-")
    env = {**os.environ, "GIT_DIR": tmp, "GIT_WORK_TREE": str(ROOT)}
    try:
        if run(["git", "init", "-q"], env=env, cwd=str(ROOT)).returncode != 0:
            return None
        if run(["git", "add", "-A"], env=env, cwd=str(ROOT)).returncode != 0:
            return None
        out = run(["git", "ls-files", "--cached"], env=env, cwd=str(ROOT))
        return [line for line in out.stdout.splitlines() if line.strip()]
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def local_terms():
    """从本机数据源派生"不该进仓库"的个性化词。"""
    terms = set()

    vpath = ROOT / "vocab.json"
    if vpath.is_file():
        try:
            cats = json.loads(vpath.read_text(encoding="utf-8")).get("categories", {})
            for cat in ("person", "company", "project", "partner"):
                for it in cats.get(cat, []):
                    t = (it.get("term") or "").strip()
                    if t:
                        terms.add(t)
                    for var in (it.get("variants") or []):
                        var = (var or "").strip()
                        if var:
                            terms.add(var)
        except Exception:
            pass

    spath = ROOT / "vocab_seed.py"
    if spath.is_file():
        try:
            sys.path.insert(0, str(ROOT))
            import vocab_seed as seed                     # noqa: WPS433
            for name in ("HIGH_LEVEL", "COMPANIES", "PROJECTS", "PARTNERS"):
                val = getattr(seed, name, None)
                if isinstance(val, dict):
                    terms.update(k for k in val if isinstance(k, str))
                elif isinstance(val, list):
                    for it in val:
                        if isinstance(it, dict):
                            if it.get("term"):
                                terms.add(it["term"])
                            terms.update(x for x in (it.get("variants") or []) if x)
        except Exception:
            pass

    # *.local.md：只取 Markdown 表格首列（那是"姓名"列），不做全文捞词——
    # 全文捞词会把"历史/工作/平台"这类通用词当成敏感词，导致大片误报。
    for p in (ROOT / "references").glob("*.local.md"):
        try:
            for line in p.read_text(encoding="utf-8").splitlines():
                if not line.lstrip().startswith("|"):
                    continue
                cell = line.strip().strip("|").split("|")[0].strip()
                cell = re.sub(r"[*`\[\]]", "", cell)
                if re.fullmatch(r"[\u4e00-\u9fff]{2,4}", cell):
                    terms.add(cell)
        except Exception:
            pass

    manual = ROOT / "denylist.local.txt"
    if manual.is_file():
        for line in manual.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line and not line.startswith("#"):
                terms.add(line)

    allow = set()
    apath = ROOT / "allow.local.txt"
    if apath.is_file():
        for line in apath.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line and not line.startswith("#"):
                allow.add(line)

    # 过滤：去掉通用词与占位符。
    # 中文词一律要求 ≥3 字；2 字只保留"非通用称谓/缩写"（用 GENERIC_OK 兜底）。
    terms = {t for t in terms
             if len(t) >= 2 and t not in GENERIC_OK and not t.startswith("××")}
    return sorted(terms, key=len, reverse=True), allow


def is_probably_binary(path: Path) -> bool:
    try:
        with open(path, "rb") as f:
            chunk = f.read(8192)
        return b"\x00" in chunk
    except Exception:
        return True


def scan(paths, terms, allow):
    """allow 里的条目形如 `文件名:词` 或 `文件名`，命中即跳过（表示"有意保留"）。"""
    hits = []
    for rel in paths:
        p = ROOT / rel
        if not p.is_file() or is_probably_binary(p):
            continue
        try:
            text = p.read_text(encoding="utf-8", errors="ignore")
        except Exception:
            continue
        for i, line in enumerate(text.splitlines(), 1):
            for label, pat in GENERIC_PATTERNS:
                if pat.search(line):
                    hits.append((rel, i, f"[{label}] {pat.search(line).group(0)[:40]}"))
            for t in terms:
                if not t or t not in line:
                    continue
                if rel in allow or f"{rel}:{t}" in allow:
                    continue
                if rel.endswith(".local.md"):        # 本机数据文件，本就不入库
                    continue
                hits.append((rel, i, f"[个性化词] {t}"))
    return hits


def main():
    print("── 仓库安全体检（确认个性化数据不会入库）──")
    files = committable_files()
    if files is None:
        print("❌ 无法枚举待提交文件（git 不可用？）")
        return 2
    terms, allow = local_terms()
    print(f"  待提交文件：{len(files)} 个")
    print(f"  本机个性化词：{len(terms)} 个（来自 vocab.json / vocab_seed.py / *.local.md / denylist.local.txt）")
    if allow:
        print(f"  允许名单：{len(allow)} 条（allow.local.txt，表示有意保留）")
    if VERBOSE:
        print("  ── 文件清单 ──")
        for f in sorted(files):
            print(f"    {f}")

    hits = scan(files, terms, allow)
    print()
    if not hits:
        print("✅ 通过：待提交文件里没有发现个性化数据或凭证")
        print("   （个性化数据仍在本地生效：vocab.json / vocab_seed.py / hr_etl.py /")
        print("     references/*.local.md / .env —— 它们已被 .gitignore 挡住）")
        return 0

    print(f"❌ 发现 {len(hits)} 处问题，**不要提交**：")
    seen = set()
    for rel, i, what in hits:
        key = (rel, what)
        if key in seen:
            continue
        seen.add(key)
        print(f"   {rel}:{i}  {what}")
    print()
    print("   处理：把该内容移出待提交文件（改成示例/模板），或加进 .gitignore。")
    return 1


if __name__ == "__main__":
    sys.exit(main())
