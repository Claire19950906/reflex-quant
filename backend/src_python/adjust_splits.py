#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
adjust_splits.py — 对 ohlcv_daily 里未复权的拆股做历史复权

背景
----
库里 3 个真拆股被当成暴跌写进了曲线和回测：
    NVDA 2021-07-19/20   4:1   → 记录里出现 -75%
    NVDA 2024-06-09/10  10:1   → -90%
    TSLA 2022-08-24/25   3:1   → -67%
（AAPL / MSFT 在 2001-01-01 也各自 -50%，但那两天不是真拆股，是入库首段瑕疵，
  本脚本不动它们，只在报告里点名。）

做法
----
对每个事件：把 **拆股日之前** 的 OHLC 按 1/N 缩放（后复权到当前价口径），
拆股日之后的价格保持不变。原始行先 dump 到 backend/ohlcv_daily_split_backup_<日期>.jsonl。

用法
----
    python3 adjust_splits.py --dry-run
    python3 adjust_splits.py --apply
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import sqlite3
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
BACKEND = os.path.dirname(_HERE)
DB_PATH = os.path.join(BACKEND, "trident_event_bus.db")

# (asset, 拆股生效日 UTC, 比例分母)
EVENTS = [
    # 已经复权过（NVDA 4:1 2021-07-19、NVDA 10:1 2024-06-09、TSLA 3:1 2022-08-24）
    # 不要再放进列表 —— 重复执行会把历史价二次缩放。
    ("AAPL", "2014-06-08", 7),
    ("AAPL", "2020-08-30", 4),
    ("TSLA", "2020-08-30", 5),
]


def ts_of(day: str) -> int:
    return int(dt.datetime.strptime(day, "%Y-%m-%d").replace(tzinfo=dt.timezone.utc).timestamp())


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()
    apply = args.apply and not args.dry_run

    conn = sqlite3.connect(DB_PATH)
    dump_path = os.path.join(BACKEND, f"ohlcv_daily_split_backup_{dt.date.today().isoformat()}_r2.jsonl")
    rows_to_dump: list[dict] = []

    print("事件 → 受影响行数（拆股日之前）")
    for asset, day, n in EVENTS:
        cut = ts_of(day)
        cnt = conn.execute(
            "SELECT count(*) FROM ohlcv_daily WHERE asset=? AND ts<?", (asset, cut)
        ).fetchone()[0]
        nxt = conn.execute(
            "SELECT close FROM ohlcv_daily WHERE asset=? AND ts>=? ORDER BY ts LIMIT 1", (asset, cut)
        ).fetchone()
        prv = conn.execute(
            "SELECT close FROM ohlcv_daily WHERE asset=? AND ts<? ORDER BY ts DESC LIMIT 1", (asset, cut)
        ).fetchone()
        print(f"  {asset} {day} {n}:1  前置 {cnt} 行  前收 {prv[0] if prv else '-'} → 拆股日收 {nxt[0] if nxt else '-'}")
        if prv and nxt:
            print(f"     复权后前收 ≈ {prv[0] / n:.2f}，与 {nxt[0]} 之比 {(prv[0] / n) / nxt[0]:.3f}")
        if not apply:
            continue

        rows = conn.execute(
            "SELECT asset, ts, open, high, low, close, volume, source FROM ohlcv_daily "
            "WHERE asset=? AND ts<?", (asset, cut)
        ).fetchall()
        for r in rows:
            rows_to_dump.append({
                "asset": r[0], "ts": r[1], "open": r[2], "high": r[3], "low": r[4],
                "close": r[5], "volume": r[6], "source": r[7],
                "split": f"{asset} {day} {n}:1",
            })

    if not apply:
        print("\n（dry-run，未写库）")
        conn.close()
        return 0

    with open(dump_path, "w", encoding="utf-8") as f:
        for d in rows_to_dump:
            f.write(json.dumps(d, ensure_ascii=False) + "\n")
    print(f"\n原始行已备份 {len(rows_to_dump)} 行 → {dump_path}")

    with conn:
        for asset, day, n in EVENTS:
            cut = ts_of(day)
            done = conn.execute(
                "SELECT 1 FROM data_quality_flags WHERE asset=? AND rule='split_adjusted' AND note LIKE ?",
                (asset, f"{n}:1%{day}%"),
            ).fetchone()
            if done:
                print(f"  跳过（已复权过）：{asset} {n}:1 @ {day}")
                continue
            conn.execute(
                "UPDATE ohlcv_daily SET open=open/?, high=high/?, low=low/?, close=close/? "
                "WHERE asset=? AND ts<?",
                (n, n, n, n, asset, cut),
            )
            conn.execute(
                "INSERT INTO data_quality_flags (ts, source, asset, indicator, rule, note) "
                "VALUES (?,?,?,?,?,?)",
                (cut, "ohlcv_daily", asset, "close", "split_adjusted",
                 f"{n}:1 拆股已复权 @ {day}"),
            )

    # 复权后校验
    print("\n复权后校验（拆股日附近）：")
    for asset, day, n in EVENTS:
        cut = ts_of(day)
        r = conn.execute(
            "SELECT date(ts,'unixepoch'), close FROM ohlcv_daily WHERE asset=? AND ts BETWEEN ? AND ? "
            "ORDER BY ts", (asset, cut - 3 * 86400, cut + 3 * 86400)
        ).fetchall()
        prev = None
        for d, cl in r:
            chg = f"{(cl/prev-1)*100:+.2f}%" if prev else "—"
            print(f"  {asset} {d} {cl} {chg}")
            prev = cl
    conn.close()
    return 0


def _has_flags_table(conn: sqlite3.Connection) -> bool:
    return conn.execute(
        "SELECT count(*) FROM sqlite_master WHERE type='table' AND name='data_quality_flags'"
    ).fetchone()[0] > 0


if __name__ == "__main__":
    sys.exit(main())
