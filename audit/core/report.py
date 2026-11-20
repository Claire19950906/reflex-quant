"""AuditReport —— 输出契约与判定逻辑（纯函数，零依赖）。"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

VERSION = "audit.v1"
SEVERITY_LABEL = {1: "info", 2: "warn", 3: "critical"}

# severity → 扣分（grade 用）
_PENALTY = {1: 0.0, 2: 0.08, 3: 0.25}


@dataclass
class Finding:
    code: str
    severity: int
    layer: str
    detail: str = ""
    rule_id: str = ""
    remediation: str = ""
    evidence_refs: Dict[str, Any] = field(default_factory=dict)
    basis: str = "decision"  # decision=决策当时可判；outcome=需事后行情

    @property
    def is_complete(self) -> bool:
        return bool(self.remediation) and bool(self.evidence_refs)

    @property
    def missing_parts(self) -> List[str]:
        miss = []
        if not self.remediation:
            miss.append("remediation")
        if not self.evidence_refs:
            miss.append("evidence_refs")
        return miss

    def to_dict(self) -> Dict[str, Any]:
        return {
            "code": self.code,
            "severity": self.severity,
            "layer": self.layer,
            "detail": self.detail,
            "rule_id": self.rule_id,
            "remediation": self.remediation,
            "evidence_refs": self.evidence_refs,
            "basis": self.basis,
        }


@dataclass
class LayerResult:
    layer: str
    status: str  # checked | not_applicable | error | short_circuit
    note: str = ""
    elapsed_ms: float = 0.0
    findings: List[Finding] = field(default_factory=list)
    parts: List[Dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "layer": self.layer,
            "status": self.status,
            "note": self.note,
            "elapsed_ms": round(self.elapsed_ms, 2),
            "findings": len(self.findings),
            "parts": self.parts,
        }


def decide_verdict(findings: List[Finding], layers: List[LayerResult], mode: str = "full") -> str:
    if any(l.status == "short_circuit" for l in layers):
        return "insufficient"
    checked = sum(1 for l in layers if l.status == "checked")
    if checked == 0:
        return "insufficient"
    # pre-trade 模式只认"决策当时可判"的 finding；事后型码留到结算复盘
    considered = [f for f in findings if mode == "full" or f.basis == "decision"]
    if any(f.severity >= 3 for f in considered):
        return "reject"
    if any(f.severity == 2 for f in considered):
        return "warn"
    return "clean"


def compute_grade(findings: List[Finding], mode: str = "full") -> float:
    considered = [f for f in findings if mode == "full" or f.basis == "decision"]
    penalty = sum(_PENALTY.get(min(3, max(1, int(f.severity))), 0.1) for f in considered)
    return round(max(0.0, min(1.0, 1.0 - penalty)), 4)


def build_report(
    decision: Dict[str, Any],
    layers: List[LayerResult],
    *,
    elapsed_ms: float = 0.0,
    mode: str = "full",
    source: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    findings = [f for l in layers for f in l.findings]
    incomplete = [f.to_dict() for f in findings if not f.is_complete]

    checked = sum(1 for l in layers if l.status == "checked")
    na = sum(1 for l in layers if l.status == "not_applicable")
    err = sum(1 for l in layers if l.status == "error")
    total = len(layers)
    # 层跑了但没跑全（子检查器缺数据）——单独计数，别让它冒充"完整审计"
    partial = sum(1 for l in layers
                  if l.status == "checked" and any(pt.get("status") == "not_applicable" for pt in l.parts))

    did = decision.get("id", decision.get("decision_id"))
    return {
        "version": VERSION,
        "mode": mode,
        "trace_id": f"dec-{did}" if did not in (None, "") else "dec-unknown",
        "created_at": datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds"),
        "verdict": decide_verdict(findings, layers, mode),
        "grade": compute_grade(findings, mode),
        "subject": {
            "asset": str(decision.get("target_asset") or ""),
            "action": str(decision.get("suggested_action") or ""),
            "confidence": decision.get("evidence_confidence"),
            "degraded": bool(int(decision.get("analysis_degraded") or 0)),
        },
        "findings": [f.to_dict() for f in findings],
        "coverage": {
            "checked": checked,
            "not_applicable": na,
            "error": err,
            "partial": partial,
            "total_layers": total,
            "ratio": round(checked / total, 4) if total else 0.0,
        },
        "layers": [l.to_dict() for l in layers],
        "incomplete": incomplete,
        "source": source or {},
        "elapsed_ms": round(elapsed_ms, 2),
    }


def summarize(reports: List[Dict[str, Any]]) -> Dict[str, Any]:
    """批量回放的汇总（W2 baseline 用）。"""
    n = len(reports)
    verdicts: Dict[str, int] = {}
    codes: Dict[str, int] = {}
    basis: Dict[str, int] = {}
    layers: Dict[str, Dict[str, int]] = {}
    total_findings = 0
    incomplete = 0
    elapsed: List[float] = []

    for r in reports:
        verdicts[r["verdict"]] = verdicts.get(r["verdict"], 0) + 1
        total_findings += len(r["findings"])
        incomplete += len(r["incomplete"])
        elapsed.append(float(r.get("elapsed_ms") or 0.0))
        for f in r["findings"]:
            codes[f["code"]] = codes.get(f["code"], 0) + 1
            b = f.get("basis", "decision")
            basis[b] = basis.get(b, 0) + 1
        for l in r["layers"]:
            d = layers.setdefault(l["layer"], {"checked": 0, "not_applicable": 0, "error": 0})
            d[l["status"]] = d.get(l["status"], 0) + 1

    elapsed.sort()

    def _pct(p: float) -> Optional[float]:
        if not elapsed:
            return None
        idx = min(len(elapsed) - 1, int(round((len(elapsed) - 1) * p)))
        return round(elapsed[idx], 2)

    return {
        "decisions": n,
        "findings": total_findings,
        "findings_per_decision": round(total_findings / n, 3) if n else 0.0,
        "incomplete_findings": incomplete,
        "audited_ratio": round(
            sum(v for k, v in verdicts.items() if k != "insufficient") / n, 4
        ) if n else 0.0,
        "verdicts": dict(sorted(verdicts.items(), key=lambda kv: -kv[1])),
        "error_codes": dict(sorted(codes.items(), key=lambda kv: -kv[1])),
        "basis": dict(sorted(basis.items(), key=lambda kv: -kv[1])),
        "layers": layers,
        "latency_ms": {"p50": _pct(0.50), "p95": _pct(0.95), "max": _pct(1.0)},
    }
