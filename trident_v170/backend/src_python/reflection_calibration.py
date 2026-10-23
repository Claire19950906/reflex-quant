#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Reflection system magic-number offline calibration tool (v231+).

Sweeps a grid for WR_DELTA / CONFIRM_BAND / MIN_SAMPLES / LUCK_THRESHOLD
on trade_reflections_v2 history.  Bootstrap F1 + greedy coordinate ascent.
Writes Markdown report + CSV.  Zero new dependencies.

Usage:
    cd backend/src_python
    python reflection_calibration.py --db ../trident_event_bus.db \\
        --report-out ../docs/CALIBRATION_REPORT.md
"""
from __future__ import annotations

import argparse
import csv
import os
import sqlite3
import statistics
import sys
from collections import defaultdict
from dataclasses import dataclass, asdict
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

DEFAULT_GRIDS: Dict[str, List[float]] = {
    "wr_delta":       [0.05, 0.075, 0.10, 0.125, 0.15, 0.175, 0.20, 0.25, 0.30],
    "confirm_band":   [0.02, 0.03, 0.05, 0.07, 0.10],
    "min_samples":    [3, 5, 7, 10, 15, 20, 30, 50],
    "luck_threshold": [0.1, 0.15, 0.2, 0.25, 0.3, 0.35, 0.4, 0.5],
}


@dataclass
class CalibrationResult:
    param_name: str
    best_value: float
    best_score: float
    n_trials: int
    pareto_table: List[Tuple[float, float]]
    evidence_n: int

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


def _load_reflections(conn: sqlite3.Connection) -> List[Dict[str, Any]]:
    try:
        rows = conn.execute(
            """
            SELECT layer, sub_layer, signal_name,
                   pnl_pct,
                   COALESCE(luck_vs_signal_score, 0.0) AS luck
              FROM trade_reflections_v2
             WHERE layer != '' AND sub_layer != '' AND signal_name != ''
               AND enrich_status IN ('ok', 'partial')
               AND pnl_pct IS NOT NULL
            """
        ).fetchall()
    except sqlite3.OperationalError as e:
        print("[calibration] DB read failed: " + str(e), file=sys.stderr)
        return []

    out: List[Dict[str, Any]] = []
    for r in rows:
        pnl = float(r["pnl_pct"] or 0.0)
        if pnl > 0.005:
            label = "win"
        elif pnl < -0.005:
            label = "loss"
        else:
            label = "neutral"
        out.append({
            "layer":       r["layer"] or "",
            "sub_layer":   r["sub_layer"] or "",
            "signal_name": r["signal_name"] or "",
            "pnl_pct":     pnl,
            "luck":        float(r["luck"] or 0.0),
            "label":       label,
        })
    return out


def _aggregate(rows: List[Dict[str, Any]]) -> Dict[Tuple[str, str, str], Dict[str, float]]:
    """Group by (layer, sub_layer, signal_name) and compute win_rate + avg_luck + n."""
    agg: Dict[Tuple[str, str, str], List[Dict[str, Any]]] = defaultdict(list)
    for r in rows:
        agg[(r["layer"], r["sub_layer"], r["signal_name"])].append(r)
    out: Dict[Tuple[str, str, str], Dict[str, float]] = {}
    for key, items in agg.items():
        n = len(items)
        wins = sum(1 for x in items if x["label"] == "win")
        losses = sum(1 for x in items if x["label"] == "loss")
        wr = wins / n if n > 0 else 0.0
        luck_vals = [x["luck"] for x in items if x["label"] in ("win", "loss")]
        luck = statistics.mean(luck_vals) if luck_vals else 0.0
        out[key] = {"n": n, "win_rate": wr, "avg_luck": luck}
    return out


def _classify_one(agg: Dict[str, float],
                  wr_delta_refine: float,
                  confirm_band: float,
                  min_samples: int,
                  luck_threshold: float) -> str:
    """Mirror of meta_reflection_loop._classify_finding logic."""
    n = agg["n"]
    if n < min_samples:
        return "low_sample_skip"
    wr = agg["win_rate"]
    luck = agg["avg_luck"]
    if luck > luck_threshold and wr > 0.55:
        return "unstable_luck"
    if luck < luck_threshold and wr > 0.55:
        return "stable_skill"
    if wr < 0.5 + confirm_band:
        return "invalidated"
    if wr > 0.5 + wr_delta_refine:
        return "refined"
    return "confirmed"


def _overall_f1(aggs: Dict[Tuple[str, str, str], Dict[str, float]],
                wr_delta_refine: float,
                confirm_band: float,
                min_samples: int,
                luck_threshold: float) -> float:
    """Macro-F1 across good / bad predictions vs ground truth (win-rate)."""
    if not aggs:
        return 0.0
    per_class_tp = defaultdict(int)
    per_class_fp = defaultdict(int)
    per_class_fn = defaultdict(int)
    pred_to_good_bad = {
        "confirmed":       "good",
        "refined":         "good",
        "stable_skill":    "good",
        "invalidated":     "bad",
        "unstable_luck":   "bad",
        "low_sample_skip": "neutral",
    }
    for agg in aggs.values():
        pred = _classify_one(agg, wr_delta_refine, confirm_band, min_samples, luck_threshold)
        true_wr = agg["win_rate"]
        if true_wr > 0.55:
            true_label = "good"
        elif true_wr < 0.45:
            true_label = "bad"
        else:
            true_label = "neutral"
        pred_label = pred_to_good_bad.get(pred, "neutral")
        if pred_label == "neutral":
            continue
        if pred_label == true_label:
            per_class_tp[pred_label] += 1
        else:
            per_class_fp[pred_label] += 1
            per_class_fn[true_label] += 1

    f1s = []
    for cls in ("good", "bad"):
        tp = per_class_tp[cls]
        fp = per_class_fp[cls]
        fn = per_class_fn[cls]
        if tp + fp == 0 or tp + fn == 0:
            f1s.append(0.0)
            continue
        p = tp / (tp + fp)
        r = tp / (tp + fn)
        if (p + r) > 0:
            f1s.append(2 * p * r / (p + r))
        else:
            f1s.append(0.0)
    return statistics.mean(f1s) if f1s else 0.0


def _bootstrap_score(aggs: Dict[Tuple[str, str, str], Dict[str, float]],
                     params: Dict[str, float],
                     n_iter: int = 50) -> float:
    """Mean F1 over n_iter bootstrap samples."""
    if not aggs:
        return 0.0
    keys = list(aggs.keys())
    if len(keys) < 2:
        return 0.0
    scores = []
    n_keys = len(keys)
    for i in range(n_iter):
        sampled_keys = [keys[(i * 31 + j * 17) % n_keys] for j in range(n_keys)]
        sampled = {k: aggs[k] for k in sampled_keys}
        s = _overall_f1(sampled, **params)
        scores.append(s)
    return statistics.mean(scores) if scores else 0.0


def calibrate_param(param_name: str,
                    grid: List[float],
                    aggs: Dict[Tuple[str, str, str], Dict[str, float]],
                    other_params: Dict[str, float],
                    n_bootstrap: int = 50) -> CalibrationResult:
    """Hold others fixed, sweep param_name over grid; pick value with max F1."""
    # _overall_f1 expects canonical names; map grid names onto them.
    name_map = {
        "wr_delta":       "wr_delta_refine",
        "confirm_band":   "confirm_band",
        "min_samples":    "min_samples",
        "luck_threshold": "luck_threshold",
    }
    canon_name = name_map.get(param_name, param_name)
    # Translate other_params keys to canonical names
    canonical_other = {name_map.get(k, k): v for k, v in other_params.items()}

    results: List[Tuple[float, float]] = []
    for v in grid:
        params = dict(canonical_other)
        params[canon_name] = v
        s = _bootstrap_score(aggs, params, n_iter=n_bootstrap)
        results.append((v, s))
    best_v, best_s = max(results, key=lambda x: (x[1], -x[0]))
    return CalibrationResult(
        param_name=param_name,
        best_value=best_v,
        best_score=best_s,
        n_trials=n_bootstrap * len(grid),
        pareto_table=results,
        evidence_n=len(aggs),
    )


def run_calibration(db_path: str,
                    grids: Optional[Dict[str, List[float]]] = None,
                    n_bootstrap: int = 50,
                    report_out: Optional[str] = None,
                    csv_out: Optional[str] = None) -> Dict[str, CalibrationResult]:
    """Main entry point. Returns Dict[param_name, CalibrationResult]."""
    grids = grids or DEFAULT_GRIDS
    if not os.path.exists(db_path):
        print("[calibration] DB not found: " + db_path, file=sys.stderr)
        return {}

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    rows = _load_reflections(conn)
    aggs = _aggregate(rows)
    print("[calibration] loaded " + str(len(rows)) + " reflections, "
          + str(len(aggs)) + " unique groups", file=sys.stderr)

    if 0 < len(aggs) < 5:
        print("[calibration] WARNING: <5 groups, results not statistically meaningful",
              file=sys.stderr)

    base_params: Dict[str, float] = {
        "wr_delta":       0.15,
        "confirm_band":   0.05,
        "min_samples":    5,
        "luck_threshold": 0.30,
    }

    results: Dict[str, CalibrationResult] = {}
    for name, grid in grids.items():
        other_params = {k: v for k, v in base_params.items() if k != name}
        results[name] = calibrate_param(
            name, grid, aggs, other_params, n_bootstrap=n_bootstrap,
        )
        max_s = max(s for _, s in results[name].pareto_table)
        if results[name].best_score >= max_s * 0.95:
            base_params[name] = results[name].best_value

    if csv_out:
        _write_csv(results, csv_out)
    if report_out:
        _write_report(results, report_out, len(aggs))

    conn.close()
    return results


def _write_csv(results: Dict[str, CalibrationResult], path: str) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["param", "value", "score"])
        for name, r in results.items():
            for v, s in r.pareto_table:
                w.writerow([name, v, "{:.4f}".format(s)])
    print("[calibration] CSV written -> " + path)


def _write_report(results: Dict[str, CalibrationResult],
                  path: str,
                  n_groups: int) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    lines: List[str] = []
    lines.append("# Magic Number Calibration Report")
    lines.append("")
    lines.append("_Generated: " + datetime.now().strftime("%Y-%m-%d %H:%M:%S") + "_")
    lines.append("_Evidence: " + str(n_groups) + " unique groups_")
    lines.append("")
    lines.append("## Recommended Thresholds")
    lines.append("")
    lines.append("| param | current | recommended | delta | meaning |")
    lines.append("|---|---|---|---|---|")
    rows_def = [
        ("wr_delta",       "0.15", "WR change delta for invalid/refine"),
        ("confirm_band",   "0.05", "WR stable band"),
        ("min_samples",    "5",    "minimum sample count for meta-loop"),
        ("luck_threshold", "0.30", "luck threshold for luck-dominant verdict"),
    ]
    for name, current, meaning in rows_def:
        r = results.get(name)
        if r is None:
            continue
        if name == "min_samples":
            best_str = "{:d}".format(int(r.best_value))
        else:
            best_str = "{:.3f}".format(r.best_value)
        try:
            c = float(current)
            if name == "min_samples":
                diff = int(r.best_value) - int(c)
                diff_str = "{:+d}".format(diff)
            else:
                diff = r.best_value - c
                diff_str = "{:+.3f}".format(diff)
        except (ValueError, TypeError):
            diff_str = "?"
        lines.append("| " + name + " | " + current + " | " + best_str
                     + " | " + diff_str + " | " + meaning + " |")
    lines.append("")
    lines.append("## Pareto Front")
    lines.append("")
    lines.append("| param | value | F1 |")
    lines.append("| --- | --- | --- |")
    for name, r in results.items():
        for v, s in r.pareto_table:
            mark = " <-best" if abs(v - r.best_value) < 1e-9 else ""
            if name == "min_samples":
                v_str = "{:d}".format(int(v))
            else:
                v_str = "{:.3f}".format(v)
            lines.append("| " + name + " | " + v_str + " | "
                         + "{:.4f}".format(s) + mark + " |")
    lines.append("")
    lines.append("## Notes")
    lines.append("")
    lines.append("- F1 is macro-F1 over (good, bad). Higher is better.")
    lines.append("- Groups < 5: not statistically meaningful.")
    lines.append("- Recommendations are greedy coordinate ascent; not global optimum.")
    lines.append("- Apply by writing the 4 values to .env as META_* (see config.py).")

    with open(path, "w") as f:
        f.write("\n".join(lines))
    print("[calibration] Report written -> " + path)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Magic Number offline calibration (bootstrap + greedy)")
    parser.add_argument("--db", default="../trident_event_bus.db",
                        help="SQLite path")
    parser.add_argument("--report-out", default="../docs/CALIBRATION_REPORT.md",
                        help="Markdown report output path")
    parser.add_argument("--csv-out", default="../docs/CALIBRATION_RESULT.csv",
                        help="CSV output path")
    parser.add_argument("--n-bootstrap", type=int, default=50,
                        help="bootstrap iterations per grid point")
    args = parser.parse_args()

    here = os.path.dirname(os.path.abspath(__file__))
    db_path = os.path.normpath(os.path.join(here, args.db))
    report_out = os.path.normpath(os.path.join(here, args.report_out))
    csv_out = os.path.normpath(os.path.join(here, args.csv_out))

    results = run_calibration(
        db_path=db_path,
        n_bootstrap=args.n_bootstrap,
        report_out=report_out,
        csv_out=csv_out,
    )
    if not results:
        sys.exit(1)
    print("")
    print("[recommended] write into .env:")
    for name, r in results.items():
        v = "{:d}".format(int(r.best_value)) if name == "min_samples" else "{:g}".format(r.best_value)
        print("  META_" + name.upper() + "=" + v
              + "    # F1=" + "{:.4f}".format(r.best_score))


if __name__ == "__main__":
    main()
