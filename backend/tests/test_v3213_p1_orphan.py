#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
test_v3213_p1_orphan.py — v321.3 P1 孤儿引用清理/恢复 的回归 (全离线)

背景: 「重置交易历史」会删 ai_decisions / raw_news / news_events, 但
trade_decisions 的两个指针还指着它们 → 1998 条死指针 (实测)。
本模块的三个关键性质:
  1. ai_decision_id 指向已删行 → 必须置 NULL
  2. news_id 的孤儿判定**必须用 news_events.news_id 的值相等**, 不能用
     raw_news.id 的存在性 —— 用错了会误清 1780 条本来可追溯的决策
  3. 清理后要能把误清的 news_id 重建回来 (按 asset + 时间窗)
"""
from __future__ import annotations

import os
import sys

SRC = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SRC_PY = os.path.join(SRC, "src_python")
if SRC_PY not in sys.path:
    sys.path.insert(0, SRC_PY)


def _mk_db():
    import sqlite3
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("""CREATE TABLE ai_decisions (
        id INTEGER PRIMARY KEY, target_asset TEXT, suggested_action TEXT, ts INTEGER)""")
    conn.execute("""CREATE TABLE raw_news (
        id INTEGER PRIMARY KEY, content TEXT)""")
    conn.execute("""CREATE TABLE news_events (
        id INTEGER PRIMARY KEY, news_id INTEGER, asset TEXT, ts INTEGER,
        action TEXT, ai_decision_id INTEGER)""")
    conn.execute("""CREATE TABLE trade_decisions (
        id INTEGER PRIMARY KEY, decision_ts REAL, asset TEXT, intended_action TEXT,
        news_id INTEGER, ai_decision_id INTEGER, context_json TEXT)""")
    conn.commit()
    return conn


def test_orphan_ai_decision_id_is_cleared():
    import repair_orphan_decision_refs as rp
    conn = _mk_db()
    conn.execute("INSERT INTO ai_decisions VALUES (1,'BTC','BUY',1000)")
    conn.execute("INSERT INTO trade_decisions VALUES (1,1000,'BTC','BUY',NULL,999,'{}')")
    conn.execute("INSERT INTO news_events VALUES (1,50,'BTC',1000,'BUY',1)")
    conn.commit()
    st = rp.run(conn)
    assert st["orphan_ai_decision_id"] == 1
    assert st["remaining_orphan_ai_decision_id"] == 0
    v = conn.execute("SELECT ai_decision_id FROM trade_decisions WHERE id=1").fetchone()[0]
    assert v is None, "指向已删 ai_decisions 的指针必须置 NULL"


def test_news_id_kept_when_news_events_still_has_same_value():
    """**最关键的一条**: raw_news 行被删 ≠ news_id 指针失效。

    追溯靠的是 `news_events.news_id = trade_decisions.news_id` 的**值相等**,
    不依赖 raw_news 行还在。所以这种情况**不能清** news_id。
    """
    import repair_orphan_decision_refs as rp
    conn = _mk_db()
    conn.execute("INSERT INTO ai_decisions VALUES (1,'BTC','BUY',1000)")
    conn.execute("INSERT INTO news_events VALUES (1,50,'BTC',1000,'BUY',1)")
    # raw_news 里**没有** id=50 (被重置删了), 但 news_events.news_id 仍是 50
    conn.execute("INSERT INTO trade_decisions VALUES (1,1000,'BTC','BUY',50,1,'{}')")
    conn.commit()
    st = rp.run(conn)
    assert st["orphan_news_id"] == 0, "news_events 里还有同值 → 不该判为孤儿"
    v = conn.execute("SELECT news_id FROM trade_decisions WHERE id=1").fetchone()[0]
    assert v == 50, "news_id 必须保留 (值相等仍可追溯)"
    assert st["trace_pct"] == 100.0


def test_news_id_cleared_when_value_gone_everywhere():
    import repair_orphan_decision_refs as rp
    conn = _mk_db()
    conn.execute("INSERT INTO ai_decisions VALUES (1,'BTC','BUY',1000)")
    conn.execute("INSERT INTO trade_decisions VALUES (1,1000,'BTC','BUY',777,1,'{}')")
    conn.commit()
    st = rp.run(conn)
    assert st["orphan_news_id"] == 1
    v = conn.execute("SELECT news_id FROM trade_decisions WHERE id=1").fetchone()[0]
    assert v is None


def test_restore_news_id_by_asset_and_time():
    """被误清的 news_id 要能按 (asset, 时间窗) 从 news_events 找回来。"""
    import repair_orphan_decision_refs as rp
    conn = _mk_db()
    conn.execute("INSERT INTO ai_decisions VALUES (1,'XAU','BUY',1000)")
    conn.execute("INSERT INTO news_events VALUES (1,9001,'XAU',100030,'BUY',1)")
    # 决策: news_id 已被清空, 带 upstream_cleared 标记
    conn.execute("INSERT INTO trade_decisions VALUES (1,100000,'XAU','BUY',NULL,1,"
                 "'{\"upstream_cleared_at\":1}')")
    conn.commit()
    n = rp._restore_news_refs(conn)
    assert n == 1
    v = conn.execute("SELECT news_id FROM trade_decisions WHERE id=1").fetchone()[0]
    assert v == 9001


def test_repair_is_idempotent():
    import repair_orphan_decision_refs as rp
    conn = _mk_db()
    conn.execute("INSERT INTO ai_decisions VALUES (1,'BTC','BUY',1000)")
    conn.execute("INSERT INTO news_events VALUES (1,50,'BTC',1000,'BUY',1)")
    conn.execute("INSERT INTO trade_decisions VALUES (1,1000,'BTC','BUY',50,999,'{}')")
    conn.commit()
    s1 = rp.run(conn)
    s2 = rp.run(conn)
    assert s2["orphan_ai_decision_id"] == 0
    assert s2["remaining_orphan_ai_decision_id"] == 0
    assert s2["restored_news_id"] == 0
    assert s1["trace_pct"] == s2["trace_pct"] == 100.0
