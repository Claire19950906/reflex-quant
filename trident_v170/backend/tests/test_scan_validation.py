"""全面扫描验证测试：证据/校验层、决策闸门、快速模拟、归因解析的边界与异常。

本测试聚焦纯函数（不依赖外部服务 / DB / 网络），覆盖：
  · evidence       —— 归一化 / 卡方检验除零与越界 / 矛盾检测 / 置信度合并
  · decision_guard —— 非法输入的兜底、缺字段的最小行评估
  · quick_sim      —— 盈亏统计的除零保护（decided=0、空样本）
  · review_agent   —— JSON 解析（正常/损坏/markdown）、观测性越界 code、硬条件归因
"""

import os
import sys

SRC = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "src_python"))
if SRC not in sys.path:
    sys.path.insert(0, SRC)

import evidence
import decision_guard
import quick_sim
import review_agent as r


# ───────────────────────── evidence ─────────────────────────

def test_to_evidence_score_clamps_and_handles_bad_input():
    assert evidence.to_evidence_score(0.0) == 5.0
    assert evidence.to_evidence_score(1.0) == 10.0
    assert evidence.to_evidence_score(-1.0) == 0.0
    assert evidence.to_evidence_score(2.0) == 10.0          # 越界 clamp
    assert evidence.to_evidence_score("not-a-number") == 5.0  # 非法 → 中性 0 分
    assert evidence.to_evidence_score(None) == 5.0


def test_normalize_asset_aliases_and_empty():
    assert evidence.normalize_asset("XAUUSD") == "XAU"
    assert evidence.normalize_asset("gold") == "XAU"
    assert evidence.normalize_asset(" BTC ") == "BTC"
    assert evidence.normalize_asset("") == ""
    assert evidence.normalize_asset("NONE") == ""
    assert evidence.normalize_asset(None) == ""


def test_chi_square_insufficient_sample_no_division():
    for total in (0, 1, 7):
        res = evidence.chi_square_test(0, total)
        assert res["sufficient_sample"] is False
        assert res["p_value"] == 1.0
        assert res["chi_square"] == 0.0


def test_chi_square_clamps_wins_to_total():
    # wins > total 时被 clamp，绝不产生负数/崩溃
    res = evidence.chi_square_test(999, 10)
    assert res["wins"] == 10
    assert res["losses"] == 0
    assert res["sample_size"] == 10


def test_chi_square_significance():
    res = evidence.chi_square_test(15, 20)
    assert res["sufficient_sample"] is True
    assert res["p_value"] <= 0.05
    assert res["significant"] is True


def test_detect_contradictions_conflicting_directions():
    factors = {
        "news_sentiment": {"score": 0.8},
        "trend": {"score": -0.8},
        "market_confirmation": {"score": -0.6},
        "funding": {"score": -0.7},
    }
    cont = evidence.detect_contradictions(factors)
    assert any(c["type"] == "sentiment_vs_trend" for c in cont)
    assert any(c["type"] == "sentiment_vs_confirmation" for c in cont)
    assert any(c["type"] == "sentiment_vs_funding" for c in cont)


def test_unified_confidence_caps_and_penalties():
    test = {"sufficient_sample": True, "p_value": 0.01}
    conf = evidence.unified_confidence(
        1.0, test,
        [{"type": "x"}, {"type": "y"}, {"type": "z"}, {"type": "w"}],
    )
    assert 0 <= conf["confidence"] <= 100
    assert conf["contradiction_penalty"] <= evidence._MAX_CONTRADICTION_PENALTY


# ─────────────────────── decision_guard ───────────────────────

def test_decision_guard_clamp_bad_input():
    assert decision_guard._clamp("abc") == 0.0
    assert decision_guard._clamp(None) == 0.0
    assert decision_guard._clamp(2.0) == 1.0
    assert decision_guard._clamp10(None) == 5.0
    assert decision_guard._clamp10(99) == 10.0


def test_decision_guard_evaluate_minimal_row_no_crash():
    out = decision_guard.evaluate_decision({})
    assert "final_score" in out
    assert "action" in out
    assert "verdict" in out
    assert out["action"] in ("BUY", "SELL", "HOLD")


def test_decision_guard_evaluate_bad_context_json():
    out = decision_guard.evaluate_decision(
        {"decision_context": "not-json", "sentiment_score": "x"}
    )
    assert "final_score" in out  # 非法值被 _clamp 兜底，不崩溃


# ──────────────────────── quick_sim ────────────────────────

def test_profitability_all_breakeven_no_division_by_zero():
    rows = [
        {"verdict": "BREAKEVEN", "pnl_pct": 0.0, "pnl_usdt": 0.0, "notional_usdt": 1000},
    ]
    out = quick_sim._profitability(rows, 1000)
    assert out["wins"] == 0 and out["losses"] == 0
    assert out["breakeven"] == 1
    assert out["winrate_pct"] is None


def test_profitability_empty_rows():
    out = quick_sim._profitability([], 1000)
    assert out["winrate_pct"] is None
    assert out["avg_pnl_pct"] is None
    assert out["profitable"] is None


def test_profitability_mixed():
    rows = [
        {"verdict": "WIN", "pnl_pct": 5.0, "pnl_usdt": 50.0, "notional_usdt": 1000},
        {"verdict": "LOSS", "pnl_pct": -2.0, "pnl_usdt": -20.0, "notional_usdt": 1000},
        {"verdict": "BREAKEVEN", "pnl_pct": 0.0, "pnl_usdt": 0.0, "notional_usdt": 1000},
    ]
    out = quick_sim._profitability(rows, 1000)
    assert out["wins"] == 1 and out["losses"] == 1
    assert out["winrate_pct"] == 50.0


# ─────────────────────── review_agent ───────────────────────

def test_parse_json_valid():
    assert r._parse_json('{"a": 1}') == {"a": 1}
    assert r._parse_json("[1, 2]") == [1, 2]


def test_parse_json_markdown_wrapped():
    assert r._parse_json('```json\n{"a": 1}\n```') == {"a": 1}


def test_parse_json_garbage_returns_none():
    assert r._parse_json("not json at all") is None
    assert r._parse_json("") is None
    assert r._parse_json(None) is None


def test_reason_observability_out_of_range_falls_back():
    assert r.reason_observability(9999) == "llm"
    assert r.reason_observability(0) == "llm"
    assert r.reason_observability(-1) == "llm"


def test_rule_based_failure_hard_conditions():
    assert r._rule_based_failure({})["reason_code"] == 1
    assert r._rule_based_failure({"decision_context": "{}"})["reason_code"] == 1
    assert r._rule_based_failure({"decision_context": '{"status":"ok"}'}) is None