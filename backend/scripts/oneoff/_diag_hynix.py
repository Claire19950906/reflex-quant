#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""v211 P1-B 诊断：查 海力士 历史脏数据 → 准备迁移。"""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src_python"))

import db

conn = db.get_connection()
print("=== Hynix-related raw_news (最近 10 条) ===")
hynix_news = []
for row in conn.execute(
    "SELECT id, content, gate_main_asset, status, source FROM raw_news "
    "WHERE content LIKE '%海力士%' OR content LIKE '%SK Hynix%' "
    "OR content LIKE '%SKHynix%' OR content LIKE '%000660%' "
    "OR content LIKE '%hynix%' OR content LIKE '%hbm%' "
    "ORDER BY id DESC LIMIT 20"
):
    hynix_news.append(row)
    print(" id={:6d} status={:10s} gate={:8s} src={:20s}".format(
        row[0], row[3] or "", row[2] or "", row[4] or "",
    ))
    print("    content:", (row[1] or "")[:120])
print(" total hynix-related raw_news found (in LIMIT 20):", len(hynix_news))
print()

print("=== unclassified_events 海力士 相关 (NO_MAPPING_FOR_ASSET) ===")
n_uc = 0
for row in conn.execute(
    "SELECT id, news_id, title, target_asset, llm_market_category, reason, layer "
    "FROM unclassified_events "
    "WHERE reason LIKE 'NO_MAPPING_FOR_ASSET%' "
    "  AND (title LIKE '%海力士%' OR title LIKE '%SK Hynix%' OR title LIKE '%SKHynix%' "
    "       OR title LIKE '%hynix%' OR title LIKE '%HBM%' OR title LIKE '%000660%' "
    "       OR title LIKE '%三星%' OR title LIKE '%美光%' OR title LIKE '%台积电%')"
):
    n_uc += 1
    print(" id={:4d} news_id={:6d} target={:8s} cat={:8s}".format(
        row[0], row[1], row[3] or "?", row[4] or "?",
    ))
    print("   reason={} layer={}".format(row[5] or "", row[6] or ""))
    print("   title:", (row[2] or "")[:120])
print(" total unclassified hynix-related:", n_uc)
print()

print("=== unclassified_events 总样本 (NO_MAPPING, sample 5) ===")
for row in conn.execute(
    "SELECT reason, COUNT(*) FROM unclassified_events "
    "WHERE reason LIKE 'NO_MAPPING%' GROUP BY reason ORDER BY 2 DESC LIMIT 5"
):
    print(" reason={:30s} count={}".format(row[0] or "", row[1]))
print()

print("=== ai_decisions 中 海力士 相关 (最近 10) ===")
for row in conn.execute(
    "SELECT id, news_id, target_asset, market_category, status, reasoning "
    "FROM ai_decisions "
    "WHERE reasoning LIKE '%海力士%' OR reasoning LIKE '%SK Hynix%' "
    "OR reasoning LIKE '%000660%' OR reasoning LIKE '%HBM%' "
    "ORDER BY id DESC LIMIT 10"
):
    print(" id={:6d} news_id={:6d} target={:10s} cat={:10s} status={}".format(
        row[0], row[1], row[2] or "?", row[3] or "?", row[4] or "?",
    ))
    print("    reasoning:", (row[5] or "")[:80])
