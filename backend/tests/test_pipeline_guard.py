# -*- coding: utf-8 -*-
"""v420：管线对账守卫的断言用例。

守的是「守卫本身不能说谎」：
  · 该报断链时必须报断链（漏报等于没有守卫）
  · 数据不够时必须报 unknown，不能报 ok（把「不知道」当成「没问题」是这类工具最危险的错法）
"""
from __future__ import annotations

import os
import sqlite3
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pipeline_guard as pg


def _db(with_decisions=True, filtered_clean=True):
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("""CREATE TABLE raw_news (
        id INTEGER PRIMARY KEY, ts INTEGER, source TEXT, status TEXT DEFAULT 'PENDING',
        is_noise INTEGER DEFAULT 0, gate_unavailable INTEGER DEFAULT 0,
        gate_verdict TEXT DEFAULT '', filter_reason TEXT DEFAULT '')""")
    conn.execute("""CREATE TABLE ai_decisions (
        id INTEGER PRIMARY KEY, ts INTEGER, suggested_action TEXT,
        sentiment_score REAL, event_strength REAL, impact_score REAL,
        evidence_confidence REAL, decision_kind TEXT, hard_data_status TEXT,
        reasoning TEXT, structured_check TEXT, heat_score REAL, asset_impact REAL)""")
    conn.execute("""CREATE TABLE source_status (
        source_id TEXT PRIMARY KEY, status TEXT, last_ok_ts TEXT, total_fail INTEGER,
        last_error TEXT)""")
    conn.execute("CREATE TABLE shadow_ledger (decision_id INTEGER)")

    now = int(time.time())
    # 入库：近 1h 有 40 条，近 7 天另有 40 条 → 基线 0.24/h，近 1h 40 应判正常
    for i in range(40):
        conn.execute("INSERT INTO raw_news (ts, status) VALUES (?, 'DONE')", (now - i * 60,))
    for i in range(40):
        conn.execute("INSERT INTO raw_news (ts, status) VALUES (?, 'DONE')", (now - 3 * 86400 - i * 60,))

    if not filtered_clean:
        # 静默归档：没判定、没理由、被 FILTERED
        conn.execute("""INSERT INTO raw_news (ts, status, gate_verdict, filter_reason, is_noise)
                        VALUES (?, 'FILTERED', '', '', 0)""", (now - 600,))
        # 判了可交易却被归档
        conn.execute("""INSERT INTO raw_news (ts, status, gate_verdict, filter_reason, is_noise)
                        VALUES (?, 'FILTERED', '可交易', '', 0)""", (now - 600,))

    if with_decisions:
        for i in range(10):
            conn.execute("""INSERT INTO ai_decisions
                (ts, suggested_action, sentiment_score, event_strength, impact_score,
                 evidence_confidence, decision_kind, hard_data_status)
                VALUES (?, 'HOLD', 0, 0, NULL, 0, 'analysis', 'ok')""", (now - i * 60,))

    conn.execute("""INSERT INTO source_status (source_id, status, last_ok_ts, total_fail, last_error)
                    VALUES ('jin10', 'online', datetime('now','localtime'), 0, '')""")
    conn.commit()
    return conn


def test_clean_pipeline_reports_ok():
    conn = _db(filtered_clean=True)
    rep = pg.run(conn)
    codes = {c["code"]: c for c in rep["checks"]}
    assert codes["silent_archive"]["severity"] == "ok"
    assert codes["tradeable_filtered"]["severity"] == "ok"


def test_silent_archive_is_broken_not_degraded():
    """静默归档是「数据凭空消失」，必须最重级别。"""
    conn = _db(filtered_clean=False)
    rep = pg.run(conn)
    codes = {c["code"]: c for c in rep["checks"]}
    assert codes["silent_archive"]["severity"] == "broken"
    assert codes["silent_archive"]["value"] == 1
    assert codes["tradeable_filtered"]["severity"] == "broken"
    assert rep["overall"] == "broken"


def test_structure_missing_detected():
    """impact_score 全空 = 方向分必为 0，必须报断链。"""
    conn = _db()
    rep = pg.run(conn)
    codes = {c["code"]: c for c in rep["checks"]}
    assert codes["structure_missing"]["severity"] == "broken"
    assert codes["structure_missing"]["value"] == 10


def test_no_data_is_unknown_not_ok():
    """没有决策时要报 unknown，不能因为「没违规」就报正常。"""
    conn = _db(with_decisions=False)
    rep = pg.run(conn)
    codes = {c["code"]: c for c in rep["checks"]}
    assert codes["structure_missing"]["severity"] == "unknown"
    assert codes["routing_miss"]["severity"] == "unknown"


def test_routing_miss_over_threshold():
    conn = _db(with_decisions=False)
    now = int(time.time())
    for i in range(10):
        conn.execute("""INSERT INTO ai_decisions
            (ts, suggested_action, sentiment_score, event_strength, impact_score,
             evidence_confidence, decision_kind, hard_data_status)
            VALUES (?, 'HOLD', 0.2, 0.3, 5.0, 8.0, 'routing_miss', 'missing:none_target')""",
                     (now - i * 60,))
    conn.commit()
    rep = pg.run(conn)
    codes = {c["code"]: c for c in rep["checks"]}
    assert codes["routing_miss"]["severity"] == "degraded"
    assert codes["routing_miss"]["value"] == 10
