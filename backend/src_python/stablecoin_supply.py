#!/usr/bin/env python3
"""稳定币总量采集器（CoinGecko，免费无 key）。

为什么需要：
  · 稳定币是加密市场里"随时能开枪的弹药"：总量涨，说明有新钱铸出来准备买；
    总量缩，说明有人在赎回离场。这条指标比"交易所净流入"更容易免费拿到，
    且方向含义一致。
  · DefiLlama 的稳定币接口在本机时通时断（TLS 抖动），不能做常驻源；
    CoinGecko 的 /coins/markets 稳定，直接按币种市值加总即可。

写入 external_indicators，source='coingecko'，asset='STABLE'：
  sc_total_mcap    主要稳定币总市值（美元）
  sc_usdt_share    USDT 占比（%）
  sc_change_24h    加权 24h 涨跌（%），近似"当日弹药增减"
  sc_usdt_mcap     USDT 市值（美元）
  sc_usdc_mcap     USDC 市值（美元）

限制：公共接口约 10-30 次/分钟；10 分钟一轮、1 次请求，安全。
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

URL = "https://api.coingecko.com/api/v3/coins/markets"
HEADERS = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                         "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0 Safari/537.36",
           "Accept": "application/json"}
TIMEOUT = 15
IDS = "tether,usd-coin,dai,first-digital-usd,usde,true-usd,paypal-usd"


def _fetch() -> Optional[List[Dict[str, Any]]]:
    last = None
    for attempt in range(3):
        try:
            r = requests.get(URL, params={"vs_currency": "usd", "ids": IDS,
                                          "order": "market_cap_desc"},
                             headers=HEADERS, timeout=TIMEOUT)
            r.raise_for_status()
            data = r.json()
            return data if isinstance(data, list) else None
        except Exception as exc:
            last = exc
            time.sleep(2 * (attempt + 1))
    print(f"[ERROR] CoinGecko 稳定币拉取失败: {type(last).__name__}: {last}", flush=True)
    return None


def main() -> int:
    rows = _fetch()
    if not rows:
        return 1

    total = 0.0
    weighted = 0.0
    by_symbol: Dict[str, float] = {}
    for coin in rows:
        mcap = coin.get("market_cap") or 0
        chg = coin.get("market_cap_change_percentage_24h") or 0
        if not mcap:
            continue
        total += mcap
        weighted += mcap * chg
        sym = str(coin.get("symbol") or "").lower()
        by_symbol[sym] = mcap

    if total <= 0:
        print("[ERROR] 稳定币总市值为 0，拒绝写入", flush=True)
        return 1

    ts = int(time.time())
    change = weighted / total
    payload = [
        ("sc_total_mcap", total, {"unit": "USD", "coins": len(rows)}),
        ("sc_change_24h", round(change, 4), {"unit": "%", "window": "24h", "weighted": True}),
        ("sc_usdt_mcap", by_symbol.get("usdt"), {"unit": "USD"}),
        ("sc_usdc_mcap", by_symbol.get("usdc"), {"unit": "USD"}),
        ("sc_usdt_share", round(by_symbol.get("usdt", 0) / total * 100, 2), {"unit": "%"}),
    ]
    written = 0
    for indicator, value, meta in payload:
        if value is None:
            continue
        external_data.ingest_external(
            asset="STABLE", indicator=indicator,
            records=[{"ts": ts, "value": float(value)}],
            source="coingecko", meta=meta,
        )
        written += 1

    print(f"[{time.strftime('%H:%M:%S')}] 稳定币总量 {total/1e9:.2f}B "
          f"({change:+.2f}% 24h) USDT 占比 {payload[4][1]}% 写 {written} 条", flush=True)
    return 0 if written else 1


if __name__ == "__main__":
    sys.exit(main())
