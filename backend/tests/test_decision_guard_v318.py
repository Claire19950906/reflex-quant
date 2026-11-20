"""v318 — 交易闸门重构回归测试（修复「长期无可执行信号」）。

线上实测到的根因，逐条钉死：
  1. volatility 缺 ATR 时白送 +0.10 看多分 → final_score 80% 挤在 +0.100
  2. 权重和 1.15 + impact_boost → final_score 冲出 [-1,1]
  3. 缺失因子稀释方向 → 中位分 +0.0929，永远够不到 BUY 门槛 0.4
  4. BTC 快照顶替其它品种（512/1995 行 asset_key_absent）
  5. 「没有行情」被编码成「价格持平 = 中性确认」
  6. 影响度（无方向量度）用加法推方向
  7. HOLD 行被市场上下文造出方向
  8. sentiment_score 与 suggested_action 符号错位 → SELL 永远出不来
  9. 闸门两个真源（evidence import 时 10 vs config .env 20）→ import 顺序决定行为
"""
from __future__ import annotations

import json
import os
import sys

SRC = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "src_python"))
if SRC not in sys.path:
    sys.path.insert(0, SRC)

import decision_guard
import evidence


# ─────────────────────────── 测试夹具 ───────────────────────────

def _ctx(assets: dict) -> str:
    return json.dumps({"assets": assets})


def _asset_ctx(**over) -> dict:
    """一个「行情正常」的品种快照；over 可逐项覆盖成异常态。"""
    base = {
        "status": "ok",
        "change_24h_pct": 1.0,
        "change_24h_str": "+1.00%",
        "funding_rate_pct": 0.01,
        "funding_rate_str": "+0.0100%",
        "basis_bps": None,
        "oi_change_pct": None,
        "stats_7d": {"trend": "Unknown", "atr_pct": 0.0, "atr_str": "N/A"},
    }
    base.update(over)
    return base


# ─────────────────────── 1. 缺 ATR 不再白送看多分 ───────────────────────

def test_missing_atr_no_constant_bullish_bonus():
    """根因 1：atr_pct=0 时 volatility 不得给出 +1.0（最大看多）。"""
    out = decision_guard.evaluate_decision({
        "sentiment_score": 0.0,
        "target_asset": "BTC",
        "decision_context": _ctx({"BTC": _asset_ctx()}),
    })
    vol = out["factors"]["volatility"]
    assert vol["available"] is False
    assert vol["score"] == 0.0
    assert out["final_score"] == 0.0


def test_scores_do_not_collapse_to_constant():
    """根因 1：同样的输入不能永远得到同一个 final_score（过去 80% 都是 +0.100）。"""
    scores = set()
    for sentiment in (0.0, 0.2, 0.5, -0.3, 0.9):
        out = decision_guard.evaluate_decision({
            "sentiment_score": sentiment, "decision_context": "{}",
        })
        scores.add(out["final_score"])
    assert len(scores) > 3


# ─────────────────────── 2. 权重与取值范围 ───────────────────────

def test_directional_weights_sum_to_one():
    assert abs(sum(decision_guard.DIRECTIONAL_WEIGHTS.values()) - 1.0) < 1e-9


def test_final_score_always_within_unit_range():
    """根因 2：影响度拉满也不能冲出 [-1,1]。"""
    for sentiment in (1.0, -1.0, 0.7):
        out = decision_guard.evaluate_decision({
            "sentiment_score": sentiment,
            "target_asset": "BTC",
            "impact_score": 10, "heat_score": 10, "asset_impact": 10,
            "decision_context": _ctx({"BTC": _asset_ctx()}),
        })
        assert -1.0 <= out["final_score"] <= 1.0


# ─────────────────────── 3. 缺失因子不稀释方向 ───────────────────────

def test_missing_factors_do_not_dilute_direction():
    """根因 3：只有 sentiment 可得时，方向分就是 sentiment 本身。"""
    out = decision_guard.evaluate_decision({
        "sentiment_score": 0.8, "target_asset": "BTC", "decision_context": "{}",
    })
    assert out["final_score"] == 0.8
    assert out["coverage"] == decision_guard.DIRECTIONAL_WEIGHTS["news_sentiment"]
    assert out["available_factors"] == ["news_sentiment"]


def test_coverage_reflects_available_evidence():
    out = decision_guard.evaluate_decision({
        "sentiment_score": 0.5, "target_asset": "BTC",
        "decision_context": _ctx({"BTC": _asset_ctx()}),
    })
    assert "news_sentiment" in out["available_factors"]
    assert "market_confirmation" in out["available_factors"]   # 24h 动量代理
    assert out["coverage"] > decision_guard.DIRECTIONAL_WEIGHTS["news_sentiment"]


# ─────────────────────── 4. 不拿 BTC 顶替其它品种 ───────────────────────

def test_no_btc_substitution_for_other_asset():
    """根因 4：XAU 没有自己的快照时，不得拿 BTC 的趋势/涨跌当证据。"""
    out = decision_guard.evaluate_decision({
        "sentiment_score": 0.5, "target_asset": "XAU",
        "decision_context": _ctx({"BTC": _asset_ctx(
            stats_7d={"trend": "Strong Bull", "atr_pct": 1.0, "atr_str": "1.00%"},
        )}),
    })
    assert out["factors"]["trend"]["available"] is False
    assert out["factors"]["market_confirmation"]["available"] is False
    assert out["final_score"] == 0.5


# ─────────────────────── 5. 没有行情 ≠ 中性确认 ───────────────────────

def test_unavailable_market_is_not_neutral_confirmation():
    """根因 5：status=unavailable 时不得把「没有行情」当成「价格持平=确认」。"""
    out = decision_guard.evaluate_decision({
        "sentiment_score": 0.5, "target_asset": "BTC",
        "decision_context": _ctx({"BTC": _asset_ctx(
            status="unavailable", change_24h_pct=0.0, change_24h_str="N/A",
            funding_rate_pct=0.0, funding_rate_str="N/A",
        )}),
    })
    assert out["factors"]["market_confirmation"]["available"] is False
    assert out["factors"]["funding"]["available"] is False
    assert out["final_score"] == 0.5


def test_real_flat_price_is_a_valid_neutral_reading():
    """对照：真的读到 0.00% 涨跌 → 代理有效（得分 0，方向中性）。"""
    out = decision_guard.evaluate_decision({
        "sentiment_score": 0.5, "target_asset": "BTC",
        "decision_context": _ctx({"BTC": _asset_ctx(
            change_24h_pct=0.0, change_24h_str="+0.00%",
        )}),
    })
    conf = out["factors"]["market_confirmation"]
    assert conf["available"] is True
    assert conf["score"] == 0.0


# ─────────────────────── 6. 影响度不再推方向 ───────────────────────

def test_impact_never_flips_direction():
    """根因 6：影响度是无方向量度，只能放大幅度，不能翻转符号。"""
    base = {"sentiment_score": -0.6, "target_asset": "BTC", "decision_context": "{}"}
    low = decision_guard.evaluate_decision(dict(base, impact_score=1, heat_score=1, asset_impact=1))
    high = decision_guard.evaluate_decision(dict(base, impact_score=10, heat_score=10, asset_impact=10))
    assert low["final_score"] < 0.0
    assert high["final_score"] < 0.0
    assert abs(high["final_score"]) > abs(low["final_score"])


def test_impact_cannot_create_direction_without_ai_view():
    out = decision_guard.evaluate_decision({
        "sentiment_score": 0.0, "impact_score": 10, "heat_score": 10,
        "asset_impact": 10, "decision_context": "{}",
    })
    assert out["final_score"] == 0.0
    assert out["raw_action"] == "HOLD"


# ─────────────────────── 7. 锚点规则：AI 没观点就没有方向 ───────────────────────

def test_hold_cannot_get_direction_from_market_context():
    """根因 7：AI 观望时，市场偏空不得把它变成 SELL 单。"""
    out = decision_guard.evaluate_decision({
        "sentiment_score": 0.0, "target_asset": "BTC",
        "decision_context": _ctx({"BTC": _asset_ctx(
            stats_7d={"trend": "Strong Bear", "atr_pct": 2.0, "atr_str": "2.00%"},
        )}),
    })
    assert out["final_score"] == 0.0
    assert out["raw_action"] == "HOLD"
    assert out["action"] == "HOLD"
    # 市场的独立读数仍然记录，供研究端查看
    assert out["market_bias"] is not None
    assert out["market_bias"] < 0.0


def test_quality_factors_do_not_change_direction():
    """cluster_heat / historical_confidence 只调 confidence，不动方向。"""
    plain = decision_guard.evaluate_decision({
        "sentiment_score": 0.5, "decision_context": "{}", "cluster_size": 1,
    })
    rich = decision_guard.evaluate_decision({
        "sentiment_score": 0.5, "decision_context": "{}", "cluster_size": 5,
    })
    assert plain["final_score"] == rich["final_score"]
    assert rich["confidence"] > plain["confidence"]      # 同族新闻佐证 → 更可信


def test_history_sample_raises_confidence_not_direction():
    """历史胜率显著（χ² 通过）→ 置信度提高，方向不变。"""
    plain = decision_guard.evaluate_decision({
        "sentiment_score": 0.5, "decision_context": "{}",
    })
    proven = decision_guard.evaluate_decision({
        "sentiment_score": 0.5, "decision_context": "{}",
        "history_sample": {"total": 20, "wins": 20, "scope": "asset"},
    })
    assert plain["final_score"] == proven["final_score"]
    assert proven["confidence"] > plain["confidence"]
    assert plain["factors"]["historical_confidence"]["available"] is False
    assert proven["factors"]["historical_confidence"]["available"] is True


# ─────────────────────── 8. 分数与方向符号对齐 ───────────────────────

def test_sign_alignment_with_suggested_action():
    """根因 8：suggested_action=SELL 但分数为正 → 按方向对齐。"""
    out = decision_guard.evaluate_decision({
        "sentiment_score": 0.5, "suggested_action": "SELL",
        "target_asset": "BTC", "decision_context": "{}",
    })
    assert out["final_score"] < 0.0
    assert out["raw_action"] == "SELL"
    assert "对齐" in out["factors"]["news_sentiment"]["explanation"]


def test_aligned_score_is_not_double_flipped():
    out = decision_guard.evaluate_decision({
        "sentiment_score": -0.5, "suggested_action": "SELL",
        "target_asset": "BTC", "decision_context": "{}",
    })
    assert out["final_score"] == -0.5


# ─────────────────────── 9. 闸门单一真源 ───────────────────────

def test_confidence_gate_follows_process_env():
    os.environ["EVIDENCE_CONFIDENCE_GATE"] = "23.5"
    try:
        assert evidence.confidence_gate() == 23.5
    finally:
        os.environ.pop("EVIDENCE_CONFIDENCE_GATE", None)


def test_guard_uses_live_gate_not_import_snapshot():
    """闸门必须在调用时求值，不能被 import 顺序冻结。"""
    os.environ["EVIDENCE_CONFIDENCE_GATE"] = "50"
    try:
        out = decision_guard.evaluate_decision({
            "sentiment_score": 0.45, "target_asset": "BTC", "decision_context": "{}",
        })
        assert out["confidence_detail"]["gate"] == 50.0
        assert out["action"] == "HOLD"      # 27 < 50 → 不放行
    finally:
        os.environ.pop("EVIDENCE_CONFIDENCE_GATE", None)


def test_evidence_evaluate_keeps_old_signature():
    """向后兼容：旧的调用方式（不传 coverage/risk/corroboration）仍然可用。"""
    out = evidence.evaluate(
        {"news_sentiment": {"score": 0.5}}, 0.5, "BUY", 0, 0,
    )
    assert out["gated_action"] in ("BUY", "HOLD")
    assert "confidence_detail" in out


def test_evidence_evaluate_accepts_quality_inputs():
    out = evidence.evaluate(
        {"news_sentiment": {"score": 0.5}}, 0.5, "BUY", 0, 0,
        coverage=0.4, risk_factor=0.95, corroboration_factor=1.04,
    )
    detail = out["confidence_detail"]
    assert detail["coverage"] == 0.4
    assert detail["risk_factor"] == 0.95
    assert detail["corroboration_factor"] == 1.04


def test_gate_verdict_exclude_still_blocks():
    out = decision_guard.evaluate_decision({
        "sentiment_score": 0.9, "gate_verdict": "排除",
        "target_asset": "BTC", "decision_context": "{}",
    })
    assert out["action"] == "HOLD"
    assert out["gate_blocked"] is True
