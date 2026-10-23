"""v171 llm_schema ISO-8601 强校验 — 单元测试。

覆盖：
- AI_DECISION_SCHEMA 中 data_anchor_ts / impact_curve / event_window_minutes 字段定义
- validate_time_fields 后置强校验（拒绝模糊时间）
- validate_window_minutes 后置强校验（范围 / 类型）
- 完整 schema 校验仍能 pass（含新字段）
"""
from __future__ import annotations

import os
import sys

SRC = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "src_python"))
if SRC not in sys.path:
    sys.path.insert(0, SRC)

import llm_schema


# ────────────────────────────────────────────────────────────────────────────
# AI_DECISION_SCHEMA 字段完整性
# ────────────────────────────────────────────────────────────────────────────

def test_schema_has_iso8601_pattern_for_data_anchor():
    da = llm_schema.AI_DECISION_SCHEMA["properties"]["data_anchor_ts"]
    assert "pattern" in da, "data_anchor_ts 应有 ISO-8601 pattern"
    pat = da["pattern"]
    assert "N/A" in pat
    assert "\\d{4}-\\d{2}-\\d{2}" in pat
    assert "Z" in pat or "[+\\-]" in pat


def test_schema_has_impact_curve():
    props = llm_schema.AI_DECISION_SCHEMA["properties"]
    assert "impact_curve" in props
    ic = props["impact_curve"]
    assert ic["type"] == "object"
    for wl in ("5m", "30m", "2h", "1d"):
        assert wl in ic["properties"]


def test_schema_has_event_window_minutes():
    props = llm_schema.AI_DECISION_SCHEMA["properties"]
    assert "event_window_minutes" in props
    ewm = props["event_window_minutes"]
    assert ewm["maximum"] == 4320
    assert ewm["minimum"] == 0


# ────────────────────────────────────────────────────────────────────────────
# validate_time_fields 后置强校验
# ────────────────────────────────────────────────────────────────────────────

def test_validate_time_fields_accepts_iso():
    ok, errs = llm_schema.validate_time_fields(
        {"data_anchor_ts": "2025-01-15 09:30:00"}
    )
    assert ok is True
    assert errs == []


def test_validate_time_fields_accepts_iso_with_z():
    ok, errs = llm_schema.validate_time_fields(
        {"data_anchor_ts": "2025-01-15T09:30:00Z"}
    )
    assert ok is True


def test_validate_time_fields_accepts_na():
    ok, errs = llm_schema.validate_time_fields({"data_anchor_ts": "N/A"})
    assert ok is True


def test_validate_time_fields_accepts_empty():
    ok, errs = llm_schema.validate_time_fields({"data_anchor_ts": ""})
    assert ok is True


def test_validate_time_fields_accepts_missing():
    ok, errs = llm_schema.validate_time_fields({})
    assert ok is True


def test_validate_time_fields_rejects_vague_text():
    for vague in ["上周五", "昨晚", "刚刚", "now", "2 hours ago"]:
        ok, errs = llm_schema.validate_time_fields({"data_anchor_ts": vague})
        assert ok is False, f"应被拒: {vague!r}"
        assert any("data_anchor_ts" in e for e in errs)


def test_validate_time_fields_rejects_short_format():
    ok, errs = llm_schema.validate_time_fields({"data_anchor_ts": "2025-01-15 09:30"})
    assert ok is False


def test_validate_time_fields_rejects_non_string():
    ok, errs = llm_schema.validate_time_fields({"data_anchor_ts": 2025011509})
    assert ok is False


def test_validate_time_fields_accepts_non_dict():
    ok, errs = llm_schema.validate_time_fields("not a dict")
    assert ok is True


# ────────────────────────────────────────────────────────────────────────────
# validate_window_minutes 后置强校验
# ────────────────────────────────────────────────────────────────────────────

def test_validate_window_minutes_accepts_int():
    ok, errs = llm_schema.validate_window_minutes({"event_window_minutes": 120})
    assert ok is True


def test_validate_window_minutes_accepts_zero():
    ok, errs = llm_schema.validate_window_minutes({"event_window_minutes": 0})
    assert ok is True


def test_validate_window_minutes_accepts_max():
    ok, errs = llm_schema.validate_window_minutes({"event_window_minutes": 4320})
    assert ok is True


def test_validate_window_minutes_rejects_negative():
    ok, errs = llm_schema.validate_window_minutes({"event_window_minutes": -1})
    assert ok is False


def test_validate_window_minutes_rejects_over_max():
    ok, errs = llm_schema.validate_window_minutes({"event_window_minutes": 4321})
    assert ok is False


def test_validate_window_minutes_rejects_bool():
    ok, errs = llm_schema.validate_window_minutes({"event_window_minutes": True})
    assert ok is False


def test_validate_window_minutes_accepts_na_string():
    ok, errs = llm_schema.validate_window_minutes({"event_window_minutes": "N/A"})
    assert ok is True


def test_validate_window_minutes_rejects_other_string():
    ok, errs = llm_schema.validate_window_minutes({"event_window_minutes": "120m"})
    assert ok is False


def test_validate_window_minutes_accepts_missing():
    ok, errs = llm_schema.validate_window_minutes({})
    assert ok is True


# ────────────────────────────────────────────────────────────────────────────
# validate_ai_decision 全字段校验（新字段含入）
# ────────────────────────────────────────────────────────────────────────────

def _full_decision(**overrides) -> dict:
    base = {
        "reasoning_path": "x" * 120,
        "asset_nature": "test",
        "factor_ranking": [{"factor": "test", "rank": 1, "direction": "中性"}],
        "impact_magnitude": 50,
        "asset_reasoning": "test",
        "sentiment_score": 0.1,
        "suggested_action": "HOLD",
        "reasoning": "test",
        "market_category": "OTHER",
        "target_asset": "BTC",
        "prediction_type": "continuation",
        "event_phase": "early",
        "market_confirmation": "unknown",
        "expected_horizon": "intraday",
        "invalidation_condition": "test",
        "event_strength": "medium",
        "direct_catalyst": False,
        "heat_score": 5.0,
        "impact_score": 5.0,
        "asset_impact": 5.0,
        "attention_score": 50,
        "elasticity_window": {
            "minutes": 60, "decay": "medium", "peak_after_minutes": 10,
        },
        "data_anchor_ts": "2025-01-15 09:30:00",
        "hard_data_evidence": "test",
        "event_type_code": "macro_rates",
        "verification": "pass | 一致",
        "snapshot_proximity": 5,
        # v181 结构化产出（准优先）3 个判断项
        "asset_classification": "高Beta",
        "fund_driver": "测试驱动力",
        "consensus_vs_us": "一致",
        # v171 新字段
        "impact_curve": {"5m": 0.5, "30m": 0.4, "2h": 0.2, "1d": 0.1},
        "event_window_minutes": 120,
    }
    base.update(overrides)
    return base


def test_validate_ai_decision_with_new_fields():
    """含 impact_curve / event_window_minutes 的完整 JSON 仍能通过 schema。"""
    result = _full_decision()
    ok, errs = llm_schema.validate_ai_decision(result)
    if not ok:
        for e in errs[:5]:
            print("SCHEMA ERR:", e)
    assert ok is True, f"schema 校验失败: {errs[:3]}"


def test_validate_ai_decision_with_vague_time_fails():
    """data_anchor_ts 填 '上周五' → schema pattern 拒（apply_fallback=False 时严格模式）。"""
    result = _full_decision(data_anchor_ts="上周五")
    ok, errs = llm_schema.validate_ai_decision(result, apply_fallback=False)
    assert ok is False
    assert any("data_anchor_ts" in e for e in errs)


def test_validate_ai_decision_with_huge_window_fails():
    """event_window_minutes = 99999 超出 4320 → 拒（apply_fallback=False 严格模式）。"""
    result = _full_decision(event_window_minutes=99999)
    ok, errs = llm_schema.validate_ai_decision(result, apply_fallback=False)
    assert ok is False


def test_validate_ai_decision_fallback_saves_vague_time():
    """apply_fallback=True (默认) → 模糊时间被替换为 ISO 当前时间 + 标记 _fallback_used=True。"""
    import clock as _clock
    _clock._reset_state_for_tests()
    result = _full_decision(data_anchor_ts="上周五")
    ok, errs = llm_schema.validate_ai_decision(result, apply_fallback=True)
    assert ok is True, errs
    assert result.get("_fallback_used") is True
    # data_anchor_ts 应被替换为 ISO 严格格式
    import tz_utils
    chk = tz_utils.validate_iso8601(result["data_anchor_ts"])
    assert chk["ok"] is True


def test_validate_ai_decision_fallback_saves_huge_window():
    """apply_fallback=True → 越界 window 被替换为 60 + _fallback_used=True。"""
    result = _full_decision(event_window_minutes=99999)
    ok, errs = llm_schema.validate_ai_decision(result, apply_fallback=True)
    assert ok is True, errs
    assert result.get("_fallback_used") is True
    assert result["event_window_minutes"] == 60


def test_validate_ai_decision_fallback_fills_missing_impact_curve():
    """apply_fallback=True → impact_curve 缺省 / 空 → 4 窗口零值占位（满足 minProperties=1）。"""
    result = _full_decision()
    # 空 dict 也应被 fallback
    result["impact_curve"] = {}
    ok, errs = llm_schema.validate_ai_decision(result, apply_fallback=True)
    assert ok is True, errs
    assert result.get("_fallback_used") is True
    assert set(result["impact_curve"].keys()) == {"5m", "30m", "2h", "1d"}