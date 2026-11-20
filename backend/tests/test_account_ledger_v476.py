# -*- coding: utf-8 -*-
"""v476 账户唯一口径：市价重估 / 手续费 / 对账 / 回补。"""
from __future__ import annotations

import sqlite3
import time

import pytest

import account_ledger as al
from trident2 import store as t2store

pytestmark = pytest.mark.smoke  # 快速门禁集


@pytest.fixture()
def conn(monkeypatch):
    monkeypatch.setenv("PAPER_INITIAL_EQUITY_USDT", "10000")
    monkeypatch.setenv("MAX_GROSS_LEVERAGE_PCT", "200")
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    t2store.ensure_schema(c)
    c.execute(
        "CREATE TABLE IF NOT EXISTS market_ticks (symbol TEXT, price REAL, ts INTEGER)"
    )
    yield c
    c.close()


def _open(conn, asset, side, qty, entry, fee_paid=0.0):
    pid = t2store.insert_position(
        conn, asset=asset, side=side, qty=qty, entry_price=entry,
        entry_ts=int(time.time()), status="open",
    )
    if fee_paid:
        conn.execute("UPDATE t2_positions SET fee_paid=? WHERE id=?", (fee_paid, pid))
        conn.commit()
    return pid


def _tick(conn, asset, price, age_s=0):
    conn.execute("INSERT INTO market_ticks (symbol, price, ts) VALUES (?,?,?)",
                 (asset, price, int(time.time()) - age_s))
    conn.commit()


def test_canonical_marks_open_position_and_accrues_fee(conn):
    _open(conn, "ETH", "short", 2.78, 1800.0)
    _tick(conn, "ETH", 1810.0)

    c = al.canonical(conn)
    assert c["n_open"] == 1
    assert c["unrealized"] == pytest.approx(-27.8, abs=1e-6)
    assert c["unbooked_entry_fee"] == pytest.approx(1800 * 2.78 * 8 / 10_000, abs=1e-6)
    assert c["equity"] == pytest.approx(10000 - 27.8 - 4.0032, abs=1e-6)
    assert c["leverage_pct"] > 0


def test_book_entry_fee_is_idempotent_and_keeps_equity(conn):
    pid = _open(conn, "ETH", "short", 2.78, 1800.0)
    _tick(conn, "ETH", 1800.0)
    before = al.canonical(conn)["equity"]

    fee = al.book_entry_fee(conn, pid)
    assert fee == pytest.approx(4.0032, abs=1e-6)
    assert al.book_entry_fee(conn, pid) == 0.0  # 第二次不再扣

    after = al.canonical(conn)
    assert after["unbooked_entry_fee"] == 0
    assert after["equity"] == pytest.approx(before, abs=1e-6)
    assert after["cash"] == pytest.approx(10000 - 4.0032, abs=1e-6)


def test_close_position_charges_exit_fee_and_revalues(conn):
    pid = _open(conn, "XAU", "long", 1.0, 1000.0, fee_paid=0.8)
    _tick(conn, "XAU", 1010.0)

    res = t2store.close_position(conn, pid, 1010.0)
    gross = 10.0
    fee_total = 0.8 + 1010.0 * 8 / 10_000
    net = gross - fee_total
    assert res["gross_pnl"] == pytest.approx(gross)
    assert res["fee_paid"] == pytest.approx(fee_total)
    assert res["realized_pnl"] == pytest.approx(net)

    c = al.canonical(conn)
    assert c["n_open"] == 0
    assert c["realized"] == pytest.approx(net)
    assert c["equity"] == pytest.approx(10000 + net)


def test_reconcile_blocks_null_pnl_and_duplicate_runs(conn):
    pid = _open(conn, "XAU", "long", 1.0, 1000.0)
    conn.execute(
        "UPDATE t2_positions SET status='closed', exit_price=1000, realized_pnl=NULL WHERE id=?",
        (pid,))
    conn.execute("CREATE TABLE paper_trading_runs (id INTEGER PRIMARY KEY, status TEXT, "
                 "strategy_id INTEGER, spec_version TEXT, started_at TEXT)")
    conn.execute("INSERT INTO paper_trading_runs (id, status, strategy_id, spec_version, started_at) "
                 "VALUES (1,'RUNNING',1,'a','2026-11-19'),(2,'RUNNING',1,'a','2026-11-19')")
    conn.commit()

    codes = {i["code"] for i in al.reconcile(conn)}
    assert "R4_PNL_NULL" in codes
    assert "R6_DUP_RUNS" in codes


def test_rebuild_fixes_null_pnl_and_writes_account(conn, monkeypatch):
    pid = _open(conn, "XAU", "long", 1.0, 1000.0)
    conn.execute("UPDATE t2_positions SET status='closed', exit_price=1010, realized_pnl=NULL WHERE id=?", (pid,))
    _tick(conn, "XAU", 1010.0)
    conn.commit()

    out = al.rebuild(conn, apply=True, backfill_closed=True)
    assert out["after"]["n_closed"] == 1
    row = conn.execute("SELECT realized_pnl, fee_paid, gross_pnl FROM t2_positions WHERE id=?", (pid,)).fetchone()
    assert row["gross_pnl"] == pytest.approx(10.0)
    assert row["realized_pnl"] == pytest.approx(10.0 - 0.8 - 0.808)
    acc = conn.execute("SELECT cash, equity, realized_pnl FROM t2_account WHERE id=1").fetchone()
    assert acc["realized_pnl"] == pytest.approx(row["realized_pnl"])
    assert acc["cash"] == pytest.approx(10000 + row["realized_pnl"])


def test_snapshot_writes_drawdown(conn):
    _tick(conn, "XAU", 1000.0)
    out = al.snapshot(conn, apply=True)
    assert out["equity"] == pytest.approx(10000)
    assert out["drawdown_pct"] == pytest.approx(0.0)
    row = conn.execute("SELECT COUNT(*) FROM account_snapshots").fetchone()[0]
    assert row == 1


def test_rebuild_backfills_closed_fee_and_keeps_net_consistent(conn):
    pid = _open(conn, "XAU", "long", 1.0, 1000.0)
    conn.execute("UPDATE t2_positions SET status='closed', exit_price=1010, "
                 "realized_pnl=10, fee_paid=0 WHERE id=?", (pid,))
    _tick(conn, "XAU", 1010.0)
    conn.commit()

    out = al.rebuild(conn, apply=True, backfill_closed=True)
    fee = (1000 + 1010) * 8 / 10_000
    row = conn.execute("SELECT realized_pnl, fee_paid, gross_pnl FROM t2_positions WHERE id=?", (pid,)).fetchone()
    assert row["fee_paid"] == pytest.approx(fee)
    assert row["realized_pnl"] == pytest.approx(10 - fee)
    assert row["gross_pnl"] == pytest.approx(10)
    acc = conn.execute("SELECT cash, realized_pnl FROM t2_account WHERE id=1").fetchone()
    assert acc["realized_pnl"] == pytest.approx(10 - fee)
    assert acc["cash"] == pytest.approx(10000 + 10 - fee)
