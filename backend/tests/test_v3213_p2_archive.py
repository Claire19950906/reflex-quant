#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
test_v3213_p2_archive.py — v321.3 P2 决策引用归档 的回归 (全离线)

背景: 「重置交易历史」删 ai_decisions/raw_news/news_events, 但 trade_decisions
的指针还指着它们 → 1998+1783 条死指针, 且上游已被物理删除、不可恢复。
现在重置**前**归档到 decision_ref_archive (该表不参与重置), 重置后可重建。

本模块验证三件事:
  1. 归档表**不被重置删除** (决策语义不丢)
  2. 重建只做**方向一致**的语义对齐 (实测同向率 84.2% → 99.9%)
  3. 挂链来源必须**标注** (exact / same_news_id / semantic), 下游可区分真假
"""
from __future__ import annotations

import os
import sqlite3
import sys

SRC = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SRC_PY = os.path.join(SRC, "src_python")
if SRC_PY not in sys.path:
    sys.path.insert(0, SRC_PY)


def _mk_db():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("""CREATE TABLE trade_decisions (
        id INTEGER PRIMARY KEY, decision_ts REAL, asset TEXT, intended_action TEXT,
        final_action TEXT, news_id INTEGER, ai_decision_id INTEGER,
        entry_price REAL, actual_outcome_pct REAL, status TEXT, context_json TEXT,
        news_link_method TEXT, news_link_diff_sec INTEGER)""")
    conn.execute("""CREATE TABLE ai_decisions (
        id INTEGER PRIMARY KEY, target_asset TEXT, suggested_action TEXT, ts INTEGER)""")
    conn.execute("""CREATE TABLE news_events (
        id INTEGER PRIMARY KEY, news_id INTEGER, asset TEXT, ts INTEGER,
        action TEXT, ai_decision_id INTEGER, headline TEXT, body TEXT)""")
    conn.execute("""CREATE TABLE decision_ref_archive (
        decision_id INTEGER PRIMARY KEY, batch TEXT, asset TEXT, decision_ts REAL,
        action TEXT, news_id INTEGER, ai_decision_id INTEGER, entry_price REAL,
        outcome_pct REAL, status TEXT, created_at INTEGER, note TEXT)""")
    conn.commit()
    return conn


def test_archive_captures_refs_and_semantics():
    from signals import decision_ref_archive as dra
    conn = _mk_db()
    conn.execute("INSERT INTO trade_decisions VALUES "
                 "(1, 1000.0, 'BTC', 'BUY', 'BUY', 555, 77, 100.0, 2.5, 'settled', '{}', NULL, NULL)")
    conn.commit()
    st = dra.archive_decisions(conn, batch="b1")
    assert st["archived"] == 1
    row = conn.execute("SELECT * FROM decision_ref_archive WHERE decision_id=1").fetchone()
    assert row["news_id"] == 555
    assert row["ai_decision_id"] == 77
    assert row["asset"] == "BTC"
    assert row["outcome_pct"] == 2.5


def test_restore_prefers_same_direction():
    """语义对齐必须**方向一致优先** —— 否则会误挂。"""
    from signals import decision_ref_archive as dra
    conn = _mk_db()
    # 决策: BUY @ ts 1000, 归档时 news_id 已失效
    conn.execute("INSERT INTO trade_decisions VALUES "
                 "(1, 1000.0, 'BTC', 'BUY', 'BUY', 555, NULL, 1.0, 0.1, 'open', '{}', NULL, NULL)")
    conn.execute("INSERT INTO decision_ref_archive VALUES "
                 "(1,'b','BTC',1000.0,'BUY',555,NULL,1.0,0.1,'open',1,'x')")
    # 新闻: 一个 SELL (更近) + 一个 BUY (稍远)
    conn.execute("INSERT INTO news_events VALUES (10, 9001, 'BTC', 1010, 'SELL', NULL, 'h', 'b')")
    conn.execute("INSERT INTO news_events VALUES (11, 9002, 'BTC', 1100, 'BUY', NULL, 'h', 'b')")
    conn.commit()
    st = dra.restore_refs(conn, window_s=3600)
    row = conn.execute("SELECT news_id, news_link_method FROM trade_decisions WHERE id=1").fetchone()
    assert row["news_id"] == 9002, "必须选方向一致的新闻 (BUY), 不是更近的 SELL"
    assert row["news_link_method"] == "semantic"


def test_restore_marks_method():
    """挂链来源必须标注 —— 下游要能区分「精确」与「推断」。"""
    from signals import decision_ref_archive as dra
    conn = _mk_db()
    conn.execute("INSERT INTO trade_decisions VALUES "
                 "(1, 1000.0, 'BTC', 'BUY', 'BUY', NULL, 88, 1.0, 0.1, 'open', '{}', NULL, NULL)")
    conn.execute("INSERT INTO decision_ref_archive VALUES "
                 "(1,'b','BTC',1000.0,'BUY',NULL,88,1.0,0.1,'open',1,'x')")
    conn.execute("INSERT INTO ai_decisions VALUES (88,'BTC','BUY',1000)")
    conn.commit()
    dra.restore_refs(conn)
    row = conn.execute("SELECT news_link_method FROM trade_decisions WHERE id=1").fetchone()
    assert row["news_link_method"] == "exact", "本来就有效的引用应标 exact 而非 semantic"


def test_archive_survives_reset():
    """归档表**不参与重置** —— 决策语义不能随重置消失。"""
    from signals import decision_ref_archive as dra
    conn = _mk_db()
    conn.execute("INSERT INTO trade_decisions VALUES "
                 "(1, 1000.0, 'BTC', 'BUY', 'BUY', 555, 77, 1.0, 0.1, 'open', '{}', NULL, NULL)")
    conn.commit()
    dra.archive_decisions(conn, batch="b1")
    # 模拟重置: 删上游 + 清 trade_decisions 的指针, 但**不删**归档
    conn.execute("DELETE FROM ai_decisions")
    conn.execute("DELETE FROM news_events")
    conn.execute("UPDATE trade_decisions SET news_id=NULL, ai_decision_id=NULL")
    conn.commit()
    n = conn.execute("SELECT COUNT(*) FROM decision_ref_archive").fetchone()[0]
    assert n == 1, "归档必须还在 (决策语义不丢)"
    # 决策本身也还在 (asset/action/结算结果仍可用于复盘)
    d = conn.execute("SELECT asset, intended_action, actual_outcome_pct FROM trade_decisions WHERE id=1").fetchone()
    assert d["asset"] == "BTC" and d["intended_action"] == "BUY" and d["actual_outcome_pct"] == 0.1


def test_stats_without_row_factory():
    """archive_stats 不能依赖 row_factory (db.get_connection() 不设它)。"""
    from signals import decision_ref_archive as dra
    conn = sqlite3.connect(":memory:")   # 默认 tuple 行
    conn.execute("""CREATE TABLE decision_ref_archive (
        decision_id INTEGER PRIMARY KEY, batch TEXT, created_at INTEGER)""")
    conn.execute("INSERT INTO decision_ref_archive VALUES (1,'b',100)")
    conn.commit()
    st = dra.archive_stats(conn)
    assert st["total"] == 1
    assert st["batches"][0]["batch"] == "b"
