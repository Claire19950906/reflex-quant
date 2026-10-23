"""Data-alignment gate (P0) — foreign-key orphan detection across the 5-link chain.

Coverage:
  · Empty DB → all checks return empty
  · Healthy seed (raw_news → ai_decisions → quick_sim_trades) → no orphans
  · Each of the 11 link types: insert orphan row → check detects it
  · summarize/is_clean helpers behave correctly
  · CLI smoke test (run_and_print)
  · Backstop works even when FK pragma is disabled (legacy DB scenario)
"""

from __future__ import annotations

import os
import sqlite3
import sys

SRC = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "src_python"))
if SRC not in sys.path:
    sys.path.insert(0, SRC)

import db
import integrity_check as ic


# ────────────────────────────────────────────────────────────────────
# Fixtures
# ────────────────────────────────────────────────────────────────────

def _fresh_db() -> sqlite3.Connection:
    """Build a fresh in-memory DB with the full schema applied."""
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    db.migrate(conn)
    return conn


def _seed_raw_news(conn: sqlite3.Connection) -> int:
    """Insert one minimal raw_news row, return its id."""
    cur = conn.execute(
        """INSERT INTO raw_news (source, content, content_fingerprint)
           VALUES (?, ?, ?)""",
        ("test", "stub content for orphan tests", "fp_seed_001"),
    )
    return int(cur.lastrowid)


def _seed_ai_decision(conn: sqlite3.Connection, news_id: int) -> int:
    """Insert one minimal ai_decisions row pointing to news_id."""
    cur = conn.execute(
        """INSERT INTO ai_decisions
           (news_id, sentiment_score, suggested_action, reasoning)
           VALUES (?, ?, ?, ?)""",
        (news_id, 0.5, "HOLD", "test reasoning"),
    )
    return int(cur.lastrowid)


def _seed_news_event(conn: sqlite3.Connection, news_id: int, decision_id: int | None) -> int:
    cur = conn.execute(
        """INSERT INTO news_events (news_id, decision_id, ts, source, asset, action, score)
           VALUES (?, ?, ?, ?, ?, ?, ?)""",
        (news_id, decision_id, 1_700_000_000, "test", "primary", "HOLD", 0.5),
    )
    return int(cur.lastrowid)


def _seed_factor_snapshot(conn: sqlite3.Connection, news_id: int) -> int:
    cur = conn.execute(
        """INSERT INTO factor_snapshots (news_id, ts, factors) VALUES (?, ?, ?)""",
        (news_id, 1_700_000_000, '{"test": 1.0}'),
    )
    return int(cur.lastrowid)


def _seed_hermes_obs(conn: sqlite3.Connection, news_id: int | None,
                     decision_id: int | None) -> int:
    cur = conn.execute(
        """INSERT INTO hermes_observations
           (news_id, decision_id, ts, source, asset, observation_type, content)
           VALUES (?, ?, ?, ?, ?, ?, ?)""",
        (news_id, decision_id, 1_700_000_000, "test", "primary", "news", "obs"),
    )
    return int(cur.lastrowid)


def _seed_signal_perf(conn: sqlite3.Connection, decision_id: int) -> int:
    cur = conn.execute(
        """INSERT INTO signal_performance
           (decision_id, asset, ts, action, is_correct)
           VALUES (?, ?, ?, ?, ?)""",
        (decision_id, "primary", 1_700_000_000, "HOLD", "UNKNOWN"),
    )
    return int(cur.lastrowid)


def _seed_quick_sim(conn: sqlite3.Connection, decision_id: int,
                    news_id: int | None) -> int:
    cur = conn.execute(
        """INSERT INTO quick_sim_trades
           (decision_id, news_id, asset, action, entry_price, entry_ts, horizon_minutes)
           VALUES (?, ?, ?, ?, ?, ?, ?)""",
        (decision_id, news_id, "primary", "BUY", 100.0, 1_700_000_000, 60),
    )
    return int(cur.lastrowid)


def _seed_trade_reflection(conn: sqlite3.Connection, decision_id: int,
                            news_id: int | None) -> int:
    cur = conn.execute(
        """INSERT INTO trade_reflections
           (decision_id, news_id, asset, action, verdict)
           VALUES (?, ?, ?, ?, ?)""",
        (decision_id, news_id, "primary", "BUY", "WIN"),
    )
    return int(cur.lastrowid)


def _insert_orphan_news_event_news_id(conn: sqlite3.Connection, missing_news_id: int) -> int:
    """Bypass FK by temporarily disabling pragma, then re-enable."""
    conn.execute("PRAGMA foreign_keys=OFF;")
    try:
        cur = conn.execute(
            """INSERT INTO news_events (news_id, ts) VALUES (?, ?)""",
            (missing_news_id, 1_700_000_000),
        )
        return int(cur.lastrowid)
    finally:
        conn.execute("PRAGMA foreign_keys=ON;")


def _insert_orphan_ai_decision(conn: sqlite3.Connection, missing_news_id: int) -> int:
    """ai_decisions.news_id HAS declared FK → bypass pragma."""
    conn.execute("PRAGMA foreign_keys=OFF;")
    try:
        cur = conn.execute(
            """INSERT INTO ai_decisions
               (news_id, sentiment_score, suggested_action, reasoning)
               VALUES (?, ?, ?, ?)""",
            (missing_news_id, 0.5, "HOLD", "orphan"),
        )
        return int(cur.lastrowid)
    finally:
        conn.execute("PRAGMA foreign_keys=ON;")


def _insert_orphan_factor_snap(conn: sqlite3.Connection, missing_news_id: int) -> int:
    conn.execute("PRAGMA foreign_keys=OFF;")
    try:
        cur = conn.execute(
            """INSERT INTO factor_snapshots (news_id, ts) VALUES (?, ?)""",
            (missing_news_id, 1_700_000_000),
        )
        return int(cur.lastrowid)
    finally:
        conn.execute("PRAGMA foreign_keys=ON;")


def _insert_orphan_hermes_news(conn: sqlite3.Connection, missing_news_id: int) -> int:
    conn.execute("PRAGMA foreign_keys=OFF;")
    try:
        cur = conn.execute(
            """INSERT INTO hermes_observations
               (news_id, ts, source, asset, observation_type, content)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (missing_news_id, 1_700_000_000, "test", "primary", "news", "orphan"),
        )
        return int(cur.lastrowid)
    finally:
        conn.execute("PRAGMA foreign_keys=ON;")


def _insert_orphan_news_event_decision(conn: sqlite3.Connection, missing_decision_id: int) -> int:
    conn.execute("PRAGMA foreign_keys=OFF;")
    try:
        cur = conn.execute(
            """INSERT INTO news_events (news_id, decision_id, ts)
               VALUES (?, ?, ?)""",
            (1, missing_decision_id, 1_700_000_000),
        )
        return int(cur.lastrowid)
    finally:
        conn.execute("PRAGMA foreign_keys=ON;")


def _insert_orphan_signal_perf(conn: sqlite3.Connection, missing_decision_id: int) -> int:
    conn.execute("PRAGMA foreign_keys=OFF;")
    try:
        cur = conn.execute(
            """INSERT INTO signal_performance
               (decision_id, asset, ts, action, is_correct)
               VALUES (?, ?, ?, ?, ?)""",
            (missing_decision_id, "primary", 1_700_000_000, "HOLD", "UNKNOWN"),
        )
        return int(cur.lastrowid)
    finally:
        conn.execute("PRAGMA foreign_keys=ON;")


def _insert_orphan_quick_sim_decision(conn: sqlite3.Connection, missing_decision_id: int) -> int:
    """quick_sim_trades.decision_id HAS declared FK → bypass pragma."""
    conn.execute("PRAGMA foreign_keys=OFF;")
    try:
        cur = conn.execute(
            """INSERT INTO quick_sim_trades
               (decision_id, asset, action, entry_price, entry_ts, horizon_minutes)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (missing_decision_id, "primary", "BUY", 100.0, 1_700_000_000, 60),
        )
        return int(cur.lastrowid)
    finally:
        conn.execute("PRAGMA foreign_keys=ON;")


def _insert_orphan_quick_sim_news(conn: sqlite3.Connection, missing_news_id: int) -> int:
    conn.execute("PRAGMA foreign_keys=OFF;")
    try:
        cur = conn.execute(
            """INSERT INTO quick_sim_trades
               (decision_id, news_id, asset, action, entry_price, entry_ts, horizon_minutes)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (1, missing_news_id, "primary", "BUY", 100.0, 1_700_000_000, 60),
        )
        return int(cur.lastrowid)
    finally:
        conn.execute("PRAGMA foreign_keys=ON;")


def _insert_orphan_hermes_decision(conn: sqlite3.Connection, missing_decision_id: int) -> int:
    conn.execute("PRAGMA foreign_keys=OFF;")
    try:
        cur = conn.execute(
            """INSERT INTO hermes_observations
               (decision_id, ts, source, asset, observation_type, content)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (missing_decision_id, 1_700_000_000, "test", "primary", "news", "orphan"),
        )
        return int(cur.lastrowid)
    finally:
        conn.execute("PRAGMA foreign_keys=ON;")


def _insert_orphan_trade_reflection(conn: sqlite3.Connection, missing_decision_id: int,
                                    missing_news_id: int | None = None) -> int:
    """trade_reflections.decision_id is PRIMARY KEY but has no FK → direct insert."""
    cur = conn.execute(
        """INSERT INTO trade_reflections
           (decision_id, news_id, asset, action, verdict)
           VALUES (?, ?, ?, ?, ?)""",
        (missing_decision_id, missing_news_id, "primary", "BUY", "WIN"),
    )
    return int(cur.lastrowid)


# ────────────────────────────────────────────────────────────────────
# Tests — module + helpers
# ────────────────────────────────────────────────────────────────────

def test_module_imports():
    """Smoke: integrity_check module exposes the 3 expected helpers."""
    assert callable(ic.run_checks)
    assert callable(ic.summarize)
    assert callable(ic.is_clean)
    assert callable(ic.run_and_print)
    assert len(ic._CHECKS) == 11


def test_empty_db_is_clean():
    conn = _fresh_db()
    try:
        results = ic.run_checks(conn)
        assert len(results) == 11
        for name, rows in results.items():
            assert rows == [], f"{name} expected empty, got {rows}"
        assert ic.is_clean(results)
        counts = ic.summarize(results)
        assert all(v == 0 for v in counts.values())
    finally:
        conn.close()


def test_healthy_seed_no_orphans():
    """Full chain: raw_news → ai_decisions → downstream."""
    conn = _fresh_db()
    try:
        nid = _seed_raw_news(conn)
        did = _seed_ai_decision(conn, nid)
        _seed_news_event(conn, nid, did)
        _seed_factor_snapshot(conn, nid)
        _seed_hermes_obs(conn, nid, did)
        _seed_signal_perf(conn, did)
        _seed_quick_sim(conn, did, nid)
        _seed_trade_reflection(conn, did, nid)
        results = ic.run_checks(conn)
        assert ic.is_clean(results), f"expected clean, got {ic.summarize(results)}"
    finally:
        conn.close()


# ────────────────────────────────────────────────────────────────────
# Tests — one per orphan link type (raw_news parents)
# ────────────────────────────────────────────────────────────────────

def test_orphan_ai_decisions_news_id_detected():
    conn = _fresh_db()
    try:
        _seed_raw_news(conn)
        _insert_orphan_ai_decision(conn, missing_news_id=99_999)
        results = ic.run_checks(conn)
        rows = results["ai_decisions.news_id"]
        assert len(rows) == 1
        assert rows[0]["missing_parent"] == 99_999
        assert not ic.is_clean(results)
    finally:
        conn.close()


def test_orphan_news_events_news_id_detected():
    conn = _fresh_db()
    try:
        _insert_orphan_news_event_news_id(conn, missing_news_id=99_999)
        results = ic.run_checks(conn)
        assert len(results["news_events.news_id"]) == 1
    finally:
        conn.close()


def test_orphan_factor_snapshots_news_id_detected():
    conn = _fresh_db()
    try:
        _insert_orphan_factor_snap(conn, missing_news_id=99_999)
        results = ic.run_checks(conn)
        assert len(results["factor_snapshots.news_id"]) == 1
    finally:
        conn.close()


def test_orphan_hermes_observations_news_id_detected():
    conn = _fresh_db()
    try:
        _insert_orphan_hermes_news(conn, missing_news_id=99_999)
        results = ic.run_checks(conn)
        assert len(results["hermes_observations.news_id"]) == 1
    finally:
        conn.close()


def test_orphan_trade_reflections_news_id_detected():
    conn = _fresh_db()
    try:
        _insert_orphan_trade_reflection(conn, missing_decision_id=1,
                                        missing_news_id=99_999)
        results = ic.run_checks(conn)
        assert len(results["trade_reflections.news_id"]) == 1
        assert results["trade_reflections.news_id"][0]["missing_parent"] == 99_999
    finally:
        conn.close()


# ────────────────────────────────────────────────────────────────────
# Tests — one per orphan link type (ai_decisions parents)
# ────────────────────────────────────────────────────────────────────

def test_orphan_news_events_decision_id_detected():
    conn = _fresh_db()
    try:
        _seed_raw_news(conn)
        _insert_orphan_news_event_decision(conn, missing_decision_id=99_999)
        results = ic.run_checks(conn)
        assert len(results["news_events.decision_id"]) == 1
        assert results["news_events.news_id"] == []
    finally:
        conn.close()


def test_orphan_signal_performance_decision_id_detected():
    conn = _fresh_db()
    try:
        _insert_orphan_signal_perf(conn, missing_decision_id=99_999)
        results = ic.run_checks(conn)
        assert len(results["signal_performance.decision_id"]) == 1
    finally:
        conn.close()


def test_orphan_quick_sim_trades_decision_id_detected():
    conn = _fresh_db()
    try:
        _insert_orphan_quick_sim_decision(conn, missing_decision_id=99_999)
        results = ic.run_checks(conn)
        assert len(results["quick_sim_trades.decision_id"]) == 1
    finally:
        conn.close()


def test_orphan_hermes_observations_decision_id_detected():
    conn = _fresh_db()
    try:
        _insert_orphan_hermes_decision(conn, missing_decision_id=99_999)
        results = ic.run_checks(conn)
        assert len(results["hermes_observations.decision_id"]) == 1
    finally:
        conn.close()


def test_orphan_trade_reflections_decision_id_detected():
    conn = _fresh_db()
    try:
        _insert_orphan_trade_reflection(conn, missing_decision_id=99_999,
                                        missing_news_id=None)
        results = ic.run_checks(conn)
        assert len(results["trade_reflections.decision_id"]) == 1
        assert results["trade_reflections.news_id"] == []
    finally:
        conn.close()


def test_orphan_quick_sim_trades_news_id_detected():
    conn = _fresh_db()
    try:
        nid = _seed_raw_news(conn)
        _seed_ai_decision(conn, nid)
        _insert_orphan_quick_sim_news(conn, missing_news_id=99_999)
        results = ic.run_checks(conn)
        assert len(results["quick_sim_trades.news_id"]) == 1
        assert results["quick_sim_trades.decision_id"] == []
    finally:
        conn.close()


# ────────────────────────────────────────────────────────────────────
# Tests — cleanup + CLI + backstop
# ────────────────────────────────────────────────────────────────────

def test_orphan_cleared_after_fix():
    conn = _fresh_db()
    try:
        _seed_raw_news(conn)
        _insert_orphan_ai_decision(conn, missing_news_id=99_999)
        results = ic.run_checks(conn)
        assert len(results["ai_decisions.news_id"]) == 1
        conn.execute("DELETE FROM ai_decisions WHERE news_id = ?", (99_999,))
        conn.commit()
        results = ic.run_checks(conn)
        assert results["ai_decisions.news_id"] == []
        assert ic.is_clean(results)
    finally:
        conn.close()


def test_run_and_print_clean_returns_zero(capsys):
    conn = _fresh_db()
    try:
        rc = ic.run_and_print(conn)
        assert rc == 0
        out = capsys.readouterr().out
        assert "11 links scanned" in out
        assert "0 orphans total" in out
    finally:
        conn.close()


def test_run_and_print_dirty_returns_one(capsys):
    conn = _fresh_db()
    try:
        _insert_orphan_ai_decision(conn, missing_news_id=99_999)
        rc = ic.run_and_print(conn)
        assert rc == 1
        out = capsys.readouterr().out
        assert "1 orphans total" in out
        assert "❌" in out
    finally:
        conn.close()


def test_backstop_works_with_legacy_fk_off_db():
    """Legacy DB: PRAGMA foreign_keys=OFF, no FK rejection at insert.
    Backstop must still detect the orphan."""
    conn = _fresh_db()
    try:
        conn.execute("PRAGMA foreign_keys=OFF;")
        conn.execute(
            """INSERT INTO ai_decisions
               (news_id, sentiment_score, suggested_action, reasoning)
               VALUES (?, ?, ?, ?)""",
            (99_999, 0.5, "HOLD", "legacy orphan"),
        )
        results = ic.run_checks(conn)
        assert len(results["ai_decisions.news_id"]) == 1
    finally:
        conn.close()


def test_summarize_keys_match_check_count():
    conn = _fresh_db()
    try:
        results = ic.run_checks(conn)
        counts = ic.summarize(results)
        assert set(counts.keys()) == {name for name, _, _, _ in ic._CHECKS}
        assert all(isinstance(v, int) for v in counts.values())
    finally:
        conn.close()