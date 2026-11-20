#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""资金费率历史补齐（v425）：把 Binance 交易所自己的 8 小时费率历史拉进库。

为什么非做不可
--------------
分析页 `/edge` 与影子账本都靠 "极端资金费率 → 反向" 来判优势。但：
  · 老表 flow_snapshots 的写入方 market_flow.py 已经不在仓库里，11-17 起全线停更；
  · 活得数据 external_indicators 里只有 5 天（11-13 才开始采）。
5 天 = 15 个 8 小时点，连分位阈值都标不出来 —— 也就是说此前那些"ETH 做空有优势"的结论，
样本窗口窄得离谱。

交易所自己保留完整历史：`GET /fapi/v1/fundingRate?symbol=X&limit=1000` 一次给 1000 个
8 小时结算点 ≈ 333 天。这是公开市场数据，不是"补停机期内部积压"，可以直接补。

写入：external_indicators，indicator='bn_funding_rate'（与实时采集同一条指标，同一条管道），
source='binance:fundingRate:backfill'。ts 用交易所给的结算时刻（秒）。
用 (asset, indicator, ts) 去重插入，重复跑不会写重。

用法：
  python3 backfill_funding.py --days 330            # 补 330 天
  python3 backfill_funding.py --days 330 --dry-run  # 只打印，不写
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
import time
import urllib.parse
import urllib.request
from typing import Any, Dict, List, Optional

import db

SYMBOLS = (("BTC", "BTCUSDT"), ("ETH", "ETHUSDT"), ("SOL", "SOLUSDT"))
API = "https://fapi.binance.com/fapi/v1/fundingRate"
INDICATOR = "bn_funding_rate"
SOURCE = "binance:fundingRate:backfill"
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/120.0 Safari/537.36",
    "Accept": "application/json",
}


def _get(url: str, tries: int = 3, timeout: float = 15.0) -> Any:
    last: Optional[Exception] = None
    for i in range(tries):
        try:
            req = urllib.request.Request(url, headers=HEADERS)
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return json.loads(r.read().decode("utf-8"))
        except Exception as e:  # 网络抖动/被墙都会落这里，退避重试
            last = e
            time.sleep(1.5 * (i + 1))
    raise RuntimeError(f"GET 失败 {url}: {last}")


def fetch(symbol: str, days: int) -> List[Dict[str, Any]]:
    """分页拉：从 days 天前拉到现在，8 小时一格。"""
    start = int(time.time()) - days * 86_400
    rows: List[Dict[str, Any]] = []
    cursor = start
    while True:
        q = urllib.parse.urlencode({"symbol": symbol, "limit": 1000, "startTime": cursor * 1000})
        batch = _get(f"{API}?{q}")
        if not isinstance(batch, list) or not batch:
            break
        rows.extend(batch)
        newest = int(batch[-1]["fundingTime"]) // 1000
        if newest <= cursor:
            break
        cursor = newest + 1
        if newest >= int(time.time()) - 8 * 3_600:
            break
    return rows


def store(conn: sqlite3.Connection, asset: str, rows: List[Dict[str, Any]],
          dry_run: bool = False) -> int:
    now = int(time.time())
    n = 0
    for r in rows:
        ts = int(r["fundingTime"]) // 1000
        v = float(r["fundingRate"])
        if dry_run:
            n += 1
            continue
        # 去重靠 (asset, indicator, ts)：external_indicators 没有唯一约束，
        # 所以先查后插，避免重复跑把同一时刻写成两行（分位统计会被重复值污染）。
        cur = conn.execute(
            "SELECT 1 FROM external_indicators WHERE asset=? AND indicator=? AND ts=? LIMIT 1",
            (asset, INDICATOR, ts),
        )
        if cur.fetchone():
            cur.close()
            continue
        cur.close()
        conn.execute(
            "INSERT INTO external_indicators(asset, indicator, value, ts, source, ingested_at) "
            "VALUES(?,?,?,?,?,?)", (asset, INDICATOR, v, ts, SOURCE, now),
        )
        n += 1
    if not dry_run:
        conn.commit()
    return n


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="补资金费率历史")
    ap.add_argument("--days", type=int, default=330)
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args(argv)
    conn = db.get_connection()
    total = 0
    try:
        for asset, symbol in SYMBOLS:
            try:
                rows = fetch(symbol, a.days)
            except Exception as e:
                print(f"{asset}: 拉取失败 {e}")
                continue
            n = store(conn, asset, rows, dry_run=a.dry_run)
            total += n
            if rows:
                print(f"{asset}: 拉到 {len(rows)} 点（{len(rows)*8} 小时），新增 {n}")
            else:
                print(f"{asset}: 拉到 0 点")
    finally:
        conn.close()
    print(f"合计新增 {total} 点")
    return 0


if __name__ == "__main__":
    sys.exit(main())
