#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""backfill_csi300_valuation.py — 回填沪深300 PE（v467）

为什么
------
沪深300 一直 `unsupported`：库里只有个股级 `cn_equity_fundamentals`（9 行），没有任何指数级估值。
中证指数官网的 `perf/index-perf` 接口按日返回指数行情，字段里带 **`peg`（实测就是 PE，沪深300 ≈13.09）**，
这是官方口径，比自己算的加权 PE 可信。

注意
----
- 接口 `indexCode` 必填、`startDate/endDate` 必填（不填报 400 Parameter Errors）；
- 一次拉太长会被截断，按年循环拉；
- 字段名 `peg` 有歧义，但沪深300 取值 13 左右和 PE 量级一致，脚本里只取这一列并标口径。

用法
----
    python3 scripts/oneoff/backfill_csi300_valuation.py --dry-run
    python3 scripts/oneoff/backfill_csi300_valuation.py --apply
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import time
import urllib.request
from datetime import datetime
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[2]
DB = BACKEND / "trident_event_bus.db"
URL = ("https://www.csindex.com.cn/csindex-home/perf/index-perf"
       "?indexCode=000300&startDate={s}&endDate={e}")
UA = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7)",
      "Accept": "application/json"}
KEY = "csi300.pe"
KEY_CLOSE = "csi300.close"      # 同接口的收盘，用来反推"指数盈利(EPS)=点位/PE"


def fetch(url: str, tries: int = 4) -> dict:
    last: Exception | None = None
    for i in range(tries):
        try:
            req = urllib.request.Request(url, headers=UA)
            return json.loads(urllib.request.urlopen(req, timeout=25).read())
        except Exception as exc:  # noqa: BLE001
            last = exc
            time.sleep(2 * (i + 1))
    raise last  # type: ignore[misc]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--start-year", type=int, default=2019)
    args = ap.parse_args()
    if not (args.apply or args.dry_run):
        ap.print_help()
        return 2

    today = datetime.now()
    rows: list[tuple[int, str, float]] = []
    closes: list[tuple[int, str, float]] = []
    for year in range(args.start_year, today.year + 1):
        s, e = f"{year}0101", f"{year}{today.strftime('%m%d')}"
        try:
            payload = fetch(URL.format(s=s, e=e))
        except Exception as exc:  # noqa: BLE001
            print(f"  [失败] {year}: {type(exc).__name__}: {exc}")
            continue
        data = payload.get("data") or []
        got = 0
        for r in data:
            pe = r.get("peg")
            close = r.get("close")
            d = str(r.get("tradeDate") or "")
            if pe is None or len(d) != 8:
                continue
            try:
                ts = int(datetime.strptime(d, "%Y%m%d").replace(hour=12).timestamp())
                rows.append((ts, d, float(pe)))
                if close is not None:
                    closes.append((ts, d, float(close)))
                got += 1
            except Exception:  # noqa: BLE001
                continue
        print(f"  {year}: {got} 天")
    rows.sort()
    closes.sort()
    print(f"[收盘] {len(closes)} 天，最新 {closes[-1][1]} = {closes[-1][2]:.2f}" if closes else "[收盘] 无")
    if not rows:
        print("[done] 无数据")
        return 1
    print(f"[汇总] {len(rows)} 天，{rows[0][1]} → {rows[-1][1]}，最新 PE {rows[-1][2]:.2f}")

    conn = sqlite3.connect(str(DB), timeout=120)
    conn.execute("PRAGMA busy_timeout=120000")
    try:
        have = {r[0] for r in conn.execute(
            "SELECT DISTINCT date(ts,'unixepoch','localtime') FROM macro_snapshots "
            "WHERE metric_key=? AND value IS NOT NULL", (KEY,))}
        new = [(ts, v) for ts, d, v in rows if d[:4] + "-" + d[4:6] + "-" + d[6:] not in have]
        have_c = {r[0] for r in conn.execute(
            "SELECT DISTINCT date(ts,'unixepoch','localtime') FROM macro_snapshots "
            "WHERE metric_key=? AND value IS NOT NULL", (KEY_CLOSE,))}
        new_c = [(ts, v) for ts, d, v in closes if d[:4] + "-" + d[4:6] + "-" + d[6:] not in have_c]
        print(f"[去重] PE 库内 {len(have)}/新增 {len(new)} | 收盘 库内 {len(have_c)}/新增 {len(new_c)}")
        if args.apply and (new or new_c):
            payload = json.dumps({"index": "000300", "field": "peg(=PE)", "backfill": True},
                                 ensure_ascii=False)
            conn.executemany(
                "INSERT INTO macro_snapshots(ts,category,source,metric_key,value,unit,status,payload)"
                " VALUES(?,?,?,?,?,?,?,?)",
                [(ts, "valuation", "csindex", KEY, v, "x", "ok", payload) for ts, v in new]
                + [(ts, "index_price", "csindex", KEY_CLOSE, v, "pt", "ok", payload)
                   for ts, v in new_c])
            conn.commit()
            print(f"[done] 写库 {len(new) + len(new_c)} 行")
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
