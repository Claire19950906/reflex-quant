#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""v483：门卫未判定(gate_unavailable=2)不得进纸面台账。"""
from __future__ import annotations

import sqlite3
import sys
import time
from pathlib import Path

_HERE = Path(__file__).resolve().parent
_SRC = _HERE.parent / "src_python"
sys.path.insert(0, str(_SRC))

import config
import db
from quick_sim import pending_signals

_DID = [20000]


def _setup_db() -> sqlite3.Connection:
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    db.migrate(c)
    return c


def _insert(c: sqlite3.Connection, *, gate_unavailable: int) -> int:
    _DID[0] += 1
    did = _DID[0]
    ts = int(time.time())
    c.execute(
        "INSERT INTO raw_news (source, content, published_at, ts, gate_unavailable) "
        "VALUES ('t', 'c', '', ?, ?)",
        (ts, gate_unavailable),
    )
    news_id = c.execute("SELECT last_insert_rowid()").fetchone()[0]
    c.execute(
        """INSERT INTO ai_decisions
           (id, news_id, sentiment_score, suggested_action, reasoning,
            target_asset, evidence_confidence, trade_gate_reason,
            event_strength, event_phase, evidence_action, status, created_at)
           VALUES (?, ?, 0.0, 'BUY', '', 'XAU', 30.0, ?, 'medium', 'mid', 'BUY',
                   'AUTO_APPROVED', datetime('now', 'localtime'))""",
        (did, news_id, config.TRADE_GATE_PASSED_REASON),
    )
    c.commit()
    return did


def _ids(c: sqlite3.Connection) -> set:
    return {r["id"] for r in pending_signals(c, limit=50)}


def test_gate_unverified_excluded():
    c = _setup_db()
    unverified = _insert(c, gate_unavailable=2)
    assert unverified not in _ids(c)


def test_gate_verified_included():
    c = _setup_db()
    verified = _insert(c, gate_unavailable=0)
    assert verified in _ids(c)
