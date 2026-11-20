"""子检查器级依赖：缺哪张表只跳哪个 checker，不再整层作废。"""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from audit import audit
from audit.core.context import CheckContext
from audit.examples.selftest import DEGRADED


def _providers(tmp_path, *, rules=(), drivers=(), samples=(), trades=(), decisions=()):
    root = Path(tmp_path)
    (root / "rules.json").write_text(json.dumps(
        {"version": "providers.v1", "rules": list(rules)}, ensure_ascii=False), encoding="utf-8")
    (root / "drivers.json").write_text(json.dumps(
        {"version": "providers.v1", "nodes": list(drivers)}, ensure_ascii=False), encoding="utf-8")
    (root / "history.json").write_text(json.dumps(
        {"version": "providers.v1", "samples": list(samples),
         "trades": list(trades), "decisions": list(decisions)}, ensure_ascii=False), encoding="utf-8")
    (root / "kline.json").write_text(json.dumps(
        {"version": "providers.v1", "bars": []}, ensure_ascii=False), encoding="utf-8")
    return CheckContext.from_file(root)


def _event_parts(report):
    layer = next(l for l in report["layers"] if l["layer"] == "event_causality")
    return {p["part"]: p["status"] for p in layer["parts"]}, layer


def test_no_trades_only_skips_sample_integrity(tmp_path):
    """交易无关的客户：没有成交样本 → 只跳 sample_integrity，其余照跑。"""
    ctx = _providers(
        tmp_path,
        drivers=[{"asset": "WTI", "label": "OPEC 增产", "is_core": 1, "active": 1}],
        samples=[{"decision_id": 1, "asset": "WTI", "is_correct": "LOSS", "forward_pnl": -1.0, "ts": 1}],
    )
    r = audit(DEGRADED, ctx)
    parts, layer = _event_parts(r)
    assert layer["status"] == "checked"
    assert parts["event_logic"] == "checked"
    assert parts["historical_counterfactual"] == "checked"
    assert parts["sample_integrity"] == "not_applicable"
    assert "trades" in layer["note"], "跳过的原因必须写进层备注"
    assert r["coverage"]["partial"] >= 1, "部分审的层要单独计数"


def test_no_samples_only_skips_counterfactual(tmp_path):
    ctx = _providers(
        tmp_path,
        drivers=[{"asset": "WTI", "label": "OPEC 增产", "is_core": 1, "active": 1}],
        trades=[{"id": 1, "decision_id": 1, "asset": "WTI", "action": "BUY", "pnl_pct": -1.0, "settled": 1}],
    )
    r = audit(DEGRADED, ctx)
    parts, layer = _event_parts(r)
    assert parts["historical_counterfactual"] == "not_applicable"
    assert parts["sample_integrity"] == "checked"
    assert layer["status"] == "checked"


def test_no_drivers_kills_event_logic_but_not_sample_integrity(tmp_path):
    """因果/事件链依赖 drivers；没有 drivers 时这两处不可用，但样本完整性仍能审。"""
    ctx = _providers(
        tmp_path,
        trades=[{"id": 1, "decision_id": 1, "asset": "WTI", "action": "BUY", "pnl_pct": -1.0, "settled": 1}],
        samples=[],
    )
    r = audit(DEGRADED, ctx)
    parts, layer = _event_parts(r)
    assert parts["event_logic"] == "not_applicable"
    assert layer["status"] == "checked", "还有子检查器能跑，层就不算整体不可用"
    causal = next(l for l in r["layers"] if l["layer"] == "causal")
    assert causal["status"] == "not_applicable", "因果层读 drivers，缺了要如实报"


def test_everything_missing_marks_layer_not_applicable_with_reasons(tmp_path):
    ctx = _providers(tmp_path)
    r = audit(DEGRADED, ctx)
    parts, layer = _event_parts(r)
    assert layer["status"] == "not_applicable"
    assert all(v == "not_applicable" for v in parts.values())
    for need in ("drivers", "samples", "trades"):
        assert need in layer["note"], f"缺 {need} 要在备注里说清楚"
