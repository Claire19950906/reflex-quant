#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""回填 event_kline_impact.direction（用新口径：窗口末超额方向）。

为什么（v322 实测）
------------------
旧口径 `_classify_direction(pre, max_high, min_low)` 用的是**窗口内极值**：
    up_move = (max_high - pre) / pre ; dn_move = (pre - min_low) / pre ; 谁大判谁
→ 判的是"**振幅**"，不是"事件后价格往哪走"。
→ 先冲高再回落的窗口也会记成 "up" → 归因页会把**没导致上涨的事件**
  指认为"导致上涨"（因为它摸过高点）。

新口径：direction = **窗口末价** vs 事件前收盘 的**超额**变化
        （超额 = 减去同期其他品种的等权中位数，剥离市场 beta）

幅度 impact_pct 仍用极值 —— 幅度衡量"波动多大"，用极值是对的。

用法：
    python3 scripts/backfill_kline_impact_direction.py --dry-run
    python3 scripts/backfill_kline_impact_direction.py
"""
from __future__ import annotations
from pathlib import Path

import argparse
import os
import sqlite3
import sys
import time

BASE_DIR = str(Path(__file__).resolve().parents[2])
SRC = os.path.join(BASE_DIR, "src_python")
DB = os.environ.get("TRIDENT_DB_PATH") or os.path.join(
    BASE_DIR, "trident_event_bus.db")

if SRC not in sys.path:
    sys.path.insert(0, SRC)


def _load_series(conn):
    """把 market_ticks 按 symbol 载入内存序列 [(ts, price), ...]（一次全表扫）。"""
    series = {}
    for r in conn.execute(
            "SELECT symbol, ts, price FROM market_ticks ORDER BY symbol, ts"):
        series.setdefault(r["symbol"], []).append((int(r["ts"]), float(r["price"])))
    return series


def _close_at_or_before(series, ts):
    """序列里 ts <= 给定 ts 的最后一根 close（找不到返回 None）。"""
    lo, hi = 0, len(series) - 1
    found = None
    while lo <= hi:
        mid = (lo + hi) // 2
        if series[mid][0] <= ts:
            found = series[mid][1]
            lo = mid + 1
        else:
            hi = mid - 1
    return found


def _bench_move(series, symbol, t_start, t_end, min_symbols=3):
    """同期其他品种的等权中位数涨跌幅。"""
    moves = []
    for sym, ser in series.items():
        if sym == symbol:
            continue
        pre = _close_at_or_before(ser, t_start - 1)
        post = _close_at_or_before(ser, t_end)
        if not pre or not post or pre <= 0:
            continue
        moves.append((post - pre) / pre)
    if len(moves) < min_symbols:
        return 0.0
    moves.sort()
    mid = len(moves) // 2
    if len(moves) % 2:
        return moves[mid]
    return (moves[mid - 1] + moves[mid]) / 2.0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default=DB)
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    from engine import event_kline_impact_worker as wk

    if not os.path.exists(args.db):
        print("DB 不存在: %s" % args.db)
        return 2
    conn = sqlite3.connect(args.db, timeout=60)
    conn.row_factory = sqlite3.Row
    try:
        conn.execute("PRAGMA busy_timeout=60000;")
        rows = conn.execute(
            """SELECT id, asset, event_time, impact_window_start,
                      impact_window_end, pre_event_close, direction
               FROM event_kline_impact"""
        ).fetchall()
        print("[INFO] 待回填 %d 行" % len(rows))
        series = _load_series(conn)
        print("[INFO] market_ticks 序列: %s" %
              {k: len(v) for k, v in series.items()})
        old_dist = {}
        new_dist = {}
        changed = 0
        plans = []
        t0 = time.time()
        for r in rows:
            symbol = (r["asset"] or "").upper()
            t_start = int(r["impact_window_start"] or 0)
            t_end = int(r["impact_window_end"] or 0)
            pre = float(r["pre_event_close"] or 0)
            ser = series.get(symbol)
            if not symbol or not ser or t_start <= 0 or t_end <= 0 or pre <= 0:
                continue
            close_after = _close_at_or_before(ser, t_end)
            if not close_after:
                continue
            bench = _bench_move(series, symbol, t_start, t_end)
            new_dir = wk._classify_direction(pre, close_after, bench)
            old_dir = (r["direction"] or "").lower()
            old_dist[old_dir] = old_dist.get(old_dir, 0) + 1
            new_dist[new_dir] = new_dist.get(new_dir, 0) + 1
            if new_dir != old_dir:
                changed += 1
                plans.append((r["id"], new_dir))
        print("[INFO] 旧方向分布: %s" % old_dist)
        print("[INFO] 新方向分布: %s" % new_dist)
        print("[INFO] 方向会变的: %d / %d (%.1f%%)  ← 旧口径判错的比例"
              % (changed, len(rows), changed / max(1, len(rows)) * 100))
        if args.dry_run:
            print("\n(dry-run) 未做任何修改")
            return 0
        upd = 0
        for rid, new_dir in plans:
            delay = 0.4
            for attempt in range(8):
                try:
                    conn.execute(
                        "UPDATE event_kline_impact SET direction=? WHERE id=?",
                        (new_dir, rid))
                    conn.commit()
                    upd += 1
                    break
                except sqlite3.OperationalError as e:
                    msg = str(e).lower()
                    if ("locked" in msg or "busy" in msg) and attempt < 7:
                        time.sleep(delay)
                        delay = min(delay * 1.6, 3.0)
                        continue
                    print("[WARN] 更新 id=%s 失败: %s" % (rid, e))
                    break
        print("\n[OK] 已更新 %d 行 (耗时 %.1fs)" % (upd, time.time() - t0))
        return 0
    finally:
        try:
            conn.close()
        except Exception:
            pass


if __name__ == "__main__":
    sys.exit(main())
