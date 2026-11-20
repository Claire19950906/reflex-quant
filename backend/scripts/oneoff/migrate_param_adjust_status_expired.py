"""v339: strategy_param_adjustments 的 status CHECK 补上 'expired'（幂等迁移）。

问题：api_server 的注释与列表端点一直宣称 status 可取
`pending/approved/rejected/applied/expired`，但建表 CHECK 只允许前四个。
结果「证据过期」的建议无法标记，86 条 40 天前的 pending 只能永远躺在待审队列里。

SQLite 不支持 ALTER TABLE 改 CHECK，只能重建表。本表无索引、无触发器、
无视图引用，122 行，重建在单个事务内完成，失败自动回滚。

用法（带 dotenv 的解释器）：
  /Library/Frameworks/Python.framework/Versions/3.14/bin/python3 \
      migrate_param_adjust_status_expired.py [--dry-run]
"""

from __future__ import annotations

import argparse
import sqlite3
import sys

import sys as _sys
from pathlib import Path as _Path
_sys.path.insert(0, str(_Path(__file__).resolve().parents[2] / "src_python"))
import db

NEW_DDL = """
CREATE TABLE strategy_param_adjustments__v339 (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    asset        TEXT    NOT NULL DEFAULT '*',
    action       TEXT    NOT NULL DEFAULT '*',
    param_key    TEXT    NOT NULL DEFAULT '',
    direction    TEXT    NOT NULL DEFAULT '',
    magnitude    REAL    DEFAULT NULL,
    sample_size  INTEGER NOT NULL DEFAULT 0,
    avg_confidence REAL  DEFAULT NULL,
    evidence_summary TEXT NOT NULL DEFAULT '',
    first_suggested_at TEXT NOT NULL DEFAULT (datetime('now')),
    last_suggested_at  TEXT NOT NULL DEFAULT (datetime('now')),
    status       TEXT    NOT NULL DEFAULT 'pending'
        CHECK (status IN ('pending', 'approved', 'rejected', 'applied', 'expired')),
    applied_strategy_version_id INTEGER DEFAULT NULL,
    applied_setting_change TEXT DEFAULT '',
    note         TEXT    NOT NULL DEFAULT '',
    created_at   TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at   TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE (asset, action, param_key, direction, status)
)
"""


def already_migrated(conn: sqlite3.Connection) -> bool:
    row = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='strategy_param_adjustments'"
    ).fetchone()
    if not row or not row[0]:
        return True  # 表不存在 -> 让 db 建表逻辑处理（已含 expired）
    return "expired" in row[0]


def migrate(conn: sqlite3.Connection, dry_run: bool) -> dict:
    if already_migrated(conn):
        return {"status": "already_migrated", "rows": 0}

    before = conn.execute("SELECT COUNT(*) FROM strategy_param_adjustments").fetchone()[0]
    colnames = [r[1] for r in conn.execute("PRAGMA table_info(strategy_param_adjustments)")]
    if dry_run:
        return {"status": "would_migrate", "rows": before, "columns": len(colnames)}

    conn.execute("BEGIN IMMEDIATE")
    try:
        conn.execute("DROP TABLE IF EXISTS strategy_param_adjustments__v339")
        conn.execute(NEW_DDL)
        cols = ", ".join(colnames)
        conn.execute(
            f"INSERT INTO strategy_param_adjustments__v339 ({cols}) "
            f"SELECT {cols} FROM strategy_param_adjustments"
        )
        conn.execute("DROP TABLE strategy_param_adjustments")
        conn.execute("ALTER TABLE strategy_param_adjustments__v339 RENAME TO strategy_param_adjustments")
        # 显式写过的 id 不会自动推高 sqlite_sequence，这里校正一次
        max_id = conn.execute("SELECT COALESCE(MAX(id), 0) FROM strategy_param_adjustments").fetchone()[0]
        conn.execute("DELETE FROM sqlite_sequence WHERE name='strategy_param_adjustments'")
        conn.execute("INSERT INTO sqlite_sequence (name, seq) VALUES ('strategy_param_adjustments', ?)",
                     (max_id,))
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    after = conn.execute("SELECT COUNT(*) FROM strategy_param_adjustments").fetchone()[0]
    return {"status": "migrated", "rows_before": before, "rows_after": after, "max_id": max_id}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()
    conn = db.get_connection()
    conn.execute("PRAGMA busy_timeout=60000")
    try:
        print(migrate(conn, args.dry_run))
        print("状态分布:", conn.execute(
            "SELECT status, COUNT(*) FROM strategy_param_adjustments GROUP BY 1").fetchall())
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
