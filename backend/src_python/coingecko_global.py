#!/usr/bin/env python3
"""CoinGecko 全市场资金体量采集器。

为什么需要：
  · 系统里有钱包、有杠杆（Bybit/OKX），但没有"整个市场有多大"的基准。
    没有总量基准，"资金流入 5 亿"这种数字无法判断是大是小。
  · CoinGecko /global 一次返回：总市值、24h 成交额、BTC 占比、ETH 占比、
    活跃币种数。这四项正好构成"体量对比"的刻度尺。

写入 external_indicators，source='coingecko'：
  cg_total_mcap_usd      全市场总市值（美元）
  cg_total_vol_usd       全市场 24h 成交额（美元）
  cg_btc_dominance       BTC 市值占比（%）
  cg_eth_dominance       ETH 市值占比（%）
  cg_market_cap_change   24h 市值变化（%）

限制：不需要 key 的公共接口，约 10-30 次/分钟；本采集器 10 分钟一轮，安全。
"""
import os
import sys
import time
from typing import Any, Dict, List, Optional

import requests

# 本机存在 DOUBAO_* 转发代理变量，requests 默认 trust_env=True 会误命中代理并断连；
# 实测关闭后同一请求稳定返回 200。
requests.Session.trust_env = False

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import external_data

URL = "https://api.coingecko.com/api/v3/global"
HEADERS = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                         "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0 Safari/537.36",
           "Accept": "application/json"}
TIMEOUT = 12


def _get() -> Optional[Dict[str, Any]]:
    last = None
    for attempt in range(3):
        try:
            r = requests.get(URL, headers=HEADERS, timeout=TIMEOUT)
            r.raise_for_status()
            return r.json().get("data") or {}
        except Exception as exc:
            last = exc
            time.sleep(2 * (attempt + 1))
    print(f"[ERROR] CoinGecko 拉取失败: {type(last).__name__}: {last}", flush=True)
    return None


def main() -> int:
    data = _get()
    if not data:
        return 1

    ts = int(time.time())
    mcap = (data.get("total_market_cap") or {}).get("usd")
    vol = (data.get("total_volume") or {}).get("usd")
    btc_d = data.get("market_cap_percentage", {}).get("btc")
    eth_d = data.get("market_cap_percentage", {}).get("eth")
    chg = data.get("market_cap_change_percentage_24h_usd")

    rows: List[tuple] = [
        ("cg_total_mcap_usd", mcap, {"unit": "USD"}),
        ("cg_total_vol_usd", vol, {"unit": "USD"}),
        ("cg_btc_dominance", btc_d, {"unit": "%"}),
        ("cg_eth_dominance", eth_d, {"unit": "%"}),
        ("cg_market_cap_change", chg, {"unit": "%", "window": "24h"}),
    ]

    written = 0
    for indicator, value, meta in rows:
        if value is None:
            continue
        try:
            external_data.ingest_external(
                asset="MARKET", indicator=indicator,
                records=[{"ts": ts, "value": float(value)}],
                source="coingecko", meta=meta,
            )
            written += 1
        except Exception as exc:
            print(f"[WARN] {indicator} 写入失败: {type(exc).__name__}: {exc}", flush=True)

    print(f"[{time.strftime('%H:%M:%S')}] crypto 总市值 "
          f"{'%.2f' % (mcap/1e12) if mcap else '—'} 万亿 | BTC 占比 "
          f"{'%.1f' % btc_d if btc_d else '—'}% | 写入 {written} 条", flush=True)
    return 0 if written else 1


if __name__ == "__main__":
    sys.exit(main())
