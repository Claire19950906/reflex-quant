"""Tests for reflection_calibration.py (v231+)."""
from __future__ import annotations

import os
import sys

import pytest

# Add src_python to path
HERE = os.path.dirname(os.path.abspath(__file__))
SRC_PYTHON = os.path.normpath(os.path.join(HERE, "..", "src_python"))
if SRC_PYTHON not in sys.path:
    sys.path.insert(0, SRC_PYTHON)

import reflection_calibration as rc  # noqa: E402


def _make_aggs():
    """A small but classifiable aggs dict."""
    return {
        ("a", "b", "c1"): {"n": 20, "win_rate": 0.6, "avg_luck": 0.1},
        ("a", "b", "c2"): {"n": 15, "win_rate": 0.3, "avg_luck": 0.05},
        ("a", "b", "c3"): {"n": 10, "win_rate": 0.5, "avg_luck": 0.0},
        ("a", "b", "c4"): {"n": 12, "win_rate": 0.7, "avg_luck": 0.4},
    }


def test_classify_one_basic():
    aggs = _make_aggs()
    # c1: wr=0.6 luck=0.1 < threshold 0.3 → stable_skill
    assert rc._classify_one(aggs[("a", "b", "c1")], 0.15, 0.05, 5, 0.3) == "stable_skill"
    # c2: wr=0.3 < 0.5+0.05 → invalidated
    assert rc._classify_one(aggs[("a", "b", "c2")], 0.15, 0.05, 5, 0.3) == "invalidated"
    # c3: wr=0.5, band=0.05 → 0.5 < 0.55 → invalidated (boundary)
    assert rc._classify_one(aggs[("a", "b", "c3")], 0.15, 0.05, 5, 0.3) == "invalidated"
    # c4: wr=0.7 luck=0.4 > 0.3 → unstable_luck
    assert rc._classify_one(aggs[("a", "b", "c4")], 0.15, 0.05, 5, 0.3) == "unstable_luck"


def test_classify_one_min_samples():
    aggs = {"n": 3, "win_rate": 0.6, "avg_luck": 0.1}
    assert rc._classify_one(aggs, 0.15, 0.05, 5, 0.3) == "low_sample_skip"


def test_overall_f1_returns_float_in_range():
    aggs = _make_aggs()
    f1 = rc._overall_f1(aggs, 0.15, 0.05, 5, 0.3)
    assert 0.0 <= f1 <= 1.0


def test_overall_f1_empty_aggs():
    assert rc._overall_f1({}, 0.15, 0.05, 5, 0.3) == 0.0


def test_bootstrap_score_small_iter():
    aggs = _make_aggs()
    s = rc._bootstrap_score(aggs, {"wr_delta_refine": 0.15, "confirm_band": 0.05,
                                    "min_samples": 5, "luck_threshold": 0.3},
                             n_iter=3)
    assert 0.0 <= s <= 1.0


def test_calibrate_param_picks_best():
    aggs = _make_aggs()
    res = rc.calibrate_param(
        "wr_delta", [0.05, 0.15, 0.3], aggs,
        other_params={"confirm_band": 0.05, "min_samples": 5, "luck_threshold": 0.3},
        n_bootstrap=5,
    )
    assert res.best_value in (0.05, 0.15, 0.3)
    assert res.n_trials == 5 * 3
    assert len(res.pareto_table) == 3
    # Best score is the max in the table
    assert res.best_score == max(s for _, s in res.pareto_table)


def test_run_calibration_missing_db():
    res = rc.run_calibration("/tmp/__definitely_does_not_exist__.db")
    assert res == {}


def test_run_calibration_with_minimal_db(tmp_path):
    """End-to-end on a tiny synthesized DB."""
    import sqlite3
    db = str(tmp_path / "tiny.db")
    conn = sqlite3.connect(db)
    conn.execute("""
        CREATE TABLE trade_reflections_v2 (
            layer TEXT, sub_layer TEXT, signal_name TEXT,
            pnl_pct REAL, luck_vs_signal_score REAL, enrich_status TEXT
        )
    """)
    rows = [
        ("macro", "fed", "tightening",  0.02, 0.10, "ok"),
        ("macro", "fed", "tightening",  0.03, 0.15, "ok"),
        ("macro", "fed", "tightening", -0.01, 0.20, "ok"),
        ("micro", "rsi", "oversold",    0.01, 0.05, "ok"),
        ("micro", "rsi", "oversold",   -0.02, 0.10, "ok"),
        ("micro", "rsi", "oversold",    0.00, 0.10, "ok"),
        ("sent", "fear", "spike",       0.04, 0.30, "ok"),
        ("sent", "fear", "spike",       0.05, 0.40, "ok"),
        ("sent", "fear", "spike",      -0.03, 0.20, "ok"),
    ]
    conn.executemany("INSERT INTO trade_reflections_v2 VALUES (?,?,?,?,?,?)", rows)
    conn.commit()
    conn.close()

    res = rc.run_calibration(db, n_bootstrap=5,
                              report_out=str(tmp_path / "report.md"),
                              csv_out=str(tmp_path / "result.csv"))
    assert set(res.keys()) == {"wr_delta", "confirm_band", "min_samples", "luck_threshold"}
    for r in res.values():
        assert r.best_score >= 0.0
        assert r.evidence_n == 3
    # The reports should exist
    assert os.path.exists(tmp_path / "report.md")
    assert os.path.exists(tmp_path / "result.csv")
