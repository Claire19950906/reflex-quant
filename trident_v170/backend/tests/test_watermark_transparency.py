"""Tests for v231+ watermark transparency (reflection_cycles.source_kind)."""
from __future__ import annotations

import os
import sqlite3
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
SRC_PYTHON = os.path.normpath(os.path.join(HERE, "..", "src_python"))
if SRC_PYTHON not in sys.path:
    sys.path.insert(0, SRC_PYTHON)

import pytest  # noqa: E402

import meta_reflection_loop as mrl  # noqa: E402


# ─── 1. _resolve_source 分支 ─────────────────────────────────────

def test_resolve_source_default_no_ctx_no_demo():
    import demo_mode
    saved = demo_mode.IS_DEMO
    demo_mode.IS_DEMO = False
    try:
        kind, rid, wm = mrl._resolve_source(None)
        assert kind == "live"
        assert rid == ""
        assert wm == ""
    finally:
        demo_mode.IS_DEMO = saved


def test_resolve_source_default_demo():
    import demo_mode
    saved = demo_mode.IS_DEMO
    demo_mode.IS_DEMO = True
    try:
        kind, rid, wm = mrl._resolve_source(None)
        assert kind == "demo"
        assert rid == ""
        assert wm == ""
    finally:
        demo_mode.IS_DEMO = saved


def test_resolve_source_explicit_live():
    kind, rid, wm = mrl._resolve_source({"source_kind": "live"})
    assert kind == "live"
    assert rid == ""
    assert wm == ""


def test_resolve_source_explicit_demo_with_watermark():
    ctx = {"source_kind": "demo", "source_run_id": "run_abc123",
           "source_watermark": "Wm1-abcDEF"}
    kind, rid, wm = mrl._resolve_source(ctx)
    assert kind == "demo"
    assert rid == "run_abc123"
    assert wm == "Wm1-abcDEF"


def test_resolve_source_explicit_stress_test():
    kind, rid, wm = mrl._resolve_source({
        "source_kind": "stress_test", "source_run_id": "st_001"})
    assert kind == "stress_test"
    assert rid == "st_001"


def test_resolve_source_explicit_replay():
    kind, rid, wm = mrl._resolve_source({
        "source_kind": "replay", "source_run_id": "rp_incident_42"})
    assert kind == "replay"
    assert rid == "rp_incident_42"


def test_resolve_source_invalid_kind_falls_back_to_live():
    """白名单外的 source_kind → 'live'（防注入）"""
    kind, _, _ = mrl._resolve_source({"source_kind": "DROP TABLE x;--"})
    assert kind == "live"


def test_resolve_source_empty_string_falls_back_to_live():
    kind, _, _ = mrl._resolve_source({"source_kind": ""})
    assert kind == "live"


def test_resolve_source_truncates_long_run_id():
    long_id = "x" * 100
    _, rid, _ = mrl._resolve_source({"source_kind": "demo", "source_run_id": long_id})
    assert len(rid) == 64
    assert rid == "x" * 64


def test_resolve_source_truncates_long_watermark():
    long_wm = "Z" * 200
    _, _, wm = mrl._resolve_source({"source_kind": "demo", "source_watermark": long_wm})
    assert len(wm) == 64


# ─── 2. _write_cycle 写库 ────────────────────────────────────────

def _make_conn():
    conn = sqlite3.connect(":memory:")
    conn.execute("""
        CREATE TABLE reflection_cycles (
            id                   INTEGER PRIMARY KEY AUTOINCREMENT,
            parent_reflection_id INTEGER NOT NULL,
            parent_layer         TEXT NOT NULL DEFAULT '',
            parent_signal        TEXT NOT NULL DEFAULT '',
            cycle_level          INTEGER NOT NULL DEFAULT 1,
            finding_kind         TEXT NOT NULL DEFAULT '',
            finding_detail       TEXT NOT NULL DEFAULT '',
            evidence_json        TEXT NOT NULL DEFAULT '{}',
            lookback_days        INTEGER NOT NULL DEFAULT 7,
            source_kind          TEXT NOT NULL DEFAULT 'live',
            source_run_id        TEXT NOT NULL DEFAULT '',
            source_watermark     TEXT NOT NULL DEFAULT '',
            created_at           TEXT NOT NULL DEFAULT (datetime('now'))
        )
    """)
    return conn


def test_write_cycle_default_live():
    conn = _make_conn()
    cid = mrl._write_cycle(
        conn, parent_reflection_id=1, parent_layer="L1/S1",
        parent_signal="sig_a", cycle_level=1,
        finding_kind="refined", finding_detail="ok",
        evidence={"x": 1}, lookback_days=7)
    assert cid and cid > 0
    row = conn.execute(
        "SELECT source_kind, source_run_id, source_watermark "
        "FROM reflection_cycles WHERE id=?", (cid,)).fetchone()
    assert row == ("live", "", "")


def test_write_cycle_demo_with_watermark():
    conn = _make_conn()
    cid = mrl._write_cycle(
        conn, parent_reflection_id=0, parent_layer="L1",
        parent_signal="sig", cycle_level=1,
        finding_kind="confirmed", finding_detail="demo trial",
        evidence={"n": 5}, lookback_days=7,
        source_kind="demo", source_run_id="r1",
        source_watermark="Wm_xyz")
    assert cid
    row = conn.execute(
        "SELECT source_kind, source_run_id, source_watermark "
        "FROM reflection_cycles WHERE id=?", (cid,)).fetchone()
    assert row == ("demo", "r1", "Wm_xyz")


def test_write_cycle_stress_test():
    conn = _make_conn()
    cid = mrl._write_cycle(
        conn, parent_reflection_id=0, parent_layer="L1", parent_signal="s",
        cycle_level=1, finding_kind="refined", finding_detail="x",
        evidence={}, lookback_days=7,
        source_kind="stress_test", source_run_id="st_xyz")
    row = conn.execute(
        "SELECT source_kind FROM reflection_cycles WHERE id=?", (cid,)).fetchone()
    assert row[0] == "stress_test"


def test_write_cycle_invalid_source_kind_normalized_to_live():
    conn = _make_conn()
    cid = mrl._write_cycle(
        conn, parent_reflection_id=0, parent_layer="L1", parent_signal="s",
        cycle_level=1, finding_kind="refined", finding_detail="x",
        evidence={}, lookback_days=7,
        source_kind="malicious_input")
    row = conn.execute(
        "SELECT source_kind FROM reflection_cycles WHERE id=?", (cid,)).fetchone()
    assert row[0] == "live"


def test_write_cycle_truncates_long_fields():
    conn = _make_conn()
    cid = mrl._write_cycle(
        conn, parent_reflection_id=0, parent_layer="L1", parent_signal="s",
        cycle_level=1, finding_kind="refined", finding_detail="x",
        evidence={}, lookback_days=7,
        source_kind="demo",
        source_run_id="r" * 200, source_watermark="w" * 200)
    row = conn.execute(
        "SELECT source_run_id, source_watermark "
        "FROM reflection_cycles WHERE id=?", (cid,)).fetchone()
    assert len(row[0]) == 64
    assert len(row[1]) == 64


# ─── 3. db.py _ensure_columns 幂等补齐 ──────────────────────────

def test_db_ensure_columns_adds_source_columns():
    """模拟老库（没 source_* 列），_ensure_columns 应补齐且幂等。"""
    import db
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    try:
        conn = sqlite3.connect(path)
        conn.execute("""
            CREATE TABLE reflection_cycles (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                parent_reflection_id INTEGER NOT NULL,
                finding_kind TEXT NOT NULL DEFAULT '',
                finding_detail TEXT NOT NULL DEFAULT '',
                evidence_json TEXT NOT NULL DEFAULT '{}',
                lookback_days INTEGER NOT NULL DEFAULT 7,
                created_at TEXT NOT NULL DEFAULT (datetime('now'))
            )
        """)
        conn.commit()
        db._ensure_columns(conn, "reflection_cycles", [
            ("source_kind",      "TEXT NOT NULL DEFAULT 'live'"),
            ("source_run_id",    "TEXT NOT NULL DEFAULT ''"),
            ("source_watermark", "TEXT NOT NULL DEFAULT ''"),
        ])
        conn.commit()
        cols = {r[1] for r in conn.execute("PRAGMA table_info(reflection_cycles)")}
        assert "source_kind" in cols
        assert "source_run_id" in cols
        assert "source_watermark" in cols
        conn.execute("INSERT INTO reflection_cycles "
                     "(parent_reflection_id, finding_kind) VALUES (?, ?)",
                     (0, "old"))
        conn.commit()
        row = conn.execute(
            "SELECT source_kind, source_run_id, source_watermark "
            "FROM reflection_cycles WHERE finding_kind='old'").fetchone()
        assert row == ("live", "", "")
        db._ensure_columns(conn, "reflection_cycles", [
            ("source_kind", "TEXT NOT NULL DEFAULT 'live'")])
        conn.close()
    finally:
        os.unlink(path)


# ─── 4. E2E: run_meta_loop 写 source_kind ──────────────────────

def _seed_db(db_path: str):
    conn = sqlite3.connect(db_path)
    conn.executescript("""
        CREATE TABLE ai_decisions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            target_asset TEXT, suggested_action TEXT,
            is_correct INTEGER, forward_pnl REAL,
            evidence_confidence REAL, reasoning TEXT,
            created_at TEXT NOT NULL DEFAULT (datetime('now'))
        );
        CREATE TABLE decision_errors (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            decision_id INTEGER NOT NULL,
            error_code TEXT NOT NULL
        );
        CREATE TABLE trade_reflections_v2 (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            layer TEXT DEFAULT '', sub_layer TEXT DEFAULT '',
            signal_name TEXT DEFAULT '',
            enrich_status TEXT DEFAULT 'ok',
            pnl_pct REAL DEFAULT 0.0,
            luck_vs_signal_score REAL DEFAULT 0.0,
            created_at TEXT NOT NULL DEFAULT (datetime('now'))
        );
        CREATE TABLE reflection_cycles (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            parent_reflection_id INTEGER NOT NULL,
            parent_layer TEXT NOT NULL DEFAULT '',
            parent_signal TEXT NOT NULL DEFAULT '',
            cycle_level INTEGER NOT NULL DEFAULT 1,
            finding_kind TEXT NOT NULL DEFAULT '',
            finding_detail TEXT NOT NULL DEFAULT '',
            evidence_json TEXT NOT NULL DEFAULT '{}',
            lookback_days INTEGER NOT NULL DEFAULT 7,
            source_kind TEXT NOT NULL DEFAULT 'live',
            source_run_id TEXT NOT NULL DEFAULT '',
            source_watermark TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL DEFAULT (datetime('now'))
        );
    """)
    # 插 ≥ MIN_SAMPLES_FOR_META 行 (默认 10) trade_reflections_v2,
    # pnl_pct > 0.005 让 win_rate 偏高
    for i in range(20):
        conn.execute(
            "INSERT INTO trade_reflections_v2 "
            "(layer, sub_layer, signal_name, enrich_status, pnl_pct, luck_vs_signal_score) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            ("L1", "S1", "sig_a", "ok", 0.02, 0.1))
    conn.commit()
    conn.close()


def test_run_meta_loop_writes_source_kind(monkeypatch, tmp_path):
    import db
    db_path = str(tmp_path / "trident_test.db")

    monkeypatch.setattr(db, "get_connection",
                        lambda: sqlite3.connect(db_path))
    _seed_db(db_path)
    out = mrl.run_meta_loop(
        window_days=1, compare_days=2, max_level=1,
        source_ctx={"source_kind": "demo",
                    "source_watermark": "Wm_integration_test"})
    assert out.get("written_cycles", 0) >= 1
    conn = sqlite3.connect(db_path)
    rows = conn.execute(
        "SELECT source_kind, source_watermark FROM reflection_cycles").fetchall()
    conn.close()
    assert rows, "expected at least one row"
    for sk, wm in rows:
        assert sk == "demo"
        assert wm == "Wm_integration_test"


def test_run_meta_loop_defaults_to_live(monkeypatch, tmp_path):
    import db, demo_mode
    saved = demo_mode.IS_DEMO
    demo_mode.IS_DEMO = False
    db_path = str(tmp_path / "trident_test2.db")
    monkeypatch.setattr(db, "get_connection",
                        lambda: sqlite3.connect(db_path))
    _seed_db(db_path)
    try:
        out = mrl.run_meta_loop(window_days=1, compare_days=2, max_level=1)
        if out.get("written_cycles", 0) >= 1:
            conn = sqlite3.connect(db_path)
            rows = conn.execute(
                "SELECT DISTINCT source_kind FROM reflection_cycles").fetchall()
            conn.close()
            kinds = {r[0] for r in rows}
            assert kinds == {"live"}, f"expected {{'live'}} got {kinds}"
    finally:
        demo_mode.IS_DEMO = saved


