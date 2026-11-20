#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""backfill_treasury_auction.py — 回填美债拍卖需求（bid-to-cover / 间接投标占比）

为什么
------
v464 的 `_build_ust` 把「拍卖需求」写成硬缺口：库里没有任何拍卖数据，所以"债券好不好卖"
这一层完全没进分量。财政部有免费的官方接口（fiscaldata `auctions_query`），能给到
每场拍卖的 bid-to-cover、间接投标金额、一级交易商金额 —— 正好补上。

写库
----
每场拍卖一行，写进 `macro_snapshots`：
  metric_key = ust.auction_btc_10y / ust.auction_btc_30y / ust.auction_btc_2y
  ts         = 拍卖日（当地中午对齐）
  payload    = {indirect, offering, term, auction_date, backfill}
另有 `ust.auction_indirect_share_10y` = 间接投标金额 / 发行额（真实资金需求占比）。

用法
----
    python3 scripts/oneoff/backfill_treasury_auction.py --dry-run
    python3 scripts/oneoff/backfill_treasury_auction.py --apply
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
BASE = "https://api.fiscaldata.treasury.gov/services/api/fiscal_service/v1/accounting/od/auctions_query"

# (metric_key, 过滤条件, 取多少场)
BUCKETS = (
    ("ust.auction_btc_10y", "security_type:eq:Note,security_term:eq:10-Year", 72),
    # Bond 里混着 20Y 重开，翻 140 场才够 40 场 30Y（否则凑不满 30 场分位门槛）
    ("ust.auction_btc_30y", "security_type:eq:Bond", 140),
    ("ust.auction_btc_2y", "security_type:eq:Note,security_term:eq:2-Year", 72),
)


def _get(url: str, tries: int = 8) -> dict:
    last = None
    for i in range(tries):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "TridentAgentMVP/1.0"})
            return json.loads(urllib.request.urlopen(req, timeout=25).read())
        except Exception as exc:  # noqa: BLE001
            last = exc
            time.sleep(1.5 * (i + 1))
    raise RuntimeError(f"{type(last).__name__}: {last}")


def _num(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _fetch_bucket(cond: str, size: int) -> list[dict]:
    url = (f"{BASE}?filter={cond},bid_to_cover_ratio:gt:0&sort=-auction_date"
           f"&page%5Bsize%5D={size}")
    rows = _get(url).get("data") or []
    out = []
    for r in rows:
        btc = _num(r.get("bid_to_cover_ratio"))
        if btc is None:
            continue
        term = (r.get("security_term") or "").strip()
        # Bond 里混着 20Y 重开（19-Year X-Month），只留 30Y 段
        if cond == "security_type:eq:Bond" and not term.startswith(("29", "30")):
            continue
        out.append({"date": r.get("auction_date") or r.get("record_date"),
                    "btc": btc,
                    "indirect": _num(r.get("indirect_bidder_accepted")),
                    "offering": _num(r.get("offering_amt")),
                    "term": term})
    return out


def _ts(day: str) -> int:
    return int(datetime.strptime(day, "%Y-%m-%d").replace(hour=12).timestamp())


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
    written = 0
    try:
        for key, cond, size in BUCKETS:
            try:
                rows = _fetch_bucket(cond, size)
            except Exception as exc:  # noqa: BLE001
                print(f"  [失败] {key}: {exc}")
                continue
            if not rows:
                print(f"  [空]   {key}")
                continue
            print(f"  {key:24s} {len(rows):2d} 场 | 最新 {rows[0]['date']} "
                  f"btc={rows[0]['btc']:.2f} | 最老 {rows[-1]['date']}")
            have = {r0[0] for r0 in conn.execute(
                "SELECT DISTINCT date(ts,'unixepoch','localtime') FROM macro_snapshots "
                "WHERE metric_key=?", (key,))}
            rows = [r for r in rows if r["date"] not in have]
            print(f"      去重后新增 {len(rows)} 场")
            if not args.apply:
                continue
            for r in rows:
                payload = json.dumps({"term": r["term"], "indirect": r["indirect"],
                                      "offering": r["offering"], "backfill": True},
                                     ensure_ascii=False)
                conn.execute(
                    "INSERT INTO macro_snapshots(ts,category,source,metric_key,value,unit,status,payload)"
                    " VALUES(?,?,?,?,?,?,?,?)",
                    (_ts(r["date"]), "auction", "treasury_fiscaldata", key, r["btc"], "x", "ok", payload))
                if key == "ust.auction_btc_10y" and r["indirect"] and r["offering"]:
                    conn.execute(
                        "INSERT INTO macro_snapshots(ts,category,source,metric_key,value,unit,status,payload)"
                        " VALUES(?,?,?,?,?,?,?,?)",
                        (_ts(r["date"]), "auction", "treasury_fiscaldata",
                         "ust.auction_indirect_share_10y",
                         round(100.0 * r["indirect"] / r["offering"], 2), "%", "ok", payload))
                written += 1
            conn.commit()
    finally:
        conn.close()
    print(f"[done] 写库 {written} 行" + ("" if args.apply else "（dry-run，未写库）"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
