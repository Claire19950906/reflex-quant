#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""v483：quick_sim_trades 零成本历史行回填回归。"""
from __future__ import annotations

import sqlite3
import sys

import pytest
from pathlib import Path

_HERE = Path(__file__).resolve().parent
_ROOT = _HERE.parent
sys.path.insert(0, str(_ROOT / "src_python"))
sys.path.insert(0, str(_ROOT / "scripts"))

import backfill_quick_sim_cost_v483 as bf


def _db():
    c = sqlite3.connect(":memory:")
    c.execute(
        """CREATE TABLE quick_sim_trades (
            id INTEGER PRIMARY KEY, decision_id INTEGER, asset TEXT, action TEXT,
            entry_price REAL, exit_price REAL, max_price REAL, min_price REAL,
            notional_usdt REAL, pnl_pct REAL, pnl_usdt REAL, verdict TEXT,
            settled INTEGER, gross_pnl_pct REAL, cost_bps REAL,
            mfe_pct REAL, mae_pct REAL
        )"""
    )
    c.executemany(
        """INSERT INTO quick_sim_trades
           (id, decision_id, asset, action, entry_price, exit_price, max_price,
            min_price, notional_usdt, pnl_pct, pnl_usdt, verdict, settled,
            gross_pnl_pct, cost_bps, mfe_pct, mae_pct)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        [
            (391, 1, "WTI", "SELL", 82.316, 82.606, 82.606, 82.215, 1000,
             -0.3523, -3.523, "LOSS", 1, None, 0.0, None, 0.3523),
            (392, 2, "XAU", "SELL", 3733.79, 3746.0, 3746.0, 3728.32, 1000,
             -0.327, -3.27, "LOSS", 1, None, 0.0, None, 0.327),
        ],
    )
    c.commit()
    return c


def test_compute_fix_wti_sell():
    row = {
        "asset": "WTI", "action": "SELL", "entry_price": 82.316,
        "exit_price": 82.606, "max_price": 82.606, "min_price": 82.215,
        "notional_usdt": 1000, "pnl_pct": -0.3523,
        "gross_pnl_pct": None, "cost_bps": 0, "mfe_pct": None, "mae_pct": 0.3523,
    }
    out = bf.compute_fix(row)
    assert out["cost_bps"] == 30.0
    assert out["gross_pnl_pct"] == -0.3523
    assert out["pnl_pct"] == pytest.approx(-0.6523)
    assert out["mfe_pct"] == pytest.approx(0.1227, abs=1e-4)  # (82.316-82.215)/82.316*100
    assert out["verdict"] == "LOSS"


def test_backfill_is_idempotent(tmp_path):
    db = tmp_path / "q.db"
    src = _db()
    dest = sqlite3.connect(db)
    src.backup(dest)
    dest.close()
    src.close()

    fixed1 = bf.backfill(str(db), dry_run=False)
    fixed2 = bf.backfill(str(db), dry_run=False)
    assert len(fixed1) == 2
    assert len(fixed2) == 0
    conn = sqlite3.connect(db)
    rows = conn.execute(
        "SELECT asset, cost_bps, gross_pnl_pct, pnl_pct, mfe_pct FROM quick_sim_trades ORDER BY id"
    ).fetchall()
    conn.close()
    assert rows[0][0] == "WTI" and rows[0][1] == 30.0
    assert rows[1][0] == "XAU" and rows[1][1] == 16.0
