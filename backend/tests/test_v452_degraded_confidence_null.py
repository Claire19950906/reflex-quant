# -*- coding: utf-8 -*-
"""v452：降级决策的置信度必须落 NULL，不许写 0 占位。

背景
----
`fake_data.py` 的 F1（置信度 0 占位）在 2026-11-19 早上还有 125 条、且每小时新增。
根因：v399 只按 evidence coverage 判定"有没有算过"，而 `llm_empty / target_none /
summary_only / server_fallback` 这些降级路径的 coverage 可能 > 0，于是仍写 0.0，
被当成"算出来是 0 的低分"参与统计 —— 违反 0 号铁律（0 不得作为有效值）。

修法：落库口径增加"降级"这一维，`analysis_degraded=1` 一律 NULL。
"""
from __future__ import annotations

import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                                "src_python"))

import confidence_availability as cav


def _guard(coverage):
    return {"confidence_detail": {"coverage": coverage}}


def test_no_coverage_writes_null():
    assert cav.db_value_for_decision(_guard(0.0), 0.0) is None


def test_real_zero_kept_when_coverage_positive():
    # coverage>0 的 0.0 是真结论（因子互相抵消），不能被洗成 NULL
    assert cav.db_value_for_decision(_guard(0.5), 0.0) == 0.0


@pytest.mark.parametrize("reason", ["llm_empty", "target_none", "summary_only",
                                    "server_fallback", "asset_late_bound"])
def test_degraded_always_null(reason):
    """降级决策即使 coverage>0、分数非 0，也必须写 NULL。"""
    assert cav.db_value_for_decision(_guard(0.9), 42.0, degraded=True) is None


def test_degraded_overrides_even_when_guard_missing():
    assert cav.db_value_for_decision(None, 0.0, degraded=True) is None


def test_gate_reason_says_unavailable_not_zero_confidence():
    txt = cav.gate_reason(True, "统一置信度 0.0 低于闸门 15.0")
    assert "统一置信度" not in txt
    assert "无可证据" in txt


def test_gate_reason_keeps_normal_fallback():
    assert cav.gate_reason(False, "strength 不足") == "strength 不足"
