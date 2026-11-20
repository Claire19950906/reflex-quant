#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
guard_calibration.py —— 给 data_quality_guard 的阈值做体检

只做诊断，**不改任何阈值**。理由：阈值是"越过就不像真的"的物理边界，
不该为了少报警就往宽里调；但也不能凭感觉定死，得看真实历史分布落在哪。

两边都要看：
  · 太紧：历史上有超过 1% 的点落在阈值外 → 说明这条线会常年误报
  · 太宽：历史极值离阈值还有很大距离 → 说明这条线基本永远不会触发

用法：
  python3 guard_calibration.py
  python3 guard_calibration.py --json
"""
import argparse
import json
import sqlite3
import sys
from typing import Any, Dict, List

import db
from data_quality_guard import SANITY


def _pct(sorted_vals: List[float], q: float) -> float:
    if not sorted_vals:
        return float("nan")
    idx = int(round((len(sorted_vals) - 1) * q))
    return sorted_vals[max(0, min(idx, len(sorted_vals) - 1))]


def analyse(conn: sqlite3.Connection) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    for ind, (lo, hi, note) in SANITY.items():
        vals = [float(r[0]) for r in conn.execute(
            "SELECT value FROM external_indicators WHERE indicator=? AND value IS NOT NULL",
            (ind,)).fetchall()]
        if not vals:
            out.append({"indicator": ind, "n": 0, "verdict": "无数据"})
            continue
        s = sorted(vals)
        out_of_range = sum(1 for v in s if v < lo or v > hi)
        ratio = out_of_range / len(s)
        p1, p99 = _pct(s, 0.01), _pct(s, 0.99)
        vmin, vmax = s[0], s[-1]

        if ratio > 0.01:
            verdict = "太紧"
            detail = f"{out_of_range} 点（{ratio*100:.1f}%）落在阈值外，会常年误报"
        elif vmax - vmin == 0:
            verdict = "常量"
            detail = "历史上从来没变过，阈值再多也抓不到变化"
        elif (hi - lo) > (vmax - vmin) * 20:
            verdict = "太宽"
            detail = f"历史只在 {vmin:.4g}~{vmax:.4g} 之间，(阈值宽 {(hi-lo)/(vmax-vmin) if vmax!=vmin else float('inf'):.0f} 倍)"
        else:
            verdict = "合适"
            detail = ""

        out.append({"indicator": ind, "n": len(s), "lo": lo, "hi": hi,
                    "min": vmin, "p1": p1, "p50": _pct(s, 0.5), "p99": p99,
                    "max": vmax, "out_of_range": out_of_range,
                    "ratio": round(ratio, 4), "verdict": verdict, "detail": detail})
    return out


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--json", action="store_true")
    args, _ = ap.parse_known_args(argv)
    conn = sqlite3.connect(db.get_db_path(), timeout=30)
    conn.execute("PRAGMA busy_timeout=30000")
    rows = analyse(conn)
    conn.close()

    if args.json:
        print(json.dumps(rows, ensure_ascii=False, indent=2, default=str))
        return 0

    print(f"{'指标':<26} {'n':>6} {'历史范围':>26} {'阈值':>20}  结论")
    for r in rows:
        if r["verdict"] == "无数据":
            print(f"{r['indicator']:<26} {'0':>6} {'—':>26} {'—':>20}  无数据")
            continue
        rng = f"{r['min']:.4g} ~ {r['max']:.4g}"
        thr = f"{r['lo']:.4g} ~ {r['hi']:.4g}"
        tail = f"  {r['verdict']}"
        if r["detail"]:
            tail += f"：{r['detail']}"
        print(f"{r['indicator']:<26} {r['n']:>6} {rng:>26} {thr:>20}{tail}")

    counts = {}
    for r in rows:
        counts[r["verdict"]] = counts.get(r["verdict"], 0) + 1
    print("\n小计：", "，".join(f"{k} {v}" for k, v in counts.items()))
    return 0


if __name__ == "__main__":
    sys.exit(main())
