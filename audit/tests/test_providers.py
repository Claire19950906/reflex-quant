"""Provider 层测试：文件 / HTTP / 快照 / 空源，全部离线（不发真实网络请求）。

跑法：python3 -m pytest audit/tests -q
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from audit import audit
from audit.core.context import CheckContext
from audit.examples.selftest import DEGRADED
from audit.providers import VERSION, HttpBundle, NullBundle


def _write_providers(root: Path, *, rules: list | None = None,
                     samples: list | None = None, trades: list | None = None,
                     decisions: list | None = None) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    (root / "rules.json").write_text(
        json.dumps({"version": VERSION, "rules": rules or []}, ensure_ascii=False), encoding="utf-8")
    (root / "drivers.json").write_text(
        json.dumps({"version": VERSION, "nodes": []}, ensure_ascii=False), encoding="utf-8")
    (root / "history.json").write_text(
        json.dumps({"version": VERSION, "samples": samples or [],
                    "trades": trades or [], "decisions": decisions or []}, ensure_ascii=False),
        encoding="utf-8")
    (root / "kline.json").write_text(
        json.dumps({"version": VERSION, "bars": []}, ensure_ascii=False), encoding="utf-8")
    return root


def test_null_bundle_marks_layers_not_applicable():
    ctx = CheckContext.from_bundle(NullBundle())
    r = audit(DEGRADED, ctx)
    status = {l["layer"]: l["status"] for l in r["layers"]}
    assert status["rules"] == "not_applicable"
    assert status["causal"] == "not_applicable"
    assert status["data"] == "checked"


def test_file_bundle_enables_rules_layer(tmp_path):
    root = _write_providers(tmp_path / "p", rules=[{"id": 1, "rule": "不加杠杆", "active": 1, "kind": 0}])
    ctx = CheckContext.from_file(root)
    r = audit(DEGRADED, ctx)
    status = {l["layer"]: l["status"] for l in r["layers"]}
    assert status["rules"] == "checked"
    assert any(f["code"] == "E005" for f in r["findings"]), "有铁律但 reasoning 未引用 → 应触发 E005"


def test_snapshot_drives_sample_integrity(tmp_path):
    """provider 里的成交样本能驱动 E020（不再依赖生产库）。"""
    decisions = [{"id": 9100, "target_asset": "WTI", "trade_gate_reason": "gate_failed",
                  "evidence_action": "HOLD", "evidence_confidence": 12.0}]
    trades = [{"id": 1, "decision_id": 9100, "asset": "WTI", "action": "BUY",
               "pnl_pct": -1.2, "settled": 1}]
    root = _write_providers(tmp_path / "p", trades=trades, decisions=decisions,
                            samples=[{"decision_id": 9100, "asset": "WTI", "is_correct": "LOSS",
                                      "forward_pnl": -1.2, "ts": 1}])
    ctx = CheckContext.from_file(root)
    dec = dict(DEGRADED, id=9100)
    r = audit(dec, ctx)
    assert any(f["code"] == "E020" for f in r["findings"])
    e020 = next(f for f in r["findings"] if f["code"] == "E020")
    assert e020["evidence_refs"], "E020 必须带可追溯证据"


def test_http_bundle_reads_local_file_url(tmp_path):
    """用 file:// 伪装 HTTP，验证加载路径而不发真实网络请求。"""
    root = _write_providers(tmp_path / "p", rules=[{"id": 7, "rule": "止损优先", "active": 1, "kind": 0}])
    bundle = HttpBundle(root.as_uri())
    data = bundle.rules()
    assert data.rules and data.rules[0]["rule"] == "止损优先"
    ctx = CheckContext.from_bundle(bundle)
    assert ctx.available["rules"] is True


def test_http_bundle_failure_degrades_not_crashes():
    bundle = HttpBundle("http://127.0.0.1:9/nonexistent", timeout=0.5)
    ctx = CheckContext.from_bundle(bundle)
    assert ctx.available["rules"] is False
    assert ctx.notes, "加载失败必须留痕，不能静默当已审过"
    r = audit(DEGRADED, ctx)
    assert all(l["status"] != "not_applicable" or l["note"] for l in r["layers"])


def test_snapshot_isolated_from_callers_mutations(tmp_path):
    ctx = CheckContext.from_file(_write_providers(
        tmp_path / "p",
        rules=[{"id": 1, "rule": "不加杠杆", "active": 1, "kind": 0}],
    ))
    # 快照是内存库：反复审计不应互相污染（E020 会写库的场景在 audit 层已剥离）
    r1 = audit(DEGRADED, ctx)
    r2 = audit(DEGRADED, ctx)
    assert [f["code"] for f in r1["findings"]] == [f["code"] for f in r2["findings"]]
