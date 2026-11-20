# -*- coding: utf-8 -*-
"""v342 — 把「方向错」与「节奏错」拆开（horizon_reflection）

钉死的五件事：
  1. 长期也亏 → direction_wrong（该改推理）
  2. 短期亏、长期赚 → timing_wrong（该改执行，不要改推理）
  3. 长周期对 T+1 品种不可执行 → horizon_mismatch（不判"判断错"）
  4. no_price → no_data（不猜）
  5. 幂等：同一 decision_id 重复跑覆盖写，不重复计数
"""
from __future__ import annotations

import os
import sqlite3
import sys

import pytest

SRC = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "src_python"))
if SRC not in sys.path:
    sys.path.insert(0, SRC)

import horizon_reflection as hr


def _row(h, status, pnl=None, outcome=None, hit=None, sec=0):
    return {"horizon": h, "horizon_sec": sec, "status": status,
            "pnl_pct": pnl, "outcome_pct": outcome, "hit": hit}


# ─────────────────────── 1. 方向错 ───────────────────────

def test_direction_wrong_when_long_loses():
    rows = [_row("15m", "settled", -0.4, -0.3, 0),
            _row("1h", "settled", -0.8, -0.7, 0),
            _row("5d", "settled", -3.1, -3.0, 0)]
    res = hr.classify(rows)
    assert res["verdict"] == "direction_wrong"
    assert res["code"] == "H-DIR"
    assert res["long_horizon"] == "5d"


# ─────────────────────── 2. 节奏错（核心） ───────────────────────

def test_timing_wrong_when_short_loses_but_long_wins():
    rows = [_row("15m", "settled", -1.2, -1.1, 0),
            _row("1h", "settled", -0.6, -0.5, 0),
            _row("5d", "settled", 2.4, 2.5, 1)]
    res = hr.classify(rows)
    assert res["verdict"] == "timing_wrong"
    assert res["code"] == "H-TIME"
    assert "入场时点" in res["why"]


def test_short_loss_without_long_win_is_not_timing():
    rows = [_row("15m", "settled", -1.0, -0.9, 0),
            _row("5d", "settled", 0.3, 0.4, 1)]      # 长期只小赚，不够 0.5%
    res = hr.classify(rows)
    assert res["verdict"] == "ok"     # 不硬判节奏错


# ─────────────────────── 3. 周期不可执行 ───────────────────────

def test_horizon_mismatch_beats_everything():
    rows = [_row("1d", "not_executable"),
            _row("5d", "not_executable"),
            _row("15m", "settled", -0.2, -0.1, 0)]
    res = hr.classify(rows)
    assert res["verdict"] == "horizon_mismatch"
    assert res["code"] == "H-EXEC"


# ─────────────────────── 4. 没数据 / 半结论 ───────────────────────

def test_no_data_is_explicit():
    rows = [_row("15m", "no_price"), _row("1d", "no_price")]
    res = hr.classify(rows)
    assert res["verdict"] == "no_data"
    assert res["code"] == "H-NODATA"


def test_partial_when_long_not_settled_but_short_settled():
    rows = [_row("15m", "settled", 0.6, 0.5, 1), _row("5d", "pending")]
    res = hr.classify(rows)
    assert res["verdict"] == "partial"


# ─────────────────────── 5. 成本侵蚀 ───────────────────────

def test_cost_drag_when_raw_positive_but_net_tiny():
    rows = [_row("15m", "settled", 0.1, 0.3, 1),
            _row("1h", "settled", 0.05, 0.25, 1),
            _row("5d", "settled", 0.12, 0.35, 1)]
    res = hr.classify(rows)
    assert res["verdict"] == "cost_drag"
    assert res["code"] == "H-COST"


# ─────────────────────── 6. 落库与聚合 ───────────────────────

@pytest.fixture()
def db(tmp_path):
    import trade_decisions as td
    import multi_horizon_settle as mhs
    path = str(tmp_path / "hr.db")
    conn = sqlite3.connect(path)
    td.init_schema(conn)
    for col, decl in (("status", "TEXT"), ("decision_source", "TEXT"),
                      ("score", "REAL"), ("confidence", "REAL")):
        try:
            conn.execute(f"ALTER TABLE trade_decisions ADD COLUMN {col} {decl}")
        except sqlite3.OperationalError:
            pass
    mhs.init_schema(conn)
    hr.init_schema(conn)
    conn.commit()
    return conn


def test_run_once_writes_and_is_idempotent(db):
    conn = db
    conn.execute("INSERT INTO trade_decisions (id, asset, intended_action, final_action, "
                 " decision_type, entry_price, decision_ts, status) "
                 "VALUES (1,'BTC','BUY','BUY','trade',68000,1000,'x')")
    for h, sec, status, pnl, hit in (("15m", 900, "settled", -1.2, 0),
                                     ("1h", 3600, "settled", -0.8, 0),
                                     ("5d", 432000, "settled", 2.2, 1)):
        conn.execute("INSERT INTO decision_outcomes_horizon (decision_id, horizon, "
                     "horizon_sec, asset, action, entry_price, decision_ts, target_ts, "
                     "outcome_pct, pnl_pct, hit, status, computed_at) "
                     "VALUES (1,?,?,?,?,?,?,?,?,?,?,?,?)",
                     (h, sec, "BTC", "BUY", 68000, 1000, 1000 + sec,
                      pnl, pnl, hit, status, 1000))
    conn.commit()

    out = hr.run_once(conn, limit=10)
    assert out["decisions"] == 1
    assert out["by_verdict"]["timing_wrong"] == 1

    again = hr.run_once(conn, limit=10)      # 幂等：不新增行
    assert again["decisions"] == 1
    n = conn.execute("SELECT COUNT(*) FROM decision_horizon_reflection").fetchone()[0]
    assert n == 1

    stats = hr.read_stats(conn, days=365000)
    assert stats["total"] == 1
    assert stats["direction_vs_timing"] == {"direction_wrong": 0, "timing_wrong": 1}
