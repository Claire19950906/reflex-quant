#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""verify_outcome_pct.py — 结算收益口径自检(只读)。

用法: python3 src_python/verify_outcome_pct.py
"""
from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

DB = Path(__file__).resolve().parents[2] / "trident_event_bus.db"


def main() -> int:
    conn = sqlite3.connect(str(DB), timeout=30)
    conn.row_factory = sqlite3.Row
    q = ("SELECT COUNT(*) n, "
         "SUM(CASE WHEN ABS(actual_outcome_pct) > 90 THEN 1 ELSE 0 END) extreme, "
         "AVG(CASE WHEN actual_outcome_pct IS NOT NULL AND actual_outcome_pct != 0 "
         "         THEN actual_outcome_pct END) avg_nz, "
         "MAX(ABS(actual_outcome_pct)) mx, AVG(pnl_pct) avg_pnl "
         "FROM trade_reflections_v2 WHERE ")
    print("trade_reflections_v2")
    for tag, where in (("全部", "1=1"),
                       ("结算行", "paper_run_id IS NULL"),
                       ("worker行", "paper_run_id IS NOT NULL")):
        r = conn.execute(q + where).fetchone()
        print(f"  {tag:<8} n={r['n']:<6} |幅度|>90%|={r['extreme']:<5} "
              f"actual均值(非零)={r['avg_nz']} 最大|actual|={r['mx']} pnl均值={r['avg_pnl']}")
    r = conn.execute(
        "SELECT COUNT(*) n, AVG(actual_outcome_pct) avg, "
        "MIN(actual_outcome_pct) mn, MAX(actual_outcome_pct) mx, "
        "SUM(CASE WHEN ABS(actual_outcome_pct) > 90 THEN 1 ELSE 0 END) extreme "
        "FROM trade_decisions WHERE status='settled' AND actual_outcome_pct IS NOT NULL"
    ).fetchone()
    print(f"trade_decisions(settled) n={r['n']} 均值={r['avg']} "
          f"区间=[{r['mn']}, {r['mx']}] |幅度|>90%|={r['extreme']}")
    # 结算行两列一致性
    r = conn.execute(
        "SELECT COUNT(*) FROM trade_reflections_v2 WHERE paper_run_id IS NULL "
        "AND pnl_pct IS NOT NULL AND actual_outcome_pct IS NOT NULL "
        "AND ABS(actual_outcome_pct - pnl_pct) > 0.001"
    ).fetchone()[0]
    print(f"结算行 actual_outcome_pct != pnl_pct 的行数: {r}")
    conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
