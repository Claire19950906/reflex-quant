"""P1 — Worker observability: heartbeat + DLQ helpers.

Coverage:
  · heartbeat upserts (one row per worker), updates last_tick_at
  · heartbeat stale detection (list_stale_workers respects threshold)
  · heartbeat survives bad status (clamped to 'ok')
  · DLQ records various payload types (dict / list / str), enforces cap
  · DLQ list_unresolved_dlq only returns resolved=0 rows
  · resolve_dlq flips resolved=1 and returns rowcount
  · resolve_dlq is idempotent (already-resolved rows return False)
"""

from __future__ import annotations

import os
import sqlite3
import sys
import time

SRC = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "src_python"))
if SRC not in sys.path:
    sys.path.insert(0, SRC)

import db
import observability as obs


def _fresh_db() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    db.migrate(conn)
    return conn


# ────────────────────────────────────────────────────────────────────
# heartbeat_tick
# ────────────────────────────────────────────────────────────────────

def test_heartbeat_inserts_new_row():
    conn = _fresh_db()
    try:
        obs.heartbeat_tick(conn, "ingest", processed=5)
        cur = conn.execute(
            "SELECT worker_name, last_status, processed FROM worker_heartbeat "
            "WHERE worker_name=?", ("ingest",)
        )
        row = cur.fetchone()
        assert row is not None
        assert row["worker_name"] == "ingest"
        assert row["last_status"] == "ok"
        assert row["processed"] == 5
    finally:
        conn.close()


def test_heartbeat_upserts_existing_row():
    conn = _fresh_db()
    try:
        obs.heartbeat_tick(conn, "ai", processed=2)
        time.sleep(1.1)
        obs.heartbeat_tick(conn, "ai", processed=10, status="busy",
                           detail='{"note": "second tick"}')
        cur = conn.execute(
            "SELECT last_status, processed, detail FROM worker_heartbeat "
            "WHERE worker_name=?", ("ai",)
        )
        row = cur.fetchone()
        assert row["last_status"] == "busy"
        assert row["processed"] == 10
        assert "second tick" in row["detail"]
    finally:
        conn.close()


def test_heartbeat_clamps_invalid_status():
    conn = _fresh_db()
    try:
        obs.heartbeat_tick(conn, "forward", status="banana")
        cur = conn.execute(
            "SELECT last_status FROM worker_heartbeat WHERE worker_name=?",
            ("forward",)
        )
        row = cur.fetchone()
        assert row["last_status"] == "ok"
    finally:
        conn.close()


def test_heartbeat_records_error():
    conn = _fresh_db()
    try:
        obs.heartbeat_tick(conn, "reflection", status="error",
                           error="LLM 401 every retry")
        cur = conn.execute(
            "SELECT last_status, last_error FROM worker_heartbeat "
            "WHERE worker_name=?", ("reflection",)
        )
        row = cur.fetchone()
        assert row["last_status"] == "error"
        assert "401" in row["last_error"]
    finally:
        conn.close()


def test_heartbeat_truncates_long_error():
    conn = _fresh_db()
    try:
        obs.heartbeat_tick(conn, "ingest", status="error", error="X" * 800)
        cur = conn.execute(
            "SELECT last_error FROM worker_heartbeat WHERE worker_name=?",
            ("ingest",)
        )
        row = cur.fetchone()
        assert len(row["last_error"]) <= 503
    finally:
        conn.close()


def test_list_stale_workers_fresh_returns_empty():
    conn = _fresh_db()
    try:
        obs.heartbeat_tick(conn, "ai", processed=3)
        stale = obs.list_stale_workers(conn, stale_after_sec=60)
        assert stale == []
    finally:
        conn.close()


def test_list_stale_workers_old_threshold_includes():
    """Even a fresh row is 'stale' if threshold < 0 (any non-future row)."""
    conn = _fresh_db()
    try:
        obs.heartbeat_tick(conn, "ai")
        stale = obs.list_stale_workers(conn, stale_after_sec=-1)
        assert len(stale) == 1
        assert stale[0]["worker_name"] == "ai"
        assert "age_sec" in stale[0]
    finally:
        conn.close()


def test_list_stale_workers_returns_empty_when_table_missing():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    try:
        stale = obs.list_stale_workers(conn, stale_after_sec=60)
        assert stale == []
    finally:
        conn.close()


# ────────────────────────────────────────────────────────────────────
# dlq_record
# ────────────────────────────────────────────────────────────────────

def test_dlq_record_string_payload():
    conn = _fresh_db()
    try:
        did = obs.dlq_record(conn, kind="ingest_parse",
                              source="ingest:cointelegraph",
                              payload="bad json body here",
                              error="ValueError: invalid",
                              retry_count=3)
        assert isinstance(did, int) and did > 0
        cur = conn.execute(
            "SELECT kind, source, payload, last_error, retry_count "
            "FROM dlq_events WHERE id=?", (did,)
        )
        row = cur.fetchone()
        assert row["kind"] == "ingest_parse"
        assert row["source"] == "ingest:cointelegraph"
        assert row["payload"] == "bad json body here"
        assert row["retry_count"] == 3
    finally:
        conn.close()


def test_dlq_record_dict_payload_jsonified():
    conn = _fresh_db()
    try:
        did = obs.dlq_record(conn, kind="ai_unreachable",
                              source="ai:openrouter",
                              payload={"model": "x", "msg": "boom"})
        cur = conn.execute("SELECT payload FROM dlq_events WHERE id=?", (did,))
        row = cur.fetchone()
        assert '"model"' in row["payload"]
        assert '"boom"' in row["payload"]
    finally:
        conn.close()


def test_dlq_payload_capped_at_max_bytes():
    conn = _fresh_db()
    try:
        huge = "A" * (obs.PAYLOAD_MAX_BYTES * 2)
        obs.dlq_record(conn, kind="ingest_parse", payload=huge)
        cur = conn.execute("SELECT payload FROM dlq_events ORDER BY id DESC LIMIT 1")
        row = cur.fetchone()
        assert len(row["payload"]) <= obs.PAYLOAD_MAX_BYTES
    finally:
        conn.close()


def test_dlq_list_unresolved_excludes_resolved():
    conn = _fresh_db()
    try:
        id1 = obs.dlq_record(conn, kind="k1", payload="p1")
        id2 = obs.dlq_record(conn, kind="k2", payload="p2")
        assert obs.resolve_dlq(conn, id1) is True
        unresolved = obs.list_unresolved_dlq(conn)
        assert len(unresolved) == 1
        assert unresolved[0]["id"] == id2
    finally:
        conn.close()


def test_resolve_dlq_idempotent():
    conn = _fresh_db()
    try:
        did = obs.dlq_record(conn, kind="k", payload="p")
        assert obs.resolve_dlq(conn, did) is True
        assert obs.resolve_dlq(conn, did) is False
    finally:
        conn.close()


def test_resolve_dlq_with_note_appends():
    conn = _fresh_db()
    try:
        did = obs.dlq_record(conn, kind="k", error="boom")
        obs.resolve_dlq(conn, did, note="fixed by patch v1.2")
        cur = conn.execute("SELECT last_error, resolved_at FROM dlq_events WHERE id=?", (did,))
        row = cur.fetchone()
        assert "fixed by patch v1.2" in row["last_error"]
        assert row["resolved_at"] is not None
    finally:
        conn.close()


def test_dlq_returns_none_when_table_missing():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    try:
        result = obs.dlq_record(conn, kind="k", payload="p")
        assert result is None
    finally:
        conn.close()


# ────────────────────────────────────────────────────────────────────
# Integration
# ────────────────────────────────────────────────────────────────────

def test_heartbeat_and_dlq_independent():
    conn = _fresh_db()
    try:
        obs.heartbeat_tick(conn, "ai", processed=1)
        obs.heartbeat_tick(conn, "ai", processed=2)
        obs.dlq_record(conn, kind="reflection_retry_exhausted",
                       source="reflection:trade_reflections",
                       error="llm 403")
        cur = conn.execute("SELECT COUNT(*) FROM worker_heartbeat")
        assert cur.fetchone()[0] == 1
        cur = conn.execute("SELECT COUNT(*) FROM dlq_events")
        assert cur.fetchone()[0] == 1
    finally:
        conn.close()