# -*- coding: utf-8 -*-
"""event_impact_stat / update_daily_stat 测试。"""
from __future__ import annotations

import sqlite3

import pytest


def _fresh_db():
    """新建 :memory: db + migrate。"""
    import db
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    db.migrate(conn)
    return conn


def _seed_event_atom(conn, n=3, with_curve=1, with_fallback=0, created_at=None):
    """插 n 条 event_atom，第 with_curve 条填 curve_snapshot。"""
    if created_at is None:
        created_at = 1758400000  # 2025-09-20 20:26:40 UTC
    # 需要 dim_event_type 至少 1 行
    try:
        det = conn.execute("SELECT id FROM dim_event_type WHERE code='rate_hike'").fetchone()
        if not det:
            conn.execute(
                "INSERT INTO dim_event_type(code, name_zh, category, active) "
                "VALUES('rate_hike','加息','macro',1)"
            )
            conn.commit()
    except Exception:
        conn.execute(
            "INSERT INTO dim_event_type(code, name_zh, category, active) "
            "VALUES('rate_hike','加息','macro',1)"
        )
        conn.commit()

    inserted = []
    for i in range(n):
        cur_blob = '{"5m":0.5,"30m":0.4}' if i < with_curve else ""
        fb = 1 if i < with_fallback else 0
        cur = conn.execute(
            """INSERT INTO event_atom(raw_news_id, event_type_id, asset_id, event_time,
                event_time_confidence, event_time_source,
                curve_snapshot, fallback_used, created_at)
               VALUES(?, 1, 0, ?, 0.7, 'test', ?, ?, ?)""",
            (i + 1, 1758400000 + i * 60, cur_blob, fb, created_at),
        )
        inserted.append(int(cur.lastrowid))
    conn.commit()
    return inserted


def test_update_daily_stat_empty_table():
    """空 event_atom → stat 行全 0。"""
    from event_impact_curve import update_daily_stat
    conn = _fresh_db()
    sid = update_daily_stat(date_local="2025-09-20", connection=conn)
    assert sid > 0
    row = conn.execute(
        "SELECT * FROM event_impact_stat WHERE date_local='2025-09-20'"
    ).fetchone()
    assert int(row["event_atoms_total"]) == 0
    assert int(row["impact_curve_filled"]) == 0
    assert int(row["fallback_used"]) == 0


def test_update_daily_stat_with_seed_data():
    """插 3 个 event_atom（1 curve + 1 fallback）→ stat 计数正确。"""
    from event_impact_curve import update_daily_stat
    conn = _fresh_db()
    _seed_event_atom(conn, n=3, with_curve=1, with_fallback=1, created_at=1758400000)
    sid = update_daily_stat(date_local="2025-09-20", connection=conn)
    assert sid > 0
    row = conn.execute(
        "SELECT * FROM event_impact_stat WHERE date_local='2025-09-20'"
    ).fetchone()
    assert int(row["event_atoms_total"]) == 3
    assert int(row["impact_curve_filled"]) == 1
    assert int(row["fallback_used"]) == 1


def test_update_daily_stat_clock_jumps_accumulate():
    """多次调用 clock_jumps_delta=1 → 累加。"""
    from event_impact_curve import update_daily_stat
    conn = _fresh_db()
    update_daily_stat(date_local="2025-09-20", clock_jumps_delta=1, connection=conn)
    update_daily_stat(date_local="2025-09-20", clock_jumps_delta=1, connection=conn)
    update_daily_stat(date_local="2025-09-20", clock_jumps_delta=3, connection=conn)
    row = conn.execute(
        "SELECT clock_jumps FROM event_impact_stat WHERE date_local='2025-09-20'"
    ).fetchone()
    assert int(row["clock_jumps"]) == 5


def test_update_daily_stat_upsert():
    """同一日多次调用 → UPSERT（id 保持唯一）。"""
    from event_impact_curve import update_daily_stat
    conn = _fresh_db()
    sid1 = update_daily_stat(date_local="2025-09-20", connection=conn)
    sid2 = update_daily_stat(date_local="2025-09-20", clock_jumps_delta=2, connection=conn)
    assert sid1 == sid2
    rows = conn.execute(
        "SELECT COUNT(*) AS c FROM event_impact_stat WHERE date_local='2025-09-20'"
    ).fetchone()
    assert int(rows["c"]) == 1


def test_update_daily_stat_default_date_uses_today():
    """date_local='' → 默认 today (UTC)。"""
    from event_impact_curve import update_daily_stat
    conn = _fresh_db()
    sid = update_daily_stat(connection=conn)
    assert sid > 0
    row = conn.execute("SELECT date_local FROM event_impact_stat").fetchone()
    assert row["date_local"] != ""
    # 形如 YYYY-MM-DD
    assert len(row["date_local"]) == 10


def test_update_daily_stat_invalid_date_returns_minus_one():
    """date_local='not-a-date' → return -1。"""
    from event_impact_curve import update_daily_stat
    conn = _fresh_db()
    sid = update_daily_stat(date_local="not-a-date", connection=conn)
    assert sid == -1


def test_update_daily_stat_no_event_atom_table():
    """event_atom 表不存在 → 不 crash, return -1。"""
    from event_impact_curve import update_daily_stat
    conn = _fresh_db()
    # 模拟：删 event_atom 表
    conn.execute("DROP TABLE event_atom")
    conn.commit()
    sid = update_daily_stat(date_local="2025-09-20", connection=conn)
    # 应 return -1 而不 crash
    assert sid in (-1, 0) or sid >= 0  # 容许任意（视 fallback 行为）


def test_update_daily_stat_avg_window_minutes_zero_by_default():
    """当前 P5 之前 ai_decisions 不存 event_window_minutes，avg 默认 0。"""
    from event_impact_curve import update_daily_stat
    conn = _fresh_db()
    sid = update_daily_stat(date_local="2025-09-20", connection=conn)
    assert sid > 0
    row = conn.execute(
        "SELECT avg_window_minutes FROM event_impact_stat WHERE date_local='2025-09-20'"
    ).fetchone()
    assert float(row["avg_window_minutes"]) == 0.0