#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""v211 P1-B 诊断3：710 NO_MAPPING_FOR_ASSET:NONE 是关于什么的？"""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src_python"))

import db

conn = db.get_connection()
print("=== 710 NO_MAPPING — 最近 30 条 title 摘要 ===")
n = 0
for row in conn.execute(
    "SELECT ue.id, ue.news_id, ue.title, ue.llm_target_asset, ue.llm_market_category, "
    "       rn.gate_main_asset, rn.gate_assets "
    "FROM unclassified_events ue "
    "LEFT JOIN raw_news rn ON ue.news_id = rn.id "
    "WHERE ue.reason='NO_MAPPING_FOR_ASSET:NONE' AND ue.layer='migration' "
    "ORDER BY ue.id DESC LIMIT 30"
):
    n += 1
    print(" id={:5d} news_id={:6d} llm_t={:8s} llm_cat={:8s} gate_main={:8s}".format(
        row[0], row[1] or 0, row[3] or "?", row[4] or "?",
        row[5] or "?",
    ))
    print("   title:", (row[2] or "")[:100])
print(" shown:", n)
print()

# 看看哪些 gate_main_asset 出现最多
print("=== 710 NO_MAPPING — gate_main_asset 分布 ===")
for row in conn.execute(
    "SELECT rn.gate_main_asset, COUNT(*) "
    "FROM unclassified_events ue LEFT JOIN raw_news rn ON ue.news_id = rn.id "
    "WHERE ue.reason='NO_MAPPING_FOR_ASSET:NONE' AND ue.layer='migration' "
    "GROUP BY rn.gate_main_asset ORDER BY 2 DESC LIMIT 20"
):
    print(" gate_main={:10s} count={}".format(row[0] or "?", row[1]))
