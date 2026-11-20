"""审计编排 —— 契约校验 → 完整性短路 → 7 层 → 报告。"""

from __future__ import annotations

import time
from typing import Any, Dict, Iterable, List, Optional

from ..checkers import layers as layer_mod
from ..checkers import legacy
from ..contracts import validate
from .context import CheckContext
from .report import Finding, LayerResult, build_report

FALLBACK_REMEDIATION = "未登记修复建议：需在 _ERROR_CODE_GUIDANCE 补一条可执行动作"


def audit(
    decision: Dict[str, Any],
    ctx: Optional[CheckContext] = None,
    *,
    strict: bool = True,
    mode: str = "full",
) -> Dict[str, Any]:
    """审一条决策。

    Args:
        decision: decision.v1 契约的 dict
        ctx: CheckContext；None = 纯离线（rules/history 层报 not_applicable）
        strict: True 时契约缺失直接 ValueError，False 时出一份 insufficient 报告
    """
    started = time.perf_counter()
    ctx = ctx or CheckContext.null()
    source = {
        "label": ctx.label,
        "available": dict(ctx.available),
        "notes": list(ctx.notes[:3]),
    }

    problems = validate(decision)
    if problems:
        if strict:
            raise ValueError("decision 不满足 decision.v1 契约：" + "；".join(problems))
        report = build_report(decision, [], elapsed_ms=0.0, mode=mode, source=source)
        report["verdict"] = "insufficient"
        report["contract_errors"] = problems
        report["elapsed_ms"] = round((time.perf_counter() - started) * 1000, 2)
        return report

    comp = legacy.is_low_completeness(decision)
    if comp is not None:
        raw = legacy.e019(decision, comp)
        f = Finding(
            code=str(raw.get("error_code") or "E019"),
            severity=int(raw.get("severity") or 1),
            layer="completeness_gate",
            detail=str(raw.get("detail") or f"完整性 {comp:.2f} < 0.7"),
            rule_id="reflection_checkers:run_all_checkers",
            remediation=legacy.remediation_for("E019") or FALLBACK_REMEDIATION,
            evidence_refs=raw.get("evidence_refs") or {"completeness_score": comp},
        )
        result = LayerResult(
            layer="completeness_gate",
            status="short_circuit",
            note=f"完整性 {comp:.2f} < 0.7：其余 7 层不跑（避免在脏数据上误报）",
            findings=[f],
        )
        report = build_report(decision, [result], elapsed_ms=(time.perf_counter() - started) * 1000, mode=mode, source=source)
        report["verdict"] = "insufficient"
        return report

    results = layer_mod.run_all(decision, ctx)
    report = build_report(decision, results, elapsed_ms=(time.perf_counter() - started) * 1000, mode=mode, source=source)
    return report


def audit_many(
    decisions: Iterable[Dict[str, Any]],
    ctx: Optional[CheckContext] = None,
    *,
    strict: bool = False,
    on_contract_error: str = "report",
    mode: str = "full",
) -> List[Dict[str, Any]]:
    """批量审计。单条契约错误默认降级为 insufficient 报告，不中断整批。"""
    out: List[Dict[str, Any]] = []
    for d in decisions:
        try:
            out.append(audit(d, ctx, strict=strict, mode=mode))
        except ValueError as exc:
            if on_contract_error == "raise":
                raise
            report = build_report(d if isinstance(d, dict) else {}, [], elapsed_ms=0.0, mode=mode)
            report["verdict"] = "insufficient"
            report["contract_errors"] = [str(exc)]
            out.append(report)
    return out
