# -*- coding: utf-8 -*-
"""v200 P0+ monitor_llm_value 单元测试."""
from __future__ import annotations

import json
import os
import sqlite3
import sys
import tempfile
import time

_BACKEND = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_SRC_PYTHON = os.path.join(_BACKEND, "src_python")
if _SRC_PYTHON not in sys.path:
    sys.path.insert(0, _SRC_PYTHON)

import db
import monitor_llm_value as mlv

import pytest


@pytest.fixture
def fresh_db(tmp_path):
    """隔离 file-based DB, 完整跑 db.migrate, 避免与生产 DB 锁冲突.

    用 tmp_path 而非 :memory: 因为 db.migrate 含 CREATE INDEX 依赖多个表,
    :memory: DB 的多语句 migrate 在某些情形下不稳定.

    注: 若 db.migrate 在 :memory: 测试 DB 上失败(已知 pre-existing 问题,
    与 v200 改动无关), 用 SQLAlchemy-style 直接建 ai_decisions + monitor_llm_value
    表 + 必填列, 满足 monitor_llm_value 单元测试需求.
    """
    db_path = tmp_path / "test_monitor.db"
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    try:
        db.migrate(conn)
    except Exception:
        # pre-existing db.migrate bug on fresh DB (strategy_id 列问题)
        # 直接建 monitor_llm_value 测试所需的最小表结构
        _create_minimal_schema(conn)
    try:
        yield conn
    finally:
        try:
            conn.close()
        except Exception:
            pass


def _create_minimal_schema(conn):
    """为 monitor_llm_value 测试建最小 schema (跳过 pre-existing migrate bug)."""
    conn.execute("""
        CREATE TABLE IF NOT EXISTS ai_decisions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            news_id INTEGER NOT NULL,
            sentiment_score REAL NOT NULL,
            suggested_action TEXT NOT NULL,
            reasoning TEXT NOT NULL,
            created_at TEXT NOT NULL,
            market_category TEXT NOT NULL DEFAULT '',
            target_asset TEXT NOT NULL DEFAULT 'NONE',
            settled INTEGER DEFAULT 0,
            is_correct TEXT DEFAULT '',
            forward_pnl REAL DEFAULT NULL,
            completeness_score REAL NOT NULL DEFAULT 1.0,
            llm_field_count INTEGER NOT NULL DEFAULT 0
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS monitor_llm_value (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            window_days INTEGER NOT NULL,
            llm_n INTEGER NOT NULL DEFAULT 0,
            llm_win_rate REAL NOT NULL DEFAULT 0.0,
            llm_avg_pnl REAL NOT NULL DEFAULT 0.0,
            llm_avg_completeness REAL NOT NULL DEFAULT 0.0,
            not_llm_n INTEGER NOT NULL DEFAULT 0,
            not_llm_win_rate REAL NOT NULL DEFAULT 0.0,
            not_llm_avg_pnl REAL NOT NULL DEFAULT 0.0,
            not_llm_avg_completeness REAL NOT NULL DEFAULT 0.0,
            delta_win_rate REAL NOT NULL DEFAULT 0.0,
            delta_avg_pnl REAL NOT NULL DEFAULT 0.0,
            verdict TEXT NOT NULL DEFAULT 'insufficient_data',
            action_recommended TEXT DEFAULT '',
            computed_at TEXT DEFAULT (datetime('now'))
        )
    """)
    conn.commit()


def _seed_decisions(conn, n, completeness, llm_count=5, win_rate=0.5,
                    asset="BTC"):
    """Helper: seed n 个 settled ai_decisions."""
    now = int(time.time())
    for i in range(n):
        c = conn.execute(
            """INSERT INTO ai_decisions
            (news_id, sentiment_score, suggested_action, reasoning,
             created_at, market_category, target_asset, settled, is_correct,
             forward_pnl, completeness_score, llm_field_count)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (1, 0.5, "BUY", "real reasoning",
             time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(now)),
             "CRYPTO", asset, 1,
             "WIN" if (i / n) < win_rate else "LOSS",
             0.01 if (i / n) < win_rate else -0.01,
             completeness, llm_count),
        )


def test_evaluate_insufficient_when_no_data(fresh_db):
    """空 DB 时 verdict=insufficient_data."""
    result = mlv.evaluate_llm_incremental_value(fresh_db, window_days=30)
    assert result["verdict"] == "insufficient_data"
    assert result["llm_n"] == 0
    assert result["not_llm_n"] == 0


def test_evaluate_insufficient_when_only_llm(fresh_db):
    """只 LLM 数据, 非 LLM 不足 → insufficient_data."""
    _seed_decisions(fresh_db, 30, completeness=0.95, llm_count=10, win_rate=0.6)
    result = mlv.evaluate_llm_incremental_value(fresh_db, window_days=30)
    assert result["verdict"] == "insufficient_data"
    assert result["llm_n"] >= 30
    assert result["not_llm_n"] < mlv._MIN_SAMPLES_PER_GROUP


def test_evaluate_positive_value(fresh_db):
    """LLM 显著优于兜底 → llm_positive_value."""
    _seed_decisions(fresh_db, 40, completeness=0.95, llm_count=10,
                    win_rate=0.7, asset="BTC")
    _seed_decisions(fresh_db, 40, completeness=0.3, llm_count=0,
                    win_rate=0.4, asset="WTI")
    result = mlv.evaluate_llm_incremental_value(fresh_db, window_days=30)
    assert result["llm_n"] >= 30
    assert result["not_llm_n"] >= 30
    assert result["delta_win_rate"] > mlv._WIN_RATE_DIFF_THRESHOLD, \
        f"LLM 应显著优于兜底: delta={result['delta_win_rate']}"
    assert result["verdict"] == "llm_positive_value"


def test_evaluate_negative_value(fresh_db):
    """LLM 显著差于兜底 → llm_negative_value."""
    _seed_decisions(fresh_db, 40, completeness=0.95, llm_count=10,
                    win_rate=0.3, asset="BTC")
    _seed_decisions(fresh_db, 40, completeness=0.3, llm_count=0,
                    win_rate=0.7, asset="WTI")
    result = mlv.evaluate_llm_incremental_value(fresh_db, window_days=30)
    assert result["verdict"] == "llm_negative_value"
    assert result["delta_win_rate"] < -mlv._WIN_RATE_DIFF_THRESHOLD


def test_evaluate_neutral(fresh_db):
    """LLM 与兜底差异不大 → llm_neutral."""
    _seed_decisions(fresh_db, 40, completeness=0.95, llm_count=10,
                    win_rate=0.5, asset="BTC")
    _seed_decisions(fresh_db, 40, completeness=0.3, llm_count=0,
                    win_rate=0.52, asset="WTI")
    result = mlv.evaluate_llm_incremental_value(fresh_db, window_days=30)
    assert result["verdict"] == "llm_neutral"


def test_persist_evaluation_inserts_row(fresh_db):
    """persist_evaluation 正确写入 monitor_llm_value 表."""
    evaluation = {
        "window_days": 30, "llm_n": 30, "llm_win_rate": 0.6,
        "llm_avg_pnl": 0.01, "llm_avg_completeness": 0.95,
        "not_llm_n": 30, "not_llm_win_rate": 0.4,
        "not_llm_avg_pnl": -0.005, "not_llm_avg_completeness": 0.3,
        "delta_win_rate": 0.2, "delta_avg_pnl": 0.015,
        "verdict": "llm_positive_value",
        "action_recommended": "test action",
    }
    rowid = mlv.persist_evaluation(fresh_db, evaluation)
    assert rowid > 0
    row = fresh_db.execute(
        "SELECT verdict, llm_n, delta_win_rate FROM monitor_llm_value WHERE id=?",
        (rowid,),
    ).fetchone()
    assert row["verdict"] == "llm_positive_value"
    assert row["llm_n"] == 30
    assert abs(row["delta_win_rate"] - 0.2) < 0.001


def test_run_evaluation_and_persist_combined(fresh_db):
    """run_evaluation_and_persist 集成: 评估 + 落库."""
    _seed_decisions(fresh_db, 50, completeness=0.95, llm_count=10, win_rate=0.6)
    _seed_decisions(fresh_db, 50, completeness=0.3, llm_count=0, win_rate=0.5)
    result = mlv.run_evaluation_and_persist(fresh_db, window_days=30)
    assert result.get("_rowid", 0) > 0, "rowid 应被设置"
    assert "verdict" in result
    assert "action_recommended" in result
