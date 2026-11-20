# -*- coding: utf-8 -*-
"""v392 · 启动期补列兜底：标记新鲜时也不会漏掉新列。"""
from __future__ import annotations

import sqlite3

import db


def test_ensure_latest_columns_adds_missing(fresh_db):
    # 模拟老库：建一个只有旧列的 decision_actions
    fresh_db.execute("DROP TABLE IF EXISTS decision_actions")
    fresh_db.execute("""
        CREATE TABLE decision_actions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            decision_id INTEGER NOT NULL,
            asset TEXT NOT NULL DEFAULT '',
            action TEXT NOT NULL,
            duration_min INTEGER NOT NULL DEFAULT 0,
            expires_at INTEGER NOT NULL DEFAULT 0,
            note TEXT NOT NULL DEFAULT '',
            source TEXT NOT NULL DEFAULT 'ui',
            created_at TEXT NOT NULL DEFAULT (datetime('now'))
        )""")
    fresh_db.commit()
    added = db.ensure_latest_columns(fresh_db)
    assert "decision_actions.scope" in added
    assert "decision_actions.event_type" in added
    cols = [r[1] for r in fresh_db.execute("PRAGMA table_info(decision_actions)")]
    assert "scope" in cols and "event_type" in cols


def test_ensure_latest_columns_idempotent(fresh_db):
    assert db.ensure_latest_columns(fresh_db) == []
