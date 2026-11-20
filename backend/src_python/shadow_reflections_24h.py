#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""shadow_reflections_24h.py — 给影子反思加一次 24 小时判决

为什么要第二个窗口
------------------
4h 结算回答的是"这条新闻的即时冲击对不对"；但新闻驱动的行情经常**越过 4h 才走出来**
（尤其 WTI 这种 1d 数据源、以及跨交易时段的事件）。只用 4h 会把"慢半拍但方向对"
判成错，也会把"冲高回落"判成对。两个窗口都记，才能分清"判断错"和"窗口短"。

做法
----
- 回读 `shadow_reflections` 的 entry_ts / entry_price / claimed_direction；
- 从 `kline_bars` 取 entry_ts+24h 的价格（优先 15m，退 1m，再退 1d）；
- 算 24h 涨跌 → up/down/flat，与声称方向比 → direction_ok / wrong / flat / insufficient；
- 只写 `verdict_24h / effective_24h / price_24h / move_pct_24h / settled_24h_at` 五列，
  **不改** 4h 的 verdict/reflection_effective（两个窗口各自成立）。

口径
----
- 未到 24h 的行写 `insufficient` 且**不写 settled_24h_at**，下一轮自动重试；
- 价格缺失同样不写 settled，避免把"没数据"钉成"判不了"；
- |move| < 0.01% 记 flat（与 4h 的 FLAT_PCT 对齐），flat 不给 effective（NULL）；
- 只读/只补列，不动任何业务表。

用法
    python3 shadow_reflections_24h.py --report
    python3 shadow_reflections_24h.py --commit
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
import time
from pathlib import Path
from typing import Any, Dict, Optional

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

import db

FLAT_PCT = 0.01
HORIZON_SEC = 86400
TIMEFRAMES = ("15m", "1m", "1d")


def _price_at(conn: sqlite3.Connection, asset: str, target_ts: int) -> Optional[float]:
    """取 target_ts 之后最近一根 K 线的收盘价（优先 15m → 1m → 1d）。"""
    for tf in TIMEFRAMES:
        row = conn.execute(
            "SELECT close FROM kline_bars WHERE symbol=? AND timeframe=? AND ts>=? "
            "ORDER BY ts LIMIT 1",
            (asset, tf, int(target_ts) * 1000),
        ).fetchone()
        if row and row[0]:
            return float(row[0])
    return None


def _direction(claimed: str) -> str:
    c = (claimed or "").upper()
    if c == "BUY":
        return "up"
    if c == "SELL":
        return "down"
    return ""


def run(commit: bool) -> Dict[str, Any]:
    conn = db.get_connection()
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout=30000")
    try:
        cols = {r[1] for r in conn.execute("PRAGMA table_info(shadow_reflections)")}
        if "verdict_24h" not in cols:
            return {"ok": False, "error": "缺少 verdict_24h 列，请先跑 db.ensure_latest_columns"}

        rows = conn.execute(
            "SELECT id, asset, claimed_direction, entry_ts, entry_price"
            "  FROM shadow_reflections"
            " WHERE COALESCE(settled_24h_at,'')='' AND entry_ts IS NOT NULL AND entry_price IS NOT NULL"
            " ORDER BY entry_ts"
        ).fetchall()
        counts = {"checked": 0, "ok_count": 0, "wrong": 0, "flat": 0, "insufficient": 0, "written": 0}
        now = int(time.time())
        for r in rows:
            rid = int(r["id"])
            counts["checked"] += 1
            target = int(r["entry_ts"]) + HORIZON_SEC
            if target > now:
                counts["insufficient"] += 1
                if commit:
                    conn.execute(
                        "UPDATE shadow_reflections SET verdict_24h='insufficient', effective_24h=NULL"
                        " WHERE id=?", (rid,))
                continue
            px = _price_at(conn, str(r["asset"]), target)
            if not px:
                counts["insufficient"] += 1
                continue
            entry = float(r["entry_price"])
            if entry <= 0:
                counts["insufficient"] += 1
                continue
            move = (px - entry) / entry * 100.0
            if abs(move) < FLAT_PCT:
                verdict, eff = "flat", None
                counts["flat"] += 1
            else:
                realized = "up" if move > 0 else "down"
                claimed = _direction(str(r["claimed_direction"]))
                if not claimed:
                    verdict, eff = "insufficient", None
                    counts["insufficient"] += 1
                elif realized == claimed:
                    verdict, eff = "direction_ok", 1
                    counts["ok_count"] += 1
                else:
                    verdict, eff = "direction_wrong", 0
                    counts["wrong"] += 1
            if commit:
                conn.execute(
                    "UPDATE shadow_reflections SET verdict_24h=?, effective_24h=?, price_24h=?,"
                    " move_pct_24h=?, settled_24h_at=?"
                    " WHERE id=?",
                    (verdict, eff, round(px, 6), round(move, 4),
                     time.strftime("%Y-%m-%dT%H:%M:%S"), rid))
                counts["written"] += 1
        if commit:
            conn.commit()
        return {"ok": True, "commit": commit, **counts}
    finally:
        conn.close()


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--commit", action="store_true")
    ap.add_argument("--report", action="store_true")
    args = ap.parse_args()
    print(json.dumps(run(commit=args.commit), ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
