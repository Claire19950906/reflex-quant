"""Tests for causal_graph_guard.py (v231+)."""
from __future__ import annotations

import os
import sqlite3
import sys

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
SRC_PYTHON = os.path.normpath(os.path.join(HERE, "..", "src_python"))
if SRC_PYTHON not in sys.path:
    sys.path.insert(0, SRC_PYTHON)

import causal_graph_guard as cg  # noqa: E402


def _make_conn():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("""
        CREATE TABLE weights (
            id    INTEGER PRIMARY KEY,
            label TEXT NOT NULL,
            weight REAL NOT NULL
        )
    """)
    rows = [
        (1, "A->B", 0.3),
        (2, "B->C", 0.5),
        (3, "C->D", 0.7),
        (4, "D->E", 0.2),
        (5, "E->F", 0.4),
    ]
    conn.executemany("INSERT INTO weights VALUES (?,?,?)", rows)
    conn.commit()
    return conn


def test_snapshot_table_reads_all_rows():
    conn = _make_conn()
    snap = cg.snapshot_table(conn, "weights")
    assert snap.table == "weights"
    assert snap.pk_col == "id"
    assert len(snap.rows) == 5
    assert snap.rows[1]["label"] == "A->B"
    assert snap.rows[1]["weight"] == 0.3


def test_snapshot_table_empty():
    conn = sqlite3.connect(":memory:")
    conn.execute("CREATE TABLE empty_t (id INTEGER PRIMARY KEY, v TEXT)")
    snap = cg.snapshot_table(conn, "empty_t")
    assert snap.rows == {}


def test_diff_snapshots_no_change():
    conn = _make_conn()
    a = cg.snapshot_table(conn, "weights")
    b = cg.snapshot_table(conn, "weights")
    rep = cg.diff_snapshots(a, b)
    assert rep.triggered is False
    assert rep.n_value_changed == 0
    assert rep.max_cell_drift == 0.0


def test_diff_snapshots_below_threshold_no_trigger():
    conn = _make_conn()
    a = cg.snapshot_table(conn, "weights")
    conn.execute("UPDATE weights SET weight = 0.35 WHERE id = 1")
    conn.commit()
    b = cg.snapshot_table(conn, "weights")
    rep = cg.diff_snapshots(a, b, max_diff_frac=0.20)
    assert rep.n_value_changed == 1
    assert rep.triggered is False


def test_diff_snapshots_value_drift_triggers():
    conn = _make_conn()
    a = cg.snapshot_table(conn, "weights")
    for rid in (1, 2, 3):
        conn.execute("UPDATE weights SET weight = ? WHERE id = ?", (0.9, rid))
    conn.commit()
    b = cg.snapshot_table(conn, "weights")
    rep = cg.diff_snapshots(a, b, max_diff_frac=0.20)
    assert rep.triggered is True


def test_diff_snapshots_hard_limit_triggers():
    conn = _make_conn()
    a = cg.snapshot_table(conn, "weights")
    conn.execute("UPDATE weights SET weight = 30.0 WHERE id = 1")
    conn.commit()
    b = cg.snapshot_table(conn, "weights")
    rep = cg.diff_snapshots(a, b, max_diff_frac=0.20, hard_limit=0.50)
    assert rep.triggered is True
    assert "single-cell swing" in rep.reason

def test_cell_drift_basic():
    assert cg._cell_drift(1.0, 1.0) == 0.0
    assert cg._cell_drift(1.0, 1.5) == pytest.approx(0.5)
    assert cg._cell_drift(0.0, 1.0) == 1.0    # capped at 1.0


def test_cell_drift_none_handling():
    assert cg._cell_drift(None, None) == 0.0
    assert cg._cell_drift(None, 1.0) == 1.0
    assert cg._cell_drift(1.0, None) == 1.0


def test_cell_drift_type_mismatch():
    assert cg._cell_drift(1, "1") == 1.0
    assert cg._cell_drift("a", "b") == 1.0
    assert cg._cell_drift("a", "a") == 0.0


def test_restore_snapshot_undoes_update():
    conn = _make_conn()
    snap = cg.snapshot_table(conn, "weights")
    conn.execute("UPDATE weights SET weight = 0.99 WHERE id = 1")
    conn.commit()
    n = cg.restore_snapshot(conn, snap)
    assert n == 5
    cur = conn.execute("SELECT weight FROM weights WHERE id = 1").fetchone()
    assert cur[0] == 0.3


def test_restore_snapshot_empty():
    conn = sqlite3.connect(":memory:")
    conn.execute("CREATE TABLE empty_t (id INTEGER PRIMARY KEY, v TEXT)")
    snap = cg.snapshot_table(conn, "empty_t")
    n = cg.restore_snapshot(conn, snap)
    assert n == 0


def test_causal_guard_benign_no_rollback():
    conn = _make_conn()
    with cg.causal_guard(conn, "benign", tables=["weights"]) as result:
        conn.execute("UPDATE weights SET weight = 0.32 WHERE id = 1")
        conn.commit()
    assert result.rolled_back is False
    assert len(result.reports) == 1
    assert result.reports[0].triggered is False


def test_causal_guard_rolls_back_drift():
    conn = _make_conn()
    with cg.causal_guard(conn, "bad", tables=["weights"]) as result:
        for rid in (1, 2, 3, 4, 5):
            conn.execute("UPDATE weights SET weight = 5.0 WHERE id = ?", (rid,))
        conn.commit()
    assert result.rolled_back is True
    cur = conn.execute("SELECT weight FROM weights WHERE id = 1").fetchone()
    assert cur[0] == 0.3   # rolled back to original
    assert "weights" in result.restored_rows


def test_causal_guard_dry_run_does_not_restore():
    conn = _make_conn()
    with cg.causal_guard(conn, "bad", tables=["weights"], dry_run=True) as result:
        for rid in (1, 2, 3, 4, 5):
            conn.execute("UPDATE weights SET weight = 5.0 WHERE id = ?", (rid,))
        conn.commit()
    assert result.reports[0].triggered is True
    assert result.rolled_back is False
    cur = conn.execute("SELECT weight FROM weights WHERE id = 1").fetchone()
    assert cur[0] == 5.0   # stayed bad (dry run)


def test_causal_guard_handles_failed_diff():
    conn = _make_conn()
    with cg.causal_guard(conn, "drop", tables=["weights"]) as result:
        conn.execute("DROP TABLE weights")
    assert len(result.reports) == 1
    assert result.reports[0].triggered is True
    assert "diff failed" in result.reports[0].reason


def test_diff_snapshots_row_appearance_triggers():
    conn = _make_conn()
    a = cg.snapshot_table(conn, "weights")
    for i in range(10, 20):
        conn.execute("INSERT INTO weights VALUES (?,?,?)", (i, "X->Y", 0.5))
    conn.commit()
    b = cg.snapshot_table(conn, "weights")
    rep = cg.diff_snapshots(a, b, max_diff_frac=0.20)
    assert rep.triggered is True
    assert rep.n_appeared == 10


def test_diff_snapshots_row_disappearance_triggers():
    conn = _make_conn()
    a = cg.snapshot_table(conn, "weights")
    for rid in (3, 4, 5):
        conn.execute("DELETE FROM weights WHERE id = ?", (rid,))
    conn.commit()
    b = cg.snapshot_table(conn, "weights")
    rep = cg.diff_snapshots(a, b, max_diff_frac=0.20)
    assert rep.triggered is True
    assert rep.n_disappeared == 3
