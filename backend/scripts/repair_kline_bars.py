#!/usr/bin/env python3
"""清理 kline_bars 里的"报价当 K 线"脏行。

问题的形状（v408 实测）：
  XAU/XAG/WTI 的部分日内周期里混进了大量这样的行 —— open=high=low=close 且 volume=0，
  时间戳**不对齐到周期**（如 4h 表里出现 01:54:53.260 这种），同一价格被反复写成一根"K 线"。
  典型成因是某个采集路径把"当前报价"当成一根 bar 落库。后果：
    · 一根真实的 4h 线被十几根假线包围 → ADX、ATR、价值区、布林带宽全部失真；
    · 回测前瞻收益直接算成荒谬值（实测 XAU 4h 的 12h 上涨占比只有 15%）。

判定（三条同时满足才算脏行，宁可漏判不要误删）：
  1) 周期属于日内集合 {1m,5m,10m,15m,30m,1h,4h}
  2) O=H=L=C 且 volume=0（没有任何价格行程，不可能是真 K 线）
  3) ts 不是该周期整秒的整数倍（不属于任何合法 bar 起点）
  日线**不在范围内**：股票的日线 ts 落在本地 16:00、加密在 00:00，按 UTC 对齐会误判。

安全性：
  · 默认 --dry-run，只审计不删；
  · `--apply` 才真删，删前把每一行的完整字段导出成 CSV（可逐行还原）；
  · 只删上面这三条同时满足的行，其它一律不碰。

用法：
  python3 scripts/repair_kline_bars.py                 # 审计
  python3 scripts/repair_kline_bars.py --apply         # 执行（先导出 CSV）
"""
import argparse
import csv
import os
import sqlite3
import sys
import time
from typing import List, Tuple

DEFAULT_DB = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "trident_event_bus.db")
OUT_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "reports", "data_repairs")
INTRADAY = {"1m": 60, "5m": 300, "10m": 600, "15m": 900, "30m": 1800, "1h": 3600, "4h": 14400}

DIRTY = """
open=high AND high=low AND low=close AND coalesce(volume,0)=0
AND cast(ts AS INTEGER) % ? != 0
"""


def quality(conn: sqlite3.Connection, symbol: str, tf: str) -> Tuple[int, float, float]:
    """(行数, 重复收盘占比%, 主间隔占比%)"""
    rows = conn.execute(
        "SELECT ts, close FROM kline_bars WHERE symbol=? AND timeframe=? AND close IS NOT NULL "
        "ORDER BY ts", (symbol, tf)).fetchall()
    if len(rows) < 3:
        return len(rows), 0.0, 0.0
    dup = sum(1 for i in range(1, len(rows)) if rows[i][1] == rows[i - 1][1]) / (len(rows) - 1) * 100
    gaps = sorted(rows[i][0] - rows[i - 1][0] for i in range(1, len(rows)))
    main = gaps[len(gaps) // 2]
    share = sum(1 for g in gaps if g == main) / len(gaps) * 100
    return len(rows), round(dup, 1), round(share, 1)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default=DEFAULT_DB)
    ap.add_argument("--apply", action="store_true", help="真正删除（默认只审计）")
    ap.add_argument("--symbols", default="", help="只处理这些品种，逗号分隔；空=全部")
    args = ap.parse_args()

    conn = sqlite3.connect(args.db, timeout=30, isolation_level=None)
    conn.execute("PRAGMA busy_timeout=30000")

    only = {s.strip().upper() for s in args.symbols.split(",") if s.strip()}
    pairs = conn.execute(
        "SELECT symbol, timeframe, count(*) FROM kline_bars GROUP BY symbol, timeframe").fetchall()
    plan: List[Tuple[str, str, int]] = []
    for symbol, tf, n in pairs:
        sec = INTRADAY.get(tf)
        if not sec:
            continue
        if only and symbol.upper() not in only:
            continue
        dirty = conn.execute(
            f"SELECT count(*) FROM kline_bars WHERE symbol=? AND timeframe=? AND {DIRTY}",
            (symbol, tf, sec * 1000)).fetchone()[0]
        if dirty:
            plan.append((symbol, tf, dirty))

    if not plan:
        print("[OK] 没有发现脏行", flush=True)
        return 0

    print(f"{'symbol':8s}{'tf':4s}{'脏行':>7s}{'清理前(行/重复%/主间隔%)':>28s}", flush=True)
    for symbol, tf, dirty in plan:
        n, dup, share = quality(conn, symbol, tf)
        print(f"{symbol:8s}{tf:4s}{dirty:7d}   {n:6d} / {dup:5.1f}% / {share:5.1f}%", flush=True)
    total = sum(p[2] for p in plan)
    print(f"\n合计 {total} 行待清理", flush=True)

    if not args.apply:
        print("[DRY-RUN] 未删除任何数据。确认无误后加 --apply", flush=True)
        return 0

    os.makedirs(OUT_DIR, exist_ok=True)
    stamp = time.strftime("%Y%m%d_%H%M%S")
    csv_path = os.path.join(OUT_DIR, f"kline_synthetic_{stamp}.csv")
    deleted = 0
    with open(csv_path, "w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(["symbol", "timeframe", "ts", "open", "high", "low", "close",
                    "volume", "fetched_at"])
        for symbol, tf, dirty in plan:
            sec = INTRADAY[tf]
            rows = conn.execute(
                f"""SELECT symbol, timeframe, ts, open, high, low, close, volume, fetched_at
                    FROM kline_bars WHERE symbol=? AND timeframe=? AND {DIRTY}""",
                (symbol, tf, sec * 1000)).fetchall()
            w.writerows(rows)
            cur = conn.execute(
                f"DELETE FROM kline_bars WHERE symbol=? AND timeframe=? AND {DIRTY}",
                (symbol, tf, sec * 1000))
            deleted += cur.rowcount if cur.rowcount and cur.rowcount > 0 else 0
            conn.commit()
            n, dup, share = quality(conn, symbol, tf)
            print(f"  清理 {symbol} {tf}: -{len(rows)} → 剩 {n} 行 / 重复 {dup}% / 主间隔 {share}%",
                  flush=True)

    print(f"\n[OK] 删除 {deleted} 行；原样备份在 {csv_path}", flush=True)
    print("     需要还原时，把该 CSV 逐行 INSERT 回 kline_bars 即可。", flush=True)
    conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
