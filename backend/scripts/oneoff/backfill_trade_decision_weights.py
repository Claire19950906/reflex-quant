#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""backfill_trade_decision_weights.py — 给**成交决策**补算信号权重。

为什么需要
----------
权重历史 (signal_weight_history) 一直只记 AI 提案 (ai_decisions.id) 的权重,
而真实盈亏 (trade_reflections_v2.pnl_pct) 记在成交决策 (trade_decisions.id) 上。
两个编号空间 → "权重 → 真实盈亏" 直接 join 只有个位数行, 权重准不准根本无从验证
(需求 #2 的根因, 不是权重算法的问题)。

本脚本用同一套 6 维口径给成交决策补算权重 (decision_space='trade'), 让
"这条决策的权重" 与 "这条决策的真实盈亏" 可以逐条对账。

    python3 backfill_trade_decision_weights.py --limit 5000
    python3 backfill_trade_decision_weights.py --all
"""
from __future__ import annotations

import argparse
import os
import sqlite3
import sys
from pathlib import Path
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src_python"))

from signals import signal_weight_evaluator as swe  # noqa: E402

DB_PATH = os.environ.get("TRIDENT_DB_PATH") or os.path.join(
    str(Path(__file__).resolve().parents[2]), "trident_event_bus.db"
)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default=DB_PATH)
    ap.add_argument("--limit", type=int, default=2000)
    ap.add_argument("--all", action="store_true", help="处理全部成交决策")
    ap.add_argument("--only-settled", action="store_true",
                    help="只处理有真实盈亏样本的成交决策 (默认开)")
    ap.add_argument("--no-only-settled", dest="only_settled", action="store_false")
    ap.set_defaults(only_settled=True)
    args = ap.parse_args()

    conn = sqlite3.connect(args.db, timeout=60)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")

    where = ["NOT EXISTS (SELECT 1 FROM signal_weight_history h "
             "WHERE h.decision_id = td.id AND h.decision_space = 'trade')"]
    if args.only_settled:
        where.append("EXISTS (SELECT 1 FROM trade_reflections_v2 r "
                     "WHERE r.decision_id = td.id AND r.pnl_pct IS NOT NULL)")
    sql = ("SELECT td.id FROM trade_decisions td WHERE %s ORDER BY td.id"
           % " AND ".join(where))
    if not args.all:
        sql += " LIMIT %d" % int(args.limit)
    rows = conn.execute(sql).fetchall()
    print("[INFO] 待补权重成交决策: %d" % len(rows))
    done = 0
    t0 = time.time()
    for i, r in enumerate(rows):
        td_id = int(r["id"])
        try:
            res = swe.evaluate_and_persist_trade(conn, td_id, trigger_kind="backfill_trade")
        except Exception as exc:  # noqa: BLE001
            print("[WARN] td=%s 失败: %s" % (td_id, exc))
            continue
        if res:
            done += 1
        if (i + 1) % 100 == 0:
            conn.commit()
            print("[PROGRESS] %d/%d (%.1fs)" % (i + 1, len(rows), time.time() - t0))
    conn.commit()
    print("[OK] 写入权重行: %d" % done)
    n = conn.execute(
        "SELECT COUNT(*) FROM signal_weight_history WHERE decision_space='trade'"
    ).fetchone()[0]
    samples = conn.execute(
        """SELECT COUNT(DISTINCT h.decision_id)
           FROM signal_weight_history h
           JOIN trade_reflections_v2 r ON r.decision_id = h.decision_id
           WHERE h.decision_space='trade' AND r.pnl_pct IS NOT NULL"""
    ).fetchone()[0]
    print("[OK] trade 空间权重行: %d, 可对账决策样本: %d" % (n, samples))
    conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
