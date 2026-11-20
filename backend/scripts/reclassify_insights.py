#!/usr/bin/env python3
"""v409.1 重判已有洞察（默认 dry-run，--commit 才写）。

为什么要有这个脚本：v409 让洞察"入库即判"，但判定发生在写入那一刻。如果规则
后来改了（比如 2026-11-17 补了「asset 空串就从正文认品种」），已经判过的行不会
自动重判 —— 它们带着旧结论躺在库里，`insight_classified_at` 非空即被跳过。

实测症状：review_insights 1266/1267 是 `sizing.take_profit_pct` / `sizing.stop_loss_pct`，
asset 字段是空串（save_insights 只解析 `[WTI]` 这种**前缀**才写 asset，这两条没前缀），
于是 market_context 查不到 atr_pct，本该能换算的退化成了 observe_only；而 1267 的
正文里明明白白写着「WTI」，库里 WTI 1h atr_pct=0.455 也是现成的。

只用 derived 字段（observe_only / observe_reason / param_key / param_bucket /
hypothesis / direction / window_h）覆盖，不动 rule / asset / source_samples 原值。
重跑本脚本的差异会收敛到 0，可反复执行。

用法：
  python3 scripts/reclassify_insights.py            # dry-run，只打印差异
  python3 scripts/reclassify_insights.py --commit    # 落库
"""
from __future__ import annotations

import argparse
import os
import sqlite3
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "src_python"))

import insight_hypothesis as ih

DB = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "trident_event_bus.db")

_FIELDS = ("insight_hypothesis", "insight_param_key", "insight_param_bucket",
           "insight_direction", "insight_window_h", "observe_only", "observe_reason")


def _row_state(row) -> tuple:
    return (row[3], row[4], row[5], row[6], row[7], row[8], row[9])


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--commit", action="store_true", help="真正写库（默认 dry-run）")
    ap.add_argument("--limit", type=int, default=0, help="最多处理多少条（0=全部）")
    ap.add_argument("--only-observe", action="store_true",
                    help="只处理当前 observe_only=1 的行")
    args = ap.parse_args()

    conn = sqlite3.connect(DB)
    conn.row_factory = sqlite3.Row
    sql = ("SELECT id, rule, COALESCE(asset,'') AS asset, insight_hypothesis, "
           "insight_param_key, insight_param_bucket, insight_direction, "
           "insight_window_h, observe_only, observe_reason, "
           "COALESCE(source_samples,0) AS n "
           "FROM review_insights WHERE active=1")
    if args.only_observe:
        sql += " AND COALESCE(observe_only,0)=1"
    sql += " ORDER BY id"
    if args.limit:
        sql += f" LIMIT {int(args.limit)}"
    rows = conn.execute(sql).fetchall()

    scanned = 0
    changed = 0
    flipped_to_actionable = 0
    for r in rows:
        scanned += 1
        before = _row_state(r)
        ctx = ih.market_context(conn, r["asset"], r["rule"])
        c = ih.classify(r["rule"], r["asset"], int(r["n"] or 0), context=ctx)
        after = (c["hypothesis"], c["param_key"], c["param_bucket"], c["direction"],
                 c["window_h"], c["observe_only"], c["observe_reason"])
        if after == before:
            continue
        changed += 1
        if int(before[5] or 0) == 1 and int(after[5] or 0) == 0:
            flipped_to_actionable += 1
        print(f"[DIFF] #{r['id']} asset={r['asset'] or '(空)'} ctx={ctx} "
              f"observe_only {before[5]}→{after[5]}")
        if int(before[5] or 0) != int(after[5] or 0):
            print(f"       reason: {str(before[6] or '')[:70]} → {str(after[6] or '')[:70]}")
        if args.commit:
            conn.execute(
                "UPDATE review_insights SET insight_hypothesis=?, insight_param_key=?,"
                " insight_param_bucket=?, insight_direction=?, insight_window_h=?,"
                " observe_only=?, observe_reason=?, insight_classified_at=datetime('now')"
                " WHERE id=?",
                (after[0], after[1], after[2], after[3], after[4], after[5], after[6],
                 int(r["id"])),
            )
    if args.commit:
        conn.commit()
    print(f"\n{'[COMMIT]' if args.commit else '[DRY-RUN]'} 扫描 {scanned} 条，"
          f"差异 {changed} 条，其中 observe_only 1→0（转为可执行）{flipped_to_actionable} 条")
    if not args.commit and changed:
        print("确认无误后加 --commit 落库。")
    conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
