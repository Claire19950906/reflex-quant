#!/usr/bin/env python3
"""
Trident Agent — Worker observability: heartbeat + DLQ helpers.

Two responsibilities:

  heartbeat.tick(conn, worker_name, processed=0, detail='', status='ok', error='')
      Insert-or-update a single row keyed by worker_name. Each worker task
      (ingest / ai / reflection / forward) calls this once per loop tick.
      Stale rows = worker stuck → watchdog / /metrics scans them.

  dlq.record(conn, kind, source='', payload='', error='', retry_count=0)
      Append a permanent record of a fatal failure (parse error after all
      retries, unreachable LLM, etc.). DLQ ≠ retry queue; it is the final
      "I have given up" sink so the rest of the system keeps running.

Both functions commit on their own. They never raise from the DB layer
on transient errors — observability must not crash the calling worker.

Schema is owned by db.py (`worker_heartbeat`, `dlq_events` tables).
This module never mutates the schema; it only reads/writes those tables.
"""

from __future__ import annotations

import json
import sqlite3
from typing import Any, Dict, List, Optional

# A worker that hasn't tick'd for more than this is considered stuck.
# Tunable by operations; default 90s (3× a normal 30s tick).
DEFAULT_STALE_AFTER_SEC = 90

# DLQ payload size cap to prevent DB bloat from a single bad message.
PAYLOAD_MAX_BYTES = 4096


def _truncate(s, limit):
    if not s:
        return ""
    s = str(s)
    if len(s) <= limit:
        return s
    return s[:limit - 3] + "..."


def heartbeat_tick(
    conn: sqlite3.Connection,
    worker_name: str,
    processed: int = 0,
    detail: str = "",
    status: str = "ok",
    error: str = "",
) -> None:
    """Record one tick for `worker_name`.

    Upserts (worker_name is UNIQUE). Each worker should call this once per
    loop iteration. A worker that hasn't tick'd in > stale seconds is dead.

    On transient DB failure: log via `print` and swallow. Observability
    must NEVER crash the caller.
    """
    if status not in ("ok", "busy", "error"):
        status = "ok"
    try:
        conn.execute(
            """
            INSERT INTO worker_heartbeat
              (worker_name, last_tick_at, last_status, last_error, processed, detail)
            VALUES (?, datetime('now', 'localtime'), ?, ?, ?, ?)
            ON CONFLICT(worker_name) DO UPDATE SET
              last_tick_at = excluded.last_tick_at,
              last_status  = excluded.last_status,
              last_error   = excluded.last_error,
              processed    = excluded.processed,
              detail       = excluded.detail
            """,
            (worker_name, status, _truncate(error, 500), int(processed),
             _truncate(detail, 1000)),
        )
        conn.commit()
    except sqlite3.Error as exc:
        print(f"[OBS] heartbeat_tick({worker_name}) failed: {exc}",
              flush=True)
        try:
            conn.rollback()
        except sqlite3.Error:
            pass


def dlq_record(
    conn: sqlite3.Connection,
    kind: str,
    source: str = "",
    payload: Any = "",
    error: str = "",
    retry_count: int = 0,
) -> Optional[int]:
    """Append a dead-letter row. Returns inserted row id (or None on error)."""
    try:
        if isinstance(payload, (dict, list)):
            payload_str = json.dumps(payload, ensure_ascii=False)
        else:
            payload_str = str(payload)
        payload_str = _truncate(payload_str, PAYLOAD_MAX_BYTES)

        cur = conn.execute(
            """
            INSERT INTO dlq_events
              (kind, source, payload, last_error, retry_count)
            VALUES (?, ?, ?, ?, ?)
            """,
            (kind, _truncate(source, 200), payload_str,
             _truncate(error, 500), int(retry_count)),
        )
        conn.commit()
        return int(cur.lastrowid)
    except sqlite3.Error as exc:
        print(f"[OBS] dlq_record({kind}) failed: {exc}", flush=True)
        try:
            conn.rollback()
        except sqlite3.Error:
            pass
        return None


def list_stale_workers(conn: sqlite3.Connection,
                       stale_after_sec: int = DEFAULT_STALE_AFTER_SEC
                       ) -> List[Dict[str, Any]]:
    """Return rows for workers whose last tick is older than `stale_after_sec`.

    Used by watchdog / /metrics endpoint. Uses julianday() arithmetic on the
    stored `last_tick_at` (localtime).
    """
    try:
        cur = conn.execute(
            """
            SELECT worker_name, last_tick_at, last_status, last_error,
                   processed,
                   CAST((julianday('now', 'localtime') - julianday(last_tick_at))
                        * 86400 AS INTEGER) AS age_sec
            FROM worker_heartbeat
            WHERE CAST((julianday('now', 'localtime') - julianday(last_tick_at))
                       * 86400 AS INTEGER) > ?
            ORDER BY age_sec DESC
            """,
            (int(stale_after_sec),),
        )
        return [dict(r) for r in cur.fetchall()]
    except sqlite3.Error:
        return []


def list_unresolved_dlq(conn: sqlite3.Connection,
                        limit: int = 100) -> List[Dict[str, Any]]:
    """Return the most recent unresolved DLQ rows."""
    try:
        cur = conn.execute(
            """
            SELECT id, kind, source, last_error, retry_count, created_at
            FROM dlq_events
            WHERE resolved = 0
            ORDER BY created_at DESC
            LIMIT ?
            """,
            (int(limit),),
        )
        return [dict(r) for r in cur.fetchall()]
    except sqlite3.Error:
        return []


def resolve_dlq(conn: sqlite3.Connection, dlq_id: int,
                note: str = "") -> bool:
    """Mark a DLQ row as resolved. Returns True on success."""
    try:
        cur = conn.execute(
            """
            UPDATE dlq_events
            SET resolved = 1, resolved_at = datetime('now', 'localtime'),
                last_error = CASE WHEN ? != '' THEN last_error || ' | resolved: ' || ?
                                   ELSE last_error END
            WHERE id = ? AND resolved = 0
            """,
            (note, _truncate(note, 200), int(dlq_id)),
        )
        conn.commit()
        return cur.rowcount > 0
    except sqlite3.Error as exc:
        print(f"[OBS] resolve_dlq({dlq_id}) failed: {exc}", flush=True)
        try:
            conn.rollback()
        except sqlite3.Error:
            pass
        return False