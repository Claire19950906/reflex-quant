#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
metals_daily.py —— 黄金/白银日线（COMEX 期货一手源重建）

背景：kline_bars 里 XAU/XAG 的 1d 数据被写坏了 ——
  161 个日期塞了 1,519 行，其中 1,344 行是同一个收盘价 4,139.28 从 2026-06
  一路复制到 2026-10，还夹着大量周六/周日的行。这种序列任何均线、波动率、
  回撤都算不对，等于假数据。

换成新浪全球期货（COMEX GC / SI）日线：
  2016-11-14 起，每天一根，o/h/l/c 都有；成交量和持仓量新浪给的是 0，
  所以 volume 一律写 NULL —— 缺失就标缺失，不拿 0 冒充。

用法：
  python3 metals_daily.py            # 默认最近 3000 根
  python3 metals_daily.py --full     # 全部（约 2,400 根）
  python3 metals_daily.py --dry-run  # 只看能拿到多少根，不写库
"""
import argparse
import json
import re
import sqlite3
import time
from datetime import datetime
from typing import Any, Dict, List, Optional

import db

URL = ("https://stock.finance.sina.com.cn/futures/api/jsonp.php/var%20_=/"
       "GlobalFuturesService.getGlobalFuturesDailyKLine")
# 新浪代码 -> 我们库里的标的
# GC=COMEX 黄金、SI=COMEX 白银、HG=COMEX 铜。
# 铜加进来不是因为要交易铜，而是要算"金铜比" —— 黄金对冲信用、铜代表实体需求，
# 这个比值是判断"避险在定价还是增长在定价"最省事的一根尺子。
METALS = {"GC": "XAU", "SI": "XAG", "HG": "HG"}
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36",
    "Referer": "https://finance.sina.com.cn/",
}


def fetch_bars(code: str) -> List[Dict[str, Any]]:
    """返回 [{ts, open, high, low, close}]，按时间升序。"""
    import requests
    # 本机有办公代理，requests 默认 trust_env=True 会走代理，这里必须关掉
    s = requests.Session()
    s.trust_env = False
    last_err = None
    for i in range(4):
        try:
            r = s.get(URL, params={"symbol": code}, headers=HEADERS, timeout=20)
            text = r.text
            # 回包是 jsonp：/*<script>...</script>*/ var _=([...])
            m = re.search(r"var\s+_=\s*\((\[.*\])\)", text, re.S)
            if not m:
                last_err = "回包里找不到 jsonp 数组"
                time.sleep(2)
                continue
            rows = json.loads(m.group(1))
            out = []
            for row in rows:
                d = str(row.get("date") or "")[:10]
                if not d:
                    continue
                dt = datetime.strptime(d, "%Y-%m-%d")
                if dt.weekday() >= 5:
                    continue          # 周末不该有 K 线，有就是脏的
                try:
                    out.append({
                        "ts": int(time.mktime(dt.timetuple())) * 1000,
                        "open": float(row["open"]),
                        "high": float(row["high"]),
                        "low": float(row["low"]),
                        "close": float(row["close"]),
                        "volume": None,   # 新浪期货日线不带成交量，如实留空
                    })
                except (KeyError, TypeError, ValueError):
                    continue
            out.sort(key=lambda x: x["ts"])
            return out
        except Exception as e:
            last_err = f"{type(e).__name__}: {e}"
            time.sleep(2)
    print(f"  [{code}] 拉取失败：{last_err}", flush=True)
    return []


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--full", action="store_true", help="全历史（约 2400 根）")
    ap.add_argument("--dry-run", action="store_true", help="只看不写")
    args, _ = ap.parse_known_args(argv)
    limit = 100000 if args.full else 3000

    conn = sqlite3.connect(db.get_db_path(), timeout=30, isolation_level=None)
    conn.execute("PRAGMA busy_timeout=30000")
    cur = conn.cursor()

    for code, asset in METALS.items():
        bars = fetch_bars(code)[-limit:]
        if not bars:
            print(f"  {asset} 没拿到数据，跳过（不写空序列）", flush=True)
            continue
        print(f"  {asset}（{code}）拿到 {len(bars)} 根："
              f"{datetime.fromtimestamp(bars[0]['ts'] / 1000):%Y-%m-%d} ~ "
              f"{datetime.fromtimestamp(bars[-1]['ts'] / 1000):%Y-%m-%d}", flush=True)
        if args.dry_run:
            continue
        # 先清掉这个标的的旧 1d（旧序列是坏的，不能和新数据混在一起）
        cur.execute("DELETE FROM kline_bars WHERE symbol=? AND timeframe='1d'", (asset,))
        for b in bars:
            cur.execute(
                """INSERT INTO kline_bars
                   (symbol, timeframe, ts, open, high, low, close, volume, fetched_at)
                   VALUES (?, '1d', ?, ?, ?, ?, ?, ?, ?)""",
                (asset, b["ts"], b["open"], b["high"], b["low"], b["close"],
                 b["volume"], int(time.time())))
        print(f"  {asset} 日线重建完成：{len(bars)} 根", flush=True)

    conn.commit()
    conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
