#!/usr/bin/env python3
"""v301+ P0+：交易成本模型单元测试。

覆盖：
  1. 默认费率（按资产类查表）
  2. env 覆盖（TRIDENT_FEE_BPS / TRIDENT_SLIPPAGE_BPS）
  3. 资产别名归一化（BTCUSDT → BTC，XAU / GOLD 互通）
  4. round_trip = 2× (fee + slip)
  5. TRIDENT_DISABLE_COSTS=1 时 apply_costs 短路
  6. None / 非数输入不崩
  8. quick_sim.settle_trades 集成（cost_bps 列 + pnl_pct 扣减 + WIN/LOSS verdict 不变）
"""
from __future__ import annotations

import os
import sqlite3
import sys
import tempfile
import time
from pathlib import Path

import pytest

# ── 把 src_python 加进 path（项目惯例）──
_HERE = Path(__file__).resolve().parent
_SRC = _HERE.parent / "src_python"
sys.path.insert(0, str(_SRC))


@pytest.fixture(autouse=True)
def _isolate_env(monkeypatch):
    """每个用例清掉 env，避免泄漏。"""
    for k in ("TRIDENT_FEE_BPS", "TRIDENT_SLIPPAGE_BPS", "TRIDENT_DISABLE_COSTS"):
        monkeypatch.delenv(k, raising=False)


# ──────────────────────────────────────────────────────────
# 1. 默认费率
# ──────────────────────────────────────────────────────────
class TestDefaultRates:
    def test_btc_round_trip(self):
        from cost_model import round_trip_cost_bps
        # BTC: fee 10 + slip 5 per side, round_trip = 2*(10+5) = 30
        assert round_trip_cost_bps("BTC") == pytest.approx(30.0)

    def test_xau_round_trip(self):
        from cost_model import round_trip_cost_bps
        # XAU: fee 5 + slip 3 per side, round_trip = 16
        assert round_trip_cost_bps("XAU") == pytest.approx(16.0)

    def test_unknown_asset_uses_safe_default(self):
        from cost_model import round_trip_cost_bps
        # 未知资产走 10 fee + 5 slip 保守值
        assert round_trip_cost_bps("UNKNOWN_TICKER_XYZ") == pytest.approx(30.0)

    def test_alias_normalization(self):
        from cost_model import round_trip_cost_bps
        assert round_trip_cost_bps("BTCUSDT") == round_trip_cost_bps("BTC")
        assert round_trip_cost_bps("btcusdt") == round_trip_cost_bps("BTC")
        assert round_trip_cost_bps("GOLD") == round_trip_cost_bps("XAU")


# ──────────────────────────────────────────────────────────
# 2. env 覆盖
# ──────────────────────────────────────────────────────────
class TestEnvOverride:
    def test_global_fee_override(self, monkeypatch):
        monkeypatch.setenv("TRIDENT_FEE_BPS", "20")
        from cost_model import round_trip_cost_bps
        # 任何资产都走 20 fee + 默认 slip 5 per side, round_trip = 50
        assert round_trip_cost_bps("BTC") == pytest.approx(50.0)
        assert round_trip_cost_bps("XYZ") == pytest.approx(50.0)

    def test_global_slippage_override(self, monkeypatch):
        monkeypatch.setenv("TRIDENT_SLIPPAGE_BPS", "10")
        from cost_model import round_trip_cost_bps
        # BTC: 10 fee + 10 slip per side, round_trip = 40
        assert round_trip_cost_bps("BTC") == pytest.approx(40.0)

    def test_both_overrides(self, monkeypatch):
        monkeypatch.setenv("TRIDENT_FEE_BPS", "3")
        monkeypatch.setenv("TRIDENT_SLIPPAGE_BPS", "2")
        from cost_model import round_trip_cost_bps
        # round_trip = 2*(3+2) = 10
        assert round_trip_cost_bps("BTC") == pytest.approx(10.0)


# ──────────────────────────────────────────────────────────
# 3. apply_costs 行为
# ──────────────────────────────────────────────────────────
class TestApplyCosts:
    def test_normal_buy(self):
        from cost_model import apply_costs
        # BTC: 10% 原始 → 扣 30bps (0.3%) → 9.7%
        assert apply_costs(10.0, "BTC", "BUY") == pytest.approx(9.7)

    def test_normal_sell(self):
        from cost_model import apply_costs
        # 成本方向与 side 无关（round-trip 概念）
        assert apply_costs(-5.0, "BTC", "SELL") == pytest.approx(-5.3)

    def test_disable_switch(self, monkeypatch):
        monkeypatch.setenv("TRIDENT_DISABLE_COSTS", "1")
        from cost_model import apply_costs, costs_globally_disabled
        assert costs_globally_disabled() is True
        assert apply_costs(10.0, "BTC") == 10.0  # 原值

    def test_none_input_passthrough(self):
        from cost_model import apply_costs
        # 不让 NaN 传播炸 compute
        assert apply_costs(None, "BTC") is None

    def test_string_input_passthrough(self):
        from cost_model import apply_costs
        assert apply_costs("N/A", "BTC") == "N/A"

    def test_zero_pnl_becomes_negative(self):
        from cost_model import apply_costs
        # 即使打平，扣完成本仍是亏（这是真实交易特征）
        assert apply_costs(0.0, "BTC") == pytest.approx(-0.3)


# ──────────────────────────────────────────────────────────
# 5. 集成：quick_sim.settle_trades 走完整链路
# ──────────────────────────────────────────────────────────
class TestQuickSimIntegration:
    """最小化的 sqlite + db.py migrate + settle_trades 集成测试。"""

    def _setup_db(self):
        """用临时数据库跑完 migrate，得到可写 conn。"""
        tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        tmp.close()
        os.environ["TRIDENT_DB_PATH"] = tmp.name
        # 强制 reload（因为 cost_model 已经 import 过）
        import db as db_mod
        db_mod.DB_PATH = tmp.name
        conn = db_mod.get_connection()
        conn.row_factory = sqlite3.Row
        db_mod.migrate(conn)  # 跑所有 CREATE TABLE / ALTER（幂等）
        return conn, tmp.name

    def _insert_decision_and_trade(self, conn, entry_ts, exit_due_ts,
                                   entry=100.0, asset="BTC", action="BUY"):
        # ai_decisions.news_id 有 FK → raw_news(id)，先插一条伪 news
        cur = conn.execute(
            """INSERT INTO raw_news
                 (source, content, ts, first_seen_ts)
               VALUES ('test', 'fake content', ?, ?)""",
            (entry_ts - 60, str(entry_ts - 60)),
        )
        news_id = cur.lastrowid
        cur = conn.execute(
            """INSERT INTO ai_decisions
                 (news_id, sentiment_score, suggested_action, reasoning, target_asset,
                  evidence_confidence, settled)
               VALUES (?, ?, ?, ?, ?, ?, 0)""",
            (news_id, 0.5, action, "test reasoning", asset, 0.9),
        )
        decision_id = cur.lastrowid
        conn.execute(
            """INSERT INTO quick_sim_trades
                 (decision_id, asset, action, entry_price, entry_ts, horizon_minutes,
                  notional_usdt, settled)
               VALUES (?, ?, ?, ?, ?, ?, 10000, 0)""",
            (decision_id, asset, action, entry, entry_ts, max(1, (exit_due_ts - entry_ts) // 60)),
        )
        conn.commit()
        return decision_id

    def test_settle_applies_costs(self, monkeypatch):
        conn, db_path = self._setup_db()
        try:
            # 把 cost_bps 显式给个 env 值，避免依赖默认值漂移
            monkeypatch.setenv("TRIDENT_FEE_BPS", "10")
            monkeypatch.setenv("TRIDENT_SLIPPAGE_BPS", "5")
            now = int(time.time())
            self._insert_decision_and_trade(
                conn, entry_ts=now - 7200, exit_due_ts=now, entry=100.0,
            )
            import quick_sim
            settled = quick_sim.settle_trades(conn, price_fn=lambda a: 110.0)
            assert len(settled) == 1
            row = settled[0]
            # raw pnl = 10%, cost = 30bps = 0.3% → net ≈ 9.7%
            assert row["pnl_pct"] == pytest.approx(9.7, abs=0.01)
            assert row["cost_bps"] == pytest.approx(30.0)
            assert row["verdict"] == "WIN"
            # DB 落库也要扣
            dbr = conn.execute(
                "SELECT pnl_pct, cost_bps, pnl_usdt FROM quick_sim_trades WHERE id=?",
                (row["id"],),
            ).fetchone()
            assert dbr["cost_bps"] == pytest.approx(30.0)
            assert dbr["pnl_pct"] == pytest.approx(9.7, abs=0.01)
        finally:
            conn.close()
            os.unlink(db_path)

    def test_settle_loser_pnl_more_negative(self, monkeypatch):
        conn, db_path = self._setup_db()
        try:
            monkeypatch.setenv("TRIDENT_FEE_BPS", "10")
            monkeypatch.setenv("TRIDENT_SLIPPAGE_BPS", "5")
            now = int(time.time())
            self._insert_decision_and_trade(
                conn, entry_ts=now - 7200, exit_due_ts=now, entry=100.0,
            )
            import quick_sim
            settled = quick_sim.settle_trades(conn, price_fn=lambda a: 99.0)
            # raw pnl = -1% → 扣 0.3% → -1.3% → LOSS
            row = settled[0]
            assert row["pnl_pct"] == pytest.approx(-1.3, abs=0.01)
            assert row["verdict"] == "LOSS"
        finally:
            conn.close()
            os.unlink(db_path)

    def test_disable_switch_keeps_raw_pnl(self, monkeypatch):
        """TRIDENT_DISABLE_COSTS=1 时 settle_trades 走老路径（裸价）。"""
        conn, db_path = self._setup_db()
        try:
            monkeypatch.setenv("TRIDENT_DISABLE_COSTS", "1")
            now = int(time.time())
            self._insert_decision_and_trade(
                conn, entry_ts=now - 7200, exit_due_ts=now, entry=100.0,
            )
            import quick_sim
            settled = quick_sim.settle_trades(conn, price_fn=lambda a: 110.0)
            # 关闭成本 → raw pnl = 10%
            assert settled[0]["pnl_pct"] == pytest.approx(10.0, abs=0.01)
            # 但 cost_bps 仍记录（用于审计"曾经想扣多少"）
            assert settled[0]["cost_bps"] == pytest.approx(30.0)
        finally:
            conn.close()
            os.unlink(db_path)