#!/usr/bin/env python3
"""生成会议记录员专属词汇字典 vocab.json v2.0（增强版）

数据来源（信息源铁律：WIKI → Obsidian → WPS → 外部，已按序查证）：
- WIKI 人事档案 hr_data.db（405 人，含职务/部门，消歧依据）
- USER.md 保护底稿（集团/11子公司/24项目 —— 唯一权威源）
- employer-intelligence 技能 entities.md / keywords.md（简称矩阵现成复用）
- wiki/companies/ _子公司 _合作伙伴 目录（合作方实体）
- wiki/projects/ 项目目录名（补充项目与简称）
- 用户实测会议转写中的口语说法

v2.0 增强：
- 拼音变体自动生成（同音纠错：'示例词' → 'shi li ci'）
- homophone 同音词专项层（人工维护高危同音词）
- disambiguation 简称消歧层（共享简称 → 候选实体 + 分管领域）
结构：categories.{person,company,project,business,industry,partner,homophone,disambiguation}
"""
import json
import os
import sqlite3
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from runtime import ensure_runtime
ensure_runtime()

import settings

# 词表种子（个人/单位数据）与代码分离：种子放 vocab_seed.py，不进 Git。
# 没有该文件时照样能跑，只是少了这几类内置种子（仍会从 WPS 人事库/知识库派生）。
try:
    from vocab_seed import (HIGH_LEVEL, COMPANIES, PROJECTS, BUSINESS,
                            INDUSTRY, PARTNERS, HOMOPHONE)
except ImportError:
    print("⚠ 未找到 vocab_seed.py（词表种子数据），仅使用 WPS/知识库派生的词条")
    HIGH_LEVEL, COMPANIES, PROJECTS = {}, [], []
    BUSINESS, INDUSTRY, PARTNERS, HOMOPHONE = [], [], [], []

try:
    from pypinyin import lazy_pinyin, Style
    HAS_PINYIN = True
except ImportError:
    HAS_PINYIN = False

OUT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "vocab.json")
# 人事库按需重建：脚本已随服务自持（同目录 hr_etl.py），不再依赖 Agent 脚本目录。
# 属**可选数据源**——脚本或源目录缺失时 person 层自动降级，不影响其余词表。
HR_DB = str(settings.hr_db_path())


def ensure_hr_db():
    if os.path.exists(HR_DB):
        return HR_DB
    etl = settings.hr_etl_script()
    if not etl:
        print("⚠ 未找到 hr_etl.py（可选数据源），person 降级为高层名单")
        return HR_DB
    try:
        print(f"⚠ hr_data.db 不存在，自动重建（{etl}）…")
        subprocess.run([sys.executable, str(etl)], capture_output=True, timeout=600)
        print("✅ hr_data.db 重建完成" if os.path.exists(HR_DB) else "❌ hr_etl 未产出 db，person 降级为高层名单")
    except Exception as e:
        print(f"⚠ hr_data.db 重建失败: {e}（person 降级为高层名单）")
    return HR_DB


def gen_pinyin_variants(term):
    """生成词条拼音变体（同音纠错用）：'示例词' → ['shi li ci', 'shilici']"""
    if not HAS_PINYIN or not term:
        return []
    try:
        pys = lazy_pinyin(term, style=Style.NORMAL)
        spaced = " ".join(pys)
        joined = "".join(pys)
        return [spaced, joined]
    except Exception:
        return []


def extract_domain(pos, dept, org):
    """从职务/部门提取分管领域关键词（消歧上下文）"""
    domains = []
    txt = f"{pos} {dept} {org}"
    for kw, d in [
        ("工程", "工程建设"), ("经营", "经营管理"), ("财务", "财务"),
        ("研发", "研发技术"), ("技术", "研发技术"), ("生产", "生产"),
        ("销售", "销售贸易"), ("贸易", "销售贸易"), ("港", "码头港务"),
        ("码头", "码头港务"), ("物流", "物流"),
        ("行政", "行政人事"), ("人事", "行政人事"), ("矿业", "矿业"),
        ("砂石", "砂石业务"),
    ]:
        if kw in txt and d not in domains:
            domains.append(d)
    return domains


def build():
    try:
        conn = sqlite3.connect(ensure_hr_db())
        rows = conn.execute("SELECT name, org, dept, position FROM employees ORDER BY name").fetchall()
        conn.close()
    except Exception:
        print("⚠ 人事库不可用，person 仅含高层名单（HIGH_LEVEL）；可重跑 hr_etl.py 重建")
        rows = []
    person = [{"term": n, "variants": list(HIGH_LEVEL.get(n, []))} for n in HIGH_LEVEL]
    seen = set(HIGH_LEVEL.keys())
    duty_index = {}   # name → {position, dept, org, domains, aliases}
    for name, org, dept, pos in rows:
        name = name.strip().replace(" ", "").replace("\u3000", "")
        if not name:
            continue
        v = []
        if "经理" in (pos or ""):
            v.append(name[0] + "经理")
        elif any(k in (pos or "") for k in ("总监", "总工", "副总", "总助", "总经理")):
            v.append(name[0] + "总")
        if name not in seen:
            seen.add(name)
            person.append({"term": name, "variants": v})
        duty_index[name] = {
            "position": pos or "",
            "dept": dept or "",
            "org": org or "",
            "domains": extract_domain(pos or "", dept or "", org or ""),
            "aliases": list(HIGH_LEVEL.get(name, v)),
        }

    # 拼音变体自动补入（2-6字词条）
    for cat_items in (person, COMPANIES, PROJECTS, BUSINESS, INDUSTRY, PARTNERS):
        for it in cat_items:
            term = it["term"]
            if 2 <= len(term) <= 6:
                for pv in gen_pinyin_variants(term):
                    if pv and pv not in it["variants"]:
                        it["variants"].append(pv)

    # 同音词专项层（人工维护高危同音词）——种子数据来自 vocab_seed.py

    # 消歧规则层：共享简称 → 候选实体列表（供 LLM 按上下文判断）
    alias_map = {}
    for name, info in duty_index.items():
        for a in info.get("aliases", []):
            alias_map.setdefault(a, []).append(name)
    DISAMBIGUATION = []
    for alias, names in sorted(alias_map.items()):
        if len(names) >= 2:
            DISAMBIGUATION.append({
                "alias": alias,
                "candidates": [
                    {"name": n, "position": duty_index[n]["position"],
                     "dept": duty_index[n]["dept"],
                     "domains": duty_index[n]["domains"]}
                    for n in names
                ],
                "rule": "根据会议讨论内容判断：提到与候选人分管领域相关的话题时，指向对应候选人",
            })

    vocab = {
        "version": "2.0",
        "updated": "2026-08-19",
        "note": ("会议记录员专属词汇字典 v2.0。三类纠错：①同音不同字（pinyin变体）"
                 "②同义词（variants）③简称消歧（disambiguation）。"
                 "数据源：WIKI hr_data.db 为主 + Obsidian/employer-intelligence 补充。"),
        "categories": {
            "person": person,
            "company": COMPANIES,
            "project": PROJECTS,
            "business": BUSINESS,
            "industry": INDUSTRY,
            "partner": PARTNERS,
            "homophone": HOMOPHONE,
            "disambiguation": DISAMBIGUATION,
        },
    }
    # 保留已有 meta（如 asr_vocabulary_id 热词表 ID——重新生成不丢失）
    try:
        if os.path.exists(OUT):
            with open(OUT, encoding="utf-8") as _f:
                old_meta = json.load(_f).get("meta")
            if old_meta:
                vocab["meta"] = old_meta
    except Exception:
        pass
    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    with open(OUT, "w", encoding="utf-8") as f:
        json.dump(vocab, f, ensure_ascii=False, indent=2)
    total = sum(len(v) for v in vocab["categories"].values())
    print(f"✅ vocab.json 已生成: {OUT}")
    print(f"   总词条 {total}: person {len(person)} / company {len(COMPANIES)} / project {len(PROJECTS)} / "
          f"business {len(BUSINESS)} / industry {len(INDUSTRY)} / partner {len(PARTNERS)} / "
          f"homophone {len(HOMOPHONE)} / disambiguation {len(DISAMBIGUATION)}")
    if not HAS_PINYIN:
        print("   ⚠ pypinyin 未安装，拼音变体未生成（pip install pypinyin）")




if __name__ == "__main__":
    build()
    # 自动挂接字典自检（2026-08-26 吸收 dsh build_vocab 设计）：
    # 重建后立即用 check_vocab.py 回查权威源，防模板/索引/乱码污染进入三道关（热词/L2/纪要归一）
    try:
        import subprocess
        r = subprocess.run(
            [sys.executable, os.path.join(os.path.dirname(os.path.abspath(__file__)), "check_vocab.py")],
            capture_output=True, text=True)
        if r.returncode != 0:
            print("\n⚠️ 字典自检发现真问题，请人工清理（见上方 check_vocab 输出）：")
            print(r.stdout[-800:] if r.stdout else r.stderr[-300:])
        else:
            print("\n✅ 字典自检通过（无硬污染/权威源缺失）")
    except Exception as e:
        print(f"\n⚠️ 字典自检跳过: {e}")
