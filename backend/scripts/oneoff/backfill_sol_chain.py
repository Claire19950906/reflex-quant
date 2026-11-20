#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""backfill_sol_chain.py — 回填 Solana 链上经济数据（v470）

为什么
------
SOL 之前只有 COT 一族（期货持仓），按 v460 立的规矩"独立数据族 <2 不发分"，一直是 unsupported。
DefiLlama 免费给两条独立的日频链上经济序列：

- **链上 TVL**（`sol.tvl`）：锁在 Solana DeFi 协议里的美元总额 —— 资本是否愿意留在这条链；
- **链上手续费**（`sol.fees`）：每天真实付出的交易/协议费（美元）—— 这条链有没有人在用、用得多不多。

两者与 COT 完全独立（一个链上资本、一个链上活动、一个期货持仓），凑够三族。

源
--
- `https://api.llama.fi/v2/historicalChainTvl/Solana` → `[{"date":秒,"tvl":美元}, ...]`（2021-03 起）
- `https://api.llama.fi/summary/fees/solana?dataType=dailyFees` → `totalDataChart: [[秒, 美元], ...]`（2021-01 起）

坑
--
- fees 端点返回 134KB 的大 JSON，只取 `totalDataChart`（`totalDataChartBreakdown` 是同一份数据按协议拆开）；
- 两个序列的起点不同（fees 早两个月），各自独立去重，不要强行对齐；
- DefiLlama 匿名限流，突增请求会 429，脚本按天去重、单次拉全量即可，不要逐日循环。

写库
----
`sol.tvl`（category='defi_tvl'）、`sol.fees`（category='chain_fees'），source='defillama'。

用法
----
    python3 scripts/oneoff/backfill_sol_chain.py --dry-run
    python3 scripts/oneoff/backfill_sol_chain.py --apply
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
UA = {"User-Agent": "Mozilla/5.0"}
TVL_URL = "https://api.llama.fi/v2/historicalChainTvl/Solana"
FEE_URL = "https://api.llama.fi/summary/fees/solana?dataType=dailyFees"


def jget(url: str, tries: int = 4):
    last: Exception | None = None
    for i in range(tries):
        try:
            req = urllib.request.Request(url, headers=UA)
            return json.loads(urllib.request.urlopen(req, timeout=40).read())
        except Exception as exc:  # noqa: BLE001
            last = exc
            time.sleep(2 * (i + 1))
    raise last  # type: ignore[misc]


def tvl_rows() -> list[tuple[str, float]]:
    data = jget(TVL_URL)
    return [(datetime.utcfromtimestamp(int(d["date"])).strftime("%Y-%m-%d"), float(d["tvl"]))
            for d in data if d.get("tvl") is not None]


def fee_rows() -> list[tuple[str, float]]:
    data = jget(FEE_URL)
    chart = data.get("totalDataChart") or []
    return [(datetime.utcfromtimestamp(int(p[0])).strftime("%Y-%m-%d"), float(p[1]))
            for p in chart if len(p) >= 2 and p[1] is not None]


def write(conn: sqlite3.Connection, key: str, category: str, unit: str,
          rows: list[tuple[str, float]], apply: bool) -> tuple[int, int]:
    have = {r[0] for r in conn.execute(
        "SELECT DISTINCT date(ts,'unixepoch','localtime') FROM macro_snapshots "
        "WHERE metric_key=? AND value IS NOT NULL", (key,))}
    new = [(d, v) for d, v in rows if d not in have]
    if apply and new:
        blob = json.dumps({"source": "defillama", "unit": unit, "backfill": True}, ensure_ascii=False)
        conn.executemany(
            "INSERT INTO macro_snapshots(ts,category,source,metric_key,value,unit,status,payload)"
            " VALUES(?,?,?,?,?,?,?,?)",
            [(int(datetime.strptime(d, "%Y-%m-%d").replace(hour=12).timestamp()),
              category, "defillama", key, v, unit, "ok", blob) for d, v in new])
        conn.commit()
    return len(have), len(new)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()
    if not (args.apply or args.dry_run):
        ap.print_help()
        return 2

    t = tvl_rows()
    f = fee_rows()
    print(f"[抓取] TVL {len(t)} 天（{t[0][0]} → {t[-1][0]}，最新 ${t[-1][1]/1e6:.1f}m）")
    print(f"[抓取] fees {len(f)} 天（{f[0][0]} → {f[-1][0]}，最新 ${f[-1][1]:,.0f}/日）")

    conn = sqlite3.connect(str(DB), timeout=120)
    conn.execute("PRAGMA busy_timeout=120000")
    try:
        for key, cat, unit, rows in (("sol.tvl", "defi_tvl", "USD", t),
                                     ("sol.fees", "chain_fees", "USD/day", f)):
            have, new = write(conn, key, cat, unit, rows, args.apply)
            print(f"[{key}] 库内 {have} | 新增 {new}{' → 已写库' if args.apply and new else ''}")
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
