# -*- coding: utf-8 -*-
"""v171+ P6：sigmoid / aggregate_by_asset / archive_old_curves 测试。"""
from __future__ import annotations

import sqlite3
import time


def _fresh_db():
    import db
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    db.migrate(conn)
    return conn


# ── build_sigmoid_curve ──────────────────────────────────────────────────────

def test_build_sigmoid_curve_basic():
    from event_impact_curve import build_sigmoid_curve
    c = build_sigmoid_curve(
        elasticity_minutes=120,
        price_at_event=2400.0,
        delta_pct_at_peak=0.85,
        peak_after_minutes=15,
        asset_code="XAU",
    )
    assert "5m" in c
    assert "30m" in c
    assert "2h" in c
    assert "1d" in c
    # 5m 比 30m 衰减更少（peak=15，所以 5m 距 peak=10, 30m 距 peak=15）
    # sigmoid：t<peak 段陡升，t>peak 段缓慢回归
    assert abs(c["5m"]["delta_pct"]) > 0
    assert c["30m"]["delta_pct"] is not None


def test_build_sigmoid_curve_zero_delta():
    from event_impact_curve import build_sigmoid_curve
    c = build_sigmoid_curve(
        elasticity_minutes=120,
        price_at_event=2400.0,
        delta_pct_at_peak=0.0,
        peak_after_minutes=15,
        asset_code="XAU",
    )
    # 全 0 → 所有 delta_pct 应为 None
    for wl, v in c.items():
        assert v["delta_pct"] is None


def test_build_sigmoid_curve_no_elasticity():
    from event_impact_curve import build_sigmoid_curve
    c = build_sigmoid_curve(
        elasticity_minutes=0,
        price_at_event=2400.0,
        delta_pct_at_peak=0.85,
        peak_after_minutes=15,
    )
    assert c == {}


# ── aggregate_by_asset ───────────────────────────────────────────────────────

def test_aggregate_by_asset_returns_4_windows():
    from event_impact_curve import record_impact_point, aggregate_by_asset
    conn = _fresh_db()
    # 插 1 行 event_atom + 5m/30m/2h/1d 各 1 个点
    conn.execute(
        "INSERT INTO dim_asset(code, name) VALUES('XAU','黄金')"
    )
    conn.commit()
    aid = conn.execute(
        "INSERT INTO event_atom(raw_news_id, event_type_id, asset_id, event_time, "
        "event_time_confidence, event_time_source, created_at) VALUES(1, 1, 1, ?, 0.7, 'test', ?)",
        (int(time.time()), int(time.time())),
    ).lastrowid
    conn.commit()
    for wl, off in [("5m", 300), ("30m", 1800), ("2h", 7200), ("1d", 86400)]:
        record_impact_point(
            event_atom_id=aid,
            asset_code="XAU",
            window_label=wl,
            sample_ts=int(time.time()) - off,
            delta_pct=0.5,
            volume_ratio=1.2,
            connection=conn,
        )
    out = aggregate_by_asset("XAU", days=30, connection=conn)
    assert "5m" in out
    assert out["5m"]["avg_delta_pct"] == 0.5
    assert out["5m"]["sample_count"] >= 1


def test_aggregate_by_asset_no_data():
    from event_impact_curve import aggregate_by_asset
    conn = _fresh_db()
    out = aggregate_by_asset("XAU", days=30, connection=conn)
    assert all(v["sample_count"] == 0 for v in out.values())


# ── archive_old_curves ───────────────────────────────────────────────────────

def test_archive_old_curves_marks_old_rows():
    from event_impact_curve import record_impact_point, archive_old_curves
    conn = _fresh_db()
    conn.execute(
        "INSERT INTO dim_asset(code, name) VALUES('XAU','黄金')"
    )
    conn.commit()
    aid = conn.execute(
        "INSERT INTO event_atom(raw_news_id, event_type_id, asset_id, event_time, "
        "event_time_confidence, event_time_source, created_at) VALUES(1, 1, 1, ?, 0.7, 'test', ?)",
        (int(time.time()), int(time.time())),
    ).lastrowid
    conn.commit()
    # 插 1 老 + 1 新
    record_impact_point(
        event_atom_id=aid,
        asset_code="XAU",
        window_label="5m",
        sample_ts=int(time.time()) - 60 * 86400,  # 60 天前
        delta_pct=0.1,
        connection=conn,
    )
    record_impact_point(
        event_atom_id=aid,
        asset_code="XAU",
        window_label="30m",
        sample_ts=int(time.time()),  # 现在
        delta_pct=0.5,
        connection=conn,
    )
    n = archive_old_curves(days=30, connection=conn)
    assert n >= 1
    # 老行应 is_archived=1，新行 is_archived=0
    rows = conn.execute("SELECT window_label, is_archived FROM event_impact_curve").fetchall()
    by_wl = {r["window_label"]: int(r["is_archived"]) for r in rows}
    assert by_wl["5m"] == 1
    assert by_wl["30m"] == 0


def test_archive_old_curves_idempotent():
    from event_impact_curve import record_impact_point, archive_old_curves
    conn = _fresh_db()
    conn.execute("INSERT INTO dim_asset(code, name) VALUES('XAU','黄金')")
    conn.commit()
    aid = conn.execute(
        "INSERT INTO event_atom(raw_news_id, event_type_id, asset_id, event_time, "
        "event_time_confidence, event_time_source, created_at) VALUES(1, 1, 1, ?, 0.7, 'test', ?)",
        (int(time.time()), int(time.time())),
    ).lastrowid
    conn.commit()
    record_impact_point(
        event_atom_id=aid, asset_code="XAU", window_label="5m",
        sample_ts=int(time.time()) - 60 * 86400, delta_pct=0.1,
        connection=conn,
    )
    n1 = archive_old_curves(days=30, connection=conn)
    n2 = archive_old_curves(days=30, connection=conn)  # 第二次应 0
    assert n1 >= 1
    assert n2 == 0  # 已归档的行不再算