#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
sge_silver.py —— 上海黄金交易所白银价格 + 国内外白银溢价

为什么加：
  黄金已经有 SGE 溢价（`sge_gold_premium_pct`），白银没有。
  白银的国内溢价更能反映实物紧张程度（盘子小、容易被逼），
  而用户同时看金银比 —— 溢价和比值一起看，才知道是"国内缺货"还是"全球在涨"。

口径：
  - SGE 报的是 **人民币/克**；折成美元/盎司 = 人民币/克 × 31.1035 ÷ USDCNY
  - 溢价 = (国内折美元/盎司 - COMEX 白银收盘) / COMEX 白银收盘 × 100%
  - COMEX 白银取本机 `kline_bars` 的 XAG 1d 收盘（同源新浪外盘，口径一致）
  - 汇率取 `fx_usdcny` 最近一笔；取不到就整条不写，不用旧汇率凑

用法：
  python3 sge_silver.py
  python3 sge_silver.py --dry-run
"""
import argparse
import sqlite3
import sys
import time
from typing import Optional

import db
import external_data

URL = "https://www.sge.com.cn/graph/quotations"
HEADERS = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                         "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0 Safari/537.36",
           "Referer": "https://www.sge.com.cn/sjzx/mrhq"}
PRODUCT = "Ag99.99"
OZ_GRAMS = 31.1035
SOURCE = "sge_silver"


def fetch(tries: int = 3) -> Optional[dict]:
    import requests
    s = requests.Session()
    s.trust_env = False
    for i in range(tries):
        try:
            r = s.get(URL, params={"instid": PRODUCT}, headers=HEADERS, timeout=20)
            if r.status_code == 200:
                return r.json()
        except Exception as e:
            print(f"  异常 {type(e).__name__}，重试")
        time.sleep(2 * (i + 1))
    return None


def main(argv: Optional[list] = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    args, _ = ap.parse_known_args(argv)

    payload = fetch()
    if not payload:
        print("[FATAL] SGE 白银取不到，本次不写库")
        return 1
    data = payload.get("data") or []
    if not data:
        print("[FATAL] 返回里没有价格序列，不写库")
        return 1
    raw = float(data[-1])
    # 上金所白银报价是 **元/千克**（黄金才是元/克）。白银不可能超过 ~200 元/克，
    # 超过 1000 就必然是每千克口径 —— 按此判定后换算成元/克，避免溢价算出天文数字。
    if raw > 1000:
        cny_g = raw / 1000.0
        print(f"  SGE {PRODUCT} {raw:,.0f} 元/千克 → {cny_g:.3f} 元/克  @ {payload.get('now')}")
    else:
        cny_g = raw
        print(f"  SGE {PRODUCT} {cny_g:.3f} 元/克（接口已是克口径）  @ {payload.get('now')}")

    conn = sqlite3.connect(db.get_db_path(), timeout=30)
    conn.execute("PRAGMA busy_timeout=30000")

    fx = conn.execute("SELECT value FROM external_indicators WHERE indicator='fx_usdcny' "
                      "ORDER BY ts DESC LIMIT 1").fetchone()
    xag = conn.execute("SELECT close, ts FROM kline_bars WHERE symbol='XAG' AND timeframe='1d' "
                       "AND close IS NOT NULL ORDER BY ts DESC LIMIT 1").fetchone()
    if not fx or not xag:
        print("  缺汇率或 COMEX 白银收盘，溢价算不出来 —— 只写国内价，不编溢价")
        usd_oz = None
    else:
        usdcny = float(fx[0])
        usd_oz = cny_g * OZ_GRAMS / usdcny
        premium = (usd_oz - float(xag[0])) / float(xag[0]) * 100
        print(f"  折美元 {usd_oz:.3f}/oz（USDCNY={usdcny:.4f}）| COMEX {float(xag[0]):.2f} "
              f"| 溢价 {premium:+.2f}%")

    if args.dry_run:
        conn.close()
        return 0

    ts = int(time.time()) * 1000
    external_data.ingest_external(asset="XAG", indicator="sge_ag9999_cny_g",
                                  records=[{"ts": ts, "value": cny_g}], source=SOURCE,
                                  meta={"unit": "CNY/g", "raw_unit": "CNY/kg",
                                        "raw": raw, "product": PRODUCT})

    # v335: 溢价打点改成"计算时刻"，并把基准 K 线的日期与年龄写进 meta。
    #   原先按 K 线所属日打点 → 周五的溢价打到周五零点，周末再算也是旧点，
    #   审计看到的永远是几十小时前的点（假 stale）。溢价本身是实时比价，
    #   ts 就该是"什么时候比出来的"，基准多旧由 meta 交代清楚，不藏。
    if xag and fx:
        bar_ts = int(xag[1])
        basis_hours = round((ts - bar_ts) / 3600000, 1)
        external_data.ingest_external(asset="XAG", indicator="sge_silver_premium_pct",
                                      records=[{"ts": ts, "value": round(premium, 2)}],
                                      source=SOURCE,
                                      meta={"unit": "pct", "usd_oz": round(usd_oz, 3),
                                            "comex_xag": float(xag[0]), "usdcny": float(fx[0]),
                                            "basis_bar_ts": bar_ts,
                                            "basis_age_hours": basis_hours,
                                            "degraded": basis_hours > 72})
    print("  已写入（source=sge_silver）")
    conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
