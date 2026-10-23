# -*- coding: utf-8 -*-
"""test_fresh_signal_count.py \u2014 v300+ \u300c\u4eca\u65e5\u4fe1\u53f7\u300d\u65b0\u9c9c\u5ea6\u7a97\u53e3\u5355\u6d4b\u3002

LLM/\u6570\u636e\u6e90\u5168\u6302\u65f6\uff0c\u65e7\u903b\u8f91\u628a\u65e9\u4e0a\u7559\u4e0b\u7684 BUY/SELL
\u4e00\u8d77\u7b97\u8fdb\u300c\u4eca\u65e5\u4fe1\u53f7\u300d\u3002FRESH_SIGNAL_HOURS \u5f15\u5165\u540e\uff1a
  - signal_count   = \u5168\u5929 BUY/SELL \u603b\u6570
  - live_signal_count = \u4ec5\u6700\u8fd1 N \u5c0f\u65f6 BUY/SELL
"""
from __future__ import annotations

import os
import sqlite3
import sys
from datetime import datetime, timedelta, timezone

import pytest

SRC = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "src_python"))
if SRC not in sys.path:
    sys.path.insert(0, SRC)


def _open_db_with_ai_decisions():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    import db as _db
    _db.migrate(conn)
    return conn


def _insert_decision(conn, suggested_action, hours_ago, news_id=1):
    ts = (datetime.now(timezone.utc) - timedelta(hours=hours_ago)).strftime("%Y-%m-%d %H:%M:%S")
    conn.execute(
        """INSERT INTO ai_decisions
           (news_id, sentiment_score, suggested_action, reasoning, created_at,
            market_category, target_asset)
           VALUES (?, ?, ?, ?, ?, ?, ?)""",
        (news_id, 0.5, suggested_action, f"r {news_id}", ts, "CRYPTO", "BTC"),
    )


def _build_sig_cond(extra_hours=0):
    sig_cond = "suggested_action IN ('BUY','SELL')"
    sig_params = []
    live_sig_cond = sig_cond
    live_sig_params = []
    if extra_hours > 0:
        live_sig_cond += " AND created_at >= datetime('now', ?)"
        live_sig_params.append(f"-{extra_hours} hours")
    else:
        live_sig_params = sig_params
    return sig_cond, sig_params, live_sig_cond, live_sig_params


def test_stale_excluded_from_live():
    conn = _open_db_with_ai_decisions()
    try:
        for i in range(5):
            _insert_decision(conn, "BUY", hours_ago=6, news_id=i + 1)
        for i in range(2):
            _insert_decision(conn, "BUY", hours_ago=1, news_id=10 + i)
        conn.commit()
        sc, sp, lc, lp = _build_sig_cond(extra_hours=4)
        total = conn.execute(f"SELECT COUNT(*) FROM ai_decisions WHERE {sc}", sp).fetchone()[0]
        live = conn.execute(f"SELECT COUNT(*) FROM ai_decisions WHERE {lc}", lp).fetchone()[0]
        assert total == 7
        assert live == 2
    finally:
        conn.close()


def test_hold_failed_never_counted():
    conn = _open_db_with_ai_decisions()
    try:
        _insert_decision(conn, "HOLD", hours_ago=1)
        _insert_decision(conn, "FAILED", hours_ago=1)
        _insert_decision(conn, "HOLD", hours_ago=10)
        _insert_decision(conn, "BUY", hours_ago=1)
        _insert_decision(conn, "SELL", hours_ago=10)
        conn.commit()
        sc, sp, lc, lp = _build_sig_cond(extra_hours=4)
        total = conn.execute(f"SELECT COUNT(*) FROM ai_decisions WHERE {sc}", sp).fetchone()[0]
        live = conn.execute(f"SELECT COUNT(*) FROM ai_decisions WHERE {lc}", lp).fetchone()[0]
        assert total == 2
        assert live == 1
    finally:
        conn.close()


def test_fresh_zero_when_llm_down():
    """\u7528\u6237\u573a\u666f\uff1a36 \u6761 BUY \u90fd\u662f 5h \u524d\u7684\u65e9\u573a\u4fe1\u53f7\uff0c
    LLM \u73b0\u5728\u5168\u6302 \u2192 live_signal_count \u5e94\u5f52\u96f6\u3002"""
    conn = _open_db_with_ai_decisions()
    try:
        for i in range(36):
            _insert_decision(conn, "BUY", hours_ago=5, news_id=i + 1)
        conn.commit()
        sc, sp, lc, lp = _build_sig_cond(extra_hours=4)
        total = conn.execute(f"SELECT COUNT(*) FROM ai_decisions WHERE {sc}", sp).fetchone()[0]
        live = conn.execute(f"SELECT COUNT(*) FROM ai_decisions WHERE {lc}", lp).fetchone()[0]
        assert total == 36, "all-day stays"
        assert live == 0, "fresh window empty \u2192 live should be 0"
    finally:
        conn.close()


def test_fresh_disabled_means_no_filter():
    conn = _open_db_with_ai_decisions()
    try:
        for i in range(10):
            _insert_decision(conn, "BUY", hours_ago=24, news_id=i + 1)
        conn.commit()
        sc, sp, lc, lp = _build_sig_cond(extra_hours=0)
        total = conn.execute(f"SELECT COUNT(*) FROM ai_decisions WHERE {sc}", sp).fetchone()[0]
        live = conn.execute(f"SELECT COUNT(*) FROM ai_decisions WHERE {lc}", lp).fetchone()[0]
        assert total == live == 10
    finally:
        conn.close()


def test_boundary():
    conn = _open_db_with_ai_decisions()
    try:
        _insert_decision(conn, "BUY", hours_ago=3, news_id=1)
        _insert_decision(conn, "BUY", hours_ago=5, news_id=2)
        conn.commit()
        sc, sp, lc, lp = _build_sig_cond(extra_hours=4)
        total = conn.execute(f"SELECT COUNT(*) FROM ai_decisions WHERE {sc}", sp).fetchone()[0]
        live = conn.execute(f"SELECT COUNT(*) FROM ai_decisions WHERE {lc}", lp).fetchone()[0]
        assert total == 2
        assert live == 1
    finally:
        conn.close()