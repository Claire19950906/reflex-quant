# -*- coding: utf-8 -*-
"""v453 P2-E16：token 成本账本单测（幂等 / 只记 TOKEN / 按用途分组）。"""

from __future__ import annotations

import os
import sqlite3
import sys
import time

SRC = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "src_python"))
if SRC not in sys.path:
    sys.path.insert(0, SRC)

import cost_ledger


def _conn() -> sqlite3.Connection:
    c = sqlite3.connect(":memory:")
    c.execute("CREATE TABLE ai_call_stats(ts INTEGER, purpose TEXT,"
              " tokens_in INTEGER, tokens_out INTEGER, status TEXT, model_id TEXT)")
    c.execute("CREATE TABLE t2_cost_ledger(id INTEGER PRIMARY KEY, ts INTEGER, day TEXT,"
              " strategy_id INTEGER, category TEXT, amount REAL, currency TEXT, note TEXT)")
    return c


def test_sync_day_writes_tokens_once_per_purpose():
    c = _conn()
    now = int(time.time())
    rows = [(now, "main", 100, 20, "ok", "m1"), (now, "main", 200, 30, "ok", "m1"),
            (now, "gate", 50, 5, "ok", "m2")]
    c.executemany("INSERT INTO ai_call_stats VALUES (?,?,?,?,?,?)", rows)
    n = cost_ledger.sync_day(conn=c)
    assert n == 2  # main / gate 各一行
    got = dict(c.execute("SELECT category, amount FROM t2_cost_ledger").fetchall())
    assert got["llm_tokens:main"] == 350.0
    assert got["llm_tokens:gate"] == 55.0
    # 再跑一次：删旧写新，不重复
    cost_ledger.sync_day(conn=c)
    assert c.execute("SELECT COUNT(*) FROM t2_cost_ledger").fetchone()[0] == 2


def test_sync_day_ignores_zero_token_purpose():
    c = _conn()
    now = int(time.time())
    c.execute("INSERT INTO ai_call_stats VALUES (?,?,0,0,'ok','m')", (now, "noise"))
    assert cost_ledger.sync_day(conn=c) == 0
