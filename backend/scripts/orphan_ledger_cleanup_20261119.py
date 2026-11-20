#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""孤儿账本清理（2026-11-19）—— 只清「挂着决策的记账行」，不碰历史事实层。

背景
----
v460/v476 两次重置后，ai_decisions 被清空，但若干「每条决策一行」的账本表没跟着清：
  · audit_gate_log        1237/1287 指向不存在的决策（96%）
  · decision_errors       231/781
  · decision_evidence     205/286
  · fundamental_shadow    205/286
  · signal_weight_history 205/286
这些行无法追溯到任何决策，按 0 号铁律「这条记录本身不该存在 → 删行」处理。

边界（很重要）
--------------
只删 `decision_id` 挂空的行。**不动** news_events / hermes_observations /
quality_rejections / signal_chain_archive 这类"事实层"大表 —— 它们记的是当时
真实发生的新闻事件与观察，删掉等于销毁研究材料；是否可用于绩效由口径决定，
不是由外键决定。它们的孤儿计数会写进报告，供后续单独决策。

用法
----
  python3 scripts/orphan_ledger_cleanup_20261119.py            # 只出清单（dry-run）
  python3 scripts/orphan_ledger_cleanup_20261119.py --apply    # 归档 + 快照 + 删除
"""
from __future__ import annotations

import argparse
import json
import os
import sqlite3
import time
from datetime import datetime

BACKEND = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DB = os.path.join(BACKEND, "trident_event_bus.db")

# 决策记账层：一条决策一行，决策没了行就不该存在
LEDGER_TABLES = (
    "audit_gate_log",
    "decision_errors",
    "decision_evidence",
    "decision_fundamental_snapshot",
    "fundamental_shadow",
    "signal_weight_history",
    "decision_threshold_snapshot",
    "kline_at_decision_cache",
    "shadow_ledger",
    "t2_orders",
    "trade_reflections_v2",
    "reflection_cycle_log",
    "causal_chain_outcome_decisions",
    "shadow_degraded",
    "causal_veto_shadow",
)

# 事实层：只统计不删
FACT_TABLES = (
    "news_events",
    "hermes_observations",
    "quality_rejections",
    "signal_chain_archive",
    "gate_evaluations",
    "factor_snapshots",
    "llm_degrade_probe",
    "trade_decisions",
    "news_trade_reviews",
)


def orphans(conn: sqlite3.Connection, table: str) -> tuple[int, int]:
    cols = {r[1] for r in conn.execute(f"pragma table_info('{table}')")}
    if "decision_id" not in cols:
        return 0, 0
    total = conn.execute(f"select count(*) from '{table}'").fetchone()[0]
    n = conn.execute(
        f"select count(*) from '{table}' x where x.decision_id is not null "
        f"and not exists(select 1 from ai_decisions d where d.id=x.decision_id)"
    ).fetchone()[0]
    return n, total


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true", help="真的删除（默认只出清单）")
    ap.add_argument("--db", default=DB)
    args = ap.parse_args()

    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    out_dir = os.path.join(BACKEND, "backups", f"orphan_ledger_{ts}")
    conn = sqlite3.connect(args.db, timeout=60)
    conn.execute("pragma busy_timeout=60000")
    conn.row_factory = sqlite3.Row

    plan = {}
    for t in LEDGER_TABLES + FACT_TABLES:
        try:
            n, total = orphans(conn, t)
        except sqlite3.Error as exc:
            print(f"  [跳过] {t}: {exc}")
            continue
        plan[t] = (n, total)

    print("表                                    孤儿 / 总数      处置")
    for t, (n, total) in sorted(plan.items(), key=lambda x: -x[1][0]):
        action = "删除" if (t in LEDGER_TABLES and n) else ("仅统计" if n else "-")
        print(f"  {t:36} {n:>7} / {total:<7} {action}")
    total_del = sum(v[0] for k, v in plan.items() if k in LEDGER_TABLES)
    print(f"\n本次可删（决策记账层）: {total_del} 行；事实层仅统计不删。")

    if not args.apply:
        print("\n[dry-run] 未改动。加 --apply 执行归档+快照+删除。")
        conn.close()
        return 0

    os.makedirs(out_dir, exist_ok=True)
    snap = os.path.join(out_dir, "pre_cleanup.db")
    print(f"\n快照 → {snap}")
    conn.execute("vacuum into ?", (snap,))

    manifest = {"ts": ts, "db": args.db, "snapshot": snap, "tables": {}}
    for t in LEDGER_TABLES:
        n, total = plan.get(t, (0, 0))
        if not n:
            continue
        rows = [dict(r) for r in conn.execute(
            f"select * from '{t}' x where x.decision_id is not null "
            f"and not exists(select 1 from ai_decisions d where d.id=x.decision_id)")]
        path = os.path.join(out_dir, f"{t}.json")
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(rows, fh, ensure_ascii=False, default=str)
        conn.execute(
            f"delete from '{t}' where decision_id is not null "
            f"and decision_id not in (select id from ai_decisions)")
        manifest["tables"][t] = {"deleted": len(rows), "before_total": total,
                                 "archive": os.path.basename(path)}
        print(f"  {t:36} 删除 {len(rows):>6} 行（归档 {os.path.basename(path)}）")

    conn.commit()
    conn.execute("analyze")
    conn.commit()

    with open(os.path.join(out_dir, "manifest.json"), "w", encoding="utf-8") as fh:
        json.dump(manifest, fh, ensure_ascii=False, indent=2)
    print(f"\n完成。归档目录: {out_dir}")
    conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
