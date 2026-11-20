# -*- coding: utf-8 -*-
"""v477 实盘适配位：未武装必须硬拒单；实盘仓位不得混进模拟账。"""
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
import trade_executor as te
from trident2 import store as t2store

pytestmark = pytest.mark.smoke  # 快速门禁集


def _db(equity: float = 10000.0):
    path = tempfile.NamedTemporaryFile(prefix="trident_live_", suffix=".db", delete=False).name
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    t2store.ensure_schema(conn)
    conn.executescript("""
    CREATE TABLE IF NOT EXISTS market_ticks (symbol TEXT, ts INTEGER, price REAL);
    CREATE TABLE IF NOT EXISTS ai_decisions (
        id INTEGER PRIMARY KEY AUTOINCREMENT, news_id INTEGER NOT NULL,
        sentiment_score REAL NOT NULL, suggested_action TEXT NOT NULL, reasoning TEXT NOT NULL,
        created_at TEXT NOT NULL DEFAULT (datetime('now')), status TEXT NOT NULL DEFAULT 'UNREAD',
        market_category TEXT NOT NULL DEFAULT 'OTHER', target_asset TEXT NOT NULL DEFAULT 'NONE',
        aggregation_key TEXT DEFAULT '', entry_price REAL DEFAULT NULL,
        entry_time TEXT DEFAULT '', settled INTEGER DEFAULT 0
    );
    CREATE TABLE IF NOT EXISTS trade_audit_log (
        id INTEGER PRIMARY KEY AUTOINCREMENT, decision_id INTEGER NOT NULL,
        asset TEXT NOT NULL DEFAULT '', symbol TEXT NOT NULL DEFAULT '', side TEXT NOT NULL DEFAULT '',
        status TEXT NOT NULL, notional_usdt REAL DEFAULT 0.0, quantity REAL DEFAULT 0.0,
        filled_price REAL DEFAULT 0.0, order_id TEXT DEFAULT '', adapter TEXT DEFAULT '',
        mode TEXT DEFAULT '', reason TEXT DEFAULT '', raw_json TEXT,
        created_at TEXT DEFAULT (datetime('now'))
    );
    """)
    conn.execute("UPDATE t2_account SET cash=?, equity=?, realized_pnl=0, fee_bps=8",
                 (equity, equity))
    conn.commit()
    return conn, path


def _env(mode: str, **kw) -> dict:
    base = {
        "TRADE_EXECUTOR_MODE": mode,
        "BINANCE_NOTIONAL_USDT": "100",
        "MAX_POSITION_USDT": "5000",
        "WHITELIST_ASSETS": "*",
    }
    base.update(kw)
    return base


@pytest.fixture()
def conn():
    c, path = _db()
    yield c, path
    c.close()
    os.unlink(path)


def test_live_real_without_key_is_refusing(conn, monkeypatch):
    monkeypatch.setenv("MAX_GROSS_LEVERAGE_PCT", "200")
    c, path = conn
    ex = te.build_default_executor(path, env=_env("live_real"))
    assert ex.adapter.name == "refusing"
    assert "missing_api_key" in ex.adapter.reason
    assert "missing_LIVE_TRADING_ACK" in ex.adapter.reason


def test_live_real_with_key_but_no_ack_is_refusing(conn, monkeypatch):
    c, path = conn
    ex = te.build_default_executor(path, env=_env(
        "live_real", BINANCE_API_KEY="k", BINANCE_API_SECRET="s"))
    assert ex.adapter.name == "refusing"
    assert "missing_LIVE_TRADING_ACK" in ex.adapter.reason


def test_live_real_notional_over_cap_is_refusing(conn):
    c, path = conn
    ex = te.build_default_executor(path, env=_env(
        "live_real", BINANCE_API_KEY="k", BINANCE_API_SECRET="s",
        LIVE_TRADING_ACK="I_UNDERSTAND_REAL_MONEY", LIVE_MAX_NOTIONAL_USDT="50"))
    assert ex.adapter.name == "refusing"
    assert "notional_over_cap" in ex.adapter.reason


def test_refusing_adapter_never_fills_and_writes_reason(conn):
    c, path = conn
    c.execute("INSERT INTO market_ticks VALUES ('XAU',?,3737.0)", (int(time.time()),))
    utc_now = time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime())
    c.execute("INSERT INTO ai_decisions (news_id, sentiment_score, suggested_action, reasoning,"
              " created_at, status, market_category, target_asset, aggregation_key,"
              " entry_price, entry_time, settled)"
              " VALUES (3,0.9,'BUY','live-guard',?,'AUTO_APPROVED','commodity','XAU','k3',3737,?,0)",
              (utc_now, utc_now))
    c.commit()

    ex = te.build_default_executor(path, env=_env("live_real"))
    res = ex.tick()
    assert res["processed"], res
    assert res["processed"][0]["status"] == te.STATUS_ADAPTER_SKIPPED
    row = c.execute("SELECT status, reason, mode FROM trade_audit_log ORDER BY id DESC LIMIT 1").fetchone()
    assert row["status"] == te.STATUS_ADAPTER_SKIPPED
    assert "live_not_armed" in (row["reason"] or "")
    assert c.execute("SELECT COUNT(*) FROM t2_positions WHERE status='open'").fetchone()[0] == 0


def test_live_position_is_segregated_from_paper_ledger(conn, monkeypatch):
    monkeypatch.setenv("MAX_GROSS_LEVERAGE_PCT", "200")
    c, _ = conn
    c.execute("INSERT INTO market_ticks VALUES ('XAU',?,3737.0)", (int(time.time()),))
    t2store.insert_position(c, asset="XAU", side="long", qty=0.1, entry_price=3737.0,
                            entry_ts=int(time.time()), status="open",
                            subaccount="live_real", strategy_id="ai_news")
    c.commit()

    can = al.canonical(c)
    assert can["n_open"] == 0
    assert can["n_live_segregated"] == 1
    codes = {i["code"] for i in al.reconcile(c)}
    assert "R12_LIVE_SEGREGATED" in codes
