# -*- coding: utf-8 -*-
"""backfill_event_time_utc 集成测试。"""
from __future__ import annotations

import os
import sqlite3
import sys

import pytest


def _fresh_db():
    """新建 :memory: sqlite + migrate。"""
    import db
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    db.migrate(conn)
    return conn


def _drop_v171_cols(conn):
    """模拟历史库：删 v171 新增列（通过建新表 + 拷贝 + RENAME 实现）。"""
    rows = list(conn.execute("PRAGMA table_info(event_atom)"))
    has_v171 = any(r[1] in {"event_time_utc_epoch", "event_time_utc_iso",
                            "event_time_display_tz", "curve_snapshot",
                            "fallback_used"} for r in rows)
    if not has_v171:
        return
    conn.execute("""CREATE TABLE event_atom__old (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        raw_news_id INTEGER, event_type_id INTEGER, asset_id INTEGER,
        event_time INTEGER, event_time_confidence REAL,
        event_time_source TEXT, magnitude_value REAL, magnitude_unit TEXT,
        actor TEXT, direction TEXT, direction_confidence REAL,
        direction_source TEXT, evidence_count INTEGER,
        parser_version TEXT, cluster_id INTEGER, reasoning TEXT,
        created_at INTEGER
    )""")
    conn.execute("INSERT INTO event_atom__old SELECT id, raw_news_id, event_type_id, asset_id, event_time, event_time_confidence, event_time_source, magnitude_value, magnitude_unit, actor, direction, direction_confidence, direction_source, evidence_count, parser_version, cluster_id, reasoning, created_at FROM event_atom")
    conn.execute("DROP TABLE event_atom")
    conn.execute("ALTER TABLE event_atom__old RENAME TO event_atom")
    conn.commit()


def test_backfill_empty_table():
    """空表 backfill → scanned=0 / updated=0 / skipped=0 / failed=0。"""
    from migrations.backfill_event_time_utc import backfill
    conn = _fresh_db()
    summary = backfill(conn, dry_run=True)
    assert summary == {"scanned": 0, "updated": 0, "skipped": 0, "failed": 0}


def test_backfill_already_filled():
    """已经有 v171 列的行 → skipped。"""
    from migrations.backfill_event_time_utc import backfill
    conn = _fresh_db()
    conn.execute("""
        INSERT INTO event_atom(raw_news_id, event_type_id, event_time,
            event_time_utc_epoch, event_time_utc_iso, event_time_display_tz,
            created_at)
        VALUES(1, 1, ?, 1758400000, '2025-09-20T20:26:40Z', 'Asia/Shanghai', 1758400000)
    """, (1758400000,))
    conn.commit()
    summary = backfill(conn, dry_run=False)
    assert summary["scanned"] >= 1
    assert summary["updated"] == 0
    assert summary["skipped"] >= 1


def test_backfill_fills_missing_columns():
    """缺 v171 列的行 → backfill 自动派生 utc_epoch / utc_iso / display_tz。"""
    from migrations.backfill_event_time_utc import backfill
    conn = _fresh_db()
    _drop_v171_cols(conn)
    conn.execute("""
        INSERT INTO event_atom(raw_news_id, event_type_id, event_time)
        VALUES(1, 1, 1758400000)
    """)
    conn.commit()
    summary = backfill(conn, dry_run=False)
    assert summary["updated"] >= 1
    cols = {r[1] for r in conn.execute("PRAGMA table_info(event_atom)")}
    assert "event_time_utc_epoch" in cols
    assert "event_time_utc_iso" in cols
    assert "event_time_display_tz" in cols
    row = conn.execute("""
        SELECT event_time_utc_epoch, event_time_utc_iso, event_time_display_tz
          FROM event_atom LIMIT 1
    """).fetchone()
    assert int(row[0]) == 1758400000
    assert row[1] == "2025-09-20T20:26:40Z"
    assert row[2] in ("Asia/Shanghai", "UTC", "US/Eastern", "US/Pacific")


def test_backfill_dry_run_does_not_update():
    """dry_run=True → summary 报告 updated 但 DB 不变。"""
    from migrations.backfill_event_time_utc import backfill
    conn = _fresh_db()
    _drop_v171_cols(conn)
    conn.execute("INSERT INTO event_atom(raw_news_id, event_type_id, event_time) VALUES(1, 1, 1758400000)")
    conn.commit()
    summary = backfill(conn, dry_run=True)
    assert summary["updated"] >= 1
    row = conn.execute("SELECT event_time_utc_iso FROM event_atom LIMIT 1").fetchone()
    assert row[0] == "" or row[0] is None


def test_backfill_partial_fill():
    """有 epoch 但缺 iso / tz → 仅补缺的。"""
    from migrations.backfill_event_time_utc import backfill
    conn = _fresh_db()
    _drop_v171_cols(conn)
    conn.execute("ALTER TABLE event_atom ADD COLUMN event_time_utc_epoch INTEGER")
    conn.execute("ALTER TABLE event_atom ADD COLUMN event_time_utc_iso TEXT NOT NULL DEFAULT ''")
    conn.execute("ALTER TABLE event_atom ADD COLUMN event_time_display_tz TEXT NOT NULL DEFAULT 'Asia/Shanghai'")
    conn.execute("INSERT INTO event_atom(raw_news_id, event_type_id, event_time, event_time_utc_epoch) VALUES(1, 1, 1758400000, 1758400000)")
    conn.commit()
    summary = backfill(conn, dry_run=False)
    assert summary["updated"] >= 1
    row = conn.execute("SELECT event_time_utc_iso, event_time_display_tz FROM event_atom LIMIT 1").fetchone()
    assert row[0] == "2025-09-20T20:26:40Z"
    assert row[1] in ("Asia/Shanghai", "UTC", "US/Eastern", "US/Pacific")


def test_backfill_handles_invalid_event_time_gracefully():
    """event_time=0 的行 → 不 crash，summary 仍输出。"""
    from migrations.backfill_event_time_utc import backfill
    conn = _fresh_db()
    _drop_v171_cols(conn)
    conn.execute("INSERT INTO event_atom(raw_news_id, event_type_id, event_time) VALUES(1, 1, 0)")
    conn.commit()
    summary = backfill(conn, dry_run=False)
    assert summary["scanned"] >= 1
    # 不 crash 即可（failed>=1 或 updated>=1 都可）


def test_backfill_main_runs_dry_run(monkeypatch, capsys):
    """main() argv 路径：--dry-run 应打印 summary。"""
    import tempfile
    from migrations.backfill_event_time_utc import main
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    try:
        import db
        conn = sqlite3.connect(path)
        db.migrate(conn)
        conn.close()
        monkeypatch.setattr(sys, "argv", ["prog", "--dry-run", "--db", path])
        rc = main()
        captured = capsys.readouterr()
        assert "[backfill]" in captured.out
        assert rc == 0
    finally:
        try:
            os.unlink(path)
        except Exception:
            pass