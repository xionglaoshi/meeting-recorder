"""J：实体核验三级匹配模块（博维 entity-verification 设计吸收）

目标：转写中的客户名/项目名/人名/金额，用证据逐一定案——
- 确证（别名表/台账/roster 唯一命中）→ 静默改，纪要不留纠错痕迹
- 未确证（多候选/查无）→ 进待确认清单，纪要留空待人工
分工：L2 校对管语言层（错别字/同音词）；本模块管项目层（专名/金额/编号）

三级匹配（按序执行，命中即停）：
L1 别名表：字典 vocab.json 的变体映射
L2 台账：WIKI projects/ companies/ 实体目录名 + employer-entities.md
L3 Roster：人事库 hr_data.db（405 人，含职务）
"""
import json
import os
import re
import sqlite3
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import settings

ROOT = os.path.dirname(os.path.abspath(__file__))
VOCAB = os.path.join(ROOT, "vocab.json")
# 下面三项都是**可选数据源**：hr_etl.py 已随服务自持（同目录），知识库与实体底稿
# 由 settings 自动探测，探测不到就相应降级，不影响转写与纪要主流程。
HR_DB = str(settings.hr_db_path())
EMP_ENTITIES = str(settings.employer_entities() or "")
WIKI = str(settings.knowledge_base() or "")


def ensure_hr_db():
    if os.path.exists(HR_DB):
        return HR_DB
    etl = settings.hr_etl_script()
    if not etl:
        return HR_DB
    try:
        subprocess.run([sys.executable, str(etl)], capture_output=True, timeout=600)
    except Exception:
        pass
    return HR_DB


# ── 数据源加载（一次性缓存）──────────────────────
_cache = {}


def _load_alias_table():
    """L1 别名表：字典变体 → 规范词（只收专属词，industry/business 通用词不进——
    避免'城投'→'国企'误伤专名）"""
    if "alias" in _cache:
        return _cache["alias"]
    mapping = {}
    try:
        with open(VOCAB, encoding="utf-8") as f:
            v = json.load(f)
        for cat, items in v.get("categories", {}).items():
            if cat in ("disambiguation", "industry", "business"):
                continue
            if isinstance(items, list):
                for it in items:
                    if not isinstance(it, dict):
                        continue
                    term = it.get("term", "")
                    for var in it.get("variants", []):
                        if var and var != term and len(var) >= 2:
                            # 同 var 同 term 去重（2026-08-26 修复共有问题）：
                            # homophone 层与 partner 层可能注册同一变体→同一规范词，
                            # 不去重会产出重复候选（同一实体出现两次）
                            if not any(t == term for t, _ in mapping.get(var, [])):
                                mapping.setdefault(var, []).append((term, cat))
    except Exception:
        pass
    _cache["alias"] = mapping
    return mapping


def _load_ledger():
    """L2 台账：WIKI projects/companies 实体名 + employer-entities 底稿"""
    if "ledger" in _cache:
        return _cache["ledger"]
    names = set()
    # employer-entities.md（权威底稿）
    try:
        text = open(EMP_ENTITIES, encoding="utf-8").read()
        for m in re.findall(r"[\u4e00-\u9fffA-Za-z0-9（）()]{3,50}", text):
            if len(m) >= 3:
                names.add(m)
    except Exception:
        pass
    # WIKI projects/ companies/ 目录+文件
    for base in (os.path.join(WIKI, "projects"), os.path.join(WIKI, "companies")):
        for root, dirs, files in os.walk(base):
            if any(x in root for x in ("_template", ".git")):
                continue
            for f in files:
                if f.endswith(".md"):
                    names.add(f[:-3])
            for d in dirs:
                if not d.startswith("_"):
                    names.add(d)
    _cache["ledger"] = names
    return names


def _load_roster():
    """L3 Roster：人事库（405 人，含职务/部门）"""
    if "roster" in _cache:
        return _cache["roster"]
    roster = {}
    try:
        conn = sqlite3.connect(ensure_hr_db())
        for name, org, dept, pos in conn.execute(
                "SELECT name, org, dept, position FROM employees"):
            roster[name.strip().replace(" ", "")] = {
                "position": pos or "", "dept": dept or "", "org": org or ""}
        conn.close()
    except Exception:
        pass
    _cache["roster"] = roster
    return roster


# ── 三级匹配 ──────────────────────────────────
def verify_entity(token: str, context: str = "") -> dict:
    """对单个实体 token 做三级匹配。
    返回 {token, verdict: 确证|待确认, canonical: 规范词, evidence: 依据, candidates: []}
    """
    t = token.strip()
    if len(t) < 2:
        return {"token": t, "verdict": "跳过", "canonical": t, "evidence": "过短", "candidates": []}

    # L1 别名表（含同音词层）
    alias = _load_alias_table()
    if t in alias:
        cands = alias[t]
        if len(cands) == 1:
            term, cat = cands[0]
            return {"token": t, "verdict": "确证", "canonical": term,
                    "evidence": f"别名表/{cat}", "candidates": []}
        # 多候选：同一称呼可能对应不同的人
        names = [c[0] for c in cands]
        return {"token": t, "verdict": "待确认", "canonical": "",
                "evidence": f"别名表多候选({len(names)}个)", "candidates": names}

    # L2 台账（项目/公司实体名）
    ledger = _load_ledger()
    exact = [n for n in ledger if n == t]
    if exact:
        return {"token": t, "verdict": "确证", "canonical": exact[0],
                "evidence": "台账/精确命中", "candidates": []}
    contains = [n for n in ledger if t in n and len(n) > len(t)]
    if len(contains) == 1:
        return {"token": t, "verdict": "确证", "canonical": contains[0],
                "evidence": "台账/包含命中", "candidates": []}
    if len(contains) > 1:
        return {"token": t, "verdict": "待确认", "canonical": "",
                "evidence": f"台账多候选({len(contains)}个)", "candidates": contains[:5]}

    # L3 Roster（人名）
    roster = _load_roster()
    if t in roster:
        info = roster[t]
        return {"token": t, "verdict": "确证", "canonical": t,
                "evidence": f"roster/{info['position']}", "candidates": []}

    # 兜底：查无
    return {"token": t, "verdict": "待确认", "canonical": "",
            "evidence": "查无（可能是新实体/口语词）", "candidates": []}


# 高风险实体抽取：优先匹配字典/台账已知词，其次常见机构后缀
def extract_entities(text: str) -> list:
    """从文本抽取高风险实体（基于已知实体词根，避免整句误抓）"""
    found = []
    # 1. 已知实体词（字典 term + 变体 + 台账名）——最长优先
    known = set()
    for var in _load_alias_table().keys():
        if len(var) >= 2:
            known.add(var)
    for t in _load_alias_table().values():
        for term, _ in t:
            if len(term) >= 2:
                known.add(term)
    known |= _load_ledger()
    known |= set(_load_roster().keys())
    # 只保留 ≤8 字的已知词，避免长句
    known = {k for k in known if 2 <= len(k) <= 8}
    for tok in sorted(known, key=lambda x: -len(x)):
        if tok in text and tok not in found:
            found.append(tok)
            if len(found) >= 30:
                break
    # 2. 常见机构后缀兜底（未在已知词里的；要求后缀前不是已知词前缀，防跨词）
    for m in re.finditer(r"(?<![\u4e00-\u9fff])([\u4e00-\u9fff]{2,6})(?:公司|集团|码头|平台|项目)", text):
        tok = m.group(0)
        # 若该 token 的核心部分已在 found（如'××'已在，'××平台'跳过）
        core = m.group(1)
        if any(core in f for f in found):
            continue
        if tok not in found and len(tok) >= 3:
            found.append(tok)
    return found


def verify_text(text: str) -> dict:
    """对整段文本做实体核验 → {verified: {token: 规范词}, uncertain: [{token, evidence, candidates}]}"""
    verified = {}
    uncertain = []
    for tok in extract_entities(text):
        r = verify_entity(tok, text)
        if r["verdict"] == "确证" and r["canonical"] and r["canonical"] != tok:
            verified[tok] = r["canonical"]
        elif r["verdict"] == "待确认":
            uncertain.append({"token": tok, "evidence": r["evidence"],
                              "candidates": r["candidates"]})
    return {"verified": verified, "uncertain": uncertain}


def apply_verified(text: str, verified: dict) -> str:
    """应用确证映射（长词优先，避免二次替换）"""
    if not verified:
        return text
    ordered = sorted(verified.items(), key=lambda kv: -len(kv[0]))
    for tok, canon in ordered:
        if tok and canon and tok != canon:
            text = text.replace(tok, canon)
    return text


if __name__ == "__main__":
    test = "今天某总讨论了某某公司和某某平台项目的进度"
    print("=== 实体抽取 ===")
    for e in extract_entities(test):
        print(" ", e)
    print("\n=== 核验结果 ===")
    r = verify_text(test)
    print("确证:", r["verified"])
    print("待确认:", r["uncertain"])
