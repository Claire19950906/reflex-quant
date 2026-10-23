# -*- coding: utf-8 -*-
"""backfill_event_time_utc.py — 一键补齐 event_atom 表的时间结构化列（v171）。

历史库通常 `event_time` 列存 epoch 秒但缺 v171 新增三列：
- event_time_utc_epoch
- event_time_utc_iso
- event_time_display_tz

本脚本根据现有 `event_time` 自动派生并 UPDATE 缺这三列的行。

用法：
    # 默认数据库
    python3 -m migrations.backfill_event_time_utc

    # 指定数据库 + dry-run
    python3 -m migrations.backfill_event_time_utc --db /path/to.db --dry-run

约束：
- 仅依赖 stdlib + tz_utils（不破坏 v170 旧环境）。
- best-effort：单行失败不阻断；最后 print summary。
"""
from __future__ import annotations

import argparse
import os
import sqlite3
import sys
from datetime import datetime, timezone
from typing import Optional

# 允许 `python3 -m migrations.backfill_event_time_utc` 直接运行
sys.path.insert(
    0,
    os.path.abspath(os.path.join(os.path.dirname(__file__), os.pardir)),
)

import db  # noqa: E402
import tz_utils  # noqa: E402


def _ensure_columns(conn: sqlite3.Connection) -> None:
    """如果缺 v171 列，先 ALTER TABLE。"""
    cur_cols = {
        row[1]
        for row in conn.execute("PRAGMA table_info(event_atom)")
    }
    adds = []
    if "event_time_utc_epoch" not in cur_cols:
        adds.append(("event_time_utc_epoch", "INTEGER"))
    if "event_time_utc_iso" not in cur_cols:
        adds.append(("event_time_utc_iso", "TEXT NOT NULL DEFAULT ''"))
    if "event_time_display_tz" not in cur_cols:
        adds.append(
            ("event_time_display_tz", "TEXT NOT NULL DEFAULT 'Asia/Shanghai'")
        )
    for col, cdef in adds:
        conn.execute(f"ALTER TABLE event_atom ADD COLUMN {col} {cdef}")
    if adds:
        conn.commit()


def _row_needs_update(row: sqlite3.Row) -> bool:
    """缺任一列即视为需 backfill。"""
    epoch = row["event_time_utc_epoch"] if "event_time_utc_epoch" in row.keys() else None
    iso = row["event_time_utc_iso"] if "event_time_utc_iso" in row.keys() else None
    tz = row["event_time_display_tz"] if "event_time_display_tz" in row.keys() else None
    return not (epoch and iso and tz)


def backfill(
    conn: sqlite3.Connection,
    *,
    dry_run: bool = False,
    batch_size: int = 500,
) -> dict:
    """遍历 event_atom，逐行补齐 utc_* 三列。

    Returns:
        {"scanned": int, "updated": int, "skipped": int, "failed": int}
    """
    _ensure_columns(conn)

    summary = {"scanned": 0, "updated": 0, "skipped": 0, "failed": 0}
    cur = conn.execute(
        """SELECT id, event_time, event_time_utc_epoch, event_time_utc_iso,
                  event_time_display_tz
             FROM event_atom
            ORDER BY id"""
    )
    rows = cur.fetchall()
    summary["scanned"] = len(rows)

    display_tz_default = tz_utils.get_display_tz_name()
    pending_updates = []

    for row in rows:
        if not _row_needs_update(row):
            summary["skipped"] += 1
            continue
        try:
            ev_epoch = int(row["event_time"])
            utc_iso = datetime.fromtimestamp(
                ev_epoch, tz=timezone.utc
            ).strftime("%Y-%m-%dT%H:%M:%SZ")
            pending_updates.append(
                (ev_epoch, utc_iso, display_tz_default, int(row["id"]))
            )
        except Exception:
            summary["failed"] += 1

    if dry_run:
        summary["updated"] = len(pending_updates)
        return summary

    # 分批 UPDATE（避免锁表）
    for i in range(0, len(pending_updates), batch_size):
        batch = pending_updates[i:i + batch_size]
        try:
            conn.executemany(
                """UPDATE event_atom
                      SET event_time_utc_epoch   = ?,
                          event_time_utc_iso     = ?,
                          event_time_display_tz  = ?
                    WHERE id = ?""",
                batch,
            )
            conn.commit()
            summary["updated"] += len(batch)
        except Exception:
            summary["failed"] += len(batch)

    return summary


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Backfill event_atom time-structured columns (v171)"
    )
    parser.add_argument("--db", help="DB path（默认从 db.get_connection()）")
    parser.add_argument("--dry-run", action="store_true",
                        help="只统计，不 UPDATE")
    parser.add_argument("--batch-size", type=int, default=500,
                        help="每批 UPDATE 大小（默认 500）")
    args = parser.parse_args()

    if args.db:
        os.environ["TRIDENT_DB_PATH"] = args.db

    conn: Optional[sqlite3.Connection] = None
    try:
        if args.db:
            conn = sqlite3.connect(args.db)
            conn.row_factory = sqlite3.Row
        else:
            conn = db.get_connection()
            conn.row_factory = sqlite3.Row

        summary = backfill(
            conn, dry_run=args.dry_run, batch_size=args.batch_size
        )
        print(
            f"[backfill] scanned={summary['scanned']} "
            f"updated={summary['updated']} "
            f"skipped={summary['skipped']} "
            f"failed={summary['failed']} "
            f"dry_run={args.dry_run}"
        )
        return 0
    except Exception as exc:
        print(f"[backfill] error: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1
    finally:
        if conn:
            try:
                conn.close()
            except Exception:
                pass


if __name__ == "__main__":
    raise SystemExit(main())