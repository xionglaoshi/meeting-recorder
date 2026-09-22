#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""vocab_seed.example.py — 词表种子数据模板

使用方式：把本文件复制成 vocab_seed.py，然后按你们的实际情况填写。

    cp vocab_seed.example.py vocab_seed.py

这些种子会和 WPS 人事库、知识库实体一起被 build_vocab.py 合并成 vocab.json，
进而用于：① ASR 热词（让专有名词被正确识别）② 同音纠错 ③ 简称消歧。

不填也能跑：build_vocab.py 会自动跳过缺失的种子，只用派生数据。

字段说明（除 HIGH_LEVEL 外都是同一种结构）：
    {"term": "规范名称", "variants": ["口语叫法", "错别字形", "英文/拼音"]}
其中 variants 是**会被替换成 term 的写法**——把语音转写里常见的错误写法填进去，
纠错效果最好。
"""

# 1. 核心人员：姓名 → 常见称呼
#    注意：写真实称呼才有纠错价值；本文件不进 Git（见 .gitignore），放本机即可。
HIGH_LEVEL = {
    # "某某某": ["某总", "某老板", "mou mou mou"],
}

# 2. 单位与子公司
COMPANIES = [
    # {"term": "××集团有限公司", "variants": ["××集团", "××"]},
]

# 3. 项目
PROJECTS = [
    # {"term": "××项目", "variants": ["××", "××工程"]},
]

# 4. 业务类型
BUSINESS = [
    # {"term": "砂石开采", "variants": ["开采业务", "砂石矿"]},
]

# 5. 行业词汇（通用名词，跨单位也常用）
INDUSTRY = [
    # {"term": "码头", "variants": ["港口", "港区"]},
]

# 6. 合作方 / 外部实体
PARTNERS = [
    # {"term": "××公司", "variants": ["××", "××有限"]},
]

# 7. 高危同音词：语音最容易听错的词，单独列出来强纠
#    格式比上面多一个 pinyin 字段
HOMOPHONE = [
    # {"pinyin": "shi li ci", "term": "示例词", "variants": ["私利词", "示例词"]},
]

# 8. 超长规范词 → 口语简称
#    ASR 热词有 15 字符上限，规范名超长时得给个口语简称。
#    不列的词条会自动取"第一个变体"，所以只有第一个变体也不够口语时才需要写在这里。
LONG_TERM_ALIAS = {
    # "××省××市××××建筑废弃物资处理有限公司": "××简称",
}
