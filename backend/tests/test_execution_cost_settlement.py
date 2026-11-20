# -*- coding: utf-8 -*-
"""v319+: 执行成本 + T+0/T+1 交收口径 的回归测试。

覆盖三条链:
  1. cost_model —— 成本口径 + 交收时点口径 (T+0/T+1) 的纯函数;
  2. db.forward_price(min_ts=...) —— T+1 品种的结算价不能落在"最早可平仓"之前;
  3. decision_engine.settle_one —— 结算按**扣完成本**的净收益判 hit;
  4. trade_executor —— 每笔委托的 audit 里必须带上成本口径 + 交收时点。

纯离线: 临时 sqlite, 不碰生产库、不联网。
"""
from __future__ import annotations

import json
import os
import sqlite3
import sys
import tempfile
import time
from datetime import datetime, timedelta, timezone

import pytest

SRC = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "src_python"))
if SRC not in sys.path:
    sys.path.insert(0, SRC)

import cost_model
import decision_engine


@pytest.fixture(autouse=True)
def _isolate_env(monkeypatch):
    for k in ("TRIDENT_FEE_BPS", "TRIDENT_SLIPPAGE_BPS", "TRIDENT_DISABLE_COSTS",
              "TRIDENT_SETTLEMENT_LAG_DAYS", "TRIDENT_SETTLEMENT_TZ"):
        monkeypatch.delenv(k, raising=False)


# ─────────────────────────── 1. 成本 / 交收口径 ───────────────────────────

class TestSettlementPolicy:
    def test_t0_assets(self):
        for a in ("BTC", "ETH", "SOL", "PAXG", "XAU", "GOLD"):
            assert cost_model.settlement_lag_days(a) == 0, a
            assert cost_model.settlement_policy(a)["t_plus"] == "T+0"

    def test_t1_equity(self):
        assert cost_model.settlement_lag_days("STOCK") == 1
        assert cost_model.settlement_policy("STOCK")["t_plus"] == "T+1"

    def test_unknown_asset_is_conservative_t1(self):
        """未知品种按最严的 T+1 —— 宁可少算不可虚高。"""
        assert cost_model.settlement_lag_days("SOME_UNKNOWN_TICKER") == 1

    def test_alias_normalization(self):
        assert cost_model.settlement_lag_days("BTCUSDT") == 0
        assert cost_model.settlement_lag_days("XAUUSD") == 0

    def test_env_override_lag_days(self, monkeypatch):
        monkeypatch.setenv("TRIDENT_SETTLEMENT_LAG_DAYS", "0")
        assert cost_model.settlement_lag_days("STOCK") == 0
        assert cost_model.settlement_lag_days("BTC") == 0

    def test_policy_carries_cost_numbers(self):
        p = cost_model.settlement_policy("XAU")
        assert p["fee_bps_per_side"] == 5.0
        assert p["slippage_bps_per_side"] == 3.0
        assert p["round_trip_bps"] == 16.0
        assert p["round_trip_pct"] == pytest.approx(0.16)


class TestExitTiming:
    def test_t0_same_day_exit_allowed(self):
        now = int(time.time())
        ok, why = cost_model.is_exit_allowed("BTC", now, now + 60)
        assert ok is True
        assert "T+0" in why

    def test_t1_same_day_exit_rejected(self):
        now = int(time.time())
        ok, why = cost_model.is_exit_allowed("STOCK", now, now + 60)
        assert ok is False
        assert "T+1" in why

    def test_t1_earliest_exit_is_next_trading_day_open(self):
        # 2026-01-05 是周一 → 次一交易日 = 周二 09:30 (Asia/Shanghai)
        entry = int(datetime(2026, 1, 5, 10, 0, tzinfo=timezone(timedelta(hours=8))).timestamp())
        earliest = cost_model.earliest_exit_ts("STOCK", entry)
        local = datetime.fromtimestamp(earliest, timezone(timedelta(hours=8)))
        assert (local.year, local.month, local.day) == (2026, 1, 6)
        assert (local.hour, local.minute) == (9, 30)

    def test_t1_friday_entry_rolls_to_monday(self):
        # 2026-01-09 周五 → 次一交易日 = 2026-01-12 周一
        entry = int(datetime(2026, 1, 9, 10, 0, tzinfo=timezone(timedelta(hours=8))).timestamp())
        local = datetime.fromtimestamp(
            cost_model.earliest_exit_ts("STOCK", entry), timezone(timedelta(hours=8))
        )
        assert (local.year, local.month, local.day) == (2026, 1, 12)

    def test_t0_earliest_exit_is_entry_itself(self):
        now = int(time.time())
        assert cost_model.earliest_exit_ts("BTC", now) == now

    def test_t1_exit_allowed_after_earliest(self):
        entry = int(datetime(2026, 1, 5, 10, 0, tzinfo=timezone(timedelta(hours=8))).timestamp())
        earliest = cost_model.earliest_exit_ts("STOCK", entry)
        ok, _ = cost_model.is_exit_allowed("STOCK", entry, earliest)
        assert ok is True


class TestEffectivePrice:
    def test_buy_pays_more(self):
        # XAU slippage 3bps → BUY 成交价高 0.03%
        assert cost_model.effective_entry_price(100.0, "XAU", "BUY") == pytest.approx(100.03)

    def test_sell_receives_less(self):
        assert cost_model.effective_entry_price(100.0, "XAU", "SELL") == pytest.approx(99.97)

    def test_exit_price_is_mirror_of_entry(self):
        # BUY 平仓是卖出 → 更低
        assert cost_model.effective_exit_price(100.0, "XAU", "BUY") == pytest.approx(99.97)
        # SELL 平仓是买回 → 更高
        assert cost_model.effective_exit_price(100.0, "XAU", "SELL") == pytest.approx(100.03)

    def test_bad_input_passthrough(self):
        assert cost_model.effective_entry_price(None, "XAU") is None
        assert cost_model.effective_entry_price("N/A", "XAU") == "N/A"


# ─────────────────────── 2. forward_price 的交收下界 ───────────────────────

def _mk_db():
    path = tempfile.NamedTemporaryFile(prefix="rq_settle_", suffix=".db", delete=False).name
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE ohlcv_intraday (asset TEXT, ts INTEGER, close REAL)")
    conn.execute("CREATE TABLE ohlcv_daily (asset TEXT, ts INTEGER, close REAL)")
    conn.commit()
    return path, conn


def test_forward_price_respects_min_ts():
    """min_ts 把结算价顶到 T+1 最早可平仓时点之后。"""
    import db
    path, conn = _mk_db()
    try:
        base = int(datetime(2026, 1, 5, 10, 0, tzinfo=timezone(timedelta(hours=8))).timestamp())
        # 同一天 (T0) 有一根 100 的 bar, 第二天 (T+1) 有一根 101 的 bar
        conn.execute("INSERT INTO ohlcv_intraday VALUES (?,?,?)", ("XAU", base, 100.0))
        conn.execute("INSERT INTO ohlcv_intraday VALUES (?,?,?)", ("XAU", base + 86400, 101.0))
        conn.commit()

        earliest = cost_model.earliest_exit_ts("STOCK", base)
        assert earliest > base, "T+1 的最早可平仓时点必须晚于入场时点"

        # 不加 min_ts → 可能拿到当天那根 (违反 T+1)
        p_loose = db.forward_price(conn, "XAU", base, settle_days=1)
        # 加 min_ts → 必须落在最早可平仓之后
        p_strict = db.forward_price(conn, "XAU", base, settle_days=1, min_ts=earliest)
        assert p_loose == pytest.approx(100.0)
        assert p_strict == pytest.approx(101.0)
    finally:
        conn.close()
        os.unlink(path)


# ─────────────────────── 3. 结算: 净收益 + hit 口径 ───────────────────────

def _mk_settle_db(asset="XAU", action="BUY", entry=100.0, fwd=100.10,
                  decision_ts=None):
    """建 trade_decisions / trade_reflections_v2 / 行情表, 返回 (path, conn, dec_row)。"""
    path = tempfile.NamedTemporaryFile(prefix="rq_settle_dec_", suffix=".db", delete=False).name
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute("CREATE TABLE ohlcv_intraday (asset TEXT, ts INTEGER, close REAL)")
    conn.execute("CREATE TABLE ohlcv_daily (asset TEXT, ts INTEGER, close REAL)")
    conn.execute(
        "CREATE TABLE trade_decisions ("
        " id INTEGER PRIMARY KEY, decision_ts REAL, asset TEXT, final_action TEXT,"
        " entry_price REAL, event_strength TEXT, context_json TEXT, status TEXT,"
        " settled_at INTEGER, actual_outcome_pct REAL)"
    )
    conn.execute(
        "CREATE TABLE trade_reflections_v2 ("
        " id INTEGER PRIMARY KEY AUTOINCREMENT, decision_id INTEGER UNIQUE, paper_run_id INTEGER,"
        " asset TEXT, dimension TEXT, subcategory TEXT, confidence_at_time REAL,"
        " actual_outcome_pct REAL, pnl_pct REAL, settled INTEGER, hit INTEGER, action TEXT,"
        " entry_price REAL, created_at TEXT)"
    )
    if decision_ts is None:
        decision_ts = int(time.time()) - 6 * 86400
    conn.execute(
        "INSERT INTO trade_decisions (id, decision_ts, asset, final_action, entry_price,"
        " event_strength, context_json, status) VALUES (1,?,?,?,?,'high','{}','open')",
        (decision_ts, asset, action, entry),
    )
    # 远期价: decision_ts + 4 天
    conn.execute("INSERT INTO ohlcv_intraday VALUES (?,?,?)",
                 (asset, int(decision_ts) + 4 * 86400, fwd))
    conn.commit()
    row = conn.execute(
        "SELECT id, asset, final_action AS action, entry_price AS price_at_decision,"
        " decision_ts AS created_at, event_strength, context_json"
        " FROM trade_decisions WHERE id=1"
    ).fetchone()
    return path, conn, row


def test_settle_uses_net_pnl_for_hit():
    """裸价 +0.10% 但 XAU 往返成本 0.16% → 净亏 → hit=0 (打平也算没赢)。"""
    path, conn, dec = _mk_settle_db(asset="XAU", action="BUY", entry=100.0, fwd=100.10)
    try:
        res = decision_engine.settle_one(conn, dec)
        assert res["action"] == "settled"
        assert res["outcome_pct"] == pytest.approx(0.1, abs=0.01)
        assert res["outcome_pct_net"] == pytest.approx(-0.06, abs=0.01)
        assert res["hit"] == 0, "被成本吃掉的微涨不算赢"

        td = conn.execute("SELECT actual_outcome_pct FROM trade_decisions WHERE id=1").fetchone()
        assert td["actual_outcome_pct"] == pytest.approx(0.1, abs=0.01), "裸价口径不变"
        rf = conn.execute("SELECT pnl_pct, hit FROM trade_reflections_v2 WHERE decision_id=1").fetchone()
        assert rf["pnl_pct"] == pytest.approx(-0.06, abs=0.01)
        assert rf["hit"] == 0
    finally:
        conn.close()
        os.unlink(path)


def test_settle_net_pnl_hit_when_edge_clears_cost():
    """裸价 +0.50% > 成本 0.16% → 净 +0.34% → hit=1。"""
    path, conn, dec = _mk_settle_db(asset="XAU", action="BUY", entry=100.0, fwd=100.50)
    try:
        res = decision_engine.settle_one(conn, dec)
        assert res["hit"] == 1
        assert res["outcome_pct_net"] == pytest.approx(0.34, abs=0.01)
    finally:
        conn.close()
        os.unlink(path)


def test_settle_sell_direction_uses_net():
    """SELL: 跌了才算赢, 且必须跌得比成本多。"""
    path, conn, dec = _mk_settle_db(asset="BTC", action="SELL", entry=100.0, fwd=99.80)
    try:
        res = decision_engine.settle_one(conn, dec)
        # BTC 往返 30bps = 0.3%; 裸价 -0.2% → 净 -0.5% → SELL 赢
        assert res["outcome_pct_net"] == pytest.approx(-0.5, abs=0.01)
        assert res["hit"] == 1
    finally:
        conn.close()
        os.unlink(path)


def test_settle_disabled_costs_keeps_raw_hit():
    """TRIDENT_DISABLE_COSTS=1 → 回放旧口径 (净=裸价)。"""
    os.environ["TRIDENT_DISABLE_COSTS"] = "1"
    try:
        path, conn, dec = _mk_settle_db(asset="XAU", action="BUY", entry=100.0, fwd=100.10)
        try:
            res = decision_engine.settle_one(conn, dec)
            assert res["outcome_pct_net"] == pytest.approx(res["outcome_pct"])
            assert res["hit"] == 1
        finally:
            conn.close()
            os.unlink(path)
    finally:
        os.environ.pop("TRIDENT_DISABLE_COSTS", None)


# ─────────────────────── 4. executor 的成本/交收记账 ───────────────────────

def test_executor_audit_records_cost_and_settlement():
    """每笔委托的 audit raw_json 必须带 cost + settlement (含最早可平仓时点)。"""
    import trade_executor as te
    from risk_manager import RiskManager

    path = tempfile.NamedTemporaryFile(prefix="rq_exec_cost_", suffix=".db", delete=False).name
    conn = sqlite3.connect(path)
    from _helpers_exec_guard import add_execution_guard_tables
    add_execution_guard_tables(conn)
    conn.execute(
        "CREATE TABLE ai_decisions (id INTEGER PRIMARY KEY AUTOINCREMENT, news_id INTEGER,"
        " sentiment_score REAL, suggested_action TEXT, reasoning TEXT, created_at TEXT,"
        " status TEXT, market_category TEXT, target_asset TEXT, aggregation_key TEXT,"
        " entry_price REAL, entry_time TEXT, settled INTEGER)"
    )
    conn.execute(
        "INSERT INTO ai_decisions (news_id, sentiment_score, suggested_action, reasoning,"
        " created_at, status, market_category, target_asset, aggregation_key, settled)"
        " VALUES (1, 0.8, 'BUY', 'r', datetime('now'), 'AUTO_APPROVED', 'crypto', 'BTC', 'k1', 0)"
    )
    conn.commit()
    conn.close()

    cfg = te.ExecutorConfig(mode="paper", notional_usdt=100.0)
    adapter = te.MockAdapter(price_lookup=lambda sym: 60000.0)
    rm = RiskManager(whitelist=("*",), max_position_usdt=5000.0)
    ex = te.TradeExecutor(cfg, adapter, rm, path)
    ex.tick()

    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    row = conn.execute("SELECT raw_json, status FROM trade_audit_log ORDER BY id DESC LIMIT 1").fetchone()
    conn.close()
    os.unlink(path)

    assert row is not None
    assert row["status"] == te.STATUS_ADAPTER_SKIPPED
    raw = json.loads(row["raw_json"])
    assert raw["cost"]["round_trip_bps"] == pytest.approx(30.0)
    assert raw["cost"]["t_plus"] == "T+0"
    assert raw["settlement"]["same_day_exit_allowed"] is True
    assert raw["settlement"]["earliest_exit_ts"] >= raw["settlement"]["entry_ts"]

