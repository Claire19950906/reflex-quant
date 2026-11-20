#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""backfill_eth_chain.py — 回填以太坊链上经济数据（v471）

为什么
------
ETH 之前只有场内资金面（资金费率 / 稳定币 / 未平仓），**没有一个关于以太坊这条链本身的读数**，
权重和只有 0.72，是三个主流加密里最薄的。DefiLlama 免费给两条完整日频序列：

- `eth.tvl`：锁在以太坊 DeFi 协议里的美元总额 —— 资本是否愿意留在这条链；
- `eth.fees`：以太坊链上每天真实付出的费用（美元，含 base fee 销毁的部分）—— 真实使用强度。

坑
--
- 链名大小写敏感：TVL 端点用 `Ethereum`，fees 端点用 `ethereum`（小写）；
- fees 的 `totalDataChart` 是 [[秒, 美元], ...]，与 SOL 同一结构；
- DefiLlama 匿名限流 429，一次拉全量、按天去重，不逐日循环。

用法
----
    python3 scripts/oneoff/backfill_eth_chain.py --dry-run
    python3 scripts/oneoff/backfill_eth_chain.py --apply
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import time
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[2]
DB = BACKEND / "trident_event_bus.db"
UA = {"User-Agent": "Mozilla/5.0", "Accept-Encoding": "identity"}
TVL_URL = "https://api.llama.fi/v2/historicalChainTvl/Ethereum"
FEE_URL = "https://api.llama.fi/summary/fees/ethereum?dataType=dailyFees"


def jget(url: str, tries: int = 8):
    last: Exception | None = None
    for i in range(tries):
        try:
            return json.loads(urllib.request.urlopen(
                urllib.request.Request(url, headers=UA), timeout=60).read())
        except Exception as exc:  # noqa: BLE001
            last = exc
            time.sleep(2 * (i + 1))
    raise last  # type: ignore[misc]


def day(ts: int) -> str:
    return datetime.fromtimestamp(int(ts), timezone.utc).strftime("%Y-%m-%d")


def rows_tvl():
    return [(day(d["date"]), float(d["tvl"]))
            for d in jget(TVL_URL) if d.get("tvl") is not None]


def rows_fees():
    chart = jget(FEE_URL).get("totalDataChart") or []
    return [(day(p[0]), float(p[1])) for p in chart if len(p) >= 2 and p[1] is not None]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()
    if not (args.apply or args.dry_run):
        ap.print_help()
        return 2

    t, f = rows_tvl(), rows_fees()
    print(f"[抓取] TVL {len(t)} 天（{t[0][0]} → {t[-1][0]}，最新 ${t[-1][1]/1e9:.2f}b）")
    print(f"[抓取] fees {len(f)} 天（{f[0][0]} → {f[-1][0]}，最新 ${f[-1][1]/1e6:.2f}m/日）")

    conn = sqlite3.connect(str(DB), timeout=120)
    conn.execute("PRAGMA busy_timeout=120000")
    try:
        for key, cat, unit, rows in (("eth.tvl", "defi_tvl", "USD", t),
                                     ("eth.fees", "chain_fees", "USD/day", f)):
            have = {r[0] for r in conn.execute(
                "SELECT DISTINCT date(ts,'unixepoch','localtime') FROM macro_snapshots "
                "WHERE metric_key=? AND value IS NOT NULL", (key,))}
            new = [(d, v) for d, v in rows if d not in have]
            if args.apply and new:
                blob = json.dumps({"source": "defillama", "unit": unit, "backfill": True},
                                  ensure_ascii=False)
                conn.executemany(
                    "INSERT INTO macro_snapshots(ts,category,source,metric_key,value,unit,status,payload)"
                    " VALUES(?,?,?,?,?,?,?,?)",
                    [(int(datetime.strptime(d, "%Y-%m-%d").replace(hour=12).timestamp()),
                      cat, "defillama", key, v, unit, "ok", blob) for d, v in new])
                conn.commit()
            print(f"[{key}] 库内 {len(have)} | 新增 {len(new)}"
                  f"{' → 已写库' if args.apply and new else ''}")
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
