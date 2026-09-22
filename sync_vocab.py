#!/usr/bin/env python3
"""同步专属词汇字典 → 百炼 ASR 定制热词表（vocabulary_id）

数据源：plugins/meeting-recorder/references/vocab.json
热词范围：company + project + business + industry + partner 的规范词
  （person 人名不进 ASR 热词：406 人挤占 500 限额且提升价值低）
约束：单词 ≤15 字符（非 ASCII），超长用词典首个口语简称替代；每库 ≤500 词
幂等：同 prefix 的热词表已存在 → update（全量替换）；不存在 → create
      （prefix 由 $MEETING_VOCAB_PREFIX 决定，默认 "meeting"；改前缀 = 新建一张表）

产物：vocabulary_id 写回 vocab.json 顶层 meta.asr_vocabulary_id
用法：python3 sync_vocab.py [--target-model qwen-audio-3.0-asr-flash-streaming]
      （解释器由同目录 runtime.py 解析：缺依赖时自动切到依赖齐全的环境）
"""
import argparse
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from runtime import ensure_runtime
ensure_runtime()

import settings
import dashscope
from dashscope.audio.asr import VocabularyService

# 超长规范词的简称替代（ASR 热词 15 字符上限）。
# 具体映射属**本机个性化数据**，放本地 vocab_seed.py，不入库；缺失时自动降级为
# "取该词条的第一个口语变体"（见下方 export_hotwords）。
try:
    from vocab_seed import LONG_TERM_ALIAS
except ImportError:
    LONG_TERM_ALIAS = {}

# 接入点与热词表前缀都不写死：默认官方公共端点 + 通用前缀，
# 用专属部署/想沿用旧表的人在本机 .env 里配 $DASHSCOPE_BASE_URL / $MEETING_VOCAB_PREFIX
BASE_HTTP = settings.dashscope_api_base()
PREFIX = settings.vocab_prefix()
VOCAB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                          "vocab.json")
MAX_CHARS = 15   # 非 ASCII 字符上限
WEIGHT = 4
LANG = "zh"


def load_key():
    """百炼 API key：环境变量优先，其次 settings 的 .env 链。"""
    return settings.require("DASHSCOPE_API_KEY")


def export_hotwords():
    v = json.load(open(VOCAB_PATH, encoding="utf-8"))
    cats = v.get("categories", {})
    words = []
    for cat in ("company", "project", "business", "industry", "partner"):
        for it in cats.get(cat, []):
            term = it.get("term", "").strip()
            if not term:
                continue
            if len(term) > MAX_CHARS:
                term = LONG_TERM_ALIAS.get(term) or (it.get("variants") or [""])[0]
            if term and term not in words:
                words.append(term)
            # 口语高频简称也进热词（说话时叫简称比全称多，热词纠正更贴合实际语音）
            for var in (it.get("variants") or []):
                var = var.strip()
                if var and 2 <= len(var) <= MAX_CHARS and var != term and var not in words:
                    words.append(var)
                if len(words) >= 480:   # 留余量防超 500 上限
                    return words
    return words


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--target-model", default="qwen-audio-3.0-asr-flash-streaming",
                    help="ASR 模型（热词表绑定的识别模型）")
    args = ap.parse_args()

    dashscope.base_http_api_url = BASE_HTTP
    dashscope.api_key = load_key()

    words = export_hotwords()
    print(f"[*] 导出热词 {len(words)} 个（company/project/business/industry/partner）")
    if len(words) > 500:
        print(f"[!] 超过 500 上限，截断")
        words = words[:500]

    vocabulary = [{"text": w, "weight": WEIGHT, "lang": LANG} for w in words]
    svc = VocabularyService()

    # 幂等：先按 prefix 查
    existing = svc.list_vocabularies(prefix=PREFIX, page_size=50)
    vid = ""
    for e in existing or []:
        if e.get("status") == "OK":
            vid = e.get("vocabulary_id", "")
            break

    if vid:
        svc.update_vocabulary(vid, vocabulary)
        print(f"[✓] 热词表已更新: {vid}（{len(words)} 词）")
    else:
        vid = svc.create_vocabulary(prefix=PREFIX,
                                    target_model=args.target_model,
                                    vocabulary=vocabulary)
        print(f"[✓] 热词表已创建: {vid}（{len(words)} 词, target={args.target_model}）")

    # 写回 vocab.json 顶层 meta
    v = json.load(open(VOCAB_PATH, encoding="utf-8"))
    v.setdefault("meta", {})
    v["meta"]["asr_vocabulary_id"] = vid
    v["meta"]["asr_target_model"] = args.target_model
    with open(VOCAB_PATH, "w", encoding="utf-8") as f:
        json.dump(v, f, ensure_ascii=False, indent=2)
    print(f"[✓] vocabulary_id 已写回 vocab.json meta: {vid}")

    # 回读校验
    q = svc.query_vocabulary(vid)
    n = len(q.get("vocabulary", []))
    print(f"[✓] 回读校验: status={q.get('status')}, 词数={n}")
    return 0 if q.get("status") == "OK" else 1


if __name__ == "__main__":
    sys.exit(main())
