# -*- coding: utf-8 -*-
"""v200 P0+ 严谨兜底 — ai_worker fallback 单元测试."""
from __future__ import annotations

import json
import sys
import os

_BACKEND = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_SRC_PYTHON = os.path.join(_BACKEND, "src_python")
if _SRC_PYTHON not in sys.path:
    sys.path.insert(0, _SRC_PYTHON)

from engine import ai_worker


def test_sanitize_marks_llm_fields():
    """LLM 真输出字段打 source=llm."""
    r = {
        "suggested_action": "BUY", "target_asset": "BTC",
        "sentiment_score": 0.7, "event_type_code": "macro_policy",
        "reasoning": "BTC bullish", "asset_nature": "BTC store of value",
        "verification": "pass | real LLM",
        "hard_data_evidence": "BTC 5m: 50000->50500",
    }
    ai_worker._sanitize_llm_result(r)
    fs = json.loads(r["field_sources"])
    assert fs.get("reasoning") == "llm"
    assert fs.get("asset_nature") == "llm"
    assert fs.get("verification") == "llm"
    assert fs.get("hard_data_evidence") == "llm"
    assert r["completeness_score"] == 1.0


def test_auto_fill_uses_rule_fallback():
    """LLM 漏填字段用 L1 规则兜底."""
    r = {}
    ai_worker._auto_fill_required_fields(
        r, snapshot_ts_iso="2024-01-01T00:00:00Z",
        evidence_lines=["BTC 5m: 50000->50500"],
    )
    fs = json.loads(r["field_sources"])
    assert fs.get("data_anchor_ts") == "rule_snapshot_ts"
    assert fs.get("hard_data_evidence") == "rule_kline_snapshot"
    assert fs.get("elasticity_window") == "rule_default_window_v176"
    assert json.loads(r["missing_fields"]) == []
    assert r["completeness_score"] == 1.0


def test_presanitize_does_not_write_fake_strings():
    """_presanitize 不再写 X1 假字符串."""
    r = {
        "suggested_action": "BUY", "target_asset": "BTC",
        "sentiment_score": 0.7, "event_type_code": "macro_policy",
        "asset_nature": "", "asset_reasoning": "", "reasoning": "",
        "invalidation_condition": "", "factor_ranking": [],
        "verification": "", "hard_data_evidence": "",
    }
    ai_worker._sanitize_llm_result(r)
    ai_worker._presanitize_for_schema(r)
    for k, v in r.items():
        if isinstance(v, str):
            assert "服务端兜底" not in v, f"X1 leak {k}={v!r}"
        elif isinstance(v, list):
            for it in v:
                if isinstance(it, dict):
                    for vk, vv in it.items():
                        if isinstance(vv, str):
                            assert "服务端兜底" not in vv
    mf = json.loads(r.get("missing_fields") or "[]")
    assert len(mf) > 0


def test_extract_factors_fallback_uses_business_kb():
    """_extract_factors_fallback 缺时优先 LLM 抽取, 否则 L2 业务知识, 否则 L5 留空.

    关键不变量: 返回值不应包含假字符串.
    """
    r = {"suggested_action": "BUY", "target_asset": "BTC",
         "asset_nature": "", "reasoning": ""}
    ff = ai_worker._extract_factors_fallback(r)
    # 业务知识兜底不强制返回 (测试 DB 是空的, 生产 DB 有数据)
    # 但若有, 不应是假字符串
    if ff:
        for f in ff:
            assert f["factor"] != "服务端兜底因素"
            assert "服务端兜底" not in f["factor"]


def test_extract_factors_fallback_returns_empty_when_no_data():
    r = {"suggested_action": "BUY", "target_asset": "ZZZ_UNKNOWN",
         "asset_nature": "", "reasoning": ""}
    ff = ai_worker._extract_factors_fallback(r)
    assert ff == []


def test_mark_field_missing_dedup():
    r = {}
    ai_worker._mark_field_missing(r, "reasoning")
    ai_worker._mark_field_missing(r, "reasoning")
    ai_worker._mark_field_missing(r, "verification")
    mf = json.loads(r["missing_fields"])
    assert mf == ["reasoning", "verification"]


def test_count_sources_by_category():
    fs = {
        "reasoning": "llm", "verification": "rule_action_score",
        "asset_nature": "business_knowledge_causal_graph",
        "f1": "historical_pattern", "f2": "llm_ensemble_consensus",
        "f3": "missing", "f4": "rule_kline",
    }
    counts = ai_worker._count_sources(fs)
    assert counts == {"llm": 1, "rule": 2, "business_kb": 1,
                      "historical": 1, "ensemble": 1, "missing": 1}


def test_business_knowledge_drivers_no_fake():
    """_fallback_business_knowledge_drivers 不写假字符串.

    测试 DB 为空时可能返回 None — 这是正确的 L5 行为.
    生产 DB 有 asset_driver_nodes 数据时应返回真实 drivers.
    """
    for asset in ["BTC", "XAU", "WTI", "ETH", "SOL", "ZZZ_UNKNOWN"]:
        drivers = ai_worker._fallback_business_knowledge_drivers(asset)
        # 不论返回什么, 都不应包含"服务端兜底"
        if drivers:
            for d in drivers:
                assert "服务端兜底" not in d, f"{asset} fake: {d}"


def test_no_fake_string_in_full_sanitize_chain():
    r = {
        "suggested_action": "BUY", "target_asset": "BTC",
        "sentiment_score": 0.7, "event_type_code": "macro_policy",
        "asset_nature": "", "asset_reasoning": "", "factor_ranking": [],
        "invalidation_condition": "", "verification": "",
        "hard_data_evidence": "", "data_anchor_ts": "",
        "elasticity_window": "", "attention_score": -1.0,
        "impact_magnitude": -1.0,
    }
    ai_worker._sanitize_llm_result(r)
    ai_worker._auto_fill_required_fields(r)
    ai_worker._presanitize_for_schema(r)

    def _scan(obj, path=""):
        if isinstance(obj, dict):
            for k, v in obj.items():
                _scan(v, f"{path}.{k}")
        elif isinstance(obj, list):
            for i, it in enumerate(obj):
                _scan(it, f"{path}[{i}]")
        elif isinstance(obj, str):
            assert "服务端兜底" not in obj, f"X1 leak {path}={obj!r}"
            assert "LLM 未填" not in obj, f"X1 leak {path}={obj!r}"
    _scan(r)
    assert r["completeness_score"] < 1.0


def test_init_result_completeness_idempotent():
    r1 = {}
    ai_worker._init_result_completeness(r1)
    assert r1["field_sources"] == "{}"
    assert r1["missing_fields"] == "[]"
    assert r1["completeness_score"] == 1.0
    r2 = {"completeness_score": 0.5}
    ai_worker._init_result_completeness(r2)
    assert r2["completeness_score"] == 0.5
