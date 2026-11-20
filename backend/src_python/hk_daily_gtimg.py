#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""hk_daily_gtimg.py — 用腾讯行情接口补港股日线（当前目标：TENCENT / 0700.HK）

为什么
------
`ohlcv_daily` 里 TENCENT 只有 1 根（src=kline_bars_1d，靠快照聚合攒出来的），
技术上等于没有序列：慢异动 / 技术指标 / 回测跑道对它全空白。

腾讯自家的行情接口直接给**前复权日 K 全历史**，不需要 key、不需要代理：
    https://web.ifzq.gtimg.cn/appstock/app/hkfqkline/get?param=hk00700,day,,,1000,qfq
返回 qfqday: [日期, 开, 收, 高, 低, 成交量, ...]（注意是**开收高低**顺序，
和常见 O/H/L/C 不同，搞错会让 high<low 直接被判非法行丢掉）。
实测 1,000 根一次返回，够覆盖多年。

落库路径与既有日线一致，不另建口径：
    kline_bars(symbol, '1d', ts=当日港股收盘 16:00 HKT 即 08:00 UTC 毫秒)
      → backfill_ohlcv_local.build_rows()
      → ohlcv_daily

用法：
    python3 hk_daily_gtimg.py --dry-run
    python3 hk_daily_gtimg.py --apply
    python3 hk_daily_gtimg.py --apply --symbols TENCENT=00700
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

URL = ("https://web.ifzq.gtimg.cn/appstock/app/hkfqkline/get"
       "?param={code},day,,,{n},qfq")
DEFAULT_MAP = {"TENCENT": "00700"}
CLOSE_HOUR_HKT = 16          # 港股收盘 16:00 HKT = 08:00 UTC
UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "Chrome/120 Safari/537.36")


def fetch(code: str, bars: int = 1000, timeout: int = 25) -> list[list]:
    """返回 qfqday 原始数组列表。"""
    url = URL.format(code=f"hk{code}", n=bars)
    req = Request(url, headers={"User-Agent": UA, "Referer": "https://gu.qq.com/"})
    with urlopen(req, timeout=timeout) as resp:
        payload = json.loads(resp.read().decode("utf-8", "replace"))
    if payload.get("code") != 0:
        raise ValueError(f"接口返回错误: {payload.get('msg') or payload}")
    node = (payload.get("data") or {}).get(f"hk{code}") or {}
    rows = node.get("qfqday") or node.get("day") or []
    if not rows:
        raise ValueError("qfqday/day 均为空")
    return rows


def to_rows(asset: str, raw: list[list]) -> list[tuple]:
    """→ [(symbol,'1d',ts_ms,o,h,l,c,v,fetched_at)]；下单顺序是 开/收/高/低。"""
    out: list[tuple] = []
    seen: set[str] = set()
    for item in raw:
        try:
            day_str = str(item[0])[:10]
            o = float(item[1])
            c = float(item[2])
            h = float(item[3])
            l = float(item[4])
            v = int(float(item[5] or 0))
        except (IndexError, TypeError, ValueError):
            continue
        if min(o, c, h, l) <= 0 or h < l:
            continue
        if o == h == l == c and v <= 0:      # 占位/停牌行
            continue
        if day_str in seen:
            continue
        seen.add(day_str)
        d = dt.datetime.strptime(day_str, "%Y-%m-%d").replace(
            hour=CLOSE_HOUR_HKT - 8, tzinfo=dt.timezone.utc)   # 08:00 UTC
        out.append((asset, "1d", int(d.timestamp() * 1000), o, h, l, c, v, int(time.time())))
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
    """kline_bars → ohlcv_daily（复用既有归一逻辑，只刷本次品种）。"""
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
    ap.add_argument("--symbols", default=",".join(f"{k}={v}" for k, v in DEFAULT_MAP.items()),
                    help="格式 ASSET=港股代码，多个用逗号")
    ap.add_argument("--bars", type=int, default=1000)
    args = ap.parse_args()

    pairs = [s.split("=") for s in args.symbols.split(",") if "=" in s]
    print(f"[INFO] 目标: {args.symbols}  {'(写入)' if args.apply else '(dry-run)'}")

    conn = sqlite3.connect(db.get_db_path(), timeout=30)
    conn.execute("PRAGMA busy_timeout=30000")
    total = 0
    try:
        for asset, code in pairs:
            asset = asset.strip().upper()
            try:
                rows = to_rows(asset, fetch(code.strip(), args.bars))
            except Exception as exc:
                print(f"  [FAIL] {asset}/{code}: {type(exc).__name__} {exc}")
                continue
            if not rows:
                print(f"  [FAIL] {asset}: 解析后 0 行")
                continue
            span = (f"{dt.datetime.fromtimestamp(rows[0][2] / 1000, dt.UTC):%Y-%m-%d} → "
                    f"{dt.datetime.fromtimestamp(rows[-1][2] / 1000, dt.UTC):%Y-%m-%d}")
            print(f"  [FETCH] {asset}: {len(rows)} 根 ({span})  "
                  f"末根 close={rows[-1][6]}")
            if args.apply:
                print(f"  [WRITE] {asset}: kline_bars 影响 {write_kline(conn, rows)} 行")
            total += len(rows)
        if args.apply:
            counts = refresh_ohlcv(conn, {a.strip().upper() for a, _ in pairs})
            print("[OK] ohlcv_daily: " + ", ".join(f"{k}={v}" for k, v in counts.items()))
        else:
            print(f"[DRY] 未写库，共 {total} 根")
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
