# -*- coding: utf-8 -*-
"""gate_evaluations 闸门留痕回归测试 (v321.4 P0, 需求 #1)。

命题（这是本次修复的核心，必须被锁死）:
    1. **被闸门拦下的候选必须留痕** —— 旧实现里它们全都不落库,
       导致"分数门槛到底拒过谁"在数据层无法统计 (实测分数型拒绝 0 条);
    2. 拒绝码必须按 pending_signals 的 SQL 顺序**首个未通过即停**,
       否则 reject_code 分布会失真、无法定位瓶颈;
    3. 幂等 —— 同一条决策重复评估只留首次读数, 不会越跑越多;
    4. 观测层不阻断交易 —— 即使 gate_evaluations 写入异常, 也不抛给调用方。

全部离线: :memory: 库, 不碰生产库、无网络、无 LLM。
"""
from __future__ import annotations

import os
import sqlite3
import sys

import pytest

SRC = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "src_python"))
if SRC not in sys.path:
    sys.path.insert(0, SRC)

from review import gate_audit as ga

_PASSED = "passed"
_ALLOWED = ["BTC", "ETH", "SOL"]
_CONF_GATE = 0.6
_TRADABLE = ("UNREAD", "APPROVED", "REVIEWED", "AUTO_APPROVED")


def _conn() -> sqlite3.Connection:
    """最小可跑的 schema: 只需要 ai_decisions / quick_sim_trades 的用到的列。"""
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript("""
        CREATE TABLE ai_decisions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            news_id INTEGER,
            sentiment_score REAL,
            suggested_action TEXT,
            target_asset TEXT,
            evidence_confidence REAL,
            evidence_action TEXT,
            trade_gate_reason TEXT,
            status TEXT,
            created_at TEXT DEFAULT (datetime('now', 'localtime'))
        );
        CREATE TABLE quick_sim_trades (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            decision_id INTEGER
        );
    """)
    return conn


def _add(conn, **kw) -> int:
    row = {
        "news_id": 1, "sentiment_score": 0.5, "suggested_action": "BUY",
        "target_asset": "BTC", "evidence_confidence": 0.8,
        "evidence_action": "BUY", "trade_gate_reason": _PASSED,
        "status": "UNREAD",
    }
    row.update(kw)
    cur = conn.execute(
        "INSERT INTO ai_decisions (news_id, sentiment_score, suggested_action, "
        " target_asset, evidence_confidence, evidence_action, trade_gate_reason, status) "
        "VALUES (?,?,?,?,?,?,?,?)",
        (row["news_id"], row["sentiment_score"], row["suggested_action"],
         row["target_asset"], row["evidence_confidence"], row["evidence_action"],
         row["trade_gate_reason"], row["status"]),
    )
    return int(cur.lastrowid)


def _run(conn) -> dict:
    return ga.record_gate_funnel(
        conn, minutes=180, passed_reason=_PASSED, allowed_assets=_ALLOWED,
        conf_gate=_CONF_GATE, tradable_statuses=_TRADABLE)


# ─────────────────────── 1. 通过 vs 被拦都必须留痕 ───────────────────────

def test_pass_and_reject_both_recorded():
    conn = _conn()
    _add(conn)                                   # 全条件满足 → passed
    _add(conn, suggested_action="HOLD")          # AI 无方向
    stats = _run(conn)
    assert stats["evaluated"] == 2
    assert stats["passed"] == 1
    rows = conn.execute(
        "SELECT passed, reject_code FROM gate_evaluations ORDER BY ai_decision_id"
    ).fetchall()
    assert len(rows) == 2, "被拦下的候选也必须留一行 —— 这是本修复的全部意义"
    assert rows[0]["passed"] == 1
    assert rows[1]["passed"] == 0
    assert rows[1]["reject_code"] == ga.REJECT_NO_DIRECTION
    conn.close()


# ─────────────────────── 2. 每个拒绝码都要能落到 ───────────────────────

@pytest.mark.parametrize("patch,code", [
    ({"suggested_action": "HOLD"}, ga.REJECT_NO_DIRECTION),
    ({"target_asset": "NONE"}, ga.REJECT_BAD_ASSET),
    ({"trade_gate_reason": "evidence_weak"}, ga.REJECT_GATE_REASON),
    ({"evidence_action": "HOLD"}, ga.REJECT_EVIDENCE_HOLD),
    ({"evidence_confidence": 0.2}, ga.REJECT_LOW_CONFIDENCE),
    ({"target_asset": "XAU"}, ga.REJECT_ASSET_NOT_ALLOWED),
    ({"status": "FAILED"}, ga.REJECT_STATUS_BLOCKED),
])
def test_each_reject_code(patch, code):
    conn = _conn()
    _add(conn, **patch)
    stats = _run(conn)
    assert stats["passed"] == 0
    assert stats["rejected"].get(code) == 1, f"{patch} 应判为 {code}"
    got = conn.execute("SELECT reject_code FROM gate_evaluations").fetchone()[0]
    assert got == code
    conn.close()


# ─────────────────────── 3. 首个未通过即停 ───────────────────────

def test_first_failing_condition_wins():
    """多个条件同时不满足时, reject_code 必须是**最靠前**那个。

    否则分桶会失真: 一个 asset 不在白名单的 HOLD 若被记成 asset_not_allowed,
    就会掩盖"AI 本身没给方向"这个真瓶颈。
    """
    conn = _conn()
    _add(conn, suggested_action="HOLD", target_asset="XAU",
         evidence_confidence=0.1, status="FAILED")
    _run(conn)
    code = conn.execute("SELECT reject_code FROM gate_evaluations").fetchone()[0]
    assert code == ga.REJECT_NO_DIRECTION
    conn.close()


# ─────────────────────── 4. 幂等 ───────────────────────

def test_idempotent_by_ai_decision_id():
    conn = _conn()
    _add(conn, suggested_action="HOLD")
    first = _run(conn)
    assert first["inserted"] == 1
    second = _run(conn)
    assert second["inserted"] == 0, "同一条决策重复评估不得重复插入"
    total = conn.execute("SELECT COUNT(*) FROM gate_evaluations").fetchone()[0]
    assert total == 1
    conn.close()


# ─────────────────────── 5. 已建仓的候选要能区分 ───────────────────────

def test_already_traded_flagged_in_context():
    conn = _conn()
    aid = _add(conn)
    conn.execute("INSERT INTO quick_sim_trades (decision_id) VALUES (?)", (aid,))
    _run(conn)
    import json
    ctx = json.loads(conn.execute(
        "SELECT context_json FROM gate_evaluations WHERE ai_decision_id=?", (aid,)
    ).fetchone()[0])
    assert ctx["already_traded"] is True
    conn.close()


# ─────────────────────── 6. 观测层不阻断 ───────────────────────

def test_observe_layer_never_raises_on_broken_db():
    """库结构缺失(没有 ai_decisions)时必须**返回空统计而不是抛异常**。

    调用方在交易周期里, 抛异常等于让观测层掐断交易链。
    """
    conn = sqlite3.connect(":memory:")
    stats = _run(conn)
    assert stats["evaluated"] == 0
    assert stats["inserted"] == 0
    conn.close()
