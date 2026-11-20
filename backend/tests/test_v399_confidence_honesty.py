#!/usr/bin/env python3
"""v399 回归：「没有证据」与「置信度低」必须是两件事。

守的三件事：
1. `confidence_availability` 只在 coverage>0 时才认为置信度"算过"；
   coverage=0（降级路径 / guard 兜底）落库必须是 NULL，不是 0.0。
2. 「算过且为 0」的 0.0 必须原样保留 —— 不能把真结论洗成缺失。
3. 闸门原因必须能把两者说开：没算过不许报「统一置信度 0.0 低于闸门」。

（反思语料的 A/B vs C 拆分沿用 v398 的 `insight_pool`，不在此文件重复定义。）
"""
from __future__ import annotations

import sys
from pathlib import Path

SRC = Path(__file__).resolve().parents[1] / "src_python"
sys.path.insert(0, str(SRC))

import confidence_availability as cav


# ─────────────────── 置信度：有 / 无 ───────────────────


def _guard(coverage, confidence=0.0, detail=True):
    d = {"confidence": confidence}
    if detail:
        d["confidence_detail"] = {"coverage": coverage, "passed_gate": False}
    return d


def test_coverage_zero_is_unavailable():
    assert cav.is_unavailable(_guard(0.0)) is True


def test_coverage_positive_is_available():
    assert cav.is_unavailable(_guard(0.35)) is False


def test_missing_detail_is_unavailable():
    # guard 异常兜底里没有 confidence_detail → 无法证明有证据 → 视为没算
    assert cav.is_unavailable({"confidence": 0.0, "verdict": "guard_pre_failed:X"}) is True
    assert cav.is_unavailable(None) is True
    assert cav.is_unavailable("not-a-dict") is True


def test_bad_coverage_value_is_unavailable():
    assert cav.is_unavailable(_guard("abc")) is True


def test_db_value_writes_null_when_unavailable():
    assert cav.db_value(_guard(0.0), 0.0) is None


def test_db_value_keeps_real_zero():
    # 算过（coverage>0）且结论就是 0 —— 这是真结论，不许洗成 NULL
    assert cav.db_value(_guard(0.25), 0.0) == 0.0


def test_db_value_keeps_number_when_available():
    assert cav.db_value(_guard(0.9, 27.29), 27.29) == 27.29


def test_db_value_none_when_computed_not_numeric():
    assert cav.db_value(_guard(0.9), "n/a") is None


def test_gate_reason_distinguishes_no_evidence_from_low_score():
    no_ev = cav.gate_reason(True, "统一置信度 0.0 低于闸门 15.0")
    low = cav.gate_reason(False, "统一置信度 3.2 低于闸门 15.0")
    assert "无可证据" in no_ev and "统一置信度" not in no_ev
    assert low == "统一置信度 3.2 低于闸门 15.0"
