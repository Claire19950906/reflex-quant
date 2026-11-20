#!/usr/bin/env python3
"""v490：深度护栏进影子账本 + 60 笔门槛的 A/B 评估。"""

import json
import sqlite3

import fundamental_shadow as fs
import importlib.util
from pathlib import Path

_SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "fundamental_depth_ab.py"
_spec = importlib.util.spec_from_file_location("depth_ab", _SCRIPT)
depth_ab = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(depth_ab)


def _guard():
    fund = {
        "available": True, "score": 0.5, "weight": 0.08,
        "ticker": "AAPL", "market": "US",
        "flags": ["gm_down", "growth_premium", "earnings_peak"],
        "components": {
            "depth_flags": ["gm_down", "opm_down"],
            "depth_earnings_zone": "peak",
            "depth_points": 16,
            "depth_gross_margin_ttm": 40.0,
        },
        "valuation_percentile": 0.2,
    }
    return {
        "factors": {
            "fundamental": fund,
            "news_sentiment": {"available": True, "score": 0.3, "weight": 0.32},
        },
        "fundamental": fund,
        "thresholds": {"buy_min": 0.35, "sell_min": -0.35},
        "action": "HOLD",
    }


def test_depth_fields_filters_non_risk_flags():
    comp = _guard()["fundamental"]
    flags, zone, points = fs._depth_fields(comp)
    assert json.loads(flags) == ["gm_down", "opm_down"]   # growth_premium/earnings_peak 不进
    assert zone == "peak"
    assert points == 16


def test_snapshot_migration_and_depth_columns():
    conn = sqlite3.connect(":memory:")
    # 先建一个 v489 之前的老表（没有 depth 三列），验证幂等补列不重建
    conn.execute(
        """CREATE TABLE decision_fundamental_snapshot (
               decision_id INTEGER PRIMARY KEY, ts INTEGER NOT NULL, asset TEXT, ticker TEXT,
               market TEXT, fund_score REAL, fund_available INTEGER, valuation REAL,
               valuation_basis TEXT, peer_rel_score REAL, peer_sector TEXT, macro_tilt REAL,
               pb_excluded TEXT, reason TEXT, sizing_mult REAL, components TEXT,
               dispersion REAL, dispersion_level TEXT, confidence REAL, bull_weight REAL,
               bear_weight REAL)""")
    comp = _guard()["fundamental"]
    fs.record(conn, 1, None, "US_STOCK", _guard(), "HOLD", flags=comp["flags"], components=comp)
    row = conn.execute(
        """SELECT depth_flags, depth_zone, depth_points
           FROM decision_fundamental_snapshot WHERE decision_id=1""").fetchone()
    assert json.loads(row[0]) == ["gm_down", "opm_down"]
    assert row[1] == "peak"
    assert row[2] == 16


def _snapshot_conn(flagged: int, clean: int, flagged_ret: float, clean_ret: float):
    conn = sqlite3.connect(":memory:")
    conn.execute(
        """CREATE TABLE decision_fundamental_snapshot (
               decision_id INTEGER PRIMARY KEY, ts INTEGER, asset TEXT, ticker TEXT,
               market TEXT, fund_score REAL, fund_available INTEGER,
               depth_flags TEXT, depth_zone TEXT, depth_points INTEGER)""")
    conn.execute("CREATE TABLE ohlcv_daily (asset TEXT, ts INTEGER, close REAL)")
    base = 1_700_000_000
    did = 1
    for i in range(flagged):
        tk = f"F{i}"
        conn.execute(
            "INSERT INTO decision_fundamental_snapshot VALUES (?,?,?,?,?,?,?,?,?,?)",
            (did, base, "US_STOCK", tk, "US", 0.5, 1, '["gm_down"]', "peak", 16))
        conn.execute("INSERT INTO ohlcv_daily VALUES (?,?,?)", (tk, base, 100.0))
        conn.execute("INSERT INTO ohlcv_daily VALUES (?,?,?)",
                     (tk, base + 10 * 86400, 100.0 * (1 + flagged_ret)))
        did += 1
    for i in range(clean):
        tk = f"C{i}"
        conn.execute(
            "INSERT INTO decision_fundamental_snapshot VALUES (?,?,?,?,?,?,?,?,?,?)",
            (did, base, "US_STOCK", tk, "US", 0.5, 1, "[]", None, 16))
        conn.execute("INSERT INTO ohlcv_daily VALUES (?,?,?)", (tk, base, 100.0))
        conn.execute("INSERT INTO ohlcv_daily VALUES (?,?,?)",
                     (tk, base + 10 * 86400, 100.0 * (1 + clean_ret)))
        did += 1
    conn.commit()
    return conn


def test_ab_insufficient_below_60():
    conn = _snapshot_conn(3, 3, -0.20, 0.10)
    out = depth_ab.build(conn, horizon=10)
    assert out["flagged"]["n"] == 3
    assert out["clean"]["n"] == 3
    assert out["verdict"] == "insufficient"


def test_ab_flags_predict_underperformance_at_60():
    conn = _snapshot_conn(30, 30, -0.20, 0.10)
    out = depth_ab.build(conn, horizon=10)
    assert out["flagged"]["n"] == 30
    assert out["clean"]["n"] == 30
    assert out["verdict"] == "flags_predict_underperformance"
    assert out["flagged"]["avg_ret"] == -0.2
    assert out["clean"]["avg_ret"] == 0.1
    assert out["by_flag"]["gm_down"] == 30


def test_unknown_depth_rows_excluded():
    conn = _snapshot_conn(30, 30, -0.20, 0.10)
    conn.execute("UPDATE decision_fundamental_snapshot SET depth_flags=NULL "
                 "WHERE decision_id=1")
    conn.commit()
    out = depth_ab.build(conn, horizon=10)
    assert out["unknown_depth"] == 1
    assert out["flagged"]["n"] == 29
