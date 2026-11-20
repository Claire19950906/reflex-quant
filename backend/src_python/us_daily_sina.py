#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""us_daily_sina.py — 用新浪美股日 K 补 AMZN / META / GOOGL 的全历史日线（v341）

为什么
------
`backfill_ohlcv.ASSETS` 里这三只早就加了，但取数走 yfinance：本机网络下
AMZN / META / GOOGL 三只**全部返回 0 行**，`ohlcv_daily` 里一直是 0 →
慢异动、技术指标、回测跑道对这三只完全空白，只能拿 insider 那 4 行凑。

实测可用通道（不需要代理、不需要 key）：
`https://stock.finance.sina.com.cn/usstock/api/json_v2.php/US_MinKService.getDailyK?symbol=AMZN`
返回从 1997 年起逐日的 o/h/l/c/v，正是 kline_bars 1d 需要的字段。

落库路径与既有日线一致，不另建口径：
    kline_bars(symbol, timeframe='1d', ts=当日 16:00 UTC 毫秒)
      → backfill_ohlcv_local.build_rows()
      → ohlcv_daily(source='kline_bars_1d')

用法:
    python3 us_daily_sina.py --dry-run
    python3 us_daily_sina.py --apply
    python3 us_daily_sina.py --apply --symbols AMZN,META,GOOGL,MSFT
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import sqlite3
import subprocess
import sys
import time
from urllib.request import Request, urlopen

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

import db

URL = ("https://stock.finance.sina.com.cn/usstock/api/json_v2.php/"
       "US_MinKService.getDailyK?symbol={sym}")
DEFAULT_SYMBOLS = ["AMZN", "META", "GOOGL"]
# 新浪接口按"当时的代码"分家：META 改名前的历史在 FB 名下，
# 只查 META 只拿到 2022 年起 1,113 根；FB 有 2012 上市起 2,796 根。
SYMBOL_ALIASES = {"META": ["FB"], "GOOGL": ["GOOG"]}
CLOSE_HOUR_UTC = 16          # 与既有 AAPL/NVDA 日线一致（都落在 16:00 UTC）
UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 Chrome/120 Safari/537.36"


def fetch(symbol: str, timeout: int = 20) -> list[dict]:
    req = Request(URL.format(sym=symbol), headers={"User-Agent": UA})
    with urlopen(req, timeout=timeout) as r:
        raw = r.read().decode("utf-8", "replace")
    data = json.loads(raw)
    if not isinstance(data, list):
        raise ValueError(f"返回格式异常: {str(data)[:80]}")
    return data


def to_rows(symbol: str, raw: list[dict]) -> list[tuple]:
    """→ [(symbol, '1d', ts_ms, o, h, l, c, v, fetched_at)]，非法行直接丢。"""
    out, seen = [], set()
    for item in raw:
        try:
            d = dt.datetime.strptime(item["d"], "%Y-%m-%d").replace(
                hour=CLOSE_HOUR_UTC, tzinfo=dt.timezone.utc)
            o, h, l, c = (float(item[k]) for k in ("o", "h", "l", "c"))
            v = int(float(item.get("v") or 0))
        except (KeyError, TypeError, ValueError):
            continue
        if c <= 0 or h <= 0 or l <= 0 or o <= 0 or h < l:
            continue
        if o == h == l == c and v <= 0:      # 占位行，不是真 K 线
            continue
        ts = int(d.timestamp() * 1000)
        day = d.strftime("%Y-%m-%d")
        if day in seen:
            continue
        seen.add(day)
        out.append((symbol, "1d", ts, o, h, l, c, v, int(time.time())))
    out.sort(key=lambda r: r[2])
    return out


def write_kline(conn: sqlite3.Connection, rows: list[tuple], tries: int = 4) -> int:
    """引擎在跑时会突发占住写锁（实测 database is locked），退避重试几次。"""
    for i in range(tries):
        try:
            return _write_kline_once(conn, rows)
        except sqlite3.OperationalError as e:
            if "locked" not in str(e).lower() or i == tries - 1:
                raise
            wait = 5 * (i + 1)
            print(f"  [LOCK] 写库被占，{wait}s 后重试 ({i + 1}/{tries - 1})")
            time.sleep(wait)
    return 0


def _write_kline_once(conn: sqlite3.Connection, rows: list[tuple]) -> int:
    cur = conn.executemany(
        "INSERT INTO kline_bars "
        "(symbol,timeframe,ts,open,high,low,close,volume,fetched_at) "
        "VALUES (?,?,?,?,?,?,?,?,?) "
        "ON CONFLICT(symbol,timeframe,ts) DO UPDATE SET "
        "open=excluded.open,high=excluded.high,low=excluded.low,"
        "close=excluded.close,volume=excluded.volume,fetched_at=excluded.fetched_at",
        rows)
    conn.commit()
    return cur.rowcount


def refresh_ohlcv(conn: sqlite3.Connection, symbols: set[str]) -> dict[str, int]:
    """kline_bars → ohlcv_daily，只刷新本次涉及的品种（复用既有归一逻辑）。"""
    from backfill_ohlcv_local import build_rows

    rows, _stats = build_rows(conn)
    picked = [r for r in rows if r[0] in symbols]
    dedup: dict[tuple[str, str], tuple] = {}
    for r in picked:
        day = dt.datetime.fromtimestamp(r[1], dt.UTC).strftime("%Y-%m-%d")
        k = (r[0], day)
        if k not in dedup or r[1] > dedup[k][1]:
            dedup[k] = r
    conn.executemany(
        "INSERT INTO ohlcv_daily (asset,ts,open,high,low,close,volume,source) "
        "VALUES (?,?,?,?,?,?,?,?) "
        "ON CONFLICT(asset,ts) DO UPDATE SET "
        "open=excluded.open,high=excluded.high,low=excluded.low,"
        "close=excluded.close,volume=excluded.volume,source=excluded.source",
        list(dedup.values()))
    conn.commit()
    return {s: sum(1 for r in dedup.values() if r[0] == s) for s in symbols}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true", help="真正写库（默认 dry-run）")
    ap.add_argument("--symbols", default=",".join(DEFAULT_SYMBOLS))
    args = ap.parse_args()
    symbols = [s.strip().upper() for s in args.symbols.split(",") if s.strip()]

    print(f"[INFO] 目标品种: {', '.join(symbols)}  {'(写入)' if args.apply else '(dry-run)'}")
    conn = sqlite3.connect(db.get_db_path(), timeout=30)
    conn.execute("PRAGMA busy_timeout=30000")
    total = 0
    try:
        for sym in symbols:
            rows: list[tuple] = []
            for src in [sym] + SYMBOL_ALIASES.get(sym, []):
                try:
                    part = to_rows(sym, fetch(src))
                except Exception as e:
                    print(f"  [FAIL] {sym}/{src}: {type(e).__name__} {e}")
                    continue
                # 别名历史在前、现名在后：同一 ts 以现名为准
                merged = {r[2]: r for r in rows}
                for r in part:
                    merged.setdefault(r[2], r)
                if len(part) > len(rows):
                    rows = sorted(merged.values(), key=lambda r: r[2])
                else:
                    rows = sorted(merged.values(), key=lambda r: r[2])
                print(f"    {src}: {len(part)} 根")
            if not rows:
                print(f"  [FAIL] {sym}: 解析后 0 行")
                continue
            span = f"{dt.datetime.fromtimestamp(rows[0][2]/1000, dt.UTC):%Y-%m-%d} → " \
                   f"{dt.datetime.fromtimestamp(rows[-1][2]/1000, dt.UTC):%Y-%m-%d}"
            print(f"  [FETCH] {sym}: {len(rows)} 根 ({span})")
            if args.apply:
                n = write_kline(conn, rows)
                print(f"  [WRITE] {sym}: kline_bars 影响 {n} 行")
            total += len(rows)
        if args.apply:
            counts = refresh_ohlcv(conn, set(symbols))
            print(f"[OK] ohlcv_daily: " + ", ".join(f"{k}={v}" for k, v in counts.items()))
        else:
            print(f"[DRY] 未写库，共 {total} 根")
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
