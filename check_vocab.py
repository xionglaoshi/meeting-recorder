#!/usr/bin/env python3
"""第二代字典校验：权威源更全，只报真问题

权威源收集：
1. employer-entities.md（集团/子公司/项目 权威底稿）
2. WIKI companies/** 所有 .md 文件名 + 目录名
3. WIKI projects/** 所有 .md 文件名 + 目录名
4. WIKI 全站所有 .md 文件名（宽泛补充）
5. hr_data.db 人名
6. USER.md 保护底稿

检查：
- company/project/partner term：不在权威源且无包含关系 → 报问题
- homophone term：同上
- disambiguation 人名：必须在 hr_data.db
"""
import json, os, re, sqlite3, subprocess, sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import settings

# ── hr_data.db 按需重建 ──
# hr_etl.py 已随服务自持（同目录），不再依赖 Agent 脚本目录；属可选数据源，
# 脚本或源目录缺失时，人事层降级为空库，其余校验照常。
HR_DB = str(settings.hr_db_path())


def ensure_hr_db():
    if os.path.exists(HR_DB):
        return HR_DB
    etl = settings.hr_etl_script()
    if not etl:
        print("⚠ 未找到 hr_etl.py（可选数据源），人事层降级")
        return HR_DB
    try:
        print(f"⚠ hr_data.db 不存在，自动重建（{etl}）…")
        subprocess.run([sys.executable, str(etl)], capture_output=True, timeout=600)
        print("✅ hr_data.db 重建完成" if os.path.exists(HR_DB) else "❌ hr_etl 未产出 db，继续（人事层降级）")
    except Exception as e:
        print(f"⚠ hr_data.db 重建失败: {e}（人事层降级）")
    return HR_DB

AUTHORITATIVE = set()

# 知识库根（可选）：settings 探测不到时留空，相关权威源自动跳过
VAULT = str(settings.knowledge_base() or "")

def add(t, src):
    t = (t or "").strip()
    if t and len(t) >= 2:
        AUTHORITATIVE.add(t)

# 1. employer-entities.md（可选数据源，未配置则跳过）
emp_path = settings.employer_entities()
emp = emp_path.read_text(encoding="utf-8") if emp_path else ""
if not emp:
    print("⚠ 未配置单位实体底稿（employer-entities.md），该权威源跳过")
for m in re.findall(r"[\u4e00-\u9fffA-Za-z0-9（）()]{3,60}", emp):
    add(m, "employer-entities")

# 2/3. 知识库 entities 目录与全站文件名（可选；未配置知识库则跳过）
if VAULT and os.path.isdir(VAULT):
    for base in (os.path.join(VAULT, "companies"), os.path.join(VAULT, "projects")):
        for root, dirs, files in os.walk(base):
            if any(x in root for x in ("_template", ".git", "archive")):
                continue
            for f in files:
                if f.endswith(".md"):
                    add(f[:-3], root)
                elif f.endswith((".docx", ".xlsx", ".pdf")):
                    add(f.rsplit(".", 1)[0], root)
            for d in dirs:
                add(d, root)

    for root, dirs, files in os.walk(VAULT):
        if any(x in root for x in (".git", "node_modules", "search-index", "log.md", "_scratch")):
            continue
        for f in files:
            if f.endswith(".md") and not f.startswith("_"):
                add(f[:-3], root)
else:
    print("⚠ 未配置知识库目录（$MEETING_KNOWLEDGE_BASE），companies/projects 权威源跳过")

# 4. hr_data.db（按需重建，缺失自动重建；不存在则跳过）
_hr_path = ensure_hr_db()
if os.path.exists(_hr_path):
    hr = sqlite3.connect(_hr_path)
    for (name,) in hr.execute("SELECT name FROM employees"):
        add(name, "hr_data")
    hr.close()
else:
    print("⚠ 人事库不可用（可选数据源），人名权威源跳过")

# 行业通用词白名单（不校验——不是企业专属实体，只用于同音纠错）
# 2026-08-26 扩充（吸收 dsh check_vocab）：业务/行业/流程词全量收录，避免误报人工维护层合法词
GENERIC_WORDS = {
    "砂石", "钒钛", "钨矿", "尾矿", "选矿", "码头", "国企", "信用评级",
    "发债", "对账", "矿山", "建材", "信息系统", "船舶运输", "保供",
    "供应链金融", "资源再生", "网络货运", "多式联运", "砂石开采",
    "有色开采", "有色选矿", "大宗贸易", "物流调度管理信息化", "供应链管理信息化",
    # 基础词/会议词
    "公司", "集团", "项目", "会议", "纪要", "报告", "方案", "合同", "协议",
    "投资", "合作", "发展", "管理", "经营", "财务", "法务", "人事", "行政",
    "供应链", "产业园", "物流", "贸易", "科技", "建设", "工程",
    "园区", "土地", "规划", "招标", "结算", "汇总", "统筹", "协调",
    "周例会", "调度会", "专项会议", "经营工作会", "晨会", "董事会", "股东会",
    # 业务/行业/流程词
    "河道", "采砂", "疏浚", "清淤", "料场", "央企", "信用", "评级",
    "债券", "台账", "水库", "电厂", "供电", "供水", "装卸", "吞吐",
    "货运", "仓储", "物流园", "开发区", "临空", "保税", "航道", "堤防",
    "围堰", "拆迁", "安置", "土地出让", "招拍挂", "立项", "可研", "环评",
    "安评", "预算", "决算", "审计", "融资", "担保", "抵押", "股权", "资产",
    "负债", "营收", "利润", "毛利率", "现金流", "应收账款", "应付账款",
    "采购", "销售",
}

# 硬污染特征（2026-08-26 吸收 dsh check_vocab）：模板/索引/frontmatter 残留——
# build_vocab 从 WIKI 文件名/目录重建时的已知污染源（index.md 被当实体等）。
# 注意：不做 isascii 误杀（那样会把合法的英文专名/缩写一并杀掉），
# 只按关键词判定。
HARD_POLLUTION = (
    "index", "template", "frontmatter", "aliases", "created", "updated",
    "tags", "status", "inbox", "attachment", "asset", "_template",
    "<", ">", "{", "}", "[", "]",
)


def is_polluted(term):
    """硬污染判定：含模板/索引/frontmatter 关键词或文件系统残留字符"""
    t = (term or "").lower()
    return any(h in t for h in HARD_POLLUTION)

vocab = json.load(open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "vocab.json"), encoding="utf-8"))
cats = vocab["categories"]
issues = []

def term_ok(term):
    """term 可信：在权威源 / 权威源包含它 / 它包含权威源某项"""
    t = term.strip()
    if t in GENERIC_WORDS:
        return True
    if t in AUTHORITATIVE:
        return True
    # 权威源包含 term（如"××"包含在"××项目"里）
    if any(t in a for a in AUTHORITATIVE if len(t) >= 3 and len(a) >= len(t)):
        return True
    # term 包含权威源某项（常见于"有限公司"后缀差异）：如 "××市城投××贸易有限公司" vs "××贸易"
    if any(a in t for a in AUTHORITATIVE if len(a) >= 4):
        return True
    return False

print("=" * 70)
print("第二代字典校验（只报真问题）")
print(f"权威源实体数: {len(AUTHORITATIVE)}")
print("=" * 70)

for cat in ("company", "project", "partner", "business", "industry"):
    items = cats.get(cat, [])
    bad = []
    for it in items:
        if is_polluted(it["term"]):
            issues.append(f"{cat} 疑似模板/索引/乱码污染: {it['term']}")
            print(f"🔴 [{cat}] {it['term']}（疑似模板/索引/乱码污染）")
            continue
        if not term_ok(it["term"]):
            bad.append(it["term"])
    if bad:
        print(f"\n⚠️ [{cat}] {len(bad)} 个 term 无法匹配权威源:")
        for b in bad:
            print(f"   - {b}")
    else:
        print(f"✅ [{cat}] {len(items)} 条全部匹配")

print("\n── [homophone] ──")
for h in cats.get("homophone", []):
    if is_polluted(h["term"]):
        issues.append(f"homophone term 疑似污染: {h['term']}")
        print(f"🔴   {h['pinyin']} → {h['term']}  <-- 疑似模板/索引/乱码污染")
    elif not term_ok(h["term"]):
        issues.append(f"homophone term 无法匹配: {h['term']}")
        print(f"⚠️   {h['pinyin']} → {h['term']}  <-- 无法匹配权威源")
    else:
        print(f"✅ {h['pinyin']} → {h['term']}")

print("\n── [disambiguation] ──")
try:
    hr_names = set(n for (n,) in sqlite3.connect(ensure_hr_db()).execute("SELECT name FROM employees"))
except Exception:
    hr_names = set()
    print("⚠ 人事库不可用，disambiguation 人名校验降级跳过")
for d in cats.get("disambiguation", []):
    for c in d.get("candidates", []):
        n = c.get("name", "")
        if n and n not in hr_names:
            issues.append(f"disambiguation 人名不在人事库: {n}")
            print(f"⚠️   {d['alias']} → {n}  <-- 不在人事库")

print("\n" + "=" * 70)
print(f"共 {len(issues)} 个真问题" if issues else "✅ 全部通过，无真问题！")
# 退出码：0=全部通过；1=有硬污染/真问题（build_vocab.py 自动挂接时据此提示，吸收 dsh 设计）
sys.exit(1 if issues else 0)
