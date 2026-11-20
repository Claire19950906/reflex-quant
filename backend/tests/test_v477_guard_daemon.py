# -*- coding: utf-8 -*-
"""v477 止损守护 / 日亏闸 / 对账告警。"""
from __future__ import annotations

import os
import sqlite3
import sys
import tempfile
import time

import pytest

SRC = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "src_python"))
if SRC not in sys.path:
    sys.path.insert(0, SRC)

import account_ledger as al
import stops
import stops_daemon as sd
import trade_preflight as pf
from trident2 import store as t2store

pytestmark = pytest.mark.smoke  # 快速门禁集


_PATHS = {}


def _db(equity: float = 10000.0) -> sqlite3.Connection:
    path = tempfile.NamedTemporaryFile(prefix="trident_guard_", suffix=".db", delete=False).name
    c = sqlite3.connect(path)
    c.row_factory = sqlite3.Row
    t2store.ensure_schema(c)
    c.executescript("""
    CREATE TABLE IF NOT EXISTS market_ticks (symbol TEXT, ts INTEGER, price REAL);
    CREATE TABLE IF NOT EXISTS account_snapshots (
        ts INTEGER PRIMARY KEY, cash REAL DEFAULT 0, realized_pnl REAL DEFAULT 0,
        unrealized REAL DEFAULT 0, equity REAL DEFAULT 0, gross_exposure REAL DEFAULT 0,
        leverage_pct REAL DEFAULT 0, n_open INTEGER DEFAULT 0, peak_equity REAL DEFAULT 0,
        drawdown_pct REAL DEFAULT 0, note TEXT DEFAULT ''
    );
    """)
    c.execute("UPDATE t2_account SET cash=?, equity=?, realized_pnl=0, fee_bps=8", (equity, equity))
    c.commit()
    _PATHS[id(c)] = path
    return c


def _tick(c, asset, price, age=0):
    c.execute("INSERT INTO market_ticks VALUES (?,?,?)", (asset, int(time.time()) - age, price))
    c.commit()


@pytest.fixture()
def conn():
    c = _db()
    yield c
    path = _PATHS.pop(id(c), None)
    c.close()
    if path:
        os.unlink(path)


def test_stop_enforce_is_idempotent_across_repeated_calls(conn):
    _tick(conn, "XAU", 97.0)
    conn.execute("INSERT INTO t2_positions (asset, side, qty, entry_price, entry_ts, stop_price, status)"
                 " VALUES ('XAU','long',10,100,?,98,'open')", (int(time.time()),))
    conn.commit()

    first = stops.enforce(conn)
    second = stops.enforce(conn)
    assert len(first) == 1
    assert second == []
    assert conn.execute("SELECT status FROM t2_positions WHERE id=1").fetchone()[0] == "closed"


def test_daily_loss_gate_denies_when_equity_drops_beyond_limit(conn, monkeypatch):
    monkeypatch.setenv("DAILY_LOSS_LIMIT_USDT", "50")
    monkeypatch.setenv("MAX_GROSS_LEVERAGE_PCT", "200")
    monkeypatch.setenv("MAX_OPEN_POSITIONS", "5")
    # 日初权益 10000（今日更早的快照），当前浮亏 -120 → 权益 ~9879
    day_start = int(time.mktime(time.localtime()[:3] + (0, 0, 0, 0, 0, -1))) - 60
    conn.execute("INSERT INTO account_snapshots (ts, equity) VALUES (?,10000)", (day_start,))
    conn.execute("INSERT INTO t2_positions (asset, side, qty, entry_price, entry_ts, status)"
                 " VALUES ('BTC','long',1,1000,?, 'open')", (int(time.time()),))
    _tick(conn, "BTC", 880.0)
    conn.commit()

    out = pf.preflight(conn, "BTC", "BUY", 100.0)
    assert out["ok"] is False
    assert any(d["code"] == "PREFLIGHT_DAILY_LOSS" for d in out["denies"])


def test_daily_loss_gate_stays_open_without_snapshot(conn, monkeypatch):
    monkeypatch.setenv("DAILY_LOSS_LIMIT_USDT", "500")
    monkeypatch.setenv("MAX_GROSS_LEVERAGE_PCT", "200")
    _tick(conn, "XAU", 3737.0)
    out = pf.preflight(conn, "XAU", "BUY", 100.0)
    assert out["ok"] is True, out["denies"]


def test_sync_alerts_inserts_and_resolves(conn):
    conn.execute("DELETE FROM t2_alerts")
    out1 = sd.sync_alerts(conn, [{"code": "R7_LEVERAGE", "severity": "block", "detail": "满仓"}])
    assert out1["inserted"] == 1
    out2 = sd.sync_alerts(conn, [{"code": "R7_LEVERAGE", "severity": "block", "detail": "满仓"}])
    assert out2["inserted"] == 0, "同 code 24h 内不应重复插"
    out3 = sd.sync_alerts(conn, [])
    assert out3["resolved"] == 1
    assert conn.execute("SELECT COUNT(*) FROM t2_alerts WHERE resolved_at IS NULL").fetchone()[0] == 0
