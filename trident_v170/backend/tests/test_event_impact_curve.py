"""v171 事件影响曲线 — 时间窗口结构化单元测试。

覆盖：
- build_minimal_curve 从 elasticity_window 字段估算曲线
- serialize_curve / deserialize_curve roundtrip
- WINDOW_LABELS / WINDOW_MINUTES 完整性
- 完整 DB 流程：record_impact_point → aggregate_impact_curve
"""
from __future__ import annotations

import os
import sqlite3
import sys

SRC = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "src_python"))
if SRC not in sys.path:
    sys.path.insert(0, SRC)

import db
import event_impact_curve as eic


def _fresh_db() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    db.migrate(conn)
    return conn


# ────────────────────────────────────────────────────────────────────────────
# WINDOW_LABELS / WINDOW_MINUTES 完整性
# ────────────────────────────────────────────────────────────────────────────

def test_window_labels_complete():
    assert eic.WINDOW_LABELS == ("5m", "30m", "2h", "1d", "extended")


def test_window_minutes_complete():
    for k in eic.WINDOW_LABELS:
        assert k in eic.WINDOW_MINUTES
    assert eic.WINDOW_MINUTES["5m"] == 5
    assert eic.WINDOW_MINUTES["30m"] == 30
    assert eic.WINDOW_MINUTES["2h"] == 120
    assert eic.WINDOW_MINUTES["1d"] == 1440


# ────────────────────────────────────────────────────────────────────────────
# build_minimal_curve
# ────────────────────────────────────────────────────────────────────────────

def test_build_minimal_curve_returns_4_windows():
    c = eic.build_minimal_curve(
        elasticity_minutes=120,
        price_at_event=2400.0,
        delta_pct_at_peak=0.85,
        peak_after_minutes=15,
        asset_code="XAU",
        sample_now=1000,
    )
    assert set(c.keys()) == {"5m", "30m", "2h", "1d"}
    assert c["5m"]["delta_pct"] == 0.85  # 100% peak
    assert c["30m"]["delta_pct"] < c["5m"]["delta_pct"]  # 衰减
    assert c["2h"]["delta_pct"] < c["30m"]["delta_pct"]
    assert c["1d"]["delta_pct"] < c["2h"]["delta_pct"]


def test_build_minimal_curve_with_zero_elasticity_returns_empty():
    c = eic.build_minimal_curve(
        elasticity_minutes=0,
        price_at_event=2400.0,
        delta_pct_at_peak=0.85,
        peak_after_minutes=15,
    )
    assert c == {}


def test_build_minimal_curve_uses_sample_now():
    c = eic.build_minimal_curve(
        elasticity_minutes=60,
        price_at_event=100.0,
        delta_pct_at_peak=1.0,
        peak_after_minutes=10,
        sample_now=1234567890,
    )
    for v in c.values():
        assert v["sample_ts"] == 1234567890


# ────────────────────────────────────────────────────────────────────────────
# serialize / deserialize
# ────────────────────────────────────────────────────────────────────────────

def test_serialize_deserialize_roundtrip():
    c = eic.build_minimal_curve(
        elasticity_minutes=60,
        price_at_event=2400.0,
        delta_pct_at_peak=1.0,
        peak_after_minutes=10,
        asset_code="XAU",
    )
    blob = eic.serialize_curve(c)
    assert isinstance(blob, str)
    d = eic.deserialize_curve(blob)
    assert set(d.keys()) == {"5m", "30m", "2h", "1d"}
    assert d["5m"]["asset_code"] == "XAU"


def test_deserialize_empty_returns_empty():
    assert eic.deserialize_curve("") == {}
    assert eic.deserialize_curve(None) == {}
    assert eic.deserialize_curve("not-json") == {}


# ────────────────────────────────────────────────────────────────────────────
# DB 流程：record / aggregate
# ────────────────────────────────────────────────────────────────────────────

def test_migrate_creates_event_impact_curve_table():
    conn = _fresh_db()
    cols = db.table_columns(conn, "event_impact_curve")
    expected = {
        "id", "event_atom_id", "asset_id", "window_label", "sample_ts",
        "price", "delta_pct", "volume_ratio", "note", "created_at",
    }
    assert expected.issubset(cols), f"event_impact_curve 缺列: {expected - cols}"
    conn.close()


def test_migrate_adds_event_atom_extra_columns():
    conn = _fresh_db()
    cols = db.table_columns(conn, "event_atom")
    assert "curve_snapshot" in cols
    assert "event_time_utc_epoch" in cols
    assert "event_time_utc_iso" in cols
    assert "event_time_display_tz" in cols
    conn.close()


def test_schema_ready_after_migrate():
    conn = _fresh_db()
    assert db.schema_ready(conn) is True
    conn.close()


def test_record_impact_point_basic():
    conn = _fresh_db()
    cur = conn.execute(
        """INSERT INTO event_atom
           (raw_news_id, event_type_id, asset_id, event_time,
            event_time_confidence, direction, created_at)
           VALUES (?, ?, ?, ?, ?, ?, ?)""",
        (1, 1, 1, 1757923800, 1.0, "bullish", 1757923800),
    )
    atom_id = cur.lastrowid

    new_id = eic.record_impact_point(
        event_atom_id=atom_id,
        asset_code="XAU",
        window_label="5m",
        sample_ts=1757924100,
        delta_pct=0.5,
        volume_ratio=1.2,
        note="unit-test",
        connection=conn,
    )
    assert new_id > 0

    agg = eic.aggregate_impact_curve(event_atom_id=atom_id, connection=conn)
    assert "5m" in agg
    assert agg["5m"]["delta_pct"] == 0.5
    assert agg["5m"]["asset_code"] == "XAU"
    conn.close()


def test_record_impact_point_rejects_bad_window():
    conn = _fresh_db()
    cur = conn.execute(
        """INSERT INTO event_atom
           (raw_news_id, event_type_id, asset_id, event_time,
            event_time_confidence, direction, created_at)
           VALUES (?, ?, ?, ?, ?, ?, ?)""",
        (1, 1, 1, 1757923800, 1.0, "bullish", 1757923800),
    )
    atom_id = cur.lastrowid
    new_id = eic.record_impact_point(
        event_atom_id=atom_id,
        asset_code="XAU",
        window_label="INVALID",
        sample_ts=1757924100,
        connection=conn,
    )
    assert new_id == -1
    conn.close()


def test_record_impact_curve_batch():
    conn = _fresh_db()
    cur = conn.execute(
        """INSERT INTO event_atom
           (raw_news_id, event_type_id, asset_id, event_time,
            event_time_confidence, direction, created_at)
           VALUES (?, ?, ?, ?, ?, ?, ?)""",
        (1, 1, 1, 1757923800, 1.0, "bullish", 1757923800),
    )
    atom_id = cur.lastrowid

    points = [
        {"window_label": "5m",  "sample_ts": 1757924100, "delta_pct": 0.9},
        {"window_label": "30m", "sample_ts": 1757925600, "delta_pct": 0.7},
        {"window_label": "2h",  "sample_ts": 1757931000, "delta_pct": 0.4},
        {"window_label": "1d",  "sample_ts": 1758010200, "delta_pct": 0.2},
    ]
    ids = eic.record_impact_curve(
        event_atom_id=atom_id, asset_code="WTI", points=points, connection=conn
    )
    assert len(ids) == 4
    agg = eic.aggregate_impact_curve(event_atom_id=atom_id, connection=conn)
    assert set(agg.keys()) == {"5m", "30m", "2h", "1d"}
    assert agg["5m"]["delta_pct"] == 0.9
    assert agg["1d"]["delta_pct"] == 0.2
    conn.close()


def test_get_impact_curve_orders_by_window():
    conn = _fresh_db()
    cur = conn.execute(
        """INSERT INTO event_atom
           (raw_news_id, event_type_id, asset_id, event_time,
            event_time_confidence, direction, created_at)
           VALUES (?, ?, ?, ?, ?, ?, ?)""",
        (1, 1, 1, 1757923800, 1.0, "bullish", 1757923800),
    )
    atom_id = cur.lastrowid

    for wl, w in [("1d", 1440), ("5m", 5), ("30m", 30), ("2h", 120)]:
        eic.record_impact_point(
            event_atom_id=atom_id,
            asset_code="XAU",
            window_label=wl,
            sample_ts=1757923800 + w * 60,
            delta_pct=0.5,
            connection=conn,
        )

    rows = eic.get_impact_curve(event_atom_id=atom_id, connection=conn)
    labels = [r["window_label"] for r in rows]
    assert labels == ["5m", "30m", "2h", "1d"]
    conn.close()


def test_record_impact_point_missing_atom_returns_minus_one():
    conn = _fresh_db()
    new_id = eic.record_impact_point(
        event_atom_id=0,
        asset_code="XAU",
        window_label="5m",
        sample_ts=1757924100,
        connection=conn,
    )
    assert new_id == -1
    conn.close()