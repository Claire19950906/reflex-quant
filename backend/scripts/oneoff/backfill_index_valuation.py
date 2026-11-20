#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""backfill_index_valuation.py — 回填标普 500 的 CAPE（席勒市盈率）

为什么
------
v460 审计时发现库里**没有任何指数级估值**：`macro_snapshots` 没有 cape / pe_index /
earnings_yield 任何键，`equity_fundamentals` 只有个股的 pe/pb/roe。所以 SPX/NDX/沪深300
在 `decision_guard` 里一律「基本面不可得」，0.08 权重让出。

CAPE 是最经典的一条指数估值锚（周期调整市盈率，抹掉盈利周期波动），multpl.com 有按月
的完整历史表（1871 起）。抓下来存成 `spx.cape`，就能算"当前 38.63 在近 30 年里是贵还是便宜"。

注意：CAPE 只覆盖 S&P 500。NDX / 沪深300 没有公开的同类口径，本脚本**不造假**。

用法
----
    python3 scripts/oneoff/backfill_index_valuation.py --dry-run
    python3 scripts/oneoff/backfill_index_valuation.py --apply
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
URL = "https://www.multpl.com/shiller-pe/table/by-month"
_ROW = re.compile(
    r"<td[^>]*>\s*([A-Z][a-z]{2} \d{1,2}, \d{4})\s*</td>\s*<td[^>]*>\s*(?:[&#x0-9;]*;)?\s*([\d.]+)",
    re.S)


def _fetch(tries: int = 6) -> list[tuple[str, float]]:
    last = None
    for i in range(tries):
        try:
            req = urllib.request.Request(
                URL, headers={"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7)"})
            html = urllib.request.urlopen(req, timeout=25).read().decode("utf-8", "replace")
            rows = [(d, float(v)) for d, v in _ROW.findall(html)]
            if not rows:
                raise ValueError("解析到 0 行（页面结构变了或被拦）")
            return rows
        except Exception as exc:  # noqa: BLE001
            last = exc
            time.sleep(2.0 * (i + 1))
    raise RuntimeError(f"{type(last).__name__}: {last}")


def _ts(day: str) -> int:
    return int(datetime.strptime(day, "%b %d, %Y").replace(hour=12).timestamp())


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()
    if not (args.apply or args.dry_run):
        ap.print_help()
        return 2

    try:
        rows = _fetch()
    except Exception as exc:  # noqa: BLE001
        print(f"[失败] {exc}")
        return 1
    rows.sort(key=lambda r: _ts(r[0]))
    print(f"抓 {len(rows)} 个观测 | 最早 {rows[0]} | 最新 {rows[-1]}")

    conn = sqlite3.connect(str(DB), timeout=120)
    conn.execute("PRAGMA busy_timeout=120000")
    written = 0
    try:
        have = {r[0] for r in conn.execute(
            "SELECT DISTINCT date(ts,'unixepoch','localtime') FROM macro_snapshots "
            "WHERE metric_key='spx.cape' AND value IS NOT NULL")}
        new = [(d, v) for d, v in rows if datetime.strptime(d, "%b %d, %Y").strftime("%Y-%m-%d") not in have]
        print(f"库内已有 {len(have)} 天，新增 {len(new)} 个观测")
        if args.apply and new:
            payload = json.dumps({"source": "multpl.com/shiller-pe", "backfill": True},
                                 ensure_ascii=False)
            conn.executemany(
                "INSERT INTO macro_snapshots(ts,category,source,metric_key,value,unit,status,payload)"
                " VALUES(?,?,?,?,?,?,?,?)",
                [(_ts(d), "valuation", "multpl", "spx.cape", v, "x", "ok", payload) for d, v in new])
            conn.commit()
            written = len(new)
    finally:
        conn.close()
    print(f"[done] 写库 {written} 行" + ("" if args.apply else "（dry-run，未写库）"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
