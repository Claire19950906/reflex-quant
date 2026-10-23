"""Causal-graph safety net - snapshot / drift-check / rollback (v231+).

The causal graph (causal_graph.py) is auto-updated from
abnormal_attributions cross-asset signals.  A bad update (e.g., a
buggy statistics formula, an LLM hallucination passed through) could
silently rewrite asset_driver_edges weights in a way that breaks
downstream decisions.  Currently there's no roll-back check - bad
updates stick.

This module gives you:

    with causal_guard(conn, label="update_cross_asset_weights",
                      tables=["asset_driver_edges"]):
        update_cross_asset_weights(...)
        # if drift > threshold, the snapshot is restored automatically

Drift is measured two ways:

  1. `row_count_drift`     - fraction of rows whose primary key
                              disappeared or appeared
  2. `value_drift`         - fraction of (key, column) cells whose
                              value changed by > value_eps

The guard restores the snapshot iff:
  row_count_drift > MAX_DIFF_FRAC (default 0.20)  OR
  value_drift     > MAX_DIFF_FRAC                  OR
  any single row had a value swing > HARD_LIMIT (default 0.50)

Decoupled from causal_graph.py: the guard is opt-in.
Pure-stdlib: works with any sqlite3 connection.  No LLM calls.
"""
from __future__ import annotations

import sqlite3
import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Any, Dict, List

MAX_DIFF_FRAC: float = 0.20   # > 20% rows drift -> roll back
HARD_LIMIT: float = 0.50       # any single cell swing > 50% -> roll back
VALUE_EPS: float = 1e-6        # treat values within this as unchanged


@dataclass
class TableSnapshot:
    table: str
    pk_col: str
    rows: Dict[Any, Dict[str, Any]] = field(default_factory=dict)
    columns: List[str] = field(default_factory=list)


@dataclass
class DriftReport:
    table: str
    rows_before: int
    rows_after: int
    n_appeared: int
    n_disappeared: int
    n_value_changed: int
    n_value_total: int
    max_cell_drift: float
    triggered: bool
    reason: str

def _rowid_or_id_columns(conn: sqlite3.Connection, table: str) -> tuple:
    """Return (pk_col, all_columns).  Prefers 'id' column if present, else rowid."""
    info = conn.execute("PRAGMA table_info({})".format(table)).fetchall()
    cols = [r[1] for r in info]
    pk_col = "id" if "id" in cols else "_rowid_"
    return pk_col, cols


def snapshot_table(conn: sqlite3.Connection, table: str) -> TableSnapshot:
    """Read all rows of `table` into a TableSnapshot."""
    pk_col, cols = _rowid_or_id_columns(conn, table)
    sel_pk = "rowid" if pk_col == "_rowid_" else pk_col
    rows_raw = conn.execute("SELECT * FROM {}".format(table)).fetchall()
    rows: Dict[Any, Dict[str, Any]] = {}
    for r in rows_raw:
        d = dict(r)
        if pk_col == "_rowid_":
            pk = d.pop("rowid")
        else:
            pk = d[pk_col]
        rows[pk] = d
    return TableSnapshot(table=table, pk_col=pk_col, rows=rows, columns=cols)


def _cell_drift(old: Any, new: Any, eps: float = VALUE_EPS) -> float:
    """Fractional drift between two cell values, in [0, +inf).

    Numeric -> relative change |new-old| / max(|old|, eps), capped at 1.0
    Type mismatch -> 1.0
    String mismatch -> 1.0
    """
    if old is None and new is None:
        return 0.0
    if old is None or new is None:
        return 1.0
    if type(old) is not type(new):
        return 1.0
    if isinstance(old, (int, float)):
        denom = max(abs(float(old)), eps)
        return min(1.0, abs(float(new) - float(old)) / denom)
    if old == new:
        return 0.0
    return 1.0


def diff_snapshots(before: TableSnapshot, after: TableSnapshot,
                   max_diff_frac: float = MAX_DIFF_FRAC,
                   hard_limit: float = HARD_LIMIT) -> DriftReport:
    """Compare two snapshots of the same table; return a DriftReport."""
    n_b = len(before.rows)
    n_a = len(after.rows)
    keys_b = set(before.rows)
    keys_a = set(after.rows)
    appeared = keys_a - keys_b
    disappeared = keys_b - keys_a

    n_val_total = 0
    n_val_changed = 0
    max_cell = 0.0
    for k in keys_b & keys_a:
        rb = before.rows[k]
        ra = after.rows[k]
        for col in set(rb) | set(ra):
            if col == before.pk_col:
                continue
            vb, va = rb.get(col), ra.get(col)
            n_val_total += 1
            d = _cell_drift(vb, va)
            if d > max_cell:
                max_cell = d
            if d > 0:
                n_val_changed += 1

    row_drift_frac = (len(appeared) + len(disappeared)) / max(1, n_b + n_a) * 2
    val_drift_frac = n_val_changed / max(1, n_val_total)

    triggered = False
    reasons = []
    if row_drift_frac > max_diff_frac:
        triggered = True
        reasons.append(
            "row drift {:.1%} > {:.0%}".format(row_drift_frac, max_diff_frac))
    if val_drift_frac > max_diff_frac:
        triggered = True
        reasons.append(
            "cell drift {:.1%} > {:.0%}".format(val_drift_frac, max_diff_frac))
    if max_cell > hard_limit:
        triggered = True
        reasons.append(
            "single-cell swing {:.1%} > {:.0%}".format(max_cell, hard_limit))

    return DriftReport(
        table=after.table,
        rows_before=n_b,
        rows_after=n_a,
        n_appeared=len(appeared),
        n_disappeared=len(disappeared),
        n_value_changed=n_val_changed,
        n_value_total=n_val_total,
        max_cell_drift=max_cell,
        triggered=triggered,
        reason="; ".join(reasons) if reasons else "ok",
    )


def restore_snapshot(conn: sqlite3.Connection, snap: TableSnapshot) -> int:
    """Delete all rows of `snap.table` and re-insert from snapshot.

    Returns number of rows restored.
    """
    cur = conn.cursor()
    try:
        cur.execute("DELETE FROM {}".format(snap.table))
        if not snap.rows:
            conn.commit()
            return 0
        all_cols = list(snap.columns)
        if snap.pk_col == "_rowid_":
            pass  # rowid auto-handled
        elif snap.pk_col not in all_cols:
            all_cols.insert(0, snap.pk_col)
        placeholders = ", ".join(["?"] * len(all_cols))
        col_list = ", ".join(all_cols)
        sql = "INSERT INTO {} ({}) VALUES ({})".format(
            snap.table, col_list, placeholders)
        n = 0
        for pk, row in snap.rows.items():
            values = [row.get(c) for c in all_cols]
            cur.execute(sql, values)
            n += 1
        conn.commit()
        return n
    except Exception:
        conn.rollback()
        raise


@dataclass
class GuardResult:
    """Summary of a guarded block."""
    label: str
    tables: List[str]
    duration_ms: float = 0.0
    reports: List[DriftReport] = field(default_factory=list)
    rolled_back: bool = False
    restored_rows: Dict[str, int] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "label":         self.label,
            "tables":        self.tables,
            "duration_ms":   round(self.duration_ms, 2),
            "reports":       [r.to_dict() for r in self.reports],
            "rolled_back":   self.rolled_back,
            "restored_rows": self.restored_rows,
        }


@contextmanager
def causal_guard(conn: sqlite3.Connection,
                 label: str,
                 tables: List[str],
                 max_diff_frac: float = MAX_DIFF_FRAC,
                 hard_limit: float = HARD_LIMIT,
                 dry_run: bool = False):
    """Snapshot -> run block -> diff -> optionally roll back.

    Yields:
        GuardResult accumulator (mutated as the block runs)

    Example::

        with causal_guard(conn, "update_x", tables=["t"]) as result:
            do_update(conn)
    """
    t0 = time.time()
    snaps: Dict[str, TableSnapshot] = {
        t: snapshot_table(conn, t) for t in tables
    }
    out = GuardResult(label=label, tables=list(tables), duration_ms=0.0)

    try:
        yield out
    finally:
        for t, snap in snaps.items():
            try:
                after = snapshot_table(conn, t)
                report = diff_snapshots(
                    snap, after,
                    max_diff_frac=max_diff_frac,
                    hard_limit=hard_limit,
                )
                out.reports.append(report)
                if report.triggered and not dry_run:
                    n = restore_snapshot(conn, snap)
                    out.rolled_back = True
                    out.restored_rows[t] = n
            except Exception as e:
                out.reports.append(DriftReport(
                    table=t, rows_before=0, rows_after=0,
                    n_appeared=0, n_disappeared=0,
                    n_value_changed=0, n_value_total=0,
                    max_cell_drift=0.0, triggered=True,
                    reason="diff failed: {}".format(e),
                ))
        out.duration_ms = (time.time() - t0) * 1000.0
