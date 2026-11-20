# -*- coding: utf-8 -*-
"""v463：止损触发与存量补挂的测试。"""
import os
import sqlite3
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src_python"))

import pytest
import stops

pytestmark = pytest.mark.smoke  # 快速门禁集


def _c(p):
    c = sqlite3.connect(p)
    c.row_factory = sqlite3.Row
    return c


@pytest.fixture(autouse=True)
def _no_default_time_stop(monkeypatch):
    """隔离 .env 里的 STOPS_DEFAULT_TIME_STOP_MIN（否则老仓会被按 240 分钟强平）。"""
    monkeypatch.setenv("STOPS_DEFAULT_TIME_STOP_MIN", "0")


def _db():
    f = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    f.close()
    p = Path(f.name)
    c = sqlite3.connect(p)
    c.executescript("""
    CREATE TABLE t2_account(id INTEGER PRIMARY KEY, mode TEXT, cash REAL, equity REAL,
                            realized_pnl REAL, fee_bps REAL, updated_at INTEGER);
    CREATE TABLE t2_positions(id INTEGER PRIMARY KEY, asset TEXT, side TEXT, qty REAL,
                              entry_price REAL, entry_ts INTEGER, stop_price REAL,
                              target_price REAL, time_stop_min INTEGER, trailing_bp REAL,
                              thesis_json TEXT, strategy_id TEXT, subaccount TEXT,
                              status TEXT, closed_ts INTEGER, exit_price REAL,
                              realized_pnl REAL, note TEXT);
    CREATE TABLE market_ticks(symbol TEXT, ts INTEGER, price REAL);
    CREATE TABLE kline_bars(symbol TEXT, timeframe TEXT, ts INTEGER, close REAL);
    CREATE TABLE t2_interventions(id INTEGER PRIMARY KEY, ts INTEGER, kind TEXT, target TEXT,
                                  note TEXT, before_json TEXT, after_json TEXT,
                                  pnl_1h_pct REAL, pnl_1d_pct REAL, counterfactual_1h_pct REAL,
                                  counterfactual_1d_pct REAL, settled INTEGER, created_at INTEGER);
    """)
    c.execute("INSERT INTO t2_account VALUES(1,'paper',10000,10000,0,8,0)")
    c.commit()
    c.close()
    return p


def _pos(p, pid, side, entry, stop, qty=10):
    c = sqlite3.connect(p)
    c.execute("INSERT INTO t2_positions(id,asset,side,qty,entry_price,entry_ts,stop_price,status) "
              "VALUES(?, 'XAU', ?, ?, ?, 1, ?, 'open')",
              (pid, side, qty, entry, stop))
    c.commit(); c.close()


def _tick(p, price):
    c = sqlite3.connect(p)
    c.execute("INSERT INTO market_ticks VALUES('XAU', 2, ?)", (price,))
    c.commit(); c.close()


def test_long_stop_closes_at_mark():
    p = _db(); _pos(p, 1, "long", 100, 98); _tick(p, 97)
    c = _c(p)
    out = stops.enforce(c)
    assert len(out) == 1 and out[0]["exit"] == 97
    row = c.execute("SELECT status, exit_price, realized_pnl FROM t2_positions WHERE id=1").fetchone()
    assert row[0] == "closed" and row[2] == -30
    acc = c.execute("SELECT cash, realized_pnl FROM t2_account WHERE id=1").fetchone()
    assert acc[0] == 9970 and acc[1] == -30
    c.close(); p.unlink()


def test_short_stop_closes_at_mark():
    p = _db(); _pos(p, 1, "short", 100, 102); _tick(p, 103)
    c = _c(p)
    out = stops.enforce(c)
    assert len(out) == 1 and out[0]["exit"] == 103
    c.close(); p.unlink()


def test_no_breach_keeps_open():
    p = _db(); _pos(p, 1, "long", 100, 98); _tick(p, 99)
    c = _c(p)
    assert stops.enforce(c) == []
    assert c.execute("SELECT status FROM t2_positions WHERE id=1").fetchone()[0] == "open"
    c.close(); p.unlink()


def test_no_mark_keeps_open():
    p = _db(); _pos(p, 1, "long", 100, 98)
    c = _c(p)
    assert stops.enforce(c) == []
    c.close(); p.unlink()


def test_backfill_long_below_short_above():
    p = _db(); _pos(p, 1, "long", 100, None); _pos(p, 2, "short", 100, None)
    c = _c(p)
    out = stops.backfill(c, 2.0)
    assert len(out) == 2
    assert c.execute("SELECT stop_price FROM t2_positions WHERE id=1").fetchone()[0] == 98.0
    assert c.execute("SELECT stop_price FROM t2_positions WHERE id=2").fetchone()[0] == 102.0
    c.close(); p.unlink()
