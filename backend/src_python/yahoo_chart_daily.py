#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""yahoo_chart_daily.py — 用 Yahoo chart API 补日线（当前目标：000660.KS / SK海力士）

为什么单写一个而不用 yfinance
----------------------------
`yf.download("000660.KS", period="max")` 直接报 "possibly delisted; no price data"，
但同一个品种走 Yahoo 的 **chart API** 是能拿到全历史的（实测 meta 里 currency=KRW、
exchangeName=KSC、firstTradeDate=2000-01-04）。yfinance 这条路时好时坏，chart 接口更稳。
另外这个接口对任何 yahoo 代码都通用，以后别的品种缺历史直接扔进来就行。

接口：
    https://query1.finance.yahoo.com/v8/finance/chart/{SYMBOL}?range=10y&interval=1d
返回 timestamp[] + indicators.quote[0].{open,high,low,close,volume}[]，
缺失点是 null，必须按索引对齐后成对丢弃（不能 zip 后再滤，会错位）。

限流：实测同一个代码连续请求会返回 0 字节。本脚本对空响应做退避重试（2/5/10 秒）。

落库路径与既有日线一致：
    kline_bars(symbol,'1d',ts=该 bar 的 UTC 毫秒) → backfill_ohlcv_local.build_rows() → ohlcv_daily

用法：
    python3 yahoo_chart_daily.py                    # dry-run，默认 000660.KS
    python3 yahoo_chart_daily.py --apply
    python3 yahoo_chart_daily.py --apply --symbols 000660.KS,^KS11 --range 10y
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import sqlite3
import sys
import time
from urllib.request import Request, urlopen

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

import db

URL = ("https://query1.finance.yahoo.com/v8/finance/chart/"
       "{sym}?range={rng}&interval=1d")
DEFAULT_SYMBOLS = ["000660.KS"]
UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "Chrome/120 Safari/537.36")


def fetch(sym: str, rng: str = "10y", tries: int = 4, timeout: int = 25):
    """返回 (timestamps, quote dict)；空响应重试。"""
    last_err = "空响应"
    for i in range(tries):
        url = URL.format(sym=sym, rng=rng)
        req = Request(url, headers={"User-Agent": UA, "Accept": "application/json"})
        try:
            with urlopen(req, timeout=timeout) as resp:
                raw = resp.read()
            if not raw:
                last_err = "返回 0 字节（限流）"
                raise ValueError(last_err)
            payload = json.loads(raw.decode("utf-8", "replace"))
            result = ((payload.get("chart") or {}).get("result") or [])
            if not result:
                raise ValueError(f"chart.result 为空: {str(payload)[:80]}")
            block = result[0]
            ts = block.get("timestamp") or []
            quote = ((block.get("indicators") or {}).get("quote") or [{}])[0]
            if not ts or not quote:
                raise ValueError("timestamp/quote 为空")
            return ts, quote
        except Exception as exc:
            last_err = f"{type(exc).__name__}: {exc}"
            if i < tries - 1:
                wait = 2 * (i + 1) + i * 3
                print(f"  [RETRY] {sym} {last_err}，{wait}s 后重试 ({i + 1}/{tries})")
                time.sleep(wait)
    raise RuntimeError(last_err)


def to_rows(asset: str, ts: list[int], quote: dict) -> list[tuple]:
    """按索引对齐后成对丢弃；非法行（任一价格 <=0 或 high<low）跳过。"""
    o = quote.get("open") or []
    h = quote.get("high") or []
    l = quote.get("low") or []
    c = quote.get("close") or []
    v = quote.get("volume") or []
    out: list[tuple] = []
    seen: set[str] = set()
    for i, t in enumerate(ts):
        try:
            oo, hh, ll, cc = (float(o[i]), float(h[i]), float(l[i]), float(c[i]))
            vv = int(float(v[i] or 0)) if i < len(v) else 0
        except (IndexError, TypeError, ValueError):
            continue
        if min(oo, hh, ll, cc) <= 0 or hh < ll:
            continue
        day = dt.datetime.fromtimestamp(t, dt.UTC).strftime("%Y-%m-%d")
        if day in seen:                     # 同日多 bar（时区/夏令时）只留第一根
            continue
        seen.add(day)
        out.append((asset, "1d", int(t) * 1000, oo, hh, ll, cc, vv, int(time.time())))
    out.sort(key=lambda r: r[2])
    return out


def write_kline(conn: sqlite3.Connection, rows: list[tuple], tries: int = 4) -> int:
    for i in range(tries):
        try:
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
        except sqlite3.OperationalError as exc:
            if "locked" not in str(exc).lower() or i == tries - 1:
                raise
            wait = 5 * (i + 1)
            print(f"  [LOCK] 写库被占，{wait}s 后重试 ({i + 1}/{tries - 1})")
            time.sleep(wait)
    return 0


def refresh_ohlcv(conn: sqlite3.Connection, symbols: set[str]) -> dict[str, int]:
    from backfill_ohlcv_local import build_rows

    rows, _stats = build_rows(conn)
    picked = [r for r in rows if r[0] in symbols]
    dedup: dict[tuple[str, str], tuple] = {}
    for r in picked:
        day = dt.datetime.fromtimestamp(r[1], dt.UTC).strftime("%Y-%m-%d")
        k = (r[0], day)
        if k not in dedup or r[1] > dedup[k][1]:
            dedup[k] = r

    # 先清掉同日的非规范行（老快照/老源），避免一天两根
    canon_ts = {r[1] for r in dedup.values()}
    for asset in symbols:
        stale = [row[0] for row in conn.execute(
            "SELECT ts FROM ohlcv_daily WHERE asset=?", (asset,)) if row[0] not in canon_ts]
        for ts_val in stale:
            day = dt.datetime.fromtimestamp(ts_val, dt.UTC).strftime("%Y-%m-%d")
            if any(dt.datetime.fromtimestamp(c, dt.UTC).strftime("%Y-%m-%d") == day
                   for c in canon_ts):
                conn.execute("DELETE FROM ohlcv_daily WHERE asset=? AND ts=?", (asset, ts_val))
    conn.commit()

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
    ap.add_argument("--range", default="10y")
    args = ap.parse_args()
    symbols = [s.strip() for s in args.symbols.split(",") if s.strip()]

    print(f"[INFO] 目标: {', '.join(symbols)}  range={args.range}  "
          f"{'(写入)' if args.apply else '(dry-run)'}")
    conn = sqlite3.connect(db.get_db_path(), timeout=30)
    conn.execute("PRAGMA busy_timeout=30000")
    try:
        for sym in symbols:
            try:
                ts, quote = fetch(sym, args.range)
            except Exception as exc:
                print(f"  [FAIL] {sym}: {exc}")
                continue
            rows = to_rows(sym, ts, quote)
            if not rows:
                print(f"  [FAIL] {sym}: 对齐后 0 行")
                continue
            span = (f"{dt.datetime.fromtimestamp(rows[0][2] / 1000, dt.UTC):%Y-%m-%d} → "
                    f"{dt.datetime.fromtimestamp(rows[-1][2] / 1000, dt.UTC):%Y-%m-%d}")
            print(f"  [FETCH] {sym}: {len(rows)} 根 ({span})  末根 close={rows[-1][6]}")
            if args.apply:
                print(f"  [WRITE] {sym}: kline_bars 影响 {write_kline(conn, rows)} 行")
        if args.apply:
            counts = refresh_ohlcv(conn, set(symbols))
            print("[OK] ohlcv_daily: " + ", ".join(f"{k}={v}" for k, v in counts.items()))
        else:
            print("[DRY] 未写库")
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
