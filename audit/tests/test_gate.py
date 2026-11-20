"""门禁测试：verdict → 拦截建议、落库幂等、覆盖率口径。全部离线。"""

from __future__ import annotations

import json
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from audit import gate
from audit.core.context import CheckContext
from audit.examples.selftest import DEGRADED

# 用 11939（reasoning_path 只有 4/8 段，E014 真实触发）——11816 因为字段优先级修复后
# 八段齐全，已经不再 reject，不能当"必然被拦"的样本。
FIXTURE = Path(__file__).parent / "fixtures" / "dec_11939.json"


def _ctx(tmp_path) -> CheckContext:
    """带 history 的上下文：event 层要有数据才会真的执行（否则整层 not_applicable）。"""
    (tmp_path / "rules.json").write_text(json.dumps(
        {"version": "providers.v1", "rules": [{"id": 1, "rule": "不加杠杆", "active": 1, "kind": 0}]},
        ensure_ascii=False), encoding="utf-8")
    (tmp_path / "drivers.json").write_text(json.dumps(
        {"version": "providers.v1",
         "nodes": [{"asset": "BTC", "label": "ETF 净流入", "is_core": 1, "active": 1}]},
        ensure_ascii=False), encoding="utf-8")
    (tmp_path / "history.json").write_text(json.dumps({
        "version": "providers.v1",
        "samples": [{"decision_id": 9001, "asset": "BTC", "is_correct": "LOSS",
                     "forward_pnl": -1.2, "ts": 1}],
        "trades": [{"id": 1, "decision_id": 11939, "asset": "BTC", "action": "BUY",
                    "pnl_pct": -0.13, "settled": 1}],
        "decisions": [{"id": 11939, "target_asset": "BTC", "event_type_code": "geopolitical_conflict",
                       "trade_gate_reason": "证据充分，允许输出方向性结论",
                       "evidence_action": "BUY", "evidence_confidence": 33.0}],
    }, ensure_ascii=False), encoding="utf-8")
    (tmp_path / "kline.json").write_text(json.dumps(
        {"version": "providers.v1", "bars": []}, ensure_ascii=False), encoding="utf-8")
    return CheckContext.from_file(tmp_path)

_DDL = """
CREATE TABLE ai_decisions (id INTEGER PRIMARY KEY, target_asset TEXT, suggested_action TEXT,
                           event_type_code TEXT, trade_gate_reason TEXT,
                           evidence_action TEXT, evidence_confidence REAL);
CREATE TABLE decision_errors (id INTEGER PRIMARY KEY AUTOINCREMENT, decision_id INTEGER NOT NULL,
                              error_code TEXT NOT NULL, origin TEXT NOT NULL DEFAULT 'checker',
                              severity INTEGER NOT NULL DEFAULT 2, detail TEXT NOT NULL DEFAULT '',
                              evidence_refs TEXT NOT NULL DEFAULT '{}', source TEXT NOT NULL DEFAULT '',
                              created_at TEXT NOT NULL DEFAULT (datetime('now')),
                              weight INTEGER NOT NULL DEFAULT 0,
                              UNIQUE(decision_id, error_code, origin, source));
"""


def _db() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(_DDL)
    return conn


def test_real_reject_decision_gets_hold_override(tmp_path):
    """真实决策（11939，推理链 4/8）：门禁必须给出 HOLD 覆盖建议，且靠进单前可判的码。"""
    dec = json.loads(FIXTURE.read_text(encoding="utf-8"))
    res = gate.decide(dec, _ctx(tmp_path), mode="pretrade")
    assert res["verdict"] == "reject"
    assert res["action_override"] == "HOLD"
    assert any(f["code"] == "E014" and f["severity"] >= 3 and f["basis"] == "decision"
               for f in res["report"]["findings"])


def test_hold_decision_never_gets_override():
    """HOLD 本来就没方向，reject 也不该给覆盖建议。"""
    dec = dict(DEGRADED, suggested_action="HOLD")
    res = gate.decide(dec, CheckContext.null(), mode="pretrade")
    assert res["action_override"] is None


def test_persist_is_idempotent_and_weighted(tmp_path):
    conn = _db()
    dec = json.loads(FIXTURE.read_text(encoding="utf-8"))
    res = gate.decide(dec, _ctx(tmp_path), mode="pretrade")
    n1 = gate.persist(res["report"], dec["id"], conn)
    assert n1 > 0
    n2 = gate.persist(res["report"], dec["id"], conn)   # 重跑
    assert n2 == 0, "同决策同码不得重复落库"
    rows = conn.execute(
        "SELECT error_code, source, weight FROM decision_errors WHERE decision_id=?", (dec["id"],)
    ).fetchall()
    assert all(r["source"] == "audit_gate" for r in rows), "source 要可区分、可回滚"
    # E014 属 event_logic 层（权重 100）+ severity 3 → 至少 130
    assert max(r["weight"] for r in rows) >= 130, "权重仍要按内核的层权重算"


def test_coverage_counts_gated_vs_total():
    conn = _db()
    conn.execute("INSERT INTO ai_decisions (id, suggested_action) "
                 "VALUES (1,'HOLD'), (2,'HOLD'), (3,'HOLD')")
    dec = dict(DEGRADED, id=1)
    res = gate.decide(dec, CheckContext.null(), mode="pretrade")
    gate._log(conn, res, gate_action="shadow", latency_ms=1.0)
    cov = gate.coverage(conn)
    assert cov == {"decisions": 3, "gated": 1, "coverage": 0.3333, "rejects": 0}


def test_missing_decision_returns_none():
    conn = _db()
    assert gate.gate_row(conn, 999999) is None


def test_env_switch(monkeypatch):
    monkeypatch.setenv("AUDIT_GATE", "off")
    assert gate.enabled() is False
    monkeypatch.setenv("AUDIT_GATE", "on")
    assert gate.enabled() is True
    monkeypatch.setenv("AUDIT_GATE_MODE", "FULL")
    assert gate.mode_from_env() == "full"
    monkeypatch.setenv("AUDIT_GATE_MODE", "nonsense")
    assert gate.mode_from_env() == "pretrade", "非法值要退回安全默认"
