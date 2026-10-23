# -*- coding: utf-8 -*-
"""v200 P0+ 完整性过滤测试."""
from __future__ import annotations

import json
import sys
import os

_BACKEND = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_SRC_PYTHON = os.path.join(_BACKEND, "src_python")
if _SRC_PYTHON not in sys.path:
    sys.path.insert(0, _SRC_PYTHON)

import reflection_checkers as rc


def test_run_all_checkers_skips_low_completeness():
    """completeness<0.7 时 run_all_checkers 跳过其他 checker, 只返回 E019."""
    decision = {
        "id": 1, "decision_id": 1,
        "completeness_score": 0.5,
        "field_sources": "{}",
        "missing_fields": "[\"reasoning\"]",
    }
    results = rc.run_all_checkers(decision)
    assert len(results) == 1
    assert results[0]["error_code"] == "E019"
    assert results[0]["sub_layer"] == "completeness"


def test_run_all_checkers_runs_normal_at_threshold():
    """completeness>=0.7 时正常运行所有 checker."""
    decision = {
        "id": 1, "decision_id": 1,
        "completeness_score": 0.7,
        "field_sources": json.dumps({"reasoning": "llm"}),
        "missing_fields": "[]",
        "sentiment_score": 0.5, "reasoning": "real reasoning",
        "factor_ranking": [{"factor": "X", "rank": 1, "direction": "中性"}],
        "hard_data_evidence": "real evidence",
    }
    results = rc.run_all_checkers(decision)
    # 至少 1 个结果 (不一定有错, 但不会只有 E019)
    assert not any(r.get("error_code") == "E019" for r in results), \
        "高 completeness 不应返回 E019"


def test_e019_low_completeness_marks_data_source():
    """E019 必须打 source='data' + severity=INFO(低权重, 不污染反思)."""
    decision = {
        "id": 42, "decision_id": 42,
        "completeness_score": 0.3,
        "field_sources": "{}",
        "missing_fields": "[\"reasoning\", \"asset_reasoning\"]",
    }
    results = rc.run_all_checkers(decision)
    assert len(results) == 1
    e019 = results[0]
    assert e019["source"] == "data"
    assert e019["severity"] == rc.SEVERITY_INFO
    assert e019["decision_id"] == 42
    # detail 包含 completeness 数值
    assert "0.3" in e019["detail"]


def test_e019_weight_low_priority():
    """E019 权重低, 不阻塞其他反思事件."""
    decision = {
        "id": 1, "decision_id": 1,
        "completeness_score": 0.5,
        "field_sources": "{}",
        "missing_fields": "[]",
    }
    results = rc.run_all_checkers(decision)
    e019_weight = results[0]["weight"]
    # E019 应该 weight 比 E001/E014 等常规错误低
    e001_sample_weight = rc._compute_weight(rc.SEVERITY_INFO, "data")  # same layer
    assert e019_weight == e001_sample_weight, "E019 should be data layer low weight"
