"""db.migrate() 顺序无关 + 幂等 测试。

针对「migrate 顺序脆弱点」：
- decision_errors / ai_call_stats 表在 migrate() 尾部 `for stmt in _INDEXES` 才 CREATE，
  而 decision_errors 的 _ensure_columns 在更早处调用。_ensure_columns 对缺失表必须 no-op
  而不是抛错，否则 fresh DB 每次启动都打 "decision_errors weight col failed" 警告。
- 幂等：连续两次 migrate 不报错、增量列不重复增加。
"""

import os
import sqlite3
import sys

import pytest

SRC = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "src_python"))
if SRC not in sys.path:
    sys.path.insert(0, SRC)

import db as _db  # noqa: E402


def _cols(conn, table: str):
    return {r[1] for r in conn.execute(f"PRAGMA table_info({table})").fetchall()}


def test_migrate_is_idempotent_and_order_independent():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    try:
        _db.migrate(conn)
        _db.migrate(conn)  # 第二次必须不抛错（幂等）

        # 代表性增量列：fresh migrate 必须全部补齐
        assert "verification_score" in _cols(conn, "review_insights")
        assert "verification_verdict" in _cols(conn, "review_insights")
        assert "weight" in _cols(conn, "decision_errors")        # _INDEXES 尾部 CREATE 时已含
        assert "suggestion_param_key" in _cols(conn, "trade_reflections")

        # 幂等：verification_score 列不应因二次 migrate 而重复
        n = conn.execute(
            "SELECT COUNT(*) FROM pragma_table_info('review_insights') "
            "WHERE name='verification_score'"
        ).fetchone()[0]
        assert n == 1
    finally:
        conn.close()


def test_ensure_columns_noop_when_table_missing():
    """顺序无关：对尚不存在的表，_ensure_columns 必须 no-op 而非抛错。"""
    conn = sqlite3.connect(":memory:")
    try:
        _db._ensure_columns(conn, "decision_errors", _db._DECISION_ERRORS_V189_COLUMNS)
        # 不应建表
        n = conn.execute(
            "SELECT COUNT(*) FROM sqlite_master WHERE type='table' AND name='decision_errors'"
        ).fetchone()[0]
        assert n == 0
    finally:
        conn.close()


def test_ensure_columns_adds_missing_column_when_table_exists():
    """老库补齐：表已存在却缺列时，_ensure_columns 仍然 ALTER 补齐。"""
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    try:
        conn.execute(
            "CREATE TABLE review_insights (id INTEGER PRIMARY KEY, asset TEXT)"
        )
        _db._ensure_columns(conn, "review_insights", _db._REVIEW_INSIGHTS_COLUMNS)
        cols = _cols(conn, "review_insights")
        for name, _ in _db._REVIEW_INSIGHTS_COLUMNS:
            assert name in cols
    finally:
        conn.close()