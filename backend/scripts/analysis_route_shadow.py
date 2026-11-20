#!/usr/bin/env python3
"""analysis_route_shadow.py — 用历史库回放「前置路由」，评估误杀率。

只读。回答 W1 上线前的唯一问题：如果当时就用 `analysis/route.py` 决定
「这条新闻要不要走主分析」，会不会把**真正跑出方向结论**的新闻挡在门外？

用法：
  python3 backend/scripts/analysis_route_shadow.py --date 2026-11-16
  python3 backend/scripts/analysis_route_shadow.py --since "2026-11-16T00:00:00"
"""
from __future__ import annotations

import argparse
import os
import sqlite3
import sys
from collections import Counter, defaultdict

_BACKEND = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_SRC = os.path.join(_BACKEND, "src_python")
if _SRC not in sys.path:
    sys.path.insert(0, _SRC)

from analysis import contract as ac
from analysis import route as ar


def _connect(path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=30.0)
    conn.row_factory = sqlite3.Row
    return conn


def _epoch(local_dt: str) -> int:
    """北京时间字符串 → epoch 秒（raw_news.ts 是 epoch）。"""
    from datetime import datetime, timezone, timedelta
    dt = datetime.strptime(local_dt[:19], "%Y-%m-%d %H:%M:%S")
    return int(dt.replace(tzinfo=timezone(timedelta(hours=8))).timestamp())


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default=os.path.join(_BACKEND, "trident_event_bus.db"))
    ap.add_argument("--date", default=None)
    ap.add_argument("--since", default=None)
    args = ap.parse_args()
    if not os.path.exists(args.db):
        print(f"[FATAL] 库不存在：{args.db}")
        return 2

    where, params = "", []
    lo, hi = None, None
    if args.date:
        lo = _epoch(f"{args.date} 00:00:00")
        hi = lo + 86400
    elif args.since:
        lo = _epoch(args.since.replace("T", " ")[:19])

    conn = _connect(args.db)
    try:
        news = conn.execute(
            "SELECT n.id, n.content, n.gate_main_asset, n.gate_assets, n.status, n.ts "
            "FROM raw_news n WHERE n.status IN ('DONE','PENDING','PROCESSING')",
            params,
        ).fetchall()
        decs = conn.execute(
            "SELECT news_id, target_asset, suggested_action, analysis_degraded, "
            "       is_sibling, market_category FROM ai_decisions").fetchall()
    finally:
        conn.close()

    by_news = defaultdict(list)
    for d in decs:
        by_news[d["news_id"]].append(d)

    if lo is not None:
        news = [r for r in news if lo <= int(r["ts"] or 0) < (hi or 10 ** 12)]

    scopes = Counter()
    reasons = Counter()
    blocked_directional = []
    core_marked_out = []
    sibling_rows = 0
    for row in news:
        rows = by_news.get(row["id"], [])
        cats = [d["market_category"] for d in rows if d["market_category"]]
        r = ar.route_news(
            gate_main_asset=row["gate_main_asset"] or "",
            gate_assets=row["gate_assets"] or "",
            news_text=row["content"] or "",
            category=(cats[0] if cats else ""),
        )
        scopes[r.scope] += 1
        reasons[r.reason] += 1
        sibling_rows += sum(1 for d in rows if d["is_sibling"])
        if not r.should_analyze:
            # 真误杀：被挡的新闻里出现了「非副品种行、未降级、方向性」的决策
            real = [d for d in rows
                    if not d["is_sibling"]
                    and not int(d["analysis_degraded"] or 0)
                    and str(d["suggested_action"] or "").upper() in ("BUY", "SELL")]
            if real:
                blocked_directional.append((row["id"], r.scope, r.reason,
                                            real[0]["suggested_action"], real[0]["target_asset"]))
            # 矛盾：被挡但主行落在核心品种（说明路由漏了核心信号）
            core_main = [d for d in rows
                         if not d["is_sibling"] and ac.scope_of(d["target_asset"]) == "core"]
            if core_main:
                core_marked_out.append((row["id"], core_main[0]["target_asset"], r.reason))

    total = sum(scopes.values())
    print(f"\n== 前置路由回放（只读）==  news={total}  范围={args.date or args.since or '全部'}\n")
    print("路由结论")
    for k, n in scopes.most_common():
        print(f"  {k:<14} {n:>5}  {(n / total * 100 if total else 0):5.1f}%"
              f"   {'不进主分析' if k == 'out_of_scope' else '进主分析'}")
    print("\n判据命中")
    for k, n in reasons.most_common():
        print(f"  {k:<20} {n:>5}")

    print(f"\n误杀检查（被挡但实际跑出 BUY/SELL 的新闻）：{len(blocked_directional)}")
    for x in blocked_directional[:10]:
        print(f"  news={x[0]} {x[1]}/{x[2]} → {x[3]} {x[4]}")

    print(f"矛盾检查（实际决策是核心品种、路由却判不进分析）：{len(core_marked_out)}")
    for x in core_marked_out[:10]:
        print(f"  news={x[0]} asset={x[1]} reason={x[2]}")

    print("\n结论口径：误杀 0 且矛盾 0 才允许把 ANALYSIS_ROUTE_V2 打开；"
          "任一非 0 都先补别名/特征词再回放。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
