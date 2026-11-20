#!/usr/bin/env python3
"""BTC 链上状态（Blockchair）：内存池拥堵、手续费、活跃度。

为什么需要：
  · 之前系统里完全没有链上口径，"BTC 要补手续费"这条一直空着。
  · 内存池里积压多少笔交易、每笔手续费多少，直接反映网络是否拥堵：
    拥堵抬升结算成本，通常抑制小额转账与链上活动，是链上需求的领先观测点。
  · market_dominance（BTC 市占率）反映资金在加密内部的回流方向，
    对"山寨季 / 避险回 BTC"这类判断有用，且是真实市值占比而非情绪打分。

写入 external_indicators asset=BTC，source=blockchair：
  btc_mempool_tx        内存池待确认交易笔数
  btc_mempool_mb        内存池积压体积（MB）
  btc_mempool_fee_usd   内存池待确认交易手续费总额（美元）
  btc_tx_24h            24 小时链上交易笔数
  btc_median_fee_usd    24 小时中位手续费（美元）
  btc_dominance_pct     BTC 市值占比（%）
"""
import os
import sys
import time
from typing import Any, Dict, Optional

import requests

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import external_data

requests.Session.trust_env = False
CHAIN_URLS = {"BTC": "https://api.blockchair.com/bitcoin/stats",
              "ETH": "https://api.blockchair.com/ethereum/stats"}
HEADERS = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                         "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0 Safari/537.36"}
TIMEOUT = 20
FIELDS = {
    "mempool_transactions": ("{p}_mempool_tx", "笔", 1),
    "mempool_size": ("{p}_mempool_mb", "MB", 1 / 1024 / 1024),
    "mempool_total_fee_usd": ("{p}_mempool_fee_usd", "USD", 1),
    "transactions_24h": ("{p}_tx_24h", "笔", 1),
    "median_transaction_fee_usd_24h": ("{p}_median_fee_usd", "USD", 1),
    "market_dominance_percentage": ("{p}_dominance_pct", "%", 1),
}


def _fetch(chain: str) -> Optional[Dict[str, Any]]:
    for attempt in range(3):
        try:
            r = requests.get(CHAIN_URLS[chain], headers=HEADERS, timeout=TIMEOUT)
            r.raise_for_status()
            return (r.json() or {}).get("data") or {}
        except Exception as exc:
            if attempt == 2:
                print(f"[WARN] Blockchair 拉取失败: {type(exc).__name__}: {exc}", flush=True)
                return None
            time.sleep(2 * (attempt + 1))
    return None


def main() -> int:
    ts = int(time.time())
    total = 0
    for chain, prefix in (("BTC", "btc"), ("ETH", "eth")):
        d = _fetch(chain)
        if not d:
            continue
        out = []
        for key, (ind_t, unit, scale) in FIELDS.items():
            raw = d.get(key)
            if raw is None:
                continue
            try:
                val = round(float(raw) * scale, 3)
            except (TypeError, ValueError):
                continue
            external_data.ingest_external(
                asset=chain, indicator=ind_t.format(p=prefix),
                records=[{"ts": ts, "value": val}],
                source="blockchair", meta={"unit": unit, "field": key},
            )
            total += 1
            out.append(f"{key}={val}")
        print(f"[{time.strftime('%H:%M:%S')}] {chain} 链上 " + " | ".join(out), flush=True)
    return 0 if total else 1


if __name__ == "__main__":
    sys.exit(main())
