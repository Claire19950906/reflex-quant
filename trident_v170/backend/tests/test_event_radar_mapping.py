# -*- coding: utf-8 -*-
"""event_radar_handler → ai_decisions 反向映射（v226+ Q5 闭环）单元测试。"""
from __future__ import annotations

import json
import sqlite3
import time
from typing import Any, Dict, List

import pytest


@pytest.fixture
def tmp_db(monkeypatch, tmp_path):
    """隔离 SQLite 到 tmp 文件，含 event_atom + ai_decisions 表。"""
    db_path = tmp_path / "test_event_bus.db"
    monkeypatch.setenv("TRIDENT_DB_PATH", str(db_path))
    import db as _db
    monkeypatch.setattr(_db, "get_db_path", lambda: str(db_path))
    conn = sqlite3.connect(str(db_path))
    try:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS event_atom (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                headline TEXT NOT NULL DEFAULT '',
                ts INTEGER NOT NULL,
                impact_score REAL NOT NULL DEFAULT 0.5
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS ai_decisions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                news_id INTEGER NOT NULL DEFAULT 0,
                target_asset TEXT NOT NULL DEFAULT 'NONE',
                suggested_action TEXT NOT NULL DEFAULT 'HOLD',
                doubao_action TEXT NOT NULL DEFAULT 'HOLD',
                sentiment_score REAL NOT NULL DEFAULT 0.0,
                event_atom_ids TEXT NOT NULL DEFAULT '[]',
                created_at TEXT NOT NULL DEFAULT (datetime('now'))
            )
        """)
        conn.commit()
    finally:
        conn.close()
    yield str(db_path)


def _insert_atom(conn, headline: str, impact_score: float = 0.5) -> int:
    cur = conn.cursor()
    cur.execute(
        "INSERT INTO event_atom (headline, ts, impact_score) VALUES (?, ?, ?)",
        (headline, int(time.time()), impact_score),
    )
    conn.commit()
    return int(cur.lastrowid)


def _insert_decision(conn, asset: str, action: str,
                     sentiment: float, atom_ids: List[int],
                     created_at: str = "") -> int:
    cur = conn.cursor()
    if created_at:
        cur.execute(
            """INSERT INTO ai_decisions
               (news_id, target_asset, doubao_action, sentiment_score,
                event_atom_ids, created_at)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (0, asset, action, sentiment, json.dumps(atom_ids), created_at),
        )
    else:
        cur.execute(
            """INSERT INTO ai_decisions
               (news_id, target_asset, doubao_action, sentiment_score, event_atom_ids)
               VALUES (?, ?, ?, ?, ?)""",
            (0, asset, action, sentiment, json.dumps(atom_ids)),
        )
    conn.commit()
    return int(cur.lastrowid)


def test_fetch_decisions_empty_atom_list(tmp_db):
    """空 atom_ids 应返回空字典，不崩。"""
    import discovery as dc
    conn = sqlite3.connect(tmp_db)
    conn.row_factory = sqlite3.Row
    try:
        out = dc._fetch_decisions_by_atom_ids(conn, [])
    finally:
        conn.close()
    assert out == {}


def test_fetch_decisions_no_matching_decisions(tmp_db):
    """event_atom 有，但无 ai_decisions 关联 → 返回空列表。"""
    import discovery as dc
    conn = sqlite3.connect(tmp_db)
    conn.row_factory = sqlite3.Row
    try:
        a1 = _insert_atom(conn, "测试新闻 1")
        out = dc._fetch_decisions_by_atom_ids(conn, [a1])
    finally:
        conn.close()
    assert out == {a1: []}


def test_fetch_decisions_single_atom_single_decision(tmp_db):
    """1 个 atom → 1 条 decision，正确提取字段。"""
    import discovery as dc
    conn = sqlite3.connect(tmp_db)
    conn.row_factory = sqlite3.Row
    try:
        a1 = _insert_atom(conn, "测试新闻 1")
        d1 = _insert_decision(conn, "BTC", "BUY", 0.85, [a1])
        out = dc._fetch_decisions_by_atom_ids(conn, [a1])
    finally:
        conn.close()
    assert a1 in out
    assert len(out[a1]) == 1
    entry = out[a1][0]
    assert entry["asset"] == "BTC"
    assert entry["action"] == "BUY"
    assert entry["score"] == 0.85
    assert entry["decision_id"] == d1


def test_fetch_decisions_multi_atom_shared(tmp_db):
    """1 个 atom 被多个 decision 引用 → 都返回，按时间倒序。"""
    import discovery as dc
    conn = sqlite3.connect(tmp_db)
    conn.row_factory = sqlite3.Row
    try:
        a1 = _insert_atom(conn, "Fed 加息")
        # 用显式 created_at 避免 datetime('now') 同值时 order 不定
        d1 = _insert_decision(conn, "BTC", "SELL", 0.7, [a1],
                              created_at="2024-01-01 10:00:00")
        d2 = _insert_decision(conn, "XAU", "BUY", 0.65, [a1],
                              created_at="2024-01-01 10:01:00")
        d3 = _insert_decision(conn, "WTI", "SELL", 0.6, [a1],
                              created_at="2024-01-01 10:02:00")
        out = dc._fetch_decisions_by_atom_ids(conn, [a1])
    finally:
        conn.close()
    assert len(out[a1]) == 3
    assets = [e["asset"] for e in out[a1]]
    assert assets == ["WTI", "XAU", "BTC"]


def test_fetch_decisions_multi_atom_in_one_decision(tmp_db):
    """1 条 decision 引用多个 atom → 反查时每个 atom 都能拿到它。"""
    import discovery as dc
    conn = sqlite3.connect(tmp_db)
    conn.row_factory = sqlite3.Row
    try:
        a1 = _insert_atom(conn, "新闻 A")
        a2 = _insert_atom(conn, "新闻 B")
        d1 = _insert_decision(conn, "ETH", "BUY", 0.8, [a1, a2])
        out = dc._fetch_decisions_by_atom_ids(conn, [a1, a2])
    finally:
        conn.close()
    assert len(out[a1]) == 1
    assert len(out[a2]) == 1
    assert out[a1][0]["asset"] == "ETH"
    assert out[a2][0]["asset"] == "ETH"
    assert out[a1][0]["decision_id"] == d1


def test_fetch_decisions_fallback_suggested_action(tmp_db):
    """doubao_action 为空时回退到 suggested_action。"""
    import discovery as dc
    conn = sqlite3.connect(tmp_db)
    conn.row_factory = sqlite3.Row
    try:
        a1 = _insert_atom(conn, "新闻")
        cur = conn.cursor()
        cur.execute(
            """INSERT INTO ai_decisions
               (news_id, target_asset, doubao_action, suggested_action,
                sentiment_score, event_atom_ids)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (0, "BTC", "", "BUY", 0.5, json.dumps([a1])),
        )
        conn.commit()
        out = dc._fetch_decisions_by_atom_ids(conn, [a1])
    finally:
        conn.close()
    assert out[a1][0]["action"] == "BUY"


def test_fetch_decisions_handles_bad_json(tmp_db):
    """event_atom_ids 是坏 JSON → 不能崩。"""
    import discovery as dc
    conn = sqlite3.connect(tmp_db)
    conn.row_factory = sqlite3.Row
    try:
        a1 = _insert_atom(conn, "新闻")
        cur = conn.cursor()
        cur.execute(
            """INSERT INTO ai_decisions
               (news_id, target_asset, doubao_action, sentiment_score,
                event_atom_ids)
               VALUES (?, ?, ?, ?, ?)""",
            (0, "BTC", "BUY", 0.5, "not-valid-json"),
        )
        conn.commit()
        out = dc._fetch_decisions_by_atom_ids(conn, [a1])
    finally:
        conn.close()
    assert a1 in out  # 不崩即通过


def test_max_affected_per_atom_caps_results(tmp_db):
    """超过 _MAX_AFFECTED_PER_ATOM 的关联 → 只保留前 8。"""
    import discovery as dc
    conn = sqlite3.connect(tmp_db)
    conn.row_factory = sqlite3.Row
    try:
        a1 = _insert_atom(conn, "新闻")
        for i in range(12):
            _insert_decision(conn, f"A{i}", "BUY", 0.5, [a1])
        out = dc._fetch_decisions_by_atom_ids(conn, [a1])
    finally:
        conn.close()
    assert len(out[a1]) == dc._MAX_AFFECTED_PER_ATOM


# ── _format_affected_hint 单元测试 ──────────────────────────────────────


def test_format_hint_empty():
    """空列表 → 空串。"""
    import discovery as dc
    assert dc._format_affected_hint([]) == ""


def test_format_hint_single():
    """1 条 → '<标> → <动作>'。"""
    import discovery as dc
    out = dc._format_affected_hint([{"asset": "BTC", "action": "BUY"}])
    assert out == "BTC → BUY"


def test_format_hint_multiple():
    """多条 → 取前 3 + 余条数。"""
    import discovery as dc
    affected = [
        {"asset": "BTC", "action": "BUY"},
        {"asset": "ETH", "action": "SELL"},
        {"asset": "XAU", "action": "BUY"},
        {"asset": "WTI", "action": "SELL"},
        {"asset": "TSLA", "action": "BUY"},
    ]
    out = dc._format_affected_hint(affected)
    assert "BTC BUY" in out
    assert "ETH SELL" in out
    assert "XAU BUY" in out
    assert "+2条" in out


def test_format_hint_three_exactly():
    """恰好 3 条 → 不显示 +N条。"""
    import discovery as dc
    affected = [
        {"asset": "BTC", "action": "BUY"},
        {"asset": "ETH", "action": "SELL"},
        {"asset": "XAU", "action": "BUY"},
    ]
    out = dc._format_affected_hint(affected)
    assert "+" not in out
    assert " · " in out
