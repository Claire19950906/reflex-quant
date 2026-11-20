#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""v211 P1-B 诊断2：深度查 710 NO_MAPPING_FOR_ASSET 是什么 ticker。"""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src_python"))

import db

conn = db.get_connection()
print("=== unclassified_events NO_MAPPING_FOR_ASSET — llm_target_asset 分布 ===")
for row in conn.execute(
    "SELECT llm_target_asset, COUNT(*) FROM unclassified_events "
    "WHERE reason='NO_MAPPING_FOR_ASSET:NONE' GROUP BY llm_target_asset ORDER BY 2 DESC LIMIT 30"
):
    print(" target={:10s} count={}".format(row[0] or "?", row[1]))
print()

print("=== unclassified_events NO_MAPPING_FOR_ASSET:NONE — layer 分布 ===")
for row in conn.execute(
    "SELECT layer, COUNT(*) FROM unclassified_events "
    "WHERE reason='NO_MAPPING_FOR_ASSET:NONE' GROUP BY layer ORDER BY 2 DESC"
):
    print(" layer={:20s} count={}".format(row[0] or "?", row[1]))
print()

print("=== 海力士的 ai_decisions 实际样子 (id=1000476, news_id=9488) ===")
for row in conn.execute(
    "SELECT ad.id, ad.news_id, ad.target_asset, ad.market_category, ad.status, ad.reasoning, rn.content "
    "FROM ai_decisions ad LEFT JOIN raw_news rn ON ad.news_id = rn.id "
    "WHERE ad.target_asset='SKHYNIX' ORDER BY ad.id DESC LIMIT 10"
):
    print(" id={} news_id={} target={} cat={} status={}".format(
        row[0], row[1], row[2], row[3], row[4]))
    print("    reasoning:", (row[5] or "")[:200])
    print("    raw_news:", (row[6] or "")[:200])
print()

# 找历史上 LLM 写过 NONE 但内容含 海力士的 ai_decisions
print("=== LLM 写 NONE 但内容含海力士的 ai_decisions (历史脏数据) ===")
n = 0
for row in conn.execute(
    "SELECT ad.id, ad.news_id, ad.target_asset, ad.market_category, ad.status, ad.reasoning, rn.content "
    "FROM ai_decisions ad LEFT JOIN raw_news rn ON ad.news_id = rn.id "
    "WHERE ad.target_asset='NONE' AND rn.content LIKE '%海力士%'"
):
    n += 1
    print(" id={} news_id={} target={} cat={} status={}".format(
        row[0], row[1], row[2], row[3], row[4]))
    print("    reasoning:", (row[5] or "")[:200])
    print("    raw_news:", (row[6] or "")[:200])
print(" count:", n)
print()

print("=== LLM 写 OTHER 但内容含海力士的 ai_decisions ===")
n = 0
for row in conn.execute(
    "SELECT ad.id, ad.news_id, ad.target_asset, ad.market_category, ad.status, ad.reasoning, rn.content "
    "FROM ai_decisions ad LEFT JOIN raw_news rn ON ad.news_id = rn.id "
    "WHERE ad.target_asset='OTHER' AND rn.content LIKE '%海力士%'"
):
    n += 1
    print(" id={} news_id={} target={} cat={} status={}".format(
        row[0], row[1], row[2], row[3], row[4]))
    print("    reasoning:", (row[5] or "")[:200])
    print("    raw_news:", (row[6] or "")[:200])
print(" count:", n)
