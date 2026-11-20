#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""backfill_metal_prices.py — 回填白银 / 铜 / 黄金期货日线（v467）

为什么
------
银、铜此前一直 `unsupported`：库里没有它们的价格，`macro_snapshots` 的 structure 层也没有
XAG/COPPER 键，`ohlcv_daily`/`kline_bars` 里也没有这两个品种。结果是只能用 COT + 美元两族，
做出来会给人"有基本面"的错觉。

有了价格之后能算两条**相对估值**（这是银铜最缺的东西）：
  - 金银比 = 金价 / 银价：比值高位 = 白银相对黄金便宜 → 对白银偏多；
  - 铜金比 = 铜价 / 金价：比值高位 = 增长预期强 → 对铜偏多。
这两个都不是"价格动量"，是跨品种的相对定价，能进基本面分量的口径。

写库
----
`xag.usd`（COMEX 白银 SI=F）、`xcu.usd`（COMEX 铜 HG=F）、`xau.usd`（COMEX 黄金 GC=F），
category='metal'，source='yfinance'，只写日线收盘。

用法
----
    python3 scripts/oneoff/backfill_metal_prices.py --dry-run --years 5
    python3 scripts/oneoff/backfill_metal_prices.py --apply   --years 5
"""
from __future__ import annotations

import argparse
import json
import time
import sqlite3
from datetime import datetime, timedelta
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[2]
DB = BACKEND / "trident_event_bus.db"

TICKERS = (("SI=F", "xag.usd"), ("HG=F", "xcu.usd"), ("GC=F", "xau.usd"))


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--years", type=int, default=5)
    args = ap.parse_args()
    if not (args.apply or args.dry_run):
        ap.print_help()
        return 2

    import yfinance as yf  # 只在这个一次性脚本里用，决策热路径不依赖它

    conn = sqlite3.connect(str(DB), timeout=120)
    conn.execute("PRAGMA busy_timeout=120000")
    written = 0
    try:
        for ticker, key in TICKERS:
            bars = None
            for attempt in range(4):          # yfinance 的 SSL 会间歇断，5y 窗口偶发空
                try:
                    bars = yf.Ticker(ticker).history(period=f"{args.years}y", interval="1d")
                    if bars is not None and len(bars):
                        break
                except Exception as exc:  # noqa: BLE001
                    if attempt == 3:
                        print(f"  [失败] {ticker}: {type(exc).__name__}: {exc}")
                    time.sleep(3)
            if bars is None or len(bars) == 0:
                print(f"  [空]   {ticker}")
                continue
            have = {r[0] for r in conn.execute(
                "SELECT DISTINCT date(ts,'unixepoch','localtime') FROM macro_snapshots "
                "WHERE metric_key=? AND value IS NOT NULL", (key,))}
            rows = []
            for idx, row in bars.iterrows():
                day = idx.strftime("%Y-%m-%d")
                close = row.get("Close")
                if close is None or day in have:
                    continue
                ts = int(datetime.strptime(day, "%Y-%m-%d").replace(hour=12).timestamp())
                rows.append((ts, key, float(close)))
            print(f"  {ticker:6s} → {key:9s} 抓 {len(bars):5d} 天 | 库内 {len(have):4d} | "
                  f"新增 {len(rows):5d} | 最新 {rows[-1][2]:.2f}" if rows else
                  f"  {ticker:6s} → {key:9s} 抓 {len(bars):5d} 天 | 无新增")
            if not args.apply or not rows:
                continue
            payload = json.dumps({"ticker": ticker, "backfill": True}, ensure_ascii=False)
            conn.executemany(
                "INSERT INTO macro_snapshots(ts,category,source,metric_key,value,unit,status,payload)"
                " VALUES(?,?,?,?,?,?,?,?)",
                [(ts, "metal", "yfinance", k, v, "USD", "ok", payload) for ts, k, v in rows])
            conn.commit()
            written += len(rows)
    finally:
        conn.close()
    print(f"[done] 写库 {written} 行" + ("" if args.apply else "（dry-run，未写库）"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
