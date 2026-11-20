"""层定义的公共骨架。

一个层 = 名字 + 若干子检查器（part）+ 依赖 + 一个可读标签。

为什么要子检查器：内核对同一张表的依赖不该捆在一起。event 层里的三个 checker
读的是三张不同的表（drivers / samples / trades），客户可能只有其中一个。
按 part 判依赖，缺哪个只跳哪个，而不是整层 not_applicable。
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple

from ...core.context import CheckContext
from ...core.report import Finding, LayerResult
from .. import legacy, remediation

FALLBACK_REMEDIATION = "未登记修复建议：需在 _ERROR_CODE_GUIDANCE 或 remediation.OVERRIDES 补一条可执行动作"


@dataclass(frozen=True)
class PartSpec:
    """一个子检查器：名字 + 内核函数 + 依赖的 provider 键。"""

    name: str
    functions: Tuple[str, ...]
    requires: Optional[str] = None


@dataclass(frozen=True)
class LayerSpec:
    name: str
    functions: Tuple[str, ...]
    label: str
    requires: Optional[str] = None
    parts: Tuple[PartSpec, ...] = ()

    def resolved_parts(self) -> Tuple[PartSpec, ...]:
        if self.parts:
            return self.parts
        return (PartSpec(self.name, self.functions, self.requires),)


def to_finding(err: Dict[str, Any], layer: str, decision: Dict[str, Any]) -> Finding:
    code = str(err.get("error_code") or err.get("code") or "")
    severity = int(err.get("severity") or 2)

    refs = err.get("evidence_refs") or {}
    if not isinstance(refs, dict):
        refs = {"raw": refs}
    if not refs and err.get("evidence"):
        # 部分内核把证据放在 evidence（JSON 字符串），例如 E020
        raw_ev = err["evidence"]
        if isinstance(raw_ev, str):
            try:
                import json
                parsed = json.loads(raw_ev)
                refs = parsed if isinstance(parsed, dict) else {"raw": parsed}
            except (ValueError, TypeError):
                refs = {"raw": raw_ev}
        elif isinstance(raw_ev, dict):
            refs = raw_ev
    if not refs:
        # 最小可追溯引用：指向被审记录 + 检出层（不填充任何未观测到的"证据"）
        refs = {
            "decision_id": decision.get("id", decision.get("decision_id")),
            "checker": layer,
            "source": "trace_only",
        }

    return Finding(
        code=code,
        severity=severity,
        layer=layer,
        detail=str(err.get("detail") or ""),
        rule_id=f"reflection_checkers:{layer}",
        remediation=remediation.for_code(code, fallback=FALLBACK_REMEDIATION),
        evidence_refs=refs,
        basis=remediation.basis_for(code),
    )


def run_spec(spec: LayerSpec, decision: Dict[str, Any], ctx: CheckContext) -> LayerResult:
    rc = legacy.load()
    started = time.perf_counter()
    out: List[Finding] = []
    errors: List[str] = []
    skipped: List[str] = []
    parts: List[Dict[str, Any]] = []
    ran = 0

    for part in spec.resolved_parts():
        # 该子检查器缺数据 → 只跳过它，并在层里留痕（绝不静默当作"已审过"）
        if part.requires and not ctx.available.get(part.requires, False):
            note = ctx.unavailable_note(part.requires)
            skipped.append(f"{part.name}: {note}")
            parts.append({"part": part.name, "status": "not_applicable", "findings": 0, "note": note})
            continue

        ran += 1
        part_findings = 0
        part_errors: List[str] = []
        for name in part.functions:
            fn = getattr(rc, name, None)
            if fn is None:
                part_errors.append(f"内核缺函数 {name}")
                continue
            try:
                raw = legacy.call_checker(fn, decision, ctx.conn)
            except Exception as exc:  # 单个 checker 失败不得拖垮整份报告
                part_errors.append(f"{name}: {type(exc).__name__}: {exc}")
                continue
            for e in raw:
                if isinstance(e, dict):
                    out.append(to_finding(e, spec.name, decision))
                    part_findings += 1
        errors.extend(part_errors)
        status = "checked" if part_findings or not part_errors else "error"
        parts.append({
            "part": part.name,
            "status": status,
            "findings": part_findings,
            "note": "; ".join(part_errors)[:200],
        })

    elapsed = (time.perf_counter() - started) * 1000
    notes = skipped + errors
    note = "; ".join(notes)[:400]

    if ran == 0:
        return LayerResult(layer=spec.name, status="not_applicable", note=note,
                           elapsed_ms=elapsed, parts=parts)
    if errors and not out:
        return LayerResult(layer=spec.name, status="error", note=note,
                           elapsed_ms=elapsed, parts=parts)
    return LayerResult(layer=spec.name, status="checked", note=note,
                       elapsed_ms=elapsed, findings=out, parts=parts)
