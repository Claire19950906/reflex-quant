"""audit 包回归测试（离线，不碰生产库）。

跑法：
    cd <repo root>
    python3 -m pytest audit/tests -q
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from audit import audit
from audit.contracts import validate
from audit.core.context import CheckContext
from audit.core.report import Finding, LayerResult, build_report, summarize
from audit.examples.selftest import CLEAN, DEGRADED, INCOMPLETE


def test_contract_requires_action_and_asset():
    problems = validate({"id": 1, "reasoning": "x"})
    assert any("suggested_action" in p for p in problems)
    assert any("target_asset" in p for p in problems)


def test_clean_sample_has_no_findings():
    r = audit(CLEAN, CheckContext.null())
    assert r["verdict"] == "clean"
    assert r["findings"] == []
    # 纯函数层（data/logic/market/reasoning）可跑；rules/causal/event 缺 provider
    assert r["coverage"]["checked"] == 4


def test_degraded_sample_is_flagged_with_remediation():
    r = audit(DEGRADED, CheckContext.null())
    assert r["verdict"] in ("warn", "reject")
    assert r["findings"]
    for f in r["findings"]:
        assert f["remediation"], f"{f['code']} 缺 remediation"
        assert f["evidence_refs"], f"{f['code']} 缺 evidence_refs"


def test_low_completeness_short_circuits():
    r = audit(INCOMPLETE, CheckContext.null())
    assert r["verdict"] == "insufficient"
    assert r["layers"][0]["status"] == "short_circuit"


def test_offline_context_marks_layers_not_applicable():
    r = audit(DEGRADED, CheckContext.null())
    na = [l["layer"] for l in r["layers"] if l["status"] == "not_applicable"]
    assert "rules" in na and "event_causality" in na
    # 不允许"静默跳过"：每个未跑层都必须给出原因
    for l in r["layers"]:
        if l["status"] == "not_applicable":
            assert l["note"], f"{l['layer']} 未说明原因"


def test_pretrade_ignores_outcome_basis():
    finding = Finding(
        code="E007", severity=3, layer="market", basis="outcome",
        remediation="x", evidence_refs={"a": 1},
    )
    layers = [LayerResult(layer="market", status="checked", findings=[finding])]
    assert build_report({"id": 1, "target_asset": "WTI"}, layers, mode="full")["verdict"] == "reject"
    assert build_report({"id": 1, "target_asset": "WTI"}, layers, mode="pretrade")["verdict"] == "clean"


def test_incomplete_finding_is_surfaced_not_hidden():
    bad = Finding(code="E099", severity=2, layer="x", remediation="", evidence_refs={})
    layers = [LayerResult(layer="x", status="checked", findings=[bad])]
    r = build_report({"id": 2, "target_asset": "BTC"}, layers)
    assert len(r["incomplete"]) == 1
    assert r["incomplete"][0]["missing_parts"] if "missing_parts" in r["incomplete"][0] else True


def test_audit_package_does_not_import_db():
    """审计层必须能在没有数据库的机器上 import。"""
    for path in (Path(__file__).resolve().parents[1]).rglob("*.py"):
        if path.name == "legacy.py":
            continue  # 适配器允许注入 stub
        text = path.read_text(encoding="utf-8")
        code_lines = [ln for ln in text.splitlines() if not ln.strip().startswith("#")]
        assert not any(ln.strip().startswith("import db") for ln in code_lines), f"{path} 直接 import db"


def test_summary_counts_basis():
    r = audit(DEGRADED, CheckContext.null())
    s = summarize([r])
    assert sum(s["basis"].values()) == len(r["findings"])
