"""PR-3 — event_atom entity table for event structuring.

Covers schema, single insert + UNIQUE, insert_event_time_candidates,
extract_and_persist end-to-end, CN<->EN direction translation, read helpers,
count aggregation, best-effort failure mode.
"""
from __future__ import annotations

import os
import sqlite3
import sys

SRC = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "src_python"))
if SRC not in sys.path:
    sys.path.insert(0, SRC)

import db
import dim_tables
import event_atom
import seed_event_dim


def _fresh_db(seed=True) -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    db.migrate(conn)
    if seed:
        seed_event_dim.seed_all(connection=conn)
    return conn


# ────────────────────────────────────────────────────────────────────
# 1. Schema
# ────────────────────────────────────────────────────────────────────

def test_migrate_creates_event_atom():
    conn = _fresh_db(seed=False)
    cols = {row[1] for row in conn.execute("PRAGMA table_info(event_atom)")}
    expected = {
        "id", "raw_news_id", "event_type_id", "asset_id", "event_time",
        "event_time_confidence", "event_time_source",
        "magnitude_value", "magnitude_unit", "actor",
        "direction", "direction_confidence", "direction_source",
        "evidence_count", "parser_version", "cluster_id",
        "reasoning", "created_at",
    }
    missing = expected - cols
    assert not missing, f"event_atom missing: {missing}"
    conn.close()


def test_migrate_creates_event_time_candidates():
    conn = _fresh_db(seed=False)
    cols = {row[1] for row in conn.execute("PRAGMA table_info(event_time_candidates)")}
    expected = {"id", "raw_news_id", "candidate_ts", "tz", "source",
               "confidence", "reasoning", "is_primary",
               "parser_version", "created_at"}
    missing = expected - cols
    assert not missing, f"event_time_candidates missing: {missing}"
    conn.close()


def test_migrate_idempotent_event_atom():
    conn = _fresh_db(seed=False)
    db.migrate(conn)
    db.migrate(conn)
    cur = conn.execute("SELECT COUNT(*) FROM event_atom")
    assert int(cur.fetchone()[0]) == 0
    cur = conn.execute("SELECT COUNT(*) FROM event_time_candidates")
    assert int(cur.fetchone()[0]) == 0
    conn.close()


# ────────────────────────────────────────────────────────────────────
# 2. Translation helpers
# ────────────────────────────────────────────────────────────────────

def test_event_type_label_to_code_known():
    assert event_atom.event_type_label_to_code("加息") == "rate_hike"
    assert event_atom.event_type_label_to_code("CPI") == "cpi"
    assert event_atom.event_type_label_to_code("OPEC减产") == "opec_cut"


def test_event_type_label_to_code_unknown_returns_none():
    assert event_atom.event_type_label_to_code("not_a_real_event") is None
    assert event_atom.event_type_label_to_code("") is None


def test_direction_cn_to_db_chinese():
    assert event_atom.direction_cn_to_db("利多") == "bullish"
    assert event_atom.direction_cn_to_db("利空") == "bearish"
    assert event_atom.direction_cn_to_db("中性") == "neutral"


def test_direction_cn_to_db_english_passthrough():
    assert event_atom.direction_cn_to_db("bullish") == "bullish"
    assert event_atom.direction_cn_to_db("bearish") == "bearish"
    assert event_atom.direction_cn_to_db("unknown") == "unknown"


def test_direction_cn_to_db_unknown_returns_unknown():
    assert event_atom.direction_cn_to_db("random") == "unknown"
    assert event_atom.direction_cn_to_db("") == "unknown"
    assert event_atom.direction_cn_to_db(None) == "unknown"


# ────────────────────────────────────────────────────────────────────
# 3. Single insert
# ────────────────────────────────────────────────────────────────────

def test_insert_event_atom_basic():
    conn = _fresh_db()
    eid = event_atom.insert_event_atom(
        raw_news_id=1,
        event_type_code="rate_hike",
        event_time=1700000000,
        asset_code="XAU",
        direction="bearish",
        connection=conn,
    )
    assert eid > 0
    row = conn.execute(
        "SELECT raw_news_id, event_type_id, asset_id, direction FROM event_atom WHERE id = ?",
        (eid,),
    ).fetchone()
    assert row["raw_news_id"] == 1
    assert row["direction"] == "bearish"
    conn.close()


def test_insert_event_atom_auto_creates_unknown_dim():
    conn = _fresh_db(seed=False)
    eid = event_atom.insert_event_atom(
        raw_news_id=1,
        event_type_code="brand_new_event",
        event_time=1700000000,
        asset_code="NEW_ASSET",
        connection=conn,
    )
    assert eid > 0
    assert dim_tables.get_event_type_by_code("brand_new_event", connection=conn) is not None
    assert dim_tables.get_asset_by_code("NEW_ASSET", connection=conn) is not None
    conn.close()


def test_insert_event_atom_unique_conflict_returns_existing():
    conn = _fresh_db()
    eid1 = event_atom.insert_event_atom(
        raw_news_id=1, event_type_code="rate_hike", event_time=1700000000,
        connection=conn,
    )
    eid2 = event_atom.insert_event_atom(
        raw_news_id=1, event_type_code="rate_hike", event_time=1700000000,
        connection=conn,
    )
    assert eid1 == eid2
    cur = conn.execute("SELECT COUNT(*) FROM event_atom")
    assert int(cur.fetchone()[0]) == 1
    conn.close()


def test_insert_event_atom_missing_required_returns_neg1():
    conn = _fresh_db()
    assert event_atom.insert_event_atom(
        raw_news_id=0, event_type_code="rate_hike", event_time=1700000000, connection=conn,
    ) == -1
    assert event_atom.insert_event_atom(
        raw_news_id=1, event_type_code="", event_time=1700000000, connection=conn,
    ) == -1
    assert event_atom.insert_event_atom(
        raw_news_id=1, event_type_code="rate_hike", event_time=0, connection=conn,
    ) == -1
    conn.close()


def test_insert_event_atom_direction_translation():
    conn = _fresh_db()
    eid = event_atom.insert_event_atom(
        raw_news_id=1, event_type_code="rate_hike", event_time=1700000000,
        direction="利空",  # Chinese -> bearish
        connection=conn,
    )
    row = conn.execute("SELECT direction FROM event_atom WHERE id=?", (eid,)).fetchone()
    assert row["direction"] == "bearish"
    conn.close()


def test_insert_event_atom_reasoning_truncated_to_120():
    conn = _fresh_db()
    long_text = "x" * 500
    eid = event_atom.insert_event_atom(
        raw_news_id=1, event_type_code="rate_hike", event_time=1700000000,
        reasoning=long_text, connection=conn,
    )
    row = conn.execute("SELECT reasoning FROM event_atom WHERE id=?", (eid,)).fetchone()
    assert len(row["reasoning"]) == 120
    conn.close()


# ────────────────────────────────────────────────────────────────────
# 4. event_time_candidates insert
# ────────────────────────────────────────────────────────────────────

def test_insert_event_time_candidate_basic():
    conn = _fresh_db()
    cid = event_atom.insert_event_time_candidate(
        raw_news_id=1, candidate_ts=1700000000,
        source="platform_published_at", confidence=0.8, is_primary=True,
        connection=conn,
    )
    assert cid > 0
    row = conn.execute(
        "SELECT candidate_ts, source, confidence, is_primary FROM event_time_candidates WHERE id=?",
        (cid,),
    ).fetchone()
    assert row["candidate_ts"] == 1700000000
    assert row["source"] == "platform_published_at"
    assert row["is_primary"] == 1
    conn.close()


def test_insert_event_time_candidate_missing_required_returns_neg1():
    conn = _fresh_db()
    assert event_atom.insert_event_time_candidate(
        raw_news_id=0, candidate_ts=1700000000, connection=conn,
    ) == -1
    assert event_atom.insert_event_time_candidate(
        raw_news_id=1, candidate_ts=0, connection=conn,
    ) == -1
    conn.close()


# ────────────────────────────────────────────────────────────────────
# 5. extract_and_persist end-to-end
# ────────────────────────────────────────────────────────────────────

def test_extract_and_persist_basic_known_events():
    conn = _fresh_db()
    text = "美联储宣布加息25个基点,黄金应声下跌,比特币承压。"
    ids = event_atom.extract_and_persist(
        text, raw_news_id=1, default_event_time=1700000000, connection=conn,
    )
    assert len(ids) >= 1
    assert all(i > 0 for i in ids)
    rows = conn.execute("SELECT COUNT(*) FROM event_time_candidates").fetchone()
    assert int(rows[0]) == 1
    conn.close()


def test_extract_and_persist_multiple_events_one_news():
    conn = _fresh_db()
    text = "美联储加息25个基点,OPEC减产原油飙升,地缘冲突推升金价。"
    ids = event_atom.extract_and_persist(
        text, raw_news_id=2, default_event_time=1700000000, connection=conn,
    )
    assert len(ids) >= 2
    rows = event_atom.get_event_atoms_by_news(2, connection=conn)
    codes = [r["event_type_code"] for r in rows]
    assert "rate_hike" in codes
    assert "opec_cut" in codes
    for r in rows:
        if r["event_type_code"] == "rate_hike":
            assert r["direction"] == "bearish"
        elif r["event_type_code"] == "opec_cut":
            assert r["direction"] == "bullish"
    conn.close()


def test_extract_and_persist_empty_text_returns_empty():
    conn = _fresh_db()
    assert event_atom.extract_and_persist(
        "", raw_news_id=1, default_event_time=1700000000, connection=conn,
    ) == []
    conn.close()


def test_extract_and_persist_no_matchable_events():
    conn = _fresh_db()
    text = "今天天气晴朗,公园里有人散步。"
    ids = event_atom.extract_and_persist(
        text, raw_news_id=1, default_event_time=1700000000, connection=conn,
    )
    assert isinstance(ids, list)
    # event_time_candidates still records primary time
    rows = conn.execute("SELECT COUNT(*) FROM event_time_candidates").fetchone()
    assert int(rows[0]) == 1
    conn.close()


def test_extract_and_persist_idempotent_same_news():
    """Re-running for same raw_news_id should not create duplicate atoms."""
    conn = _fresh_db()
    text = "美联储加息25个基点。"
    ids1 = event_atom.extract_and_persist(
        text, raw_news_id=1, default_event_time=1700000000, connection=conn,
    )
    ids2 = event_atom.extract_and_persist(
        text, raw_news_id=1, default_event_time=1700000000, connection=conn,
    )
    assert len(ids1) == len(ids2)
    cur = conn.execute("SELECT COUNT(*) FROM event_atom WHERE raw_news_id=1")
    assert int(cur.fetchone()[0]) == len(ids1)
    conn.close()


# ────────────────────────────────────────────────────────────────────
# 6. Read helpers
# ────────────────────────────────────────────────────────────────────

def test_get_event_atoms_by_news_returns_joined():
    conn = _fresh_db()
    event_atom.extract_and_persist(
        "美联储加息25个基点,黄金下跌。",
        raw_news_id=1, default_event_time=1700000000, connection=conn,
    )
    rows = event_atom.get_event_atoms_by_news(1, connection=conn)
    assert rows
    r = rows[0]
    assert r["event_type_code"] in {"rate_hike"}
    assert r["event_type_zh"]
    assert r["event_category"] == "macro"
    assert r["asset_code"] in {"XAU", "BTC", "ETH"}
    assert r["parser_version"] == "event_struct.v1"
    conn.close()


def test_get_event_atoms_by_news_empty():
    conn = _fresh_db()
    rows = event_atom.get_event_atoms_by_news(999, connection=conn)
    assert rows == []
    conn.close()


def test_get_event_time_candidates_orders_primary_first():
    conn = _fresh_db()
    event_atom.insert_event_time_candidate(
        raw_news_id=1, candidate_ts=1700000001,
        confidence=0.5, is_primary=False, connection=conn,
    )
    event_atom.insert_event_time_candidate(
        raw_news_id=1, candidate_ts=1700000002,
        confidence=0.9, is_primary=True, connection=conn,
    )
    rows = event_atom.get_event_time_candidates(1, connection=conn)
    assert len(rows) == 2
    assert rows[0]["is_primary"] == 1
    conn.close()


def test_count_events_by_type_aggregates():
    conn = _fresh_db()
    event_atom.extract_and_persist(
        "美联储加息,OPEC减产,地缘冲突。",
        raw_news_id=1, default_event_time=1700000000, connection=conn,
    )
    counts = event_atom.count_events_by_type(connection=conn)
    assert isinstance(counts, dict)
    assert counts.get("rate_hike", 0) >= 1
    assert counts.get("opec_cut", 0) >= 1
    conn.close()


def test_count_events_by_type_filters():
    conn = _fresh_db()
    event_atom.extract_and_persist(
        "美联储加息25个基点。",
        raw_news_id=1, default_event_time=1700000000, connection=conn,
    )
    only_rate = event_atom.count_events_by_type(
        event_type_code="rate_hike", connection=conn,
    )
    assert "rate_hike" in only_rate
    assert "opec_cut" not in only_rate
    conn.close()


# ────────────────────────────────────────────────────────────────────
# 7. Failure mode (best-effort)
# ────────────────────────────────────────────────────────────────────

def test_extract_and_persist_handles_garbage_text():
    conn = _fresh_db()
    ids = event_atom.extract_and_persist(
        "\x00\x01\x02\ufeff",
        raw_news_id=1, default_event_time=1700000000, connection=conn,
    )
    assert isinstance(ids, list)
    conn.close()


def test_insert_event_atom_handles_invalid_input_gracefully():
    conn = _fresh_db()
    assert event_atom.insert_event_atom(
        raw_news_id=1, event_type_code="", event_time=1700000000, connection=conn,
    ) == -1
    conn.close()