# -*- coding: utf-8 -*-
"""v453 P0-4 / P0-5：队列分诊 + 执行幂等 + R8 保留最优。

P0-4：队列水位不再把「门卫判可交易」的行一起推走（归档侧 + 认领侧 + 计数侧）。
P0-5：同一 ai_decision 只尝试一次；R8 从「兄弟>3 全拒」改成「同批只放前 3 名」。
"""

from __future__ import annotations

import os
import sqlite3
import sys

SRC = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "src_python"))
if SRC not in sys.path:
    sys.path.insert(0, SRC)

import config
import news_cutoff
import quick_sim
import sample_quality_filter as sqf


def _conn(with_gate_col: bool = False) -> sqlite3.Connection:
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    gate = ", gate_verdict TEXT" if with_gate_col else ""
    c.executescript(f"""
    CREATE TABLE ai_decisions(
        id INTEGER PRIMARY KEY, news_id INTEGER, target_asset TEXT, suggested_action TEXT,
        sentiment_score REAL, evidence_confidence REAL, trade_gate_reason TEXT,
        evidence_action TEXT, strategy_id INTEGER, strategy_version_id INTEGER,
        paper_trading_run_id INTEGER, event_strength TEXT, event_phase TEXT, status TEXT,
        created_at TEXT, analysis_degraded INTEGER, analysis_degraded_reason TEXT,
        trade_uuid TEXT);  -- v483: quick_sim 建仓会继承/回写 trade_uuid
    CREATE TABLE raw_news(id INTEGER PRIMARY KEY, source TEXT, ts INTEGER, published_at TEXT{gate});
    CREATE TABLE quick_sim_trades(id INTEGER PRIMARY KEY, decision_id INTEGER);
    CREATE TABLE trade_decisions(id INTEGER PRIMARY KEY, ai_decision_id INTEGER, final_action TEXT);
    """)
    return c


# ─────────────────── P0-4：水位分诊 ───────────────────


def test_keep_tradeable_clause_detects_column():
    c = _conn(with_gate_col=True)
    assert "gate_verdict" in news_cutoff._keep_tradeable_clause(c)
    c2 = _conn(with_gate_col=False)
    assert news_cutoff._keep_tradeable_clause(c2) == ""


# ─────────────────── P0-5a：已尝试不重复入选 ───────────────────


def _seed_decision(c, did: int, news_id: int, conf=80.0):
    c.execute(
        "INSERT INTO ai_decisions(id, news_id, target_asset, suggested_action, sentiment_score,"
        " evidence_confidence, trade_gate_reason, evidence_action, status, created_at,"
        " analysis_degraded) VALUES (?,?,?,?,?,?,?,?,?,datetime('now','localtime'),0)",
        (did, news_id, "WTI", "BUY", 50.0, conf,
         config.TRADE_GATE_PASSED_REASON, "BUY", "UNREAD"),
    )


def test_pending_signals_skips_attempted_decision():
    c = _conn()
    c.execute("INSERT INTO raw_news(id, source, ts, published_at) VALUES (1,'x',strftime('%s','now'),NULL)")
    c.execute("INSERT INTO raw_news(id, source, ts, published_at) VALUES (2,'x',strftime('%s','now'),NULL)")
    _seed_decision(c, 101, 1)
    _seed_decision(c, 102, 2)
    c.execute("INSERT INTO trade_decisions(ai_decision_id, final_action) VALUES (101,'REJECTED')")
    c.commit()
    rows = quick_sim.pending_signals(c, limit=10)
    assert [r["id"] for r in rows] == [102]


def test_pending_signals_keeps_unattempted():
    c = _conn()
    c.execute("INSERT INTO raw_news(id, source, ts, published_at) VALUES (1,'x',strftime('%s','now'),NULL)")
    _seed_decision(c, 201, 1)
    c.commit()
    rows = quick_sim.pending_signals(c, limit=10)
    assert [r["id"] for r in rows] == [201]


# ─────────────────── P0-5b：R8 保留最优 ───────────────────


def _r8_fixture(confs=(90, 80, 70, 60)):
    c = _conn()
    for i, conf in enumerate(confs, start=1):
        c.execute(
            "INSERT INTO ai_decisions(id, news_id, target_asset, suggested_action,"
            " evidence_confidence, trade_gate_reason, evidence_action, status, created_at,"
            " analysis_degraded) VALUES (?,0,'WTI','BUY',?,?, 'BUY','UNREAD',"
            " datetime('now','localtime'),0)",
            (i, conf, config.TRADE_GATE_PASSED_REASON),
        )
    c.commit()
    return c


def _check(c, did, conf):
    return sqf.pre_trade_check(
        {"id": did, "target_asset": "WTI", "evidence_action": "BUY",
         "evidence_confidence": conf,
         "trade_gate_reason": config.TRADE_GATE_PASSED_REASON,
         "status": "UNREAD", "analysis_degraded": 0},
        conn=c, max_batch_siblings=3,
    )


def test_r8_keeps_top3_instead_of_rejecting_all():
    c = _r8_fixture()
    valid = [_check(c, 1, 90)["is_valid"], _check(c, 2, 80)["is_valid"],
             _check(c, 3, 70)["is_valid"]]
    assert all(valid)
    low = _check(c, 4, 60)
    assert low["is_valid"] is False
    assert any(r.startswith("R8:同批排名=4") for r in low["reasons"]), low["reasons"]


def test_r8_rank_ties_break_by_id():
    c = _r8_fixture(confs=(80, 80, 80, 80))
    passed = [i for i in (1, 2, 3, 4) if _check(c, i, 80)["is_valid"]]
    assert passed == [1, 2, 3]
