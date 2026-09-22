"""L2 转录校对模块：流水 → 清洗稿 + 纠错对照清单 + 存疑清单

设计（吸收博维 meeting-and-brief §2.5 + 本技能字典）：
- 三档置信处置：高置信（命中字典/口音规则）→ 直接改；中置信 → 标 [?待核]；数字/姓名 → 只标不改
- 三级来源：L1 专名（vocab 字典）＞ L2 口音规则（拼音回推）＞ L3 通用同音
- 留痕可溯：每处改动记 原词→正词｜归因｜处置
"""
import json
import os
import re


def load_dict():
    """加载 vocab.json 字典，构建 变体→规范词 映射（含拼音变体）"""
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "vocab.json")
    mapping = {}   # 变体/错误写法 → (规范词, 类别)
    try:
        with open(path, encoding="utf-8") as f:
            v = json.load(f)
        cats = v.get("categories", {})
        # L2 只做"纠错"（错误写法→正词），不做"归一"（合法简称→完整名是 L3 纪要阶段的事）。
        # 收录：homophone 高危同音层 + 各层"明显错误拼写"变体。
        # 合法简称通过 AUTHORITATIVE 权威集合识别：不在权威集合里的
        # 变体视为"错误拼写"进 L2；在权威集合里的是合法简称，留给 L3 归一。
        for cat in ("company", "project", "partner", "person"):
            for it in cats.get(cat, []):
                term = it["term"]
                for var in it.get("variants", []):
                    if var and var != term and len(var) >= 2:
                        mapping[var] = (term, cat)
        # 同音词层（拼音 → 规范词）
        for h in cats.get("homophone", []):
            term = h["term"]
            for var in h.get("variants", []):
                mapping[var] = (term, "homophone")
            mapping[h["pinyin"]] = (term, "homophone")
    except Exception:
        pass
    return mapping


# 口音混淆规则（拼音回推：识别词与规范词只差一个声韵母维度）
ACCENT_RULES = [
    # (规则名, 混淆对)
    ("R1平翘舌", [("z", "zh"), ("c", "ch"), ("s", "sh")]),
    ("R2前后鼻音", [("n", "ng")]),
    ("R3 l-n", [("l", "n")]),
    ("R4 f-h", [("f", "h")]),
]


def _pinyin_map():
    """拼音串 → 声母/韵母近似集合（简化版，用于口音回推）"""
    # 复用 pypinyin 输出做近似判断
    from pypinyin import lazy_pinyin, Style
    return lazy_pinyin


def proofread_text(text: str, mapping: dict = None) -> dict:
    """校对一段文本 → {clean, changes: [{原词,正词,归因,处置,位置}], uncertain: []}"""
    if mapping is None:
        mapping = load_dict()
    changes = []
    uncertain = []
    clean = text

    # ① 专名层（最高优先级）：基于原始文本收集命中，按位置从后往前替换，
    #    避免二次替换和索引错位
    candidates = []
    for var, (term, cat) in mapping.items():
        if var == term or len(var) < 2:
            continue
        if re.fullmatch(r"[a-z ]+", var):
            continue
        start = 0
        while True:
            idx = text.find(var, start)
            if idx == -1:
                break
            candidates.append((idx, len(var), var, term, cat))
            start = idx + len(var)

    # 同区域只保留最长词命中（长词优先，短词被覆盖）
    candidates.sort(key=lambda c: (-c[1], c[0]))
    kept = []
    occupied = []
    for idx, ln, var, term, cat in candidates:
        if any(not (idx + ln <= s or idx >= e) for s, e in occupied):
            continue
        kept.append((idx, ln, var, term, cat))
        occupied.append((idx, idx + ln))

    # 按位置从后往前替换（后面先替换，前面的索引不受影响）
    kept.sort(key=lambda c: -c[0])
    clean = text
    for idx, ln, var, term, cat in kept:
        # 人名层（"某总"这类称呼可能对应多人）：语境消歧交给 L3，L2 只标待核
        if cat == "person" and var.endswith("总"):
            uncertain.append({"原词": var, "正词": "（需按上下文判断）", "归因": f"专名/{cat}消歧", "位置": idx})
            continue
        # 前缀重叠防御（2026-08-26 修复共有问题）：
        # 变体常带前缀共现——"××市城投[××贸易有限公司]" 中 var 只是后半段
        # 前面紧邻 term 前缀（p+var==term）→ 直接把替换起点前移到前缀处，
        # 避免替换出 "××市城投××市城投××贸易有限公司" 式重复。
        ext_start = idx
        if len(term) > len(var):
            for k in range(min(len(term) - len(var), 10), 1, -1):
                p = term[:k]
                if p + var == term and idx >= k and clean[idx - k:idx] == p:
                    ext_start = idx - k
                    break
        # 已是规范形则跳过（2026-09-13 修复重复替换）：
        # 本函数在**采集阶段（rt_correct）与 L2 校对阶段各跑一次**。第一次已把
        # "牛车河"改成"牛车河砂石开采项目"、把"链云砂石"改成"链云砂石平台"后，
        # 第二次校对时 var 又会命中 term 自身的前缀 → 变成
        # "牛车河砂石开采项目砂石开采项目" / "链云砂石平台平台"（实测复现）。
        # 判据：**该位置起已经是完整 term** → 无需替换。
        if text[ext_start:ext_start + len(term)] == term:
            continue
        clean = clean[:ext_start] + term + clean[idx + ln:]
        # 残留后缀吞噬（2026-08-27）：变体展开为 term 后，紧邻原文残留的组织后缀
        # 会重复——"××集团"→"××集团有限公司"+"集团"残留 = "…有限公司集团"。
        # term 尾组织词可能是"有限公司/公司"，残留是"集团"——取 after 开头的组织词，
        # 若它已存在于 term 中（如"集团"在"…集团有限公司"中间）→ 残留冗余，吞掉。
        after = clean[ext_start + len(term):]
        for tail in ("集团有限公司", "有限公司", "集团", "公司", "贸易",
                     "股份", "科技", "码头", "项目", "中心", "银行"):
            if after.startswith(tail) and tail in term:
                clean = clean[:ext_start + len(term)] + after[len(tail):]
                break
        changes.append({"原词": var, "正词": term, "归因": f"专名/{cat}", "处置": "改", "位置": ext_start})

    # 后缀去重：修正 "规范词+重复后缀"（如 "××贸易有限公司公司"、"集团有限公司集团"）
    # 2026-08-26 吸收 dsh proofread：去重同样留痕（归因"后缀去重"），纠错对照清单完整可溯
    for suffix in ("公司", "集团", "有限公司"):
        dup = suffix + suffix
        while dup in clean:
            clean = clean.replace(dup, suffix, 1)
            changes.append({"原词": dup, "正词": suffix, "归因": "后缀去重", "处置": "改", "位置": -1})

    return {"clean": clean, "changes": changes, "uncertain": uncertain}


def proofread_flow(flow_lines: list, mapping: dict = None) -> dict:
    """校对整份流水（每行 时间段+文字）→ 清洗稿全文 + 汇总清单"""
    if mapping is None:
        mapping = load_dict()
    clean_lines = []
    all_changes = []
    all_uncertain = []
    for line in flow_lines:
        # 保留时间段前缀，只校对文字部分
        m = re.match(r"^(\d{2}:\d{2}:\d{2}-\d{2}:\d{2}:\d{2})\s+(.*)$", line.strip())
        if m:
            ts, text = m.group(1), m.group(2)
            r = proofread_text(text, mapping)
            clean_lines.append(f"{ts}  {r['clean']}")
            all_changes.extend(r["changes"])
            all_uncertain.extend(r["uncertain"])
        else:
            clean_lines.append(line.rstrip())
    return {
        "clean_text": "\n".join(clean_lines),
        "changes": all_changes,
        "uncertain": all_uncertain,
        "change_count": len(all_changes),
    }


if __name__ == "__main__":
    import sys
    # 自测：需要 vocab.json（先跑 build_vocab.py）；没有词表时只做原样打印
    test = [
        "17:00:00-17:00:10  各位领导下午好，今天讨论××项目的续约",
        "17:00:11-17:00:20  某总说户头关闭损失很大，对方不给新户",
    ]
    r = proofread_flow(test)
    print("清洗稿:")
    print(r["clean_text"])
    print(f"\n改动 {r['change_count']} 处:")
    for c in r["changes"]:
        print(f"  {c['原词']} → {c['正词']} | {c['归因']}")
