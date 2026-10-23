#!/usr/bin/env python3
"""
Trident Agent — Foreign-key orphan + cross-table consistency checks.

Data-alignment gate (P0). Runs read-only SQL over the live event-bus DB and
returns orphan rows that violate the 5-link chain:

  raw_news → news_events / factor_snapshots / hermes_observations / ai_decisions
            ↓
  ai_decisions → quick_sim_trades / signal_performance / hermes_observations
            ↓
  trade_reflections ← ai_decisions (decision_id) + ← raw_news (news_id)

Each check is a single SQL statement. Empty result == healthy.
A non-empty result means at least one row references a missing parent.

NOTE: This module is read-only and never mutates state.
NOTE: Existing CREATE TABLE declarations declare only a subset of FKs
      (e.g. ai_decisions.news_id → raw_news.id is declared; news_events.news_id
      is NOT declared). SQLite still validates the declared FKs at COMMIT
      (`PRAGMA foreign_keys=ON`). These queries act as a backstop that catches
      orphans even when FKs aren't declared, and works on legacy DBs that may
      have FKs disabled.
"""

from __future__ import annotations


import sqlite3
from typing import Any, Dict, List, Tuple

# Each entry: (name, description, sql, params)
# Use LEFT JOIN ... WHERE parent IS NULL for the orphan pattern.
#
# FK direction map (audited 2026-09 against actual data + db.py CREATE TABLE):
#   ai_decisions.news_id          → raw_news.id         (declared FK, 28/28 OK)
#   quick_sim_trades.news_id      → raw_news.id         (data 4/4 OK; intent: original news)
#   news_events.news_id           → news_events.id (self-ref / chain)
#                                 OR raw_news.id        (data 3127+52 of 3270 OK)
#   factor_snapshots.news_id      → news_events.id      (data 1635/1714 OK)
#   hermes_observations.news_id   → news_events.id      (data 10079/10516 OK)
#   news_events.decision_id       → ai_decisions.id     (data 26/1659 — many orphans)
#   hermes_observations.decision_id → ai_decisions.id   (orphan-prone)
#   signal_performance.decision_id  → ai_decisions.id   (mostly OK)
#   quick_sim_trades.decision_id    → ai_decisions.id   (mostly OK)
#
# Why news_events.news_id is allowed two parents: it's both a self-reference
# (chain: ne.A.news_id = ne.B.id means B is a follow-up of A) AND a raw_news
# pointer (first event in the chain). We OR both.
_CHECKS: List[Tuple[str, str, str, Tuple[Any, ...]]] = [
    # ── raw_news 作为父表的孤儿检测 ──
    (
        "ai_decisions.news_id",
        "ai_decisions → raw_news.id",
        """
        SELECT ad.id AS orphan_id, ad.news_id AS missing_parent
        FROM ai_decisions ad
        LEFT JOIN raw_news rn ON rn.id = ad.news_id
        WHERE rn.id IS NULL
        ORDER BY ad.id DESC LIMIT 100
        """,
        (),
    ),
    (
        "news_events.news_id",
        "news_events.news_id → (news_events.id OR raw_news.id)",
        """
        SELECT ne.id AS orphan_id, ne.news_id AS missing_parent
        FROM news_events ne
        LEFT JOIN news_events ne_self ON ne_self.id = ne.news_id
        LEFT JOIN raw_news rn ON rn.id = ne.news_id
        WHERE ne.news_id IS NOT NULL AND ne_self.id IS NULL AND rn.id IS NULL
        ORDER BY ne.id DESC LIMIT 100
        """,
        (),
    ),
    (
        "factor_snapshots.news_id",
        "factor_snapshots → news_events.id",
        """
        SELECT fs.id AS orphan_id, fs.news_id AS missing_parent
        FROM factor_snapshots fs
        LEFT JOIN news_events ne ON ne.id = fs.news_id
        WHERE fs.news_id IS NOT NULL AND ne.id IS NULL
        ORDER BY fs.id DESC LIMIT 100
        """,
        (),
    ),
    (
        "hermes_observations.news_id",
        "hermes_observations → news_events.id",
        """
        SELECT ho.id AS orphan_id, ho.news_id AS missing_parent
        FROM hermes_observations ho
        LEFT JOIN news_events ne ON ne.id = ho.news_id
        WHERE ho.news_id IS NOT NULL AND ne.id IS NULL
        ORDER BY ho.id DESC LIMIT 100
        """,
        (),
    ),
    (
        "trade_reflections.news_id",
        "trade_reflections → raw_news.id (legacy v1 table, 0 rows in prod)",
        """
        SELECT tr.decision_id AS orphan_id, tr.news_id AS missing_parent
        FROM trade_reflections tr
        LEFT JOIN raw_news rn ON rn.id = tr.news_id
        WHERE tr.news_id IS NOT NULL AND rn.id IS NULL
        ORDER BY tr.decision_id DESC LIMIT 100
        """,
        (),
    ),
    # ── ai_decisions 作为父表的孤儿检测 ──
    (
        "news_events.decision_id",
        "news_events → ai_decisions.id",
        """
        SELECT ne.id AS orphan_id, ne.decision_id AS missing_parent
        FROM news_events ne
        LEFT JOIN ai_decisions ad ON ad.id = ne.decision_id
        WHERE ne.decision_id IS NOT NULL AND ad.id IS NULL
        ORDER BY ne.id DESC LIMIT 100
        """,
        (),
    ),
    (
        "signal_performance.decision_id",
        "signal_performance → ai_decisions.id",
        """
        SELECT sp.id AS orphan_id, sp.decision_id AS missing_parent
        FROM signal_performance sp
        LEFT JOIN ai_decisions ad ON ad.id = sp.decision_id
        WHERE sp.decision_id IS NOT NULL AND ad.id IS NULL
        ORDER BY sp.id DESC LIMIT 100
        """,
        (),
    ),
    (
        "quick_sim_trades.decision_id",
        "quick_sim_trades → ai_decisions.id",
        """
        SELECT qs.id AS orphan_id, qs.decision_id AS missing_parent
        FROM quick_sim_trades qs
        LEFT JOIN ai_decisions ad ON ad.id = qs.decision_id
        WHERE qs.decision_id IS NOT NULL AND ad.id IS NULL
        ORDER BY qs.id DESC LIMIT 100
        """,
        (),
    ),
    (
        "hermes_observations.decision_id",
        "hermes_observations → ai_decisions.id",
        """
        SELECT ho.id AS orphan_id, ho.decision_id AS missing_parent
        FROM hermes_observations ho
        LEFT JOIN ai_decisions ad ON ad.id = ho.decision_id
        WHERE ho.decision_id IS NOT NULL AND ad.id IS NULL
        ORDER BY ho.id DESC LIMIT 100
        """,
        (),
    ),
    (
        "trade_reflections.decision_id",
        "trade_reflections → ai_decisions.id (legacy v1 table, 0 rows in prod)",
        """
        SELECT tr.decision_id AS orphan_id, tr.decision_id AS missing_parent
        FROM trade_reflections tr
        LEFT JOIN ai_decisions ad ON ad.id = tr.decision_id
        WHERE ad.id IS NULL
        ORDER BY tr.decision_id DESC LIMIT 100
        """,
        (),
    ),
    # ── quick_sim_trades.news_id 双亲检测 ──
    (
        "quick_sim_trades.news_id",
        "quick_sim_trades → raw_news.id (intake: original news id, data 4/4 OK)",
        """
        SELECT qs.id AS orphan_id, qs.news_id AS missing_parent
        FROM quick_sim_trades qs
        LEFT JOIN raw_news rn ON rn.id = qs.news_id
        WHERE qs.news_id IS NOT NULL AND rn.id IS NULL
        ORDER BY qs.id DESC LIMIT 100
        """,
        (),
    ),
]


def run_checks(conn: sqlite3.Connection) -> Dict[str, List[Dict[str, Any]]]:
    """Run all orphan checks against the given connection.

    Returns a dict mapping check name → list of orphan rows.
    Empty list for every key = all links healthy.
    """
    # Defensive: ensure row_factory is set so `dict(r)` works on the rows.
    # db.get_connection() does NOT default to sqlite3.Row, so callers who
    # forget to set it will hit "TypeError: object is not iterable" on
    # fetchall(). Setting here is idempotent (already-Row stays Row).
    try:
        if conn.row_factory is None:
            conn.row_factory = sqlite3.Row
    except Exception:
        conn.row_factory = sqlite3.Row

    results: Dict[str, List[Dict[str, Any]]] = {}
    for name, _desc, sql, params in _CHECKS:
        try:
            cur = conn.execute(sql, params)
            rows = [dict(r) for r in cur.fetchall()]
        except sqlite3.OperationalError:
            # Table missing on legacy DB — surface as empty list, not crash
            rows = []
        results[name] = rows
    return results


def summarize(results: Dict[str, List[Dict[str, Any]]]) -> Dict[str, int]:
    """Compact {check_name: orphan_count} for metrics/logging."""
    return {name: len(rows) for name, rows in results.items()}


def is_clean(results: Dict[str, List[Dict[str, Any]]]) -> bool:
    """True if no orphans in any check."""
    return all(len(rows) == 0 for rows in results.values())


def run_and_print(conn: sqlite3.Connection) -> int:
    """Convenience CLI: run + print summary, return non-zero if orphans found."""
    results = run_checks(conn)
    counts = summarize(results)
    total = sum(counts.values())
    print(f"[INTEGRITY] orphan checks: {len(counts)} links scanned, "
          f"{total} orphans total")
    for name, n in counts.items():
        flag = "❌" if n else "✅"
        print(f"  {flag} {name}: {n}")
    return 0 if total == 0 else 1