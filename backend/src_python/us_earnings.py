#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
us_earnings.py —— 美股财报日历（Nasdaq 官方公开接口，免费、免 key）

为什么加：
  对做美股的人来说，财报是唯一会让"技术面突然失效"的东西 —— 一根跳空就能把止损打穿。
  这个源不谈方向，只回答"今晚到未来一周有几家公司在报、我盯的标的有没有在里面"。

产出两条序列（asset='US_EQ'，source='nasdaq_calendar'）：
  us_earnings_count_7d   未来 7 个自然日的财报家数
  us_earnings_watch_7d   其中属于关注标的（AAPL/MSFT/NVDA/TSLA/AMD/MU/META/AMZN/GOOGL）的家数

接口注意：Nasdaq 的 api.nasdaq.com 要求浏览器 UA，缺了会返 200 但 body 是错误页。
"""
import argparse
import json
import subprocess
import time
from datetime import datetime, timedelta
from typing import Dict, List, Optional

import external_data

BASE = "https://api.nasdaq.com/api/calendar/earnings?date={date}"
SOURCE = "nasdaq_calendar"
ASSET = "US_EQ"
WATCH = {"AAPL", "MSFT", "NVDA", "TSLA", "AMD", "MU", "META", "AMZN", "GOOGL"}


def fetch_day(date: str, tries: int = 3) -> Optional[List[dict]]:
    url = BASE.format(date=date)
    cmd = ["curl", "-s", "-m", "25", "--noproxy", "*", "--compressed",
           "-H", "User-Agent: Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36",
           "-H", "Accept: application/json, text/plain, */*",
           "-H", "Origin: https://www.nasdaq.com", "-H", "Referer: https://www.nasdaq.com/",
           url]
    for i in range(tries):
        try:
            out = subprocess.run(cmd, capture_output=True, text=True, timeout=40)
            if out.stdout.strip():
                payload = json.loads(out.stdout)
                rows = (payload.get("data") or {}).get("rows") or []
                return rows
        except Exception:
            pass
        time.sleep(2 * (i + 1))
    return None


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=7)
    ap.add_argument("--dry-run", action="store_true")
    args, _ = ap.parse_known_args(argv)

    today = datetime.now().date()
    total_count = 0
    watch_hits: Dict[str, str] = {}
    ok_days = 0

    for k in range(args.days):
        d = (today + timedelta(days=k)).isoformat()
        rows = fetch_day(d)
        if rows is None:
            print(f"  {d} 取不到（不按 0 处理）")
            continue
        ok_days += 1
        total_count += len(rows)
        for r in rows:
            sym = (r.get("symbol") or "").strip().upper()
            if sym in WATCH:
                watch_hits[sym] = f"{d} {r.get('time') or ''}".strip()
        time.sleep(0.6)                          # 官方接口别打太快

    if ok_days == 0:
        print("[FATAL] 一天都没拿到，本次不写库")
        return 1

    ts = int(time.mktime(datetime(today.year, today.month, today.day).timetuple())) * 1000
    print(f"  未来 {args.days} 天财报家数：{total_count}（{ok_days} 天有数据）")
    if watch_hits:
        print("  关注标的里有财报：" + "；".join(f"{k} {v}" for k, v in sorted(watch_hits.items())))
    else:
        print("  关注标的未来一周没有财报")

    if not args.dry_run:
        external_data.ingest_external(
            asset=ASSET, indicator="us_earnings_count_7d",
            records=[{"ts": ts, "value": float(total_count)}], source=SOURCE,
            meta={"window": f"{args.days}d", "days_with_data": ok_days})
        external_data.ingest_external(
            asset=ASSET, indicator="us_earnings_watch_7d",
            records=[{"ts": ts, "value": float(len(watch_hits))}], source=SOURCE,
            meta={"window": f"{args.days}d", "symbols": sorted(watch_hits)})
        print("  已写入 external_indicators（source=nasdaq_calendar）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
