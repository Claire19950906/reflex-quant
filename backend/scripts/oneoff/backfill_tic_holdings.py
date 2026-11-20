#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""backfill_tic_holdings.py — 回填美债外资持仓总量（TIC，v467）

为什么
------
美债的硬缺口里"TIC 外资持仓"一直没接：财政部 TIC 数据不在 fiscaldata（那个端点在
`v1/accounting/od/tic` 上 TLS 直接断），但 **`ticdata.treasury.gov/Publish/mfhhis01.txt`** 是
纯文本、可以直接抓。里面是"主要外国持有人"的月度持仓（十亿美元），每个年度块最后一行
`Grand Total` 就是当月外资持有美债总量。

口径与符号
----------
外资持仓总量下降 = 边际买盘撤退 = 对美债价格偏空。所以分量是
"近 12 个月变化"的反向，且只在数据足够新（≤75 天）时才发分 —— TIC 天然滞后两个月，
再叠加文件本身更新慢，太旧的数据不能假装是当下的需求。

用法
----
    python3 scripts/oneoff/backfill_tic_holdings.py --dry-run
    python3 scripts/oneoff/backfill_tic_holdings.py --apply
"""
from __future__ import annotations

import argparse
import json
import re
import sqlite3
import time
import urllib.request
from datetime import datetime
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[2]
DB = BACKEND / "trident_event_bus.db"
URL = "https://ticdata.treasury.gov/Publish/mfhhis01.txt"
UA = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7)"}
KEY = "ust.tic_foreign_holdings"
MONTHS = {m: i + 1 for i, m in enumerate(
    ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"])}


def fetch(url: str, tries: int = 4) -> str:
    last: Exception | None = None
    for i in range(tries):
        try:
            req = urllib.request.Request(url, headers=UA)
            return urllib.request.urlopen(req, timeout=25).read().decode("utf-8", "replace")
        except Exception as exc:  # noqa: BLE001
            last = exc
            time.sleep(2 * (i + 1))
    raise last  # type: ignore[misc]


def parse(text: str) -> list[tuple[str, float]]:
    """把年度块解析成 [(YYYY-MM, 总量十亿美元)]。

    结构（实测）：
        \tDec\tNov\t...\tJan
        Country\t2025\t2025\t...\t2025
        ...
        Grand Total\t9269.5\t9349.6\t...
    月份行在前、年份行紧跟，Grand Total 行的第 N 列对应第 N 个月。
    """
    out: list[tuple[str, float]] = []
    months: list[int] | None = None
    years: list[int] | None = None
    for line in text.splitlines():
        cells = line.replace("\r", "").split("\t")
        first = cells[0].strip()
        if first == "Country":
            years = []
            for c in cells[1:]:
                c = c.strip()
                years.append(int(c) if re.fullmatch(r"(19|20)\d\d", c) else 0)
            continue
        if first == "" and years is None:
            cand = [c.strip() for c in cells if c.strip()]
            if cand and all(re.fullmatch(r"[A-Z][a-z]{2}", c) for c in cand):
                months = [MONTHS[c] for c in cand]
            continue
        if first == "Grand Total" and months and years:
            vals = [c.strip() for c in cells[1:] if c.strip()]
            for i, v in enumerate(vals):
                if i >= len(months) or i >= len(years) or not years[i]:
                    continue
                try:
                    month, year = months[i], years[i]
                    out.append((f"{year:04d}-{month:02d}-01", float(v.replace(",", ""))))
                except ValueError:
                    continue
            months = years = None
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()
    if not (args.apply or args.dry_run):
        ap.print_help()
        return 2

    text = fetch(URL)
    rows = parse(text)
    if not rows:
        print("[failed] 解析 0 行，格式可能变了")
        return 1
    rows.sort()
    print(f"[抓取] {len(rows)} 个月，{rows[0][0]} = {rows[0][1]:.1f} → "
          f"{rows[-1][0]} = {rows[-1][1]:.1f}（十亿美元）")

    conn = sqlite3.connect(str(DB), timeout=120)
    conn.execute("PRAGMA busy_timeout=120000")
    try:
        have = {r[0] for r in conn.execute(
            "SELECT DISTINCT date(ts,'unixepoch','localtime') FROM macro_snapshots "
            "WHERE metric_key=? AND value IS NOT NULL", (KEY,))}
        new = []
        for d, v in rows:
            if d not in have:
                new.append((int(datetime.strptime(d, "%Y-%m-%d").replace(hour=12, day=15)
                                .timestamp()), v))
        print(f"[去重] 库内 {len(have)} | 新增 {len(new)}")
        if args.apply and new:
            payload = json.dumps({"source": "mfhhis01.txt", "unit": "USD bn",
                                  "backfill": True}, ensure_ascii=False)
            conn.executemany(
                "INSERT INTO macro_snapshots(ts,category,source,metric_key,value,unit,status,payload)"
                " VALUES(?,?,?,?,?,?,?,?)",
                [(ts, "tic", "treasury_tic", KEY, v, "USD bn", "ok", payload)
                 for ts, v in new])
            conn.commit()
            print(f"[done] 写库 {len(new)} 行")
        else:
            print("[done] dry-run，未写库")
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
