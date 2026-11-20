#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
crypto_derived.py —— 加密的两个派生指标：Coinbase 溢价、稳定币总供应

1) Coinbase 溢价（`cb_premium_pct`）
   美国合规交易所（Coinbase）相对全市场中间价的溢价。正值抬升通常说明
   美元通道在主动买（机构/ETF 方向），负值说明在往外卖。
   数据取自本机已有的多交易所现货快照（x_spot_coinbase 与 x_spot_median），
   不新增外部请求；但历史只从有快照的那天起算 —— 更早的日子没有，就不补。

2) 稳定币总供应（`stable_supply_total`）
   USDT + USDC 链上流通量之和，是加密市场的"干火药"。
   来自 CoinMetrics（cm_supply），两个资产分别取当日值再相加。

写入 source='derived_crypto'，asset='BTC'（这两条都是描述 BTC 市场的环境变量）。

用法：
  python3 crypto_derived.py
  python3 crypto_derived.py --dry-run
"""
import argparse
import sqlite3
import sys
import time
from typing import Dict, List, Optional

import db
import external_data

SOURCE = "derived_crypto"
# 这组快照实际采的是 ETH 现货（coinbase/bitstamp/kraken/okx 四家），
# 不是 BTC —— 曾经按 BTC 写过一次，已纠正。
ASSET_PREMIUM = "ETH"
ASSET_STABLE = "BTC"
DAYS_BACK = 30


def _per_day(conn: sqlite3.Connection, indicator: str, asset: Optional[str],
             since: int, source: Optional[str] = None) -> Dict[int, float]:
    """取每个自然日最后一笔值。ts 单位不统一（xvenue 用秒、coinmetrics 用毫秒），
    这里按量级判断：< 1e11 视为秒。

    source 用来隔离同一指标名下的不同口径：`stable_supply_total` 现在既有
    自己算的 USDT+USDC（derived_crypto），也有 DefiLlama 的全链总量。
    两个口径差 30B 以上，混着算 30 日变化会凭空多出十几个点的假信号。
    """
    sql = "SELECT ts, value FROM external_indicators WHERE indicator=?"
    args: List[object] = [indicator]
    if asset:
        sql += " AND asset=?"
        args.append(asset)
    if source:
        sql += " AND source=?"
        args.append(source)
    sql += " AND ts>=? ORDER BY ts"
    args.append(since)
    out: Dict[int, float] = {}
    for ts, value in conn.execute(sql, args):
        t = int(ts)
        sec = t / 1000 if t > 1e11 else t          # 毫秒 → 秒
        day = int(sec) // 86400
        out[day] = float(value)                     # 同日后者覆盖，取最新
    return out


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=DAYS_BACK)
    ap.add_argument("--dry-run", action="store_true")
    args, _ = ap.parse_known_args(argv)

    conn = sqlite3.connect(db.get_db_path(), timeout=30, isolation_level=None)
    conn.execute("PRAGMA busy_timeout=30000")
    since_sec = int(time.time()) - args.days * 86400
    # 下限只能用「秒」量级：xvenue 存的是秒、coinmetrics 存的是毫秒，
    # 若用毫秒做下限，秒数据会被整条过滤掉（这是个很隐蔽的错，踩过一次）。
    since_ts = since_sec

    # ---- Coinbase 溢价 ----
    # 基准不能用手算的 median：只有三家交易所时 median 往往就等于 Coinbase 自己的价，
    # 溢价会恒为 0。改成"其余交易所均值"作分母。
    # 必须按 asset 过滤：同一个 indicator 名下面同时存了 BTC 和 ETH 两种快照，
    # 不筛资产就会把 66,8xx 的 BTC 价和 1,89x 的 ETH 价相除（这坑踩过一次）。
    cb = _per_day(conn, "x_spot_coinbase", "ETH", since_ts)
    peers = {k: _per_day(conn, f"x_spot_{k}", "ETH", since_ts)
             for k in ("bitstamp", "kraken", "okx")}
    days = sorted(cb)
    if days:
        records = []
        for d in days:
            # 只看同一数量级的对照价（防陈旧报价/记错标的把均值拉飞）
            vals = [v[d] for v in peers.values()
                    if d in v and v[d] > 0 and 0.7 <= v[d] / cb[d] <= 1.4]
            if len(vals) < 2:
                continue                         # 少于两家对照就不算
            base = sum(vals) / len(vals)
            prem = (cb[d] - base) / base * 100
            if abs(prem) > 5:
                continue                         # 5% 以上的偏离对现货溢价没意义，视为脏数据
            records.append({"ts": d * 86400000, "value": round(prem, 3)})
        latest = records[-1]["value"] if records else None
        print(f"  cb_premium_pct        {len(records)} 天，最新 {latest:+.3f}%"
              if records else "  cb_premium_pct        无可用快照，跳过")
        if records and not args.dry_run:
            external_data.ingest_external(asset=ASSET_PREMIUM, indicator="cb_premium_pct",
                                          records=records, source=SOURCE,
                                          meta={"basis": "coinbase vs median spot",
                                                "unit": "pct"})
    else:
        print("  cb_premium_pct        无可用快照，跳过（历史不补）")

    # ---- 稳定币总供应 ----
    usdt = _per_day(conn, "cm_supply", "USDT", since_ts)
    usdc = _per_day(conn, "cm_supply", "USDC", since_ts)
    days2 = sorted(set(usdt) & set(usdc))
    if days2:
        records = [{"ts": d * 86400000, "value": usdt[d] + usdc[d]} for d in days2]
        latest = records[-1]["value"]
        print(f"  stable_supply_total   {len(records)} 天，最新 {latest / 1e9:,.2f}B（USDT+USDC）")
        if not args.dry_run:
            external_data.ingest_external(asset=ASSET_STABLE, indicator="stable_supply_total",
                                          records=records, source=SOURCE,
                                          meta={"components": ["USDT", "USDC"],
                                                "unit": "coins"})
    else:
        print("  stable_supply_total   两个稳定币没有同一天的数据，跳过")

    # ---- 稳定币 30 日变化（给规则层用：供应收缩=干火药变少）----
    rec = _per_day(conn, "stable_supply_total", None, since_ts, source=SOURCE)
    days3 = sorted(rec)
    if len(days3) >= 31:
        pct_records = []
        for i in range(30, len(days3)):
            base_v = rec[days3[i - 30]]
            if base_v <= 0:
                continue
            pct_records.append({"ts": days3[i] * 86400000,
                                "value": round((rec[days3[i]] / base_v - 1) * 100, 2)})
        if pct_records:
            print(f"  stable_supply_30d_pct  {len(pct_records)} 天，最新 {pct_records[-1]['value']:+.2f}%")
            if not args.dry_run:
                external_data.ingest_external(asset=ASSET_STABLE,
                                              indicator="stable_supply_30d_pct",
                                              records=pct_records, source=SOURCE,
                                              meta={"window": "30d", "unit": "pct"})

    if not args.dry_run:
        print(f"  已写入（source={SOURCE}）")
    conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
