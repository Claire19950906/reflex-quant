#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""v409.7 决策级归因刷新：把 `insight_decision_attr` 的结论写回 review_insights。

用法：
  python3 scripts/verify_insights_by_decisions.py            # 干跑，只打印
  python3 scripts/verify_insights_by_decisions.py --commit   # 落库
  python3 scripts/verify_insights_by_decisions.py --days 7 --min-group 5

写回字段：verification_method='decision_attr'、verification_score、verification_verdict、
         verification_sample_n、verification_kline_n、verified_at。
样本不足时也会写 score（证据值留档，便于观察样本在长），但 verdict 保持 insufficient_sample。
"""
from __future__ import annotations

import argparse
import os
import sqlite3
import sys
from datetime import datetime

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src_python"))

import insight_decision_attr as ia

DEFAULT_DB = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "trident_event_bus.db")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default=DEFAULT_DB)
    ap.add_argument("--days", type=int, default=7)
    ap.add_argument("--min-group", type=int, default=ia.MIN_GROUP_N)
    ap.add_argument("--commit", action="store_true")
    ap.add_argument("--limit", type=int, default=0)
    args = ap.parse_args()

    ia.MIN_GROUP_N = args.min_group
    conn = sqlite3.connect(args.db)
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        "SELECT id, rule, asset, insight_direction, created_at FROM review_insights "
        "WHERE active=1 AND insight_direction IN ('1','-1') ORDER BY id"
    ).fetchall()
    if args.limit:
        rows = rows[: args.limit]

    tally = {}
    wrote = 0
    print(f"[SCAN] active 且有方向 insights={len(rows)} days={args.days} min_group={args.min_group}")
    now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    for r in rows:
        res = ia.attribute_insight(conn, dict(r), days_back=args.days)
        v = res.get("verdict")
        tally[v] = tally.get(v, 0) + 1
        print(f"  #{r['id']} {(r['asset'] or '*'):<8} {ia.normalize_direction(r['insight_direction'])} "
              f"→ {v} score={res.get('score')} | {str(res.get('reason'))[:70]}")
        if not args.commit:
            continue
        if v in ("not_applicable",):
            continue
        conn.execute(
            "UPDATE review_insights SET verification_method='decision_attr',"
            " verification_score=?, verification_verdict=?, verification_sample_n=?,"
            " verification_kline_n=?, verified_at=? WHERE id=?",
            (res.get("score"), v,
             (res.get("n_allowed", 0) or 0) + (res.get("n_blocked", 0) or 0),
             res.get("used", 0), now_str, r["id"]),
        )
        wrote += 1
    if args.commit:
        conn.commit()
    print(f"[TALLY] {tally}")
    print(f"[{'COMMIT' if args.commit else 'DRYRUN'}] 写回 {wrote} 条")
    conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
