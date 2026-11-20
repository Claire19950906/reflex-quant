#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""backfill_btc_etf_flow.py — 回填美国 BTC 现货 ETF 每日净流入（v469）

为什么
------
加密缺的"增量资金"这一层，ETF 净流出入是免费可得的最硬证据：
它是真金白银申购/赎回，不是交易所内部换手。farside.co.uk 把 11 只美国现货 ETF
（IBIT/FBTC/BITB/ARKB/BTCO/EZBC/BRRR/HODL/BTCW/MSBT/GBTC/BTC）逐日净流入汇总成一张表。

源
--
- 首页 `https://farside.co.uk/btc/` 只给最近 ~14 个交易日；
- 全量 `https://farside.co.uk/bitcoin-etf-flow-all-data/` 给 2024-01-11 起的全部（实测 733 个交易日）。

坑
--
- 该域 SSL 间歇 UNEXPECTED_EOF，必须重试（脚本 6 次递增退避）；
- 表里负数写成 `(405.3)`，要转换；千位有逗号；
- "Total" 列是当日所有 ETF 合计，位置不固定（有的行最后一格是空），
  所以取"该行最后一个能解析成数字的格子"；
- 日期是 `02 Nov 2026` 这种格式，月份是英文缩写。

写库
----
`btc.etf_net_flow`（单位：百万美元/日），category='etf_flow'，source='farside'。

用法
----
    python3 scripts/oneoff/backfill_btc_etf_flow.py --dry-run
    python3 scripts/oneoff/backfill_btc_etf_flow.py --apply
"""
from __future__ import annotations

import argparse
import html as H
import json
import re
import sqlite3
import time
import urllib.request
from datetime import datetime
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[2]
DB = BACKEND / "trident_event_bus.db"
URL = "https://farside.co.uk/bitcoin-etf-flow-all-data/"
KEY = "btc.etf_net_flow"
UA = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7)"}
DATE_RE = re.compile(r"(\d{1,2}) ([A-Z][a-z]{2}) (\d{4})")


def fetch(url: str = URL, tries: int = 6) -> str:
    last: Exception | None = None
    for i in range(tries):
        try:
            req = urllib.request.Request(url, headers=UA)
            return urllib.request.urlopen(req, timeout=30).read().decode("utf-8", "replace")
        except Exception as exc:  # noqa: BLE001
            last = exc
            time.sleep(2 * (i + 1))
    raise last  # type: ignore[misc]


def num(cell: str) -> float | None:
    """`(405.3)` → −405.3；`1,373.8` → 1373.8；空/`-` → None。"""
    t = cell.replace(",", "").replace("\u00a0", " ").strip()
    if not t or t in {"-", "—", "N/A"}:
        return None
    neg = t.startswith("(") and t.endswith(")")
    t = t.strip("()")
    try:
        v = float(t)
    except ValueError:
        return None
    return -v if neg else v


def parse(page: str) -> list[tuple[str, float]]:
    m = re.search(r'<table class="etf"(.*?)</table>', page, re.S)
    if not m:
        return []
    rows = re.findall(r"<tr[^>]*>(.*?)</tr>", m.group(1), re.S)
    out: list[tuple[str, float]] = []
    for row in rows:
        cells = [H.unescape(re.sub(r"<[^>]+>", "", c)).strip()
                 for c in re.findall(r"<t[dh][^>]*>(.*?)</t[dh]>", row, re.S)]
        if not cells or not DATE_RE.fullmatch(cells[0].strip()):
            continue
        dm = DATE_RE.fullmatch(cells[0].strip())
        day = datetime.strptime(f"{dm.group(1)} {dm.group(2)} {dm.group(3)}", "%d %b %Y")
        total = None
        for c in reversed(cells[1:]):          # 取最后一个可解析数字 = Total 列
            v = num(c)
            if v is not None:
                total = v
                break
        if total is None:
            continue
        out.append((day.strftime("%Y-%m-%d"), total))
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()
    if not (args.apply or args.dry_run):
        ap.print_help()
        return 2

    rows = parse(fetch())
    if not rows:
        print("[failed] 解析 0 行（表格结构可能变了）")
        return 1
    rows.sort()
    print(f"[抓取] {len(rows)} 个交易日，{rows[0][0]}（{rows[0][1]:+.1f}）→ "
          f"{rows[-1][0]}（{rows[-1][1]:+.1f}）百万美元")

    conn = sqlite3.connect(str(DB), timeout=120)
    conn.execute("PRAGMA busy_timeout=120000")
    try:
        have = {r[0] for r in conn.execute(
            "SELECT DISTINCT date(ts,'unixepoch','localtime') FROM macro_snapshots "
            "WHERE metric_key=? AND value IS NOT NULL", (KEY,))}
        new = [(d, v) for d, v in rows if d not in have]
        print(f"[去重] 库内 {len(have)} | 新增 {len(new)}")
        if args.apply and new:
            blob = json.dumps({"source": "farside.co.uk", "unit": "USD m/day",
                               "scope": "US spot BTC ETFs total net flow", "backfill": True},
                              ensure_ascii=False)
            conn.executemany(
                "INSERT INTO macro_snapshots(ts,category,source,metric_key,value,unit,status,payload)"
                " VALUES(?,?,?,?,?,?,?,?)",
                [(int(datetime.strptime(d, "%Y-%m-%d").replace(hour=22).timestamp()),
                  "etf_flow", "farside", KEY, v, "USD m", "ok", blob) for d, v in new])
            conn.commit()
            print(f"[done] 写库 {len(new)} 行")
        else:
            print("[done] dry-run / 无新增")
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
