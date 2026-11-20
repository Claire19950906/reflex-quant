#!/usr/bin/env python3
"""v396 P3：schema 只卡"内容"，不卡"排版"。

清掉的是 asset_nature / factor_ranking / reasoning 三个叙述字段的 required 身份。
理由（都有真库计数支撑）：这三项实测各失败 100+ 次（asset_nature 104 / factor_ranking 103 /
reasoning 100），而方向、标的、八段 reasoning_path 全都在——是"空骨架"不是"没分析"。
这三项下游都有**非编造**的补全路径（L2 业务知识表 / 从 [总结] 段回填）。

保留 required 的：reasoning_path（≥100 字，这条是"有没有真推演"的唯一硬证据）、
标的、动作、类别、时间锚、各类分数。
"""
from __future__ import annotations

import sys
from pathlib import Path

SRC = Path(__file__).resolve().parents[1] / "src_python"
sys.path.insert(0, str(SRC))

import llm_schema


def _core(**kw):
    base = {
        "reasoning_path": "[总框架] 供给端冲击，减产 20 万桶/日。" * 8,
        "impact_magnitude": 6,
        "sentiment_score": 1,
        "suggested_action": "BUY",
        "market_category": "OIL",
        "target_asset": "WTI",
        "prediction_type": "reversal",
        "event_phase": "early",
        "market_confirmation": "unknown",
        "expected_horizon": "intraday",
        "invalidation_condition": "若 48 小时内复产则失效",
        "event_strength": "high",
        "direct_catalyst": True,
        "heat_score": 5,
        "impact_score": 6,
        "asset_impact": 4,
        "attention_score": 3,
        "data_anchor_ts": "2026-11-16T10:00:00Z",
        "elasticity_window": {"minutes": 30, "decay": "fast", "peak_after_minutes": 10},
    }
    base.update(kw)
    return base


def test_narrative_fields_optional():
    """方向 + 八段推演齐全、叙述字段空 → 判有效（不再整条作废）。"""
    ok, errors = llm_schema.validate_ai_decision(_core())
    assert ok is True, errors


def test_empty_narrative_values_not_rejected():
    """模型把键吐出来但值是空的（"" / []）—— 不算格式错误，按"未展开"处理。"""
    ok, errors = llm_schema.validate_ai_decision(
        _core(asset_nature="", factor_ranking=[], reasoning="")
    )
    assert ok is True, errors


def test_empty_values_are_recorded_and_restored():
    """空值只是"校验期间摘掉"，校验后必须原样还原。

    还原是关键：下游的 missing_fields / completeness 要靠这些空值如实记着
    "这几项没展开"。如果校验完就把键删了，一条缺了三个叙述字段的决策会显示
    completeness=1.0 —— 那才是真的骗人。
    """
    r = _core(asset_nature="", factor_ranking=[])
    llm_schema.validate_ai_decision(r)
    assert sorted(r["_empty_narrative_fields"]) == ["asset_nature", "factor_ranking"]
    assert r["asset_nature"] == "" and r["factor_ranking"] == []


def test_nonempty_but_garbage_still_fails():
    """空值放过，但填了不合规的值照样报错（不能借这个口子洗白假数据）。"""
    ok, errors = llm_schema.validate_ai_decision(
        _core(factor_ranking=[{"factor": "供给", "rank": 1, "direction": "看多"}])
    )
    assert ok is False, errors


def test_empty_reasoning_path_still_fails():
    """八段推演是硬门槛：空/太短照样判无效。"""
    ok, errors = llm_schema.validate_ai_decision(_core(reasoning_path=""))
    assert ok is False
    assert any("reasoning_path" in e for e in errors), errors


def test_short_reasoning_path_fails():
    ok, _ = llm_schema.validate_ai_decision(_core(reasoning_path="[总框架] 没东西"))
    assert ok is False


def test_action_enum_still_enforced():
    ok, errors = llm_schema.validate_ai_decision(_core(suggested_action="MAYBE"))
    assert ok is False
    assert any("suggested_action" in e for e in errors), errors


def test_target_asset_still_enforced():
    ok, errors = llm_schema.validate_ai_decision(_core(target_asset="哈铁科技"))
    assert ok is False
    assert any("target_asset" in e for e in errors), errors


# ── v396.1：两类"如实为空"的值不再触发全量 retry ─────────────────────
def test_market_category_empty_accepted_when_target_none():
    """target_asset=NONE（非核心监控品种）时分类客观上不存在，空串合法。

    实测这一格每天逼出上百次 schema 失败 → 每次多烧一次大模型全量调用。
    空串在 _sanitize 里已是"待复核"标记 + 进 unclassified_events，是如实的没有分类。
    """
    ok, errors = llm_schema.validate_ai_decision(
        _core(market_category="", target_asset="NONE", suggested_action="HOLD")
    )
    assert ok is True, errors


def test_na_placeholder_not_rejected_and_restored():
    """'N/A' 这类"没有数据"的占位视为空：校验通过，且原值原样还原（不美化）。"""
    payload = _core(hard_data_evidence="N/A", asset_nature="不适用")
    ok, errors = llm_schema.validate_ai_decision(payload)
    assert ok is True, errors
    assert payload["hard_data_evidence"] == "N/A"
    assert payload["asset_nature"] == "不适用"
    assert "hard_data_evidence" in payload["_empty_narrative_fields"]
    assert "asset_nature" in payload["_empty_narrative_fields"]


def test_real_content_still_validated():
    """放空的是"空"，不是"错"：真填了但内容不合格照样拒。"""
    ok, errors = llm_schema.validate_ai_decision(_core(reasoning="好"))
    assert ok is False
    assert any("reasoning" in e for e in errors)
