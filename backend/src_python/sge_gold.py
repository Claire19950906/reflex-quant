#!/usr/bin/env python3
"""上海黄金交易所 Au99.99 实时行情 + 中国金溢价。

为什么需要：
  · 现在系统看黄金只有国际盘（XAU），看不到中国实物端。而中国是全球最大的
    实物黄金买家，SGE 的 Au99.99 就是国内实物价的锚。
  · 把「SGE 人民币金价」按当日汇率折算成美元/盎司，再减去国际 XAU，
    得到的就是**中国金溢价**——它扩张通常意味着国内实物需求强于外盘，
    是国际金价的一个前瞻性旁证，而不是重复报价。
  · 两段数据都是真实的（SGE 官方接口 + 汇率），溢价只是换算，不是编造。

写入 external_indicators：asset='SGE_GOLD'
  sge_au9999_cny_g      SGE Au99.99 最新价（元/克）
  sge_au9999_high/low   当日区间（元/克）
  sge_gold_usd_oz        折算后的美元/盎司
  sge_gold_premium_pct   相对国际 XAU 的溢价（%），负值即折价
"""
import json
import os
import sqlite3
import sys
import time
from typing import Any, Dict, Optional

import requests

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import db
import external_data

requests.Session.trust_env = False
URL = "https://www.sge.com.cn/graph/quotations"
HEADERS = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                         "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0 Safari/537.36",
           "Referer": "https://www.sge.com.cn/sjzx/mrhq"}
TIMEOUT = 15
OZ_GRAMS = 31.1035  # 1 金衡盎司 = 31.1035 克


def _fetch() -> Optional[Dict[str, Any]]:
    for attempt in range(3):
        try:
            r = requests.get(URL, headers=HEADERS, timeout=TIMEOUT)
            r.raise_for_status()
            return r.json()
        except Exception as exc:
            if attempt == 2:
                print(f"[WARN] SGE 拉取失败: {type(exc).__name__}: {exc}", flush=True)
                return None
            time.sleep(2)


def _latest(conn: sqlite3.Connection, asset: str, like: str) -> Optional[float]:
    row = conn.execute(
        "SELECT value FROM external_indicators WHERE asset=? AND indicator LIKE ? "
        "ORDER BY ts DESC LIMIT 1", (asset, like)).fetchone()
    return float(row[0]) if row and row[0] is not None else None


def main() -> int:
    payload = _fetch()
    if not payload:
        return 1
    series = payload.get("data") or []
    if not series:
        print("[WARN] SGE 无报价数据", flush=True)
        return 1
    try:
        last = float(series[-1])
    except (TypeError, ValueError):
        print("[WARN] SGE 报价解析失败", flush=True)
        return 1

    contract = payload.get("heyue") or "Au99.99"
    hi = payload.get("max")
    lo = payload.get("min")
    ts = int(time.time())

    conn = sqlite3.connect(db.get_db_path(), timeout=30, isolation_level=None)
    conn.execute("PRAGMA busy_timeout=30000")
    # 同一 ts 只写一个值，避免"最新价/最高/最低"混在同一条指标里读不清
    recs = [{"ts": ts, "value": round(last, 3)}]
    meta: Dict[str, Any] = {"unit": "CNY/gram", "contract": contract,
                            "session_time": payload.get("now")}

    external_data.ingest_external(asset="SGE_GOLD", indicator="sge_au9999_cny_g",
                                  records=recs, source="sge", meta=meta)
    if hi:
        external_data.ingest_external(asset="SGE_GOLD", indicator="sge_au9999_high",
                                      records=[{"ts": ts, "value": round(float(hi), 3)}],
                                      source="sge", meta=meta)
    if lo:
        external_data.ingest_external(asset="SGE_GOLD", indicator="sge_au9999_low",
                                      records=[{"ts": ts, "value": round(float(lo), 3)}],
                                      source="sge", meta=meta)

    # 内外盘折算：人民币/克 → 美元/盎司
    usdcny = _latest(conn, "FX", "%CNY%") or _latest(conn, "FX", "%cny%")
    xau = _latest(conn, "XAU", "%price%")
    if usdcny and xau:
        usd_oz = last * OZ_GRAMS / usdcny
        premium = (usd_oz / xau - 1.0) * 100.0
        external_data.ingest_external(
            asset="SGE_GOLD", indicator="sge_gold_usd_oz",
            records=[{"ts": ts, "value": round(usd_oz, 2)}],
            source="sge", meta={"derived_from": ["sge_au9999", "FX.CNY", "XAU"],
                                "usdcny": usdcny})
        external_data.ingest_external(
            asset="SGE_GOLD", indicator="sge_gold_premium_pct",
            records=[{"ts": ts, "value": round(premium, 3)}],
            source="sge", meta={"derived_from": ["sge_au9999", "FX.CNY", "XAU"],
                                "xau": xau, "usdcny": usdcny})
        print(f"[{time.strftime('%H:%M:%S')}] SGE {contract}={last} 元/克 | "
              f"折美元 {usd_oz:.2f}/oz | 溢价 {premium:+.2f}%", flush=True)
    else:
        print(f"[{time.strftime('%H:%M:%S')}] SGE {contract}={last} 元/克"
              f"（缺汇率或 XAU，溢价本轮跳过）", flush=True)
    conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
