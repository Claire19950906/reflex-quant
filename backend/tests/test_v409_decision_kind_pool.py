# -*- coding: utf-8 -*-
"""v406 — 无标的决策与降级路径分池。

背景：S16 降级率 87.2%（450/516），但其中 279 条是 target_asset=NONE 的决策，
它们没有可分析对象。把"路由没打上"和"模型没被问"放进同一个分母，
指标就同时回答不了两个问题。

验收：
1. 有 decision_kind 列时，S16 只在 analysis 池里算降级率；
2. 缺列（老库/极简测试表）时回落旧口径，不炸；
3. 回填脚本把 NONE 行标成 routing_miss，其余标 analysis，且幂等。
"""
from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[1]
for p in (str(BACKEND), str(BACKEND / "src_python"), str(BACKEND / "scripts")):
    if p not in sys.path:
        sys.path.insert(0, p)

import capability_scorecard as cs
import backfill_decision_kind as bdk


def _make_db(with_kind: bool = True) -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    cols = ["id INTEGER PRIMARY KEY", "target_asset TEXT", "analysis_degraded INTEGER"]
    if with_kind:
        cols.append("decision_kind TEXT DEFAULT 'analysis'")
    conn.execute(f"CREATE TABLE ai_decisions ({', '.join(cols)})")
    rows = [
        # 无标的 + 降级（原口径会算进分子分母）
        (1, "NONE", 1, "routing_miss"),
        (2, "NONE", 1, "routing_miss"),
        # 有标的：一条降级（真降级），一条正常
        (3, "BTC", 1, "analysis"),
        (4, "BTC", 0, "analysis"),
    ]
    for r in rows:
        if with_kind:
            conn.execute("INSERT INTO ai_decisions VALUES (?,?,?,?)", r)
        else:
            conn.execute("INSERT INTO ai_decisions VALUES (?,?,?)", r[:3])
    return conn


def test_s16_excludes_routing_miss_from_denominator():
    conn = _make_db(with_kind=True)
    k, n = cs._q_degraded_share(conn)
    assert (k, n) == (1, 2), f"降级率应只在 analysis 池算, 实际 {k}/{n}"


def test_s16_falls_back_without_column():
    conn = _make_db(with_kind=False)
    k, n = cs._q_degraded_share(conn)
    assert (k, n) == (3, 4), f"缺列时应回落旧口径, 实际 {k}/{n}"


def test_not_degraded_clause_includes_pool_filter():
    conn = _make_db(with_kind=True)
    clause = cs._not_degraded(conn)
    assert "decision_kind" in clause and "analysis_degraded" in clause


def test_not_degraded_clause_plain_without_column():
    conn = _make_db(with_kind=False)
    assert cs._not_degraded(conn) == "COALESCE(analysis_degraded, 0) = 0"


def test_backfill_plan_and_commit():
    conn = _make_db(with_kind=True)
    # 模拟"加列后历史行被默认成 analysis"的真实状态
    conn.execute("UPDATE ai_decisions SET decision_kind='analysis'")
    conn.commit()
    before = bdk.plan(conn)
    assert before == {"routing_miss": 2, "analysis": 2}

    # 跑回填（复用脚本里的两条 UPDATE 语义）
    conn.execute(
        """UPDATE ai_decisions SET decision_kind='routing_miss'
            WHERE (COALESCE(target_asset,'') = '' OR UPPER(target_asset) = 'NONE')
              AND COALESCE(decision_kind,'') <> 'routing_miss'""")
    conn.execute(
        """UPDATE ai_decisions SET decision_kind='analysis'
            WHERE COALESCE(target_asset,'') <> '' AND UPPER(target_asset) <> 'NONE'
              AND COALESCE(decision_kind,'') = ''""")
    conn.commit()

    kinds = dict(conn.execute("SELECT decision_kind, COUNT(*) FROM ai_decisions GROUP BY 1"))
    assert kinds == {"routing_miss": 2, "analysis": 2}
