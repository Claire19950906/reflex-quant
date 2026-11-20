"""v342 — 判断链颗粒度：结构因子 + confidence 三量（回归测试）

钉死的三件事：
  1. 结构因子只在真能算出时参与；缺字段一律不可得（不拿 0 当"结构中性"）
  2. 结构评分进入闸门后，方向随 AI 方向对齐（同向加分 / 反向扣分），且不再被
     market_confirmation 的 unknown 吞掉
  3. confidence 拆成 direction / reliability / odds 三量，原 confidence 不变
"""
from __future__ import annotations

import json
import os
import sys

SRC = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "src_python"))
if SRC not in sys.path:
    sys.path.insert(0, SRC)

import confidence_split
import decision_guard
import structure_factor


def _ctx(assets: dict) -> str:
    return json.dumps({"assets": assets})


def _struct_asset(**over) -> dict:
    base = {
        "status": "ok",
        "price": 100.0,
        "change_24h_pct": 1.0,
        "change_24h_str": "+1.00%",
        "funding_rate_pct": 0.01,
        "funding_rate_str": "+0.0100%",
        "stats_7d": {"highest": 110.0, "lowest": 90.0, "trend": "Strong Bull",
                     "atr_pct": 1.0, "atr_str": "1.000%"},
        "mtf": {"4h": {"trend": "Bull"}, "1h": {"trend": "Bull"}},
    }
    base.update(over)
    return base


# ─────────────────────── 1. structure_factor 纯函数 ───────────────────────

def test_structure_unavailable_without_range_or_atr():
    assert structure_factor.compute({})["available"] is False
    assert structure_factor.compute({"status": "unavailable", "price": 100})["available"] is False
    no_range = structure_factor.compute({
        "status": "ok", "price": 100.0,
        "stats_7d": {"trend": "Bull", "atr_pct": 1.0},
    })
    assert no_range["available"] is False
    no_atr = structure_factor.compute({
        "status": "ok", "price": 100.0,
        "stats_7d": {"highest": 110.0, "lowest": 90.0, "trend": "Bull", "atr_pct": 0.0},
    })
    assert no_atr["available"] is False


def test_structure_accepts_7d_key_aliases_and_partial_status():
    """v342-fix: stats_7d 对外键是 highest_7d/lowest_7d（_compute_7d_stats），
    结构因子必须兼容；partial（本地兜底价）状态也应算可用。"""
    via_alias = structure_factor.compute({
        "status": "partial", "price": 100.0,
        "stats_7d": {"highest_7d": 110.0, "lowest_7d": 90.0,
                     "trend": "Strong Bull", "atr_pct": 1.0},
        "mtf": {},
    })
    assert via_alias["available"] is True
    assert via_alias["bias"] > 0
    # 两套键都缺区间 → 仍不可得
    missing = structure_factor.compute({
        "status": "ok", "price": 100.0,
        "stats_7d": {"trend": "Bull", "atr_pct": 1.0},
    })
    assert missing["available"] is False


def test_structure_bias_follows_trend_sign():
    bull = structure_factor.compute(_struct_asset())
    assert bull["available"] is True
    assert bull["bias"] > 0
    bear = structure_factor.compute(_struct_asset(
        stats_7d={"highest": 110.0, "lowest": 90.0, "trend": "Strong Bear", "atr_pct": 1.0},
        mtf={"4h": {"trend": "Bear"}, "1h": {"trend": "Bear"}},
    ))
    assert bear["bias"] < 0


def test_extreme_position_dampens_bias():
    """贴区间上沿时不敢追多：同样多头趋势，边缘位置的偏向必须小于中位。"""
    mid = structure_factor.compute(_struct_asset())
    edge = structure_factor.compute(_struct_asset(price=109.5))
    assert edge["position_bucket"] == "high"
    assert abs(edge["bias"]) < abs(mid["bias"])


def test_tf_resonance_scales_bias():
    aligned = structure_factor.compute(_struct_asset())
    conflict = structure_factor.compute(_struct_asset(
        mtf={"4h": {"trend": "Bear"}, "1h": {"trend": "Bear"}},
    ))
    assert aligned["tf_resonance"] == "aligned"
    assert conflict["tf_resonance"] == "conflict"
    assert aligned["bias"] > conflict["bias"]


# ─────────────────────── 2. 闸门接线 ───────────────────────

def test_structure_factor_enters_gate_and_aligns_to_news():
    out = decision_guard.evaluate_decision({
        "sentiment_score": 0.8,
        "suggested_action": "BUY",
        "target_asset": "BTC",
        "decision_context": _ctx({"BTC": _struct_asset()}),
    })
    f = out["factors"]["structure"]
    assert f["available"] is True
    assert f["score"] > 0            # 多头结构 + BUY = 支持
    assert "structure" in out["available_factors"]
    assert out["structure"]["available"] is True


def test_structure_opposes_news_when_market_is_against_it():
    """空头结构 + BUY → 结构应扣分（不是缺席）。"""
    out = decision_guard.evaluate_decision({
        "sentiment_score": 0.8,
        "suggested_action": "BUY",
        "target_asset": "BTC",
        "decision_context": _ctx({"BTC": _struct_asset(
            stats_7d={"highest": 110.0, "lowest": 90.0, "trend": "Strong Bear", "atr_pct": 1.0},
            mtf={"4h": {"trend": "Bear"}, "1h": {"trend": "Bear"}},
        )}),
    })
    assert out["factors"]["structure"]["score"] < 0


def test_structure_absent_keeps_old_behaviour():
    """没有价格/区间时，结构不可得，不得凭空给分（老行为不变）。"""
    out = decision_guard.evaluate_decision({
        "sentiment_score": 0.8,
        "target_asset": "BTC",
        "decision_context": _ctx({"BTC": {
            "status": "ok", "stats_7d": {"trend": "Unknown", "atr_pct": 0.0},
        }}),
    })
    f = out["factors"]["structure"]
    assert f["available"] is False
    assert f["score"] == 0.0
    assert out["available_factors"] == ["news_sentiment"]


def test_hold_still_gets_no_direction_from_structure():
    out = decision_guard.evaluate_decision({
        "sentiment_score": 0.0,
        "suggested_action": "HOLD",
        "target_asset": "BTC",
        "decision_context": _ctx({"BTC": _struct_asset()}),
    })
    assert out["final_score"] == 0.0
    assert out["action"] == "HOLD"


# ─────────────────────── 3. confidence 三量 ───────────────────────

def test_wilson_lower_is_below_point_estimate():
    lo = confidence_split.wilson_lower(56, 100)
    assert lo is not None and lo < 0.56


def test_reliability_needs_sample():
    small = confidence_split.compute(coverage=0.6, wins=6, settled=9)
    assert small["reliability"] is None       # 9 < 门槛 10 → 只报区间
    enough = confidence_split.compute(coverage=0.6, wins=30, settled=50)
    assert enough["reliability"] is not None


def test_odds_from_expected_move_vs_atr():
    out = confidence_split.compute(coverage=0.7, expected_move_pct=1.5, atr_pct=1.0)
    assert out["odds"] == 1.5
    missing = confidence_split.compute(coverage=0.7, atr_pct=None)
    assert missing["odds"] is None


def test_gate_reports_confidence_split_without_changing_confidence():
    row = {
        "sentiment_score": 0.6,
        "target_asset": "BTC",
        "impact_curve": {"2h": 1.2},
        "expected_horizon": "intraday",
        "decision_context": _ctx({"BTC": _struct_asset()}),
    }
    out = decision_guard.evaluate_decision(row)
    split = out["confidence_split"]
    for key in ("direction", "reliability", "odds", "sample", "notes"):
        assert key in split
    assert split["odds"] is not None          # 1.2% / ATR 1.0%
    assert isinstance(out["confidence"], (int, float))   # 原值仍在


def test_expected_move_horizon_mapping():
    assert decision_guard._expected_move_pct(
        {"impact_curve": {"2h": 0.8, "1d": 2.0}, "expected_horizon": "intraday"}) == 0.8
    assert decision_guard._expected_move_pct(
        {"impact_curve": {"2h": 0.8, "1d": 2.0}, "expected_horizon": "1w+"}) == 2.0
    assert decision_guard._expected_move_pct({"impact_curve": "not-json"}) is None


# ─────────────────── 4. v342 自检：覆盖度 / 明细符号 ───────────────────

def test_coverage_never_exceeds_one():
    """可得权重之和不得超过 1.0（代理权重曾固定 0.12 导致 coverage=1.02）。"""
    out = decision_guard.evaluate_decision({
        "sentiment_score": 0.6,
        "suggested_action": "BUY",
        "target_asset": "BTC",
        "decision_context": _ctx({"BTC": _struct_asset(
            stats_7d={"highest": 110.0, "lowest": 90.0, "trend": "Strong Bear", "atr_pct": 1.0},
            mtf={"4h": {"trend": "Bear"}, "1h": {"trend": "Bear"}},
        )}),
    })
    assert out["coverage"] <= 1.0


def test_structure_detail_sign_matches_bear_trend():
    bear = structure_factor.compute(_struct_asset(
        stats_7d={"highest": 110.0, "lowest": 90.0, "trend": "Strong Bear", "atr_pct": 1.0},
        mtf={"4h": {"trend": "Bear"}, "1h": {"trend": "Bear"}},
    ))
    assert "共振(-0.5)" in bear["detail"]      # 空头共振应扣向空方
