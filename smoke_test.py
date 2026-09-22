#!/usr/bin/env python3
"""smoke_test：会议记录员环境自检（环境层/模块层/链路层 三级）

用法：python3 smoke_test.py
全过 → 服务可正常使用；任一失败 → 按提示修复。
"""
import importlib
import json
import os
import sys
import traceback

ROOT = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, ROOT)

PASS = "✅"
FAIL = "❌"
results = []


def check(name, fn):
    try:
        fn()
        results.append((PASS, name))
    except Exception as e:
        results.append((FAIL, f"{name}: {e}"))
        traceback.print_exc()


# ── 环境层 ──────────────────────────────────
def env_python_deps():
    importlib.import_module("pypinyin")
    importlib.import_module("dashscope")
    importlib.import_module("fastapi")
    importlib.import_module("docx")
    importlib.import_module("openpyxl")


def env_ffmpeg():
    import subprocess
    subprocess.run(["ffmpeg", "-version"], capture_output=True, check=True, timeout=15)


def env_api_keys():
    from engine import load_api_key, load_ds_api_key
    load_api_key()      # 百炼（ASR）
    load_ds_api_key()   # DeepSeek（纪要 LLM）


# ── 模块层 ──────────────────────────────────
def mod_dict():
    from engine import load_vocab
    v = load_vocab()
    cats = v.get("categories", {})
    assert len(cats.get("person", [])) > 100, "字典 person 过少"
    assert len(cats.get("homophone", [])) > 0, "缺同音词层"
    assert len(cats.get("disambiguation", [])) > 0, "缺消歧层"


def mod_proofread():
    from proofread import proofread_flow
    from engine import load_vocab
    # 用词表里真实存在的同音词层做验证；词表为空则跳过（不误报）
    hw = load_vocab().get("categories", {}).get("homophone", [])
    if not hw:
        print("   （跳过：词表无同音词层，先跑 build_vocab.py）")
        return
    item = hw[0]
    term, variant = item.get("term", ""), (item.get("variants") or [""])[0]
    r = proofread_flow([f"17:00:00-17:00:10  今天讨论了{variant}的进展"])
    assert term in r["clean_text"], f"专名未纠正（应出现 {term}）"


def mod_materials():
    from materials import parse_material
    txt = parse_material(os.path.join(ROOT, "vocab.json"), 200)
    assert txt, "材料解析异常"


def mod_new_features():
    from engine import MeetingRecorder
    for m in ("_make_minutes_mapreduce", "_make_by_template", "_identify_speakers",
              "add_materials", "get_materials"):
        assert hasattr(MeetingRecorder, m), f"缺方法 {m}"
    from server import router
    paths = [getattr(r, "path", "") for r in router.routes]
    for p in ("/materials", "/speakers", "/speakers/apply"):
        assert any(p == str(x) for x in paths), f"缺端点 {p}"


def mod_entity_verify():
    from verify_entities import verify_text, apply_verified, extract_entities
    from engine import load_vocab
    # 用词表里真实存在的实体词做核验；词表为空（未配置种子数据）则跳过，不误报
    v = load_vocab()
    terms = [it.get("term") for cat in ("company", "project", "partner")
             for it in v.get("categories", {}).get(cat, []) if it.get("term")]
    if not terms:
        print("   （跳过：vocab.json 无实体词，先跑 build_vocab.py）")
        return
    term = terms[0]
    r = verify_text(f"今天讨论了{term}的进展")
    assert r.get("verified"), "实体核验返回异常"
    out = apply_verified(f"{term}今天推进", {term: "示例标准名"})
    assert "示例标准名" in out, "确证替换失败"


def mod_vocab_sync():
    from engine import get_vocab_id
    vid = get_vocab_id()
    assert vid, "L1 热词表未创建（跑 python3 sync_vocab.py）"


def mod_build_vocab():
    import subprocess
    r = subprocess.run([sys.executable, os.path.join(ROOT, "build_vocab.py")],
                       capture_output=True, timeout=120)
    assert "vocab.json 已生成" in r.stdout.decode(), "build_vocab 失败"


def env_qwen_asr():
    """qwen-audio 引擎就绪：ASR_MODEL 指向新模型 + 热词生成非空 + 客户端可导入"""
    import importlib.util
    spec = importlib.util.find_spec("qwen_asr")
    assert spec, "qwen_asr.py 不存在"
    from engine import ASR_MODEL, _build_hotwords
    assert "qwen-audio" in ASR_MODEL, f"ASR_MODEL 未切换 qwen-audio: {ASR_MODEL}"
    hw = _build_hotwords()
    assert len(hw) > 0, "热词生成为空（先跑 build_vocab.py 生成 vocab.json）"


# ── 链路层 ──────────────────────────────────
def link_check_vocab():
    import subprocess
    r = subprocess.run([sys.executable, os.path.join(ROOT, "check_vocab.py")],
                       capture_output=True, timeout=120)
    out = r.stdout.decode()
    assert "无真问题" in out or "全部通过" in out, f"字典校验未通过: {out[-200:]}"


def main():
    print("=" * 60)
    print("会议记录员 自检（smoke_test v1.0）")
    print("=" * 60)

    print("\n── 环境层 ──")
    check("Python 依赖（pypinyin/dashscope/fastapi/docx/openpyxl）", env_python_deps)
    check("ffmpeg 可用", env_ffmpeg)
    check("API Key（百炼 + DeepSeek）", env_api_keys)
    check("qwen-audio ASR 引擎就绪（模型/热词/客户端）", env_qwen_asr)

    print("\n── 模块层 ──")
    check("字典结构（person/homophone/disambiguation）", mod_dict)
    check("L2 校对模块（专名纠正）", mod_proofread)
    check("材料解析模块", mod_materials)
    check("字典生成器 build_vocab.py", mod_build_vocab)
    check("L1 热词表已同步（sync_vocab）", mod_vocab_sync)
    check("实体核验模块（J 三级匹配）", mod_entity_verify)
    check("新功能（MapReduce/模板/说话人/材料端点）", mod_new_features)

    print("\n── 链路层 ──")
    check("字典校验 check_vocab.py", link_check_vocab)

    print("\n" + "=" * 60)
    fails = [r for r in results if r[0] == FAIL]
    for status, name in results:
        print(f"  {status} {name}")
    print("=" * 60)
    if fails:
        print(f"\n共 {len(fails)} 项失败，请修复后重跑。")
        sys.exit(1)
    print("\n🎉 全部通过！服务可正常使用。")
    sys.exit(0)


if __name__ == "__main__":
    main()
