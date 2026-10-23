#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Backfill event_atom for past raw_news (PR-3 v181).

Idempotent: re-running is safe — extract_and_persist uses ON CONFLICT
to skip duplicates within 60-second window.

Usage:
    python3 backfill_event_atoms.py [--hours 168] [--batch 100]
"""
from __future__ import annotations

import argparse
import os
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

import db
import event_atom
import sqlite3


def backfill(hours: int = 168, batch: int = 100) -> dict:
    """Backfill event_atom for raw_news in last N hours.

    Returns {processed, atoms_inserted, atoms_total_before}.
    """
    own_conn = True
    conn = db.get_connection()
    conn.row_factory = sqlite3.Row
    try:
        before = conn.execute("SELECT COUNT(*) FROM event_atom").fetchone()[0]
        rows = conn.execute(
            """SELECT id, content, ts, timestamp FROM raw_news
               WHERE timestamp >= datetime('now', ?)
                 AND length(content) > 10
                 AND id NOT IN (SELECT DISTINCT raw_news_id FROM event_atom WHERE raw_news_id IS NOT NULL)
               ORDER BY id DESC LIMIT ?""",
            (f"-{hours} hours", batch * 50),
        ).fetchall()
        print(f"  [backfill] scanning {len(rows)} raw_news rows (last {hours}h, no atoms yet)")

        processed = 0
        atoms_inserted = 0
        t0 = time.time()
        for row in rows:
            try:
                out = event_atom.extract_and_persist(
                    row["content"] or "",
                    raw_news_id=int(row["id"]),
                    default_event_time=int(row["ts"]) if row["ts"] else int(time.time()),
                    event_time_source="backfill_v181",
                    event_time_confidence=0.6,
                    connection=conn,
                )
                processed += 1
                if isinstance(out, list):
                    atoms_inserted += len(out)
                elif isinstance(out, int) and out > 0:
                    atoms_inserted += 1
                if processed % 50 == 0:
                    conn.commit()
                    elapsed = time.time() - t0
                    rate = processed / max(elapsed, 0.1)
                    print(
                        f"  [backfill] {processed}/{len(rows)} processed"
                        f" (+{atoms_inserted} atoms, {rate:.1f} rows/s)"
                    )
            except Exception as e:
                print(f"  [backfill] news={row['id']} FAIL: {type(e).__name__}: {e}")
                continue
        conn.commit()
        after = conn.execute("SELECT COUNT(*) FROM event_atom").fetchone()[0]
        print(f"\n  [backfill] DONE: processed={processed}, inserted={atoms_inserted}, "
              f"event_atom before={before}, after={after}")
        return {
            "processed": processed,
            "atoms_inserted": atoms_inserted,
            "atoms_before": before,
            "atoms_after": after,
        }
    finally:
        conn.close()


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--hours", type=int, default=168)
    p.add_argument("--batch", type=int, default=100)
    args = p.parse_args()
    backfill(hours=args.hours, batch=args.batch)