# -*- coding: utf-8 -*-
"""backfill_intraday_from_ticks.py — 用 market_ticks(秒级) 增量生成 1m/1h K线。

为什么需要这条
--------------
``ohlcv_intraday`` 原本**没有任何定时增量任务**（只被 API/远端拉取按需写），
实测 2026-11-16 07:08 之后 1m/1h 全部停更 4 小时以上：
    XAU 1m → 07:08   BTC 1m → 06:47   WTI 1m → 07:08
而 ``market_ticks`` 一直在写到当前（BTC/ETH/SOL/WTI/XAU/XAG，2 秒一采）。

后果：决策快照的 4H/1H/15m 多周期上下文、K线形态、量比这些只能拿旧 bar，
"K线信号" 判断被钉死在几个小时前。fast 涨跌检测已绕过这张表直接用 tick，
但 slow/形态/量能那一路仍在读 bar。

做法
----
把 tick 按分钟/小时桶聚合成 OHLC，写回 ohlcv_intraday（PK = asset+interval+ts，
INSERT OR REPLACE）。只处理"上次已落库 bar 之后"的窗口，单次写入量小。

保护
----
· 桶内 tick 数 < MIN_TICKS_PER_BAR 时跳过 —— 稀疏时段不造残缺 bar；
· 写入前用 busy_timeout=30s，避免和 103 个 launchd 任务抢锁时直接报 locked；
· 只写 closed=0 的当前桶时保留 open/close 语义，历史桶靠 REPLACE 覆盖。
"""
from __future__ import annotations

import argparse
import os
import sqlite3
import sys
from pathlib import Path
import time
from typing import Dict, List, Tuple

DB_PATH = os.path.join(
    str(Path(__file__).resolve().parents[2]), "trident_event_bus.db"
)

ASSETS = ("BTC", "ETH", "SOL", "XAU", "WTI", "XAG")
# v453: 补 5m —— 此前只做 1m/1h，BTC 5m 从 11-18 00:05 起就没人写，
# 图表切到 5m 只能拿旧 bar 兜底。5m 由 60 个 1 分钟桶合成，样本门槛 15 个 tick。
MIN_TICKS_PER_BAR = {"1m": 3, "5m": 15, "1h": 20}
BUCKET_SEC = {"1m": 60, "5m": 300, "1h": 3600}


def _conn() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH, timeout=30)
    conn.execute("PRAGMA busy_timeout=30000")
    return conn


def _last_bar_ts(conn: sqlite3.Connection, asset: str, interval: str) -> int:
    row = conn.execute(
        "SELECT MAX(ts) FROM ohlcv_intraday WHERE asset = ? AND interval = ?",
        (asset, interval),
    ).fetchone()
    return int(row[0]) if row and row[0] else 0


def build_bars(conn: sqlite3.Connection, asset: str, interval: str,
               since_ts: int) -> List[Tuple]:
    step = BUCKET_SEC[interval]
    rows = conn.execute(
        "SELECT ts, price FROM market_ticks WHERE symbol = ? AND ts >= ? "
        "ORDER BY ts ASC",
        (asset, max(since_ts - step, 0)),
    ).fetchall()
    if not rows:
        return []
    buckets: Dict[int, List[Tuple[int, float]]] = {}
    for ts, price in rows:
        if not price:
            continue
        buckets.setdefault((int(ts) // step) * step, []).append((int(ts), float(price)))

    bars: List[Tuple] = []
    now = int(time.time())
    for bts, items in buckets.items():
        if len(items) < MIN_TICKS_PER_BAR[interval]:
            continue
        prices = [p for _, p in items]
        bars.append((
            asset, interval, bts,
            prices[0], max(prices), min(prices), prices[-1],
            0.0, len(items),
            0 if bts + step > now else 1,
            "market_ticks",
        ))
    return bars


def run(assets, intervals, commit: bool, backfill_days: int = 0) -> Dict[str, int]:
    conn = _conn()
    conn.execute("PRAGMA journal_mode=WAL")
    written: Dict[str, int] = {}
    try:
        for asset in assets:
            for interval in intervals:
                since = _last_bar_ts(conn, asset, interval)
                if backfill_days > 0:
                    # 补齐历史：只看最近 N 天，不受"上次已落库"限制。
                    # 用途是让 4H/1H 趋势立刻有足够连续 bar（否则新表要等一天才可用）
                    since = min(since, int(time.time()) - backfill_days * 86400)
                bars = build_bars(conn, asset, interval, since)
                if not bars:
                    continue
                conn.executemany(
                    "INSERT OR REPLACE INTO ohlcv_intraday "
                    "(asset, interval, ts, open, high, low, close, volume, trades, "
                    " closed, source) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                    bars,
                )
                # v453: 同步镜像到 kline_bars（图表与回测读的是这张表）。
                # 此前只写 ohlcv_intraday，于是 ohlcv_intraday 里 5m 是新的、
                # kline_bars 里 5m 停在 11-18 00:05 —— 图表切 5m 只能拿旧 bar。
                # ts 单位不同：ohlcv_intraday 用秒，kline_bars 用毫秒。
                _mirror = [
                    (b[0], b[1], int(b[2]) * 1000, b[3], b[4], b[5], b[6], b[7],
                     int(time.time()))
                    for b in bars
                ]
                conn.executemany(
                    "INSERT INTO kline_bars "
                    "(symbol, timeframe, ts, open, high, low, close, volume, fetched_at) "
                    "VALUES (?,?,?,?,?,?,?,?,?) "
                    "ON CONFLICT(symbol, timeframe, ts) DO UPDATE SET "
                    "open=excluded.open, high=excluded.high, low=excluded.low, "
                    "close=excluded.close, volume=excluded.volume, fetched_at=excluded.fetched_at",
                    _mirror,
                )
                written[f"{asset}/{interval}"] = len(bars)
        if commit:
            conn.commit()
        else:
            conn.rollback()
    finally:
        conn.close()
    return written


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--commit", action="store_true", help="真正写库（默认 dry-run）")
    ap.add_argument("--intervals", default="1m,5m,1h")
    ap.add_argument("--assets", default=",".join(ASSETS))
    ap.add_argument("--days", type=int, default=0, help="补齐最近 N 天（0=只补增量）")
    args = ap.parse_args()

    assets = [a.strip().upper() for a in args.assets.split(",") if a.strip()]
    intervals = [i.strip() for i in args.intervals.split(",") if i.strip()]
    for i in intervals:
        if i not in BUCKET_SEC:
            print(f"[Skip] 不支持的粒度: {i}（只支持 {list(BUCKET_SEC)}）")
            return 2

    out = run(assets, intervals, commit=args.commit, backfill_days=args.days)
    total = sum(out.values())
    for k, v in sorted(out.items()):
        print(f"  {k:<10} +{v} bar")
    print(f"[{'COMMIT' if args.commit else 'DRY-RUN'}] 合计 {total} bar")
    return 0


if __name__ == "__main__":
    sys.exit(main())
