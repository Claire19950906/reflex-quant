"""PR-1 — Dictionary tables for event structuring.

Covers schema creation, idempotent upsert, empty/unknown code handling,
direction_bias storage and retrieval, list helpers, direction_for() lookup.
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
import seed_event_dim


def _fresh_db() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    db.migrate(conn)
    return conn


# ────────────────────────────────────────────────────────────────────
# 1. Schema
# ────────────────────────────────────────────────────────────────────

def test_migrate_creates_dim_asset():
    conn = _fresh_db()
    cols = {row[1] for row in conn.execute("PRAGMA table_info(dim_asset)")}
    expected = {"id", "code", "name", "kind", "venue", "active", "created_at"}
    assert expected.issubset(cols), f"dim_asset missing columns: {expected - cols}"
    conn.close()


def test_migrate_creates_dim_event_type():
    conn = _fresh_db()
    cols = {row[1] for row in conn.execute("PRAGMA table_info(dim_event_type)")}
    expected = {"id", "code", "name_zh", "name_en", "category",
               "direction_bias", "active", "created_at"}
    assert expected.issubset(cols)
    conn.close()


def test_migrate_idempotent_dim_tables():
    conn = _fresh_db()
    db.migrate(conn)
    db.migrate(conn)
    cur = conn.execute("SELECT COUNT(*) FROM dim_asset")
    assert int(cur.fetchone()[0]) == 0
    conn.close()


# ────────────────────────────────────────────────────────────────────
# 2. dim_asset CRUD
# ────────────────────────────────────────────────────────────────────

def test_get_or_create_asset_basic():
    conn = _fresh_db()
    aid = dim_tables.get_or_create_asset("btc", name="Bitcoin", kind="crypto", connection=conn)
    assert aid > 0
    row = dim_tables.get_asset_by_code("BTC", connection=conn)
    assert row is not None
    assert row["code"] == "BTC"
    assert row["name"] == "Bitcoin"
    assert row["kind"] == "crypto"
    conn.close()


def test_get_or_create_asset_uppercased():
    conn = _fresh_db()
    a1 = dim_tables.get_or_create_asset("btc", connection=conn)
    a2 = dim_tables.get_or_create_asset("BTC", connection=conn)
    a3 = dim_tables.get_or_create_asset("  btc ", connection=conn)
    assert a1 == a2 == a3
    conn.close()


def test_get_or_create_asset_invalid_kind_falls_back_to_other():
    conn = _fresh_db()
    dim_tables.get_or_create_asset("XYZ", kind="bogus_kind", connection=conn)
    row = dim_tables.get_asset_by_code("XYZ", connection=conn)
    assert row["kind"] == "other"
    conn.close()


def test_get_or_create_asset_empty_returns_neg1():
    conn = _fresh_db()
    assert dim_tables.get_or_create_asset("", connection=conn) == -1
    assert dim_tables.get_or_create_asset(None, connection=conn) == -1
    assert dim_tables.get_or_create_asset("   ", connection=conn) == -1
    conn.close()


def test_list_assets_returns_sorted():
    conn = _fresh_db()
    dim_tables.get_or_create_asset("ETH", kind="crypto", connection=conn)
    dim_tables.get_or_create_asset("BTC", kind="crypto", connection=conn)
    dim_tables.get_or_create_asset("XAU", kind="commodity", connection=conn)
    items = dim_tables.list_assets(connection=conn)
    # ORDER BY kind, code -> 'commodity' < 'crypto', so XAU first
    codes = [a["code"] for a in items]
    assert codes == ["XAU", "BTC", "ETH"]
    conn.close()


def test_get_asset_by_code_none_for_unknown():
    conn = _fresh_db()
    assert dim_tables.get_asset_by_code("NOPE", connection=conn) is None
    assert dim_tables.get_asset_by_code("", connection=conn) is None
    conn.close()


# ────────────────────────────────────────────────────────────────────
# 3. dim_event_type CRUD
# ────────────────────────────────────────────────────────────────────

def test_get_or_create_event_type_basic():
    conn = _fresh_db()
    eid = dim_tables.get_or_create_event_type(
        "rate_hike",
        name_zh="jiaxi",
        name_en="Rate Hike",
        category="macro",
        direction_bias={"XAU": "bearish", "BTC": "bearish"},
        connection=conn,
    )
    assert eid > 0
    row = dim_tables.get_event_type_by_code("rate_hike", connection=conn)
    assert row["name_zh"] == "jiaxi"
    assert row["category"] == "macro"
    assert row["direction_bias"] == {"XAU": "bearish", "BTC": "bearish"}
    conn.close()


def test_get_or_create_event_type_lowercased():
    conn = _fresh_db()
    a = dim_tables.get_or_create_event_type("RATE_HIKE", connection=conn)
    b = dim_tables.get_or_create_event_type("rate_hike", connection=conn)
    c = dim_tables.get_or_create_event_type("  RATE_HIKE ", connection=conn)
    assert a == b == c
    conn.close()


def test_get_or_create_event_type_invalid_category():
    conn = _fresh_db()
    dim_tables.get_or_create_event_type("weird", category="not_a_category", connection=conn)
    row = dim_tables.get_event_type_by_code("weird", connection=conn)
    assert row["category"] == "other"
    conn.close()


def test_get_or_create_event_type_updates_direction_bias_only():
    conn = _fresh_db()
    dim_tables.get_or_create_event_type(
        "rate_hike", name_zh="jiaxi", category="macro",
        direction_bias={"XAU": "bearish"}, connection=conn,
    )
    # Re-call without direction_bias: must NOT wipe existing bias
    dim_tables.get_or_create_event_type("rate_hike", connection=conn)
    row = dim_tables.get_event_type_by_code("rate_hike", connection=conn)
    assert row["direction_bias"] == {"XAU": "bearish"}
    # Re-call WITH new bias: should overwrite
    dim_tables.get_or_create_event_type(
        "rate_hike", direction_bias={"BTC": "bearish"}, connection=conn,
    )
    row = dim_tables.get_event_type_by_code("rate_hike", connection=conn)
    assert row["direction_bias"] == {"BTC": "bearish"}
    conn.close()


def test_direction_for_lookup():
    conn = _fresh_db()
    seed_event_dim.seed_all(connection=conn)
    assert dim_tables.direction_for("rate_hike", "XAU", connection=conn) == "bearish"
    assert dim_tables.direction_for("rate_hike", "BTC", connection=conn) == "bearish"
    assert dim_tables.direction_for("opec_cut", "WTI", connection=conn) == "bullish"
    conn.close()


def test_direction_for_unknown_event_returns_none():
    conn = _fresh_db()
    seed_event_dim.seed_all(connection=conn)
    assert dim_tables.direction_for("nope_event", "BTC", connection=conn) is None
    conn.close()


def test_direction_for_known_event_unknown_asset_returns_none():
    conn = _fresh_db()
    seed_event_dim.seed_all(connection=conn)
    assert dim_tables.direction_for("rate_hike", "AAPL", connection=conn) is None
    conn.close()


def test_direction_for_uppercase_asset():
    conn = _fresh_db()
    seed_event_dim.seed_all(connection=conn)
    assert dim_tables.direction_for("rate_hike", "xau", connection=conn) == "bearish"
    conn.close()


# ────────────────────────────────────────────────────────────────────
# 4. Seed idempotency
# ────────────────────────────────────────────────────────────────────

def test_seed_event_dim_idempotent():
    conn = _fresh_db()
    first = seed_event_dim.seed_all(connection=conn)
    assert first["assets"] >= 10
    assert first["event_types"] >= 13
    second = seed_event_dim.seed_all(connection=conn)
    assert second["assets"] == 0
    assert second["event_types"] == 0
    cur = conn.execute("SELECT COUNT(*) FROM dim_asset")
    assert int(cur.fetchone()[0]) >= 10
    cur = conn.execute("SELECT COUNT(*) FROM dim_event_type")
    assert int(cur.fetchone()[0]) >= 13
    conn.close()


def test_seed_event_types_have_bias_for_known_pairs():
    """Every seeded event_type must have direction_bias parseable to dict."""
    conn = _fresh_db()
    seed_event_dim.seed_all(connection=conn)
    items = dim_tables.list_event_types(connection=conn)
    assert items, "should have seeded types"
    for it in items:
        assert isinstance(it["direction_bias"], dict)
    conn.close()


def test_list_event_types_sorted_by_category():
    conn = _fresh_db()
    seed_event_dim.seed_all(connection=conn)
    items = dim_tables.list_event_types(connection=conn)
    cats = [it["category"] for it in items]
    assert cats == sorted(cats)
    conn.close()