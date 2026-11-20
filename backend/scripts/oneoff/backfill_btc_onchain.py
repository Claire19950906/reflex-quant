#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""backfill_btc_onchain.py — 回填 BTC 链上活跃度（v468）

为什么
------
加密一直缺"贵不贵"的估值锚（MVRV / 已实现市值 / 交易所余额都要付费）。
但**网络使用度是免费可得的真数据**：blockchain.info 的 charts 接口给日频的
"每日活跃地址数"和"每日交易笔数"，两年窗口一次能拉 700+ 个观测。
它不是估值，但它回答一个价格答不了的问题：链上到底还有没有人在用。

写库
----
`btc.onchain_addr`（每日活跃地址）、`btc.onchain_tx`（每日交易笔数），
category='onchain'，source='blockchain.info'。

坑
--
- 该域 SSL 会间歇 UNEXPECTED_EOF，必须多次重试（脚本 6 次，递增退避）；
- 返回的 x 是 UNIX 秒（UTC 零点），落到本地日期再按天去重；
- `timespan=2years` 是滚动窗口，重复跑会重叠，靠按天去重保证不重复写。

用法
----
    python3 scripts/oneoff/backfill_btc_onchain.py --dry-run
    python3 scripts/oneoff/backfill_btc_onchain.py --apply
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import time
import urllib.request
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[2]
DB = BACKEND / "trident_event_bus.db"
BASE = "https://api.blockchain.info/charts/{chart}?timespan=2years&format=json"
UA = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7)"}

CHARTS = (("n-unique-addresses", "btc.onchain_addr"),
          ("n-transactions", "btc.onchain_tx"))


def fetch(chart: str, tries: int = 6) -> dict:
    last: Exception | None = None
    for i in range(tries):
        try:
            req = urllib.request.Request(BASE.format(chart=chart), headers=UA)
            return json.loads(urllib.request.urlopen(req, timeout=30).read())
        except Exception as exc:  # noqa: BLE001
            last = exc
            time.sleep(2 * (i + 1))
    raise last  # type: ignore[misc]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()
    if not (args.apply or args.dry_run):
        ap.print_help()
        return 2

    conn = sqlite3.connect(str(DB), timeout=120)
    conn.execute("PRAGMA busy_timeout=120000")
    total = 0
    try:
        for chart, key in CHARTS:
            try:
                payload = fetch(chart)
            except Exception as exc:  # noqa: BLE001
                print(f"  [失败] {chart}: {type(exc).__name__}: {exc}")
                continue
            points = payload.get("values") or []
            have = {r[0] for r in conn.execute(
                "SELECT DISTINCT date(ts,'unixepoch','localtime') FROM macro_snapshots "
                "WHERE metric_key=? AND value IS NOT NULL", (key,))}
            rows = []
            for pt in points:
                try:
                    ts, val = int(pt["x"]), float(pt["y"])
                except (KeyError, TypeError, ValueError):
                    continue
                day = time.strftime("%Y-%m-%d", time.localtime(ts))
                if day in have:
                    continue
                rows.append((ts, key, val))
            print(f"  {chart:20s} → {key:18s} 抓 {len(points):4d} | 库内 {len(have):4d} | "
                  f"新增 {len(rows):4d}" + (f" | 最新 {rows[-1][2]:,.0f}" if rows else ""))
            if not args.apply or not rows:
                continue
            blob = json.dumps({"chart": chart, "source": "blockchain.info",
                               "backfill": True}, ensure_ascii=False)
            conn.executemany(
                "INSERT INTO macro_snapshots(ts,category,source,metric_key,value,unit,status,payload)"
                " VALUES(?,?,?,?,?,?,?,?)",
                [(ts, "onchain", "blockchain.info", k, v, "count", "ok", blob)
                 for ts, k, v in rows])
            conn.commit()
            total += len(rows)
    finally:
        conn.close()
    print(f"[done] 写库 {total} 行" + ("" if args.apply else "（dry-run，未写库）"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
