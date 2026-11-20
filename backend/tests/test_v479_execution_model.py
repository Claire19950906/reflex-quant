# -*- coding: utf-8 -*-
"""v479 执行模型：移动止损 / 时间止损 / 滑点部分成交 / 资金费 / 下单幂等。"""
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
from trident2 import store as st
from trade_executor import MockAdapter, OrderRequest

_PATHS = {}


def _db():
    path = tempfile.NamedTemporaryFile(prefix="v479_", suffix=".db", delete=False).name
    c = sqlite3.connect(path)
    c.row_factory = sqlite3.Row
    st.ensure_schema(c)
    c.executescript("CREATE TABLE IF NOT EXISTS market_ticks (symbol TEXT, ts INTEGER, price REAL);")
    c.execute("UPDATE t2_account SET cash=10000, equity=10000, realized_pnl=0, fee_bps=8")
    c.commit()
    _PATHS[id(c)] = path
    return c


@pytest.fixture()
def conn():
    c = _db()
    yield c
    path = _PATHS.pop(id(c), None)
    c.close()
    if path:
        os.unlink(path)


def tick(c, sym, price, age=0):
    c.execute("INSERT INTO market_ticks VALUES (?,?,?)", (sym, int(time.time()) - age, price))
    c.commit()


def test_trailing_stop_tightens_and_fires(conn):
    tick(conn, "BTC", 100.0, age=300)
    pid = st.insert_position(conn, asset="BTC", side="long", qty=1, entry_price=100.0,
                             entry_ts=int(time.time()) - 200, stop_price=None,
                             trailing_bp=200, status="open", subaccount="paper")
    al.book_entry_fee(conn, pid)
    tick(conn, "BTC", 110.0, age=60)      # 最优价 110 → 轨迹止损 107.8
    tick(conn, "BTC", 107.0, age=0)       # 跌破 107.8
    closed = stops.enforce(conn)
    assert len(closed) == 1
    assert closed[0]["trigger"] == "trailing_stop"
    assert closed[0]["exit"] == 107.0


def test_time_stop_closes_without_price_move(conn):
    tick(conn, "XAU", 3730.0)
    pid = st.insert_position(conn, asset="XAU", side="long", qty=1, entry_price=3730.0,
                             entry_ts=int(time.time()) - 130, stop_price=None,
                             time_stop_min=1, status="open", subaccount="paper")
    al.book_entry_fee(conn, pid)
    closed = stops.enforce(conn)
    assert len(closed) == 1 and closed[0]["trigger"] == "time_stop"


def test_slippage_and_partial_fill(conn, monkeypatch):
    monkeypatch.setenv("PAPER_SLIPPAGE_BP", "5")
    monkeypatch.setenv("PAPER_FILL_RATIO", "0.5")
    ad = MockAdapter(price_lookup=lambda s: 100.0, simulate_fill=True)
    buy = ad.place_order(OrderRequest(symbol="BTC", side="BUY", notional_usdt=1000))
    sell = ad.place_order(OrderRequest(symbol="BTC", side="SELL", notional_usdt=1000))
    assert buy.filled_price == pytest.approx(100.05)
    assert sell.filled_price == pytest.approx(99.95)
    assert buy.filled_qty == pytest.approx(10 * 0.5)   # 1000/100 * 0.5
    assert buy.raw["slippage_bp"] == 5


def test_funding_accrues_on_close(conn, monkeypatch):
    # v491：资金费只对永续合约计（BTC 走 binance_fapi）；股票/原油现货不计
    monkeypatch.setenv("FUNDING_BPS_PER_8H", "10")
    tick(conn, "BTC", 64000)
    now = int(time.time())
    pid = st.insert_position(conn, asset="BTC", side="long", qty=0.05, entry_price=64000,
                             entry_ts=now - 8 * 3600, stop_price=None, status="open",
                             subaccount="paper")
    al.book_entry_fee(conn, pid)
    res = st.close_position(conn, pid, exit_price=64000, closed_ts=now)
    notional = 64000 * 0.05
    assert res["funding_paid"] == pytest.approx(notional * 10 / 10000, rel=0.02)
    assert res["realized_pnl"] < 0        # 价没动，但仍亏双边费 + 资金费
    row = conn.execute("SELECT funding_paid, realized_pnl FROM t2_positions WHERE id=?", (pid,)).fetchone()
    assert row["funding_paid"] > 0


def test_no_funding_on_spot_assets(conn, monkeypatch):
    """WTI 是现货价、NVDA 是股票——都不存在资金费，不能凭空扣钱。"""
    monkeypatch.setenv("FUNDING_BPS_PER_8H", "10")
    now = int(time.time())
    for asset, px, qty in (("WTI", 82.3, 60.0), ("NVDA", 150.0, 10.0)):
        tick(conn, asset, px)
        pid = st.insert_position(conn, asset=asset, side="long", qty=qty, entry_price=px,
                                 entry_ts=now - 24 * 3600, stop_price=None,
                                 status="open", subaccount="paper")
        al.book_entry_fee(conn, pid)
        res = st.close_position(conn, pid, exit_price=px, closed_ts=now)
        assert res["funding_paid"] == 0.0, f"{asset} 不该收资金费"


def test_claim_order_is_idempotent(conn):
    cid = "dec1:BTC:BUY"
    first = st.claim_order(conn, client_order_id=cid, asset="BTC", side="BUY",
                           notional_usdt=5000, decision_id=1)
    second = st.claim_order(conn, client_order_id=cid, asset="BTC", side="BUY",
                            notional_usdt=5000, decision_id=1)
    assert first and first > 0
    assert second is None
    st.update_order_status(conn, first, "FILLED", filled_price=100.0, filled_qty=50.0)
    row = conn.execute("SELECT status, filled_qty FROM t2_orders WHERE id=?", (first,)).fetchone()
    assert row["status"] == "FILLED" and row["filled_qty"] == 50.0


def test_stale_pending_order_is_cancelled(conn):
    oid = st.claim_order(conn, client_order_id="dec2:XAU:BUY", asset="XAU", side="BUY",
                         notional_usdt=100, decision_id=2)
    conn.execute("UPDATE t2_orders SET ts=? WHERE id=?", (int(time.time()) - 600, oid))
    conn.commit()
    n = st.cancel_stale_orders(conn, older_than_s=120)
    assert n == 1
    row = conn.execute("SELECT status, note FROM t2_orders WHERE id=?", (oid,)).fetchone()
    assert row["status"] == "CANCELLED" and row["note"] == "timeout"
