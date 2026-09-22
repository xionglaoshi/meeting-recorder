#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""hr_sources.example.py — 你自己的花名册类表格清单（模板）

用法：复制成本目录的 hr_sources.py，按你们实际的导出文件改。

    cp hr_sources.example.py hr_sources.py

hr_sources.py **不进 Git**（见 .gitignore）：里面会有你公司的表名与目录，属于内部信息。
不建这个文件也能跑：hr_etl.py 会退回它自带的示例清单（文件都不存在 → 全部跳过）。

kind 取值：
  attendance    考勤明细 / 花名册：含姓名、部门、岗位、入职日期
  assessment    部门月度考核表：多月在一册（每 sheet 一个月）
  daily_report  日报提交情况：姓名 × 日期
  daily_review  日报审核情况：部门 × 审核人 × 日期

列名不用严格对齐：解析时按关键字匹配表头（姓名/部门/岗位/入职…），
表头风格不一致也能吃；完全对不上时，改 hr_etl.py 里对应的 parse_* 函数。
"""

# 数据源目录（可写 ~/…；不写则用 $MEETING_HR_SRC_DIR 或本技能目录下的 roster/）
SRC_DIR = "~/wps/my-company/人事导出"

# 你的表：(文件名, 类型, 年份)
FILES = [
    ("2026年花名册.xlsx",           "attendance",   2026),
    ("2026年度部门月度考核表.xlsx",  "assessment",   2026),
    ("26年日报汇总.xls",            "daily_report", 2026),
    ("26年日报审核.xls",            "daily_review", 2026),
]

# 可选：某文件只导入 1-N 月（表内混进了往年复制的 sheet 时用）
DAILY_REPORT_MAX_MONTH = {}
