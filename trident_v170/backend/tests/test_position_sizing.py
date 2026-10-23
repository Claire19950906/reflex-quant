"""Tests for v230+ P0.1 position_sizing (Confidence-Tier Sizing)."""
from __future__ import annotations

import importlib
import sys
from pathlib import Path

import pytest

SRC = Path(__file__).resolve().parent.parent / "src_python"
sys.path.insert(0, str(SRC))

import position_sizing  # noqa: E402
import config  # v231+: QUALITY_FILTER-valid 构造信号用 TRADE_GATE_PASSED_REASON


@pytest.fixture
def fresh_ps(monkeypatch):
    """每次 reload 模块（保证环境变量生效）。"""
    def _reload(**env_overrides):
        for k, v in env_overrides.items():
            monkeypatch.setenv(k, str(v))
        importlib.reload(position_sizing)
        return position_sizing
    yield _reload


# ──────────────────────────── kill switch / disabled ──────────────────────────


def test_disabled_returns_base_unchanged(fresh_ps):
    ps = fresh_ps(POSITION_SIZING_ENABLED="0")
    notional, meta = ps.compute_notional({"evidence_confidence": 0.85}, 100.0)
    assert notional == 100.0
    assert meta["enabled"] is False
    assert meta["reason"] == "disabled"
    assert meta["tier"] == "n/a"


# ──────────────────────────── three tiers ──────────────────────────


def test_low_confidence_halves_size(fresh_ps):
    ps = fresh_ps(POSITION_SIZING_ENABLED="1")
    notional, meta = ps.compute_notional({"evidence_confidence": 0.1}, 100.0)
    assert notional == 50.0
    assert meta["tier"] == "low"
    assert meta["multiplier"] == 0.5
    assert meta["reason"] == "tier_match"


def test_medium_confidence_unchanged(fresh_ps):
    ps = fresh_ps(POSITION_SIZING_ENABLED="1")
    notional, meta = ps.compute_notional({"evidence_confidence": 0.5}, 100.0)
    assert notional == 100.0
    assert meta["tier"] == "medium"
    assert meta["multiplier"] == 1.0


def test_high_confidence_50pct_bonus(fresh_ps):
    ps = fresh_ps(POSITION_SIZING_ENABLED="1")
    notional, meta = ps.compute_notional({"evidence_confidence": 0.85}, 100.0)
    assert notional == 150.0
    assert meta["tier"] == "high"
    assert meta["multiplier"] == 1.5


# ──────────────────────────── clamp ──────────────────────────


def test_clamp_below_min_mult(fresh_ps):
    ps = fresh_ps(
        POSITION_SIZING_ENABLED="1",
        POSITION_SIZING_MIN_MULT="0.6",
    )
    notional, meta = ps.compute_notional({"evidence_confidence": 0.0}, 100.0)
    assert notional == 60.0
    assert meta["multiplier"] == 0.6
    assert meta["raw_multiplier"] == 0.5
    assert meta["reason"] == "clamp_applied"


def test_clamp_above_max_mult(fresh_ps):
    ps = fresh_ps(
        POSITION_SIZING_ENABLED="1",
        POSITION_SIZING_MAX_MULT="1.2",
    )
    notional, meta = ps.compute_notional({"evidence_confidence": 0.99}, 100.0)
    assert notional == 120.0
    assert meta["multiplier"] == 1.2
    assert meta["raw_multiplier"] == 1.5
    assert meta["reason"] == "clamp_applied"


# ──────────────────────────── missing / invalid confidence ──────────────────────────


def test_missing_confidence_returns_base(fresh_ps):
    ps = fresh_ps(POSITION_SIZING_ENABLED="1")
    notional, meta = ps.compute_notional({}, 100.0)
    assert notional == 100.0
    assert meta["tier"] == "fallback"
    assert meta["multiplier"] == 1.0
    assert meta["reason"] == "missing_confidence"


def test_out_of_range_confidence_returns_base(fresh_ps):
    ps = fresh_ps(POSITION_SIZING_ENABLED="1")
    notional, _ = ps.compute_notional({"evidence_confidence": 1.5}, 100.0)
    assert notional == 100.0
    notional, _ = ps.compute_notional({"evidence_confidence": -0.1}, 100.0)
    assert notional == 100.0


# ──────────────────────────── custom tiers via env ──────────────────────────


def test_custom_tiers_override_default(fresh_ps):
    ps = fresh_ps(
        POSITION_SIZING_ENABLED="1",
        POSITION_SIZING_TIERS_JSON='{"0.0": 0.25, "0.5": 0.75, "0.9": 3.0}',
        POSITION_SIZING_MAX_MULT="5.0",
    )
    n, _ = ps.compute_notional({"evidence_confidence": 0.3}, 100.0)
    assert n == 25.0
    n, _ = ps.compute_notional({"evidence_confidence": 0.7}, 100.0)
    assert n == 75.0
    n, _ = ps.compute_notional({"evidence_confidence": 0.95}, 100.0)
    assert n == 300.0


def test_invalid_tiers_json_falls_back_to_default(fresh_ps):
    ps = fresh_ps(
        POSITION_SIZING_ENABLED="1",
        POSITION_SIZING_TIERS_JSON="not json",
    )
    n, meta = ps.compute_notional({"evidence_confidence": 0.85}, 100.0)
    assert n == 150.0
    assert meta["tier"] == "high"


# ──────────────────────────── action-agnostic (v1 invariant) ──────────────────────────


def test_buy_and_sell_use_same_multiplier(fresh_ps):
    ps = fresh_ps(POSITION_SIZING_ENABLED="1")
    n_buy, _ = ps.compute_notional(
        {"action": "BUY", "evidence_confidence": 0.5}, 100.0,
    )
    n_sell, _ = ps.compute_notional(
        {"action": "SELL", "evidence_confidence": 0.5}, 100.0,
    )
    assert n_buy == n_sell == 100.0


# ──────────────────────────── integration with quick_sim.open_trades ──────────────────────────


def test_open_trades_uses_scaled_notional(fresh_ps, monkeypatch):
    """端到端：open_trades 调用 compute_notional → quick_sim_trades.notional_usdt 反映。

    v231+ 注意: 本测试用 confidence=0.85 (0-1 标度, 给 position_sizing 算 tier),
    而 sample_quality_filter R7 用 0-100 标度 (gate=20). 二者标度不一致 → 本测试
    的 signal 在 QUALITY_FILTER=on 时必被 R7 拒绝. 用 monkeypatch 临时关 filter 测.
    """
    monkeypatch.setenv("QUICK_SIM_QUALITY_FILTER", "off")
    ps = fresh_ps(POSITION_SIZING_ENABLED="1")
    import sqlite3
    import db
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA foreign_keys = OFF")
    db.migrate(c)
    # v231+: 用 SQLite 自带 datetime('now', 'localtime') 写, 避免 ISO 格式与 SQL 期待不匹配
    c.execute(
        """INSERT INTO raw_news (source, content, timestamp, published_at)
           VALUES ('test', 'sizing test', datetime('now'), '')"""
    )
    news_id = c.execute("SELECT last_insert_rowid() AS id").fetchone()["id"]
    c.execute(
        """INSERT INTO ai_decisions
           (id, news_id, sentiment_score, suggested_action, reasoning,
            target_asset, evidence_confidence, trade_gate_reason,
            event_strength, event_phase, evidence_action, status, created_at)
           VALUES (1, ?, 0.0, 'BUY', '', 'XAU', 0.85, ?, 'medium',
                   'mid', 'BUY', 'AUTO_APPROVED', datetime('now', 'localtime'))""",
        (news_id, config.TRADE_GATE_PASSED_REASON),
    )
    c.commit()
    from quick_sim import open_trades
    out = open_trades(c, lambda a: 100.0, limit=10)
    assert len(out) == 1
    assert out[0]["notional"] == pytest.approx(150.0, abs=1e-3)
    row = c.execute(
        "SELECT notional_usdt FROM quick_sim_trades WHERE id=1"
    ).fetchone()
    assert row["notional_usdt"] == pytest.approx(150.0, abs=1e-3)

