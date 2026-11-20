"""v342 — 多周期结算（multi_horizon_settle 回归测试）

钉死的六件事：
  1. 一条决策 × 5 个周期各写一条，主口径（5 天）不变
  2. 未到期周期不写空行；重复跑不产生重复（PK 幂等）
  3. 取不到价 → status='no_price'，不猜价
  4. 取价异常（|收益|>90%）→ status='implausible'，不给 hit
  5. T+1 品种的日内周期 → status='not_executable'（实盘做不到就不记收益）
  6. hit 按净收益判（BUY 正 / SELL 负），与主结算同口径
"""
from __future__ import annotations

import os
import sqlite3
import sys

SRC = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "src_python"))
if SRC not in sys.path:
    sys.path.insert(0, SRC)

import multi_horizon_settle as mhs
import trade_decisions


def _db(intraday_prices=None):
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    trade_decisions.init_schema(conn)
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS ohlcv_intraday (asset TEXT, ts INTEGER, close REAL);
        CREATE TABLE IF NOT EXISTS ohlcv_daily (asset TEXT, ts INTEGER, close REAL);
        """
    )
    for asset, ts, px in (intraday_prices or []):
        conn.execute("INSERT INTO ohlcv_intraday VALUES (?,?,?)", (asset, ts, px))
    # v342 迁移补的列（测试库不走 db.migrate，这里补齐同一批列）
    for name, sqltype in (("status", "TEXT"), ("decision_source", "TEXT"),
                          ("score", "REAL"), ("confidence", "REAL"),
                          ("structure_score", "REAL"), ("confidence_detail", "TEXT")):
        cols = {r[1] for r in conn.execute("PRAGMA table_info(trade_decisions)")}
        if name not in cols:
            conn.execute(f"ALTER TABLE trade_decisions ADD COLUMN {name} {sqltype}")
    mhs.init_schema(conn)
    return conn


def _rows(conn, did):
    return [dict(r) for r in conn.execute(
        "SELECT * FROM decision_outcomes_horizon WHERE decision_id=? ORDER BY horizon_sec",
        (did,))]


T0 = 1791840000          # 固定基准时间，避免浮动
ENTRY = 100.0


def test_five_horizons_written_with_prices():
    prices = [("BTC", T0 + 900, 101.0), ("BTC", T0 + 3600, 102.0),
              ("BTC", T0 + 14400, 99.0), ("BTC", T0 + 86400, 105.0),
              ("BTC", T0 + 4 * 86400, 110.0)]
    conn = _db(prices)
    res = mhs.settle_horizons(conn, decision_id=1, asset="BTC", action="BUY",
                              entry_price=ENTRY, decision_ts=T0,
                              now_ts=T0 + 5 * 86400, commit=True)
    rows = _rows(conn, 1)
    assert len(rows) == 5
    assert [r["horizon"] for r in rows] == ["15m", "1h", "4h", "1d", "5d"]
    # 15m: 101 vs 100 → +1%（扣成本后仍为正 → hit=1）
    r15 = rows[0]
    assert r15["outcome_pct"] == 1.0
    assert r15["status"] == "settled" and r15["hit"] == 1
    # 4h: 99 → -1%，BUY 亏 → hit=0
    r4h = rows[2]
    assert r4h["outcome_pct"] == -1.0 and r4h["hit"] == 0


def test_idempotent_rerun_keeps_five_rows():
    conn = _db([("BTC", T0 + 900, 101.0)])
    for _ in range(2):
        mhs.settle_horizons(conn, decision_id=2, asset="BTC", action="BUY",
                            entry_price=ENTRY, decision_ts=T0,
                            now_ts=T0 + 5 * 86400, commit=True)
    assert len(_rows(conn, 2)) == 5


def test_unexpired_horizons_are_not_written():
    conn = _db([("BTC", T0 + 900, 101.0)])
    mhs.settle_horizons(conn, decision_id=3, asset="BTC", action="BUY",
                        entry_price=ENTRY, decision_ts=T0,
                        now_ts=T0 + 1800, commit=True)
    rows = _rows(conn, 3)
    assert [r["horizon"] for r in rows] == ["15m"]      # 1h/4h/1d/5d 都还没到


def test_missing_price_is_recorded_not_guessed():
    conn = _db([])
    mhs.settle_horizons(conn, decision_id=4, asset="BTC", action="BUY",
                        entry_price=ENTRY, decision_ts=T0,
                        now_ts=T0 + 5 * 86400, commit=True)
    rows = _rows(conn, 4)
    assert len(rows) == 5
    for r in rows:
        assert r["status"] == "no_price"
        assert r["fwd_price"] is None and r["outcome_pct"] is None
        assert r["hit"] == 0


def test_implausible_move_rejected():
    conn = _db([("BTC", T0 + 900, 1000.0)])     # +900% → 取价异常
    mhs.settle_horizons(conn, decision_id=5, asset="BTC", action="BUY",
                        entry_price=ENTRY, decision_ts=T0,
                        now_ts=T0 + 900, commit=True)
    row = _rows(conn, 5)[0]
    assert row["status"] == "implausible"
    assert row["hit"] == 0
    assert row["pnl_pct"] is None


def test_t1_stock_intraday_is_not_executable():
    """T+1 股票：15m/1h/4h/1d 都在最早可平仓时点之前 → 记 not_executable。"""
    prices = [("AAPL", T0 + 900, 101.0), ("AAPL", T0 + 3600, 102.0),
              ("AAPL", T0 + 14400, 103.0), ("AAPL", T0 + 86400, 104.0)]
    conn = _db(prices)
    mhs.settle_horizons(conn, decision_id=6, asset="AAPL", action="BUY",
                        entry_price=ENTRY, decision_ts=T0,
                        now_ts=T0 + 5 * 86400, commit=True)
    rows = _rows(conn, 6)
    intraday = [r for r in rows if r["horizon"] != "5d"]
    assert intraday and all(r["status"] == "not_executable" for r in intraday)
    assert all(r["outcome_pct"] is None for r in intraday)


def test_hit_uses_net_direction_for_sell():
    prices = [("BTC", T0 + 900, 99.0)]        # -1%，SELL 赚
    conn = _db(prices)
    mhs.settle_horizons(conn, decision_id=7, asset="BTC", action="SELL",
                        entry_price=ENTRY, decision_ts=T0,
                        now_ts=T0 + 900, commit=True)
    row = _rows(conn, 7)[0]
    assert row["outcome_pct"] == -1.0
    assert row["pnl_pct"] is not None and row["pnl_pct"] < 0
    assert row["hit"] == 1


def test_run_once_scans_and_reports():
    conn = _db([("BTC", T0 + 900, 101.0), ("ETH", T0 + 900, 201.0)])
    for i, (asset, px) in enumerate((("BTC", 100.0), ("ETH", 200.0)), start=101):
        conn.execute(
            "INSERT INTO trade_decisions (decision_ts, asset, intended_action, "
            "final_action, decision_type, entry_price, status) "
            "VALUES (?,?,?,?,?,?,?)",
            (T0, asset, "BUY", "BUY", "ENTRY", px, "open"))
    summary = mhs.run_once(conn, now_ts=T0 + 5 * 86400)
    assert summary["decisions"] == 2
    assert summary["written"] == 10          # 2 × 5
    assert summary["settled"] >= 2


def test_read_horizons_returns_rows():
    conn = _db([("BTC", T0 + 900, 101.0)])
    mhs.settle_horizons(conn, decision_id=8, asset="BTC", action="BUY",
                        entry_price=ENTRY, decision_ts=T0,
                        now_ts=T0 + 900, commit=True)
    rows = mhs.read_horizons(conn, 8)
    assert rows and rows[0]["horizon"] == "15m"
