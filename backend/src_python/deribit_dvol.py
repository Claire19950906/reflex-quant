#!/usr/bin/env python3
"""Deribit 隐含波动率（DVOL）采集器。

为什么需要：
  · 现在"恐惧"只有 alternative.me 那个 0-100 的散户情绪分，它是投票结果，
    不是真金白银定出来的价。
  · DVOL 是期权市场定价出来的 30 天隐含波动率——市场愿意为"未来会大动"
    付多少钱。它涨，说明专业资金在买保护；它跌，说明市场在睡。
  · 这是唯一一条能区分"散户害怕"与"机构在防"的指标。

写入 external_indicators，source='deribit'：
  dv_dvol   BTC / ETH 30 天隐含波动率（%，年化）

限制：公共端点，10 分钟一轮。
"""
import os
import sys
import time
from typing import Any, Dict, Optional

import requests

# 本机存在 DOUBAO_* 转发代理变量，requests 默认 trust_env=True 会误命中代理并断连；
# 实测关闭后同一请求稳定返回 200。
requests.Session.trust_env = False

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import external_data

BASE = "https://www.deribit.com/api/v2/public/get_volatility_index_data"
HEADERS = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                         "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0 Safari/537.36",
           "Accept": "application/json"}
TIMEOUT = 12
WINDOW_MS = 6 * 3600 * 1000


def _dvol(currency: str) -> Optional[float]:
    now = int(time.time() * 1000)
    params = {"currency": currency, "start_timestamp": now - WINDOW_MS,
              "end_timestamp": now, "resolution": "60"}
    last = None
    for attempt in range(3):
        try:
            r = requests.get(BASE, params=params, headers=HEADERS, timeout=TIMEOUT)
            r.raise_for_status()
            rows = ((r.json() or {}).get("result") or {}).get("data") or []
            if rows:
                return float(rows[-1][4])  # [ts, open, high, low, close]
            return None
        except Exception as exc:
            last = exc
            time.sleep(2 * (attempt + 1))
    print(f"[WARN] DVOL {currency} 失败: {type(last).__name__}: {last}", flush=True)
    return None


def main() -> int:
    ts = int(time.time())
    written = 0
    for currency in ("BTC", "ETH"):
        value = _dvol(currency)
        if value is None:
            continue
        external_data.ingest_external(
            asset=currency, indicator="dv_dvol",
            records=[{"ts": ts, "value": value}],
            source="deribit", meta={"unit": "%", "window": "30d_annualized"},
        )
        written += 1
        print(f"[{time.strftime('%H:%M:%S')}] {currency} DVOL={value:.2f}", flush=True)

    print(f"[{time.strftime('%H:%M:%S')}] Deribit 写 {written} 条", flush=True)
    return 0 if written else 1


if __name__ == "__main__":
    sys.exit(main())
