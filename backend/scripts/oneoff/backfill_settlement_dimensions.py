#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""backfill_settlement_dimensions.py — v310 P0: 给自动结算行回填 A/B/C 维度

背景
----
v310 P0 把 trade_decisions.entry_price 回填到 100%、结算率拉到 95%, 于是
trade_reflections_v2 多了 1653 行自动结算行。但 _insert_settlement_reflection
原先只写 layer='decision' —— 该 layer 不在 LAYER_TO_DIMENSION 里 → dimension /
subcategory 全空 → 验收口径下这些行全算「非法维度」, 反思维度合法率只有 22.5%,
端点↔库口径也差几百条。

本脚本用 reflection_dimension_map.classify_settlement_row (与写入方**同一权威
口径**) 回填这批行的 dimension / subcategory / layer。

安全性: 默认 dry-run; --commit 才写库; 只 UPDATE 维度为空的行, 幂等。
用法:
    python3 backfill_settlement_dimensions.py
    python3 backfill_settlement_dimensions.py --commit
"""
from __future__ import annotations

import argparse
import sqlite3
import sys
import time
from collections import Counter
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src_python"))

from review import reflection_dimension_map as rdm  # noqa: E402

DB_PATH = BACKEND / "trident_event_bus.db"
LAYER_BY_DIM = {"A": "filter", "B": "analysis", "C": "strategy"}


def _hit_for(action: str, outcome_pct: float) -> int:
    if action == "BUY":
        return 1 if outcome_pct > 0 else 0
    if action == "SELL":
        return 1 if outcome_pct < 0 else 0
    return 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--commit", action="store_true")
    args = ap.parse_args()

    conn = sqlite3.connect(str(DB_PATH), timeout=60.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout=30000;")
    t0 = time.time()
    try:
        rows = conn.execute(
            """SELECT r.id AS refl_id, r.decision_id, r.action, r.pnl_pct,
                      r.hit AS refl_hit,
                      d.guard, d.decision_type, d.final_action,
                      d.intended_action, d.actual_outcome_pct
               FROM trade_reflections_v2 r
               LEFT JOIN trade_decisions d ON d.id = r.decision_id
               WHERE r.dimension IS NULL OR TRIM(r.dimension) = ''
               ORDER BY r.id"""
        ).fetchall()

        print("=" * 68)
        print("自动结算行维度回填 — v310 P0")
        print("=" * 68)
        print(f"  待回填行数: {len(rows)}")

        dist: Counter = Counter()
        subs: Counter = Counter()
        updates = []
        for r in rows:
            # 方向用 intended_action (REJECT 行 final_action='REJECTED' 不是方向)
            direction = (r["intended_action"] or r["final_action"] or r["action"] or "").strip().upper()
            outcome = r["actual_outcome_pct"]
            if outcome is None:
                outcome = r["pnl_pct"]
            # v310 P0: hit 一律按 direction+outcome 重算。
            # 旧值不可信 —— backfill_settlement_outcomes.py 用 final_action 算 hit,
            # REJECT 行 action='REJECTED' → hit 恒为 0, 闸门记录全都标成「未命中」。
            hit = _hit_for(direction, float(outcome)) if outcome is not None else None
            dim, sub = rdm.classify_settlement_row(
                guard=r["guard"], decision_type=r["decision_type"], hit=hit,
                intended_action=direction,
                outcome_pct=float(outcome) if outcome is not None else None)
            dist[dim] += 1
            subs[f"{dim}/{sub}"] += 1
            root_cause = f"{r['final_action'] or direction} {float(outcome):+.2f}% [{dim}/{sub}]"
            updates.append((dim, sub, LAYER_BY_DIM.get(dim, "decision"), hit,
                            root_cause, r["refl_id"]))

        no_dec = sum(1 for r in rows if r["guard"] is None and r["decision_type"] is None)
        print(f"  其中 trade_decisions 无对应行 (按兜底口径归 C): {no_dec}")
        print(f"  维度分布: {dict(dist)}")
        print(f"  小类分布: {dict(subs)}")

        if args.commit:
            conn.executemany(
                "UPDATE trade_reflections_v2 SET dimension=?, subcategory=?, layer=?, "
                "hit=?, root_cause=? WHERE id=?",
                updates,
            )
            conn.commit()
            print(f"\n  ✅ 已落库 {len(updates)} 行")
        else:
            print("\n  (dry-run, 未落库 — 加 --commit 生效)")
        print(f"  耗时 {time.time() - t0:.1f}s")
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
