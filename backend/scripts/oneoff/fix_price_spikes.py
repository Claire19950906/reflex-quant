# -*- coding: utf-8 -*-
"""一次性数据修复：只清**尖刺**，不碰拆股。

背景（实测 2026-11-15）：
  BTC 2023-11-06=94900、11-07=94700 —— 2024 的价格错配到 2023 的日期。
  这两根会把 5 日窗口里几十个检测点变成 "+170% 异动"。

  但同时扫出来的一堆"跳变"其实是**拆股**，不是脏数据：
    AAPL 2014-06 645.57→93.70 (7:1) / NVDA 2021-07 751→186 (4:1)
    NVDA 2024-06 1208→121 (10:1) / TSLA 2020-08 2213→498 (5:1)
  删了就是丢真历史，所以必须分开处理。

判定：跳变后 3 根内**回到原水平** = 尖刺 → 删；
      停在新水平 = 公司行为 → 不删（读序列时由
      backfill_critical_moves.sanitize_price_series 归平）。

用法：
  python3 src_python/fix_price_spikes.py            # 只报告
  python3 src_python/fix_price_spikes.py --apply    # 实际删除尖刺
"""
from __future__ import annotations

import argparse
import os
import sqlite3
import sys

DB = os.path.join(os.path.dirname(__file__), "..", "..", "trident_event_bus.db")
JUMP_PCT = 50.0
LOOKAHEAD = 3
RETURN_TOL = 30.0


def scan(conn: sqlite3.Connection) -> tuple[list[dict], list[dict]]:
    """返回 (spikes, corporate_actions)。"""
    spikes: list[dict] = []
    actions: list[dict] = []
    assets = [r[0] for r in conn.execute(
        "SELECT DISTINCT asset FROM ohlcv_daily WHERE close>0 ORDER BY asset")]
    for a in assets:
        rows = conn.execute(
            "SELECT ts, close FROM ohlcv_daily WHERE asset=? AND close>0 ORDER BY ts",
            (a,)).fetchall()
        i = 1
        while i < len(rows):
            prev_v, cur_v = rows[i - 1][1], rows[i][1]
            if prev_v <= 0 or abs((cur_v - prev_v) / prev_v * 100.0) <= JUMP_PCT:
                i += 1
                continue
            back = None
            for j in range(i, min(len(rows), i + LOOKAHEAD + 1)):
                if abs(rows[j][1] - prev_v) / prev_v * 100.0 < RETURN_TOL:
                    back = j
                    break
            if back is not None:
                spikes.append({
                    "asset": a,
                    "ts_list": [int(r[0]) for r in rows[i:back]],
                    "close_list": [float(r[1]) for r in rows[i:back]],
                    "prev": float(prev_v),
                    "back_to": float(rows[back][1]),
                })
                i = back + 1
            else:
                actions.append({
                    "asset": a, "ts": int(rows[i][0]),
                    "before": float(prev_v), "after": float(cur_v),
                    "ratio": round(prev_v / cur_v, 2) if cur_v else None,
                })
                i += 1
    return spikes, actions


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true", help="实际删除尖刺（默认只报告）")
    ap.add_argument("--db", default=DB)
    args = ap.parse_args()

    conn = sqlite3.connect(args.db)
    conn.row_factory = sqlite3.Row
    spikes, actions = scan(conn)

    print(f"[SCAN] 尖刺 {len(spikes)} 处 / 拆股类跳变 {len(actions)} 处")
    for s in spikes:
        print(f"  [SPIKE] {s['asset']:6s} {len(s['ts_list'])} 根 "
              f"({s['prev']:.2f} → {s['close_list']} → {s['back_to']:.2f})")
    for a in actions:
        print(f"  [CORP ] {a['asset']:6s} ts={a['ts']} "
              f"{a['before']:.2f}→{a['after']:.2f} (≈{a['ratio']}:1) 不删，读序列时归平")

    if not spikes:
        print("[DONE] 没有尖刺需要清理")
        return 0
    if not args.apply:
        print("[DRY-RUN] 未改动。确认后加 --apply（只删尖刺，不碰拆股）")
        return 0

    n = 0
    with conn:
        for s in spikes:
            for ts, cl in zip(s["ts_list"], s["close_list"]):
                cur = conn.execute(
                    "DELETE FROM ohlcv_daily WHERE asset=? AND ts=? AND close=?",
                    (s["asset"], ts, cl))
                n += cur.rowcount
    print(f"[APPLY] 已删除 {n} 行尖刺")
    left, _ = scan(conn)
    print(f"[VERIFY] 复扫剩余尖刺 {len(left)} 处" + ("（已清空）" if not left else ""))
    return 0


if __name__ == "__main__":
    sys.exit(main())
