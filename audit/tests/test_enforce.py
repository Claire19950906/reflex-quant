"""enforce 策略测试（纯函数，未接线）。"""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from audit import enforce
from audit.examples.selftest import DEGRADED


def _report(findings, verdict="reject"):
    return {"verdict": verdict, "findings": findings}


def _f(code="E014", severity=3, basis="decision", layer="event_causality"):
    return {"code": code, "severity": severity, "basis": basis, "layer": layer, "detail": "x" * 10}


def test_reject_direction_is_downgraded_to_hold():
    plan = enforce.resolve(_report([_f()]), {"id": 11816, "suggested_action": "SELL"})
    assert plan["should_override"] is True
    assert plan["override"] == "HOLD"
    assert plan["reasons"][0]["code"] == "E014"


def test_hold_is_never_overridden():
    plan = enforce.resolve(_report([_f()]), {"id": 1, "suggested_action": "HOLD"})
    assert plan["should_override"] is False
    assert plan["override"] is None


def test_noise_code_does_not_block(tmp_path):
    """E001a severity 2 = 缺硬数据，是管道噪音；它不该把方向单拦死。"""
    noisy = _report([_f("E001a", severity=2, layer="data")] * 5, verdict="warn")
    plan = enforce.resolve(noisy, {"id": 2, "suggested_action": "BUY"})
    assert plan["should_override"] is False, "warn 级别的噪音不能触发拦截"


def test_outcome_based_code_is_ignored_in_pretrade_policy():
    """事后型码（靠盈亏才能判）不能用来拦交易——否则就是拿结果倒推。"""
    outcome = _report([_f("E007", severity=3, basis="outcome", layer="market")])
    plan = enforce.resolve(outcome, {"id": 3, "suggested_action": "SELL"})
    assert plan["should_override"] is False


def test_allowlist_narrows_policy():
    plan = enforce.resolve(_report([_f("E014")]), {"id": 4, "suggested_action": "BUY"},
                           policy={"allow_codes": ["E021"]})
    assert plan["should_override"] is False
    plan2 = enforce.resolve(_report([_f("E021")]), {"id": 5, "suggested_action": "BUY"},
                            policy={"allow_codes": ["E021"]})
    assert plan2["should_override"] is True


def test_explain_is_readable():
    plan = enforce.resolve(_report([_f()]), {"id": 6, "suggested_action": "BUY"})
    line = enforce.explain(plan)
    assert "→ HOLD" in line and "E014" in line


def test_real_losing_decision_plan():
    """真实亏损决策（11816）：只有 E014 参与拦截，E001a 等噪音不参与。"""
    import sqlite3
    fixture = Path(__file__).parent / "fixtures" / "dec_11816.json"
    dec = json.loads(fixture.read_text(encoding="utf-8"))
    findings = [
        {"code": "E001a", "severity": 2, "basis": "decision", "layer": "data", "detail": "缺硬数据"},
        {"code": "E014", "severity": 3, "basis": "decision", "layer": "event_causality", "detail": "推理链断"},
    ]
    plan = enforce.resolve(_report(findings), dec)
    assert plan["should_override"] is True
    assert [r["code"] for r in plan["reasons"]] == ["E014"]
