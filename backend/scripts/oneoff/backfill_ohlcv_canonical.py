#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
backfill_ohlcv_canonical.py — 补齐**规范代码**日线到 ohlcv_daily (v314 P0)

为什么需要这个脚本
------------------
`ohlcv_daily` 里规范代码 (BTC/ETH/SOL/XAU/XAG/WTI/...) 的日线停在 2026-10-21,
之后 bridge 的刷新循环写的是 "SOL-USD" 这类 Yahoo 原始代码 —— 消费方读不到。
直接后果: 因果链评估有 16 条 PENDING 超过截止时间仍未评估 (验收 #6
"超期未评估" = 16), 因为 T+5 那一根本来就不存在。

本脚本做两件事
--------------
1. 用 **yfinance** 按规范代码 → Yahoo symbol 映射 (quote_sources.
   YAHOO_SYMBOL_FOR_ASSET) 拉日线, 落库用规范代码 (与刷新循环同口径);
2. yfinance 拿不到时, 用**本地 kline_bars 的 1d** 兜底 —— kline_bars 由
   行情链路持续更新 (实测更新到 2026-10-28), 比 ohlcv_daily 新。

安全约束
--------
* 只 INSERT (UNIQUE(asset,ts) + INSERT OR IGNORE) —— 不删不改既有行;
* 非法 OHLC (close<=0 / high<low) 直接丢弃, 绝不写入;
* --dry-run 只报告不落库。

用法
----
    python3 backfill_ohlcv_canonical.py --dry-run
    python3 backfill_ohlcv_canonical.py
"""
from __future__ import annotations

import argparse
import os
import sqlite3
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src_python"))

BACKEND = Path(__file__).resolve().parents[2]
DB_PATH = str(BACKEND / "trident_event_bus.db")

# 与 bridge_server.OHLCV_ASSETS 同口径 (规范代码)
CANONICAL_ASSETS = [
    "BTC", "ETH", "SOL", "XAU", "XAG", "WTI",
    "MU", "000660.KS", "NVDA", "TSLA",
]
PERIOD = "1mo"


def _yahoo_symbol(asset: str) -> str:
    try:
        from feeds import quote_sources as qs
        return qs.YAHOO_SYMBOL_FOR_ASSET.get(asset.upper(), asset)
    except Exception:
        return asset


def fetch_yfinance(asset: str):
    """→ [(ts, o, h, l, c, v)] 或 []"""
    sym = _yahoo_symbol(asset)
    try:
        import yfinance as yf
        import yf_guard as _yfg; _yfg.disable()
        df = yf.Ticker(sym).history(period=PERIOD, auto_adjust=False)
    except Exception as e:
        print(f"  [yfinance] {asset}({sym}) 失败: {type(e).__name__}: {str(e)[:80]}")
        return []
    if df is None or len(df) == 0:
        print(f"  [yfinance] {asset}({sym}) 空")
        return []
    out = []
    for idx, row in df.iterrows():
        try:
            out.append((int(idx.timestamp()), float(row["Open"]), float(row["High"]),
                        float(row["Low"]), float(row["Close"]),
                        int(row["Volume"]) if row["Volume"] > 0 else 0))
        except Exception:
            continue
    return out


def fetch_local_kline(asset: str):
    """本地 kline_bars 1d → [(ts, o, h, l, c, v)] (毫秒→秒)"""
    import json
    db = sqlite3.connect("file:%s?mode=ro" % DB_PATH, uri=True)
    try:
        rows = db.execute(
            "SELECT ts, open, high, low, close, volume FROM kline_bars "
            "WHERE symbol=? AND timeframe='1d' ORDER BY ts", (asset,)).fetchall()
    except sqlite3.Error:
        rows = []
    finally:
        db.close()
    out = []
    for ts, o, h, l, c, v in rows:
        ts_s = int(ts) // 1000 if int(ts) > 100_000_000_000 else int(ts)
        try:
            out.append((ts_s, float(o), float(h), float(l), float(c), int(v or 0)))
        except Exception:
            continue
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true", help="只报告不写库")
    args = ap.parse_args()

    if not os.path.exists(DB_PATH):
        print("[FATAL] DB 不存在: %s" % DB_PATH, file=sys.stderr)
        return 1
    conn = sqlite3.connect(DB_PATH, timeout=60)
    conn.execute("PRAGMA journal_mode=WAL")
    total_new = 0
    skipped_days = 0
    print("=== 规范代码日线补齐 (dry_run=%s) ===" % args.dry_run)
    for asset in CANONICAL_ASSETS:
        rows = fetch_yfinance(asset)
        src = "yfinance_refresh"
        if not rows:
            rows = fetch_local_kline(asset)
            src = "kline_bars_1d"
        if not rows:
            print("  %-10s 无数据" % asset)
            continue
        # 只保留合法 OHLC
        good = [r for r in rows
                if r[4] > 0 and r[2] > 0 and r[3] > 0 and r[1] > 0 and r[2] >= r[3]]
        # v314: **同资产同一天只允许一行**。
        # ohlcv_daily 的 UNIQUE(asset,ts) 挡不住"同一天不同 ts" (yfinance 日线的
        # ts 是 12:00 本地时, kline_bars_1d 是 00:00 本地时) → 实测 XAU/WTI/MU
        # 出现大量同日双行, 且 close 不同 (WTI 同日 yfinance 103.66 vs kline 110.01,
        # 差 6%)。这会让 T0→T+5 的涨跌幅度出现伪跳空, 也会让"交易日计数"错位。
        # 这里按日历日去重: 该资产已有行的日子**不再写入**。
        existing_days = set()
        for (ts,) in conn.execute(
                "SELECT ts FROM ohlcv_daily WHERE asset=?", (asset,)).fetchall():
            existing_days.add(time.strftime("%Y-%m-%d", time.localtime(int(ts))))
        to_write = []
        for r in good:
            day = time.strftime("%Y-%m-%d", time.localtime(r[0]))
            if day in existing_days:
                skipped_days += 1
                continue
            existing_days.add(day)
            to_write.append(r)
        latest = max(r[0] for r in good) if good else 0
        newest = time.strftime("%Y-%m-%d", time.localtime(latest)) if latest else "-"
        if not args.dry_run and to_write:
            conn.executemany(
                "INSERT OR IGNORE INTO ohlcv_daily "
                "(asset, ts, open, high, low, close, volume, source) VALUES (?,?,?,?,?,?,?,?)",
                [(asset, r[0], r[1], r[2], r[3], r[4], r[5], src) for r in to_write])
        total_new += len(to_write)
        print("  %-10s src=%-16s fetched=%-4d new=%-4d skipped_days=%-4d newest=%s"
              % (asset, src, len(good), len(to_write), skipped_days, newest))
    if not args.dry_run:
        conn.commit()
        conn.close()
    print("[OK] 新增 %d 行 (跳过已存在日 %d)%s"
          % (total_new, skipped_days, " (dry-run 未落库)" if args.dry_run else ""))
    return 0


if __name__ == "__main__":
    sys.exit(main())
