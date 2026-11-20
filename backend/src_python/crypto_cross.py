#!/usr/bin/env python3
"""跨交易所现货价交叉验证（Kraken / Bitstamp / Coinbase / OKX）。

为什么需要：
  · 系统里 BTC/ETH 的价格只有一个来源（金十/币安），一旦那个源报价异常
    （插针、延迟、单所价差），下游所有判断都跟着错，而且没人能发现。
  · 这里同时取 4 家独立交易所的现货最新价，算出**跨所价差**：
      价差突然放大 = 流动性断裂 / 某所异常 / 有套利空间，
    这是单源报价永远看不到的信息，也是"双验证"最直接的一种落地。

写入 external_indicators：
  asset=BTC/ETH
    x_spot_kraken / x_spot_bitstamp / x_spot_coinbase / x_spot_okx  各所最新价
    x_spot_median          四所中位数（比任何单所都稳）
    x_spot_dispersion_pct   (最高 − 最低) / 中位数 × 100，跨所离散度
    x_spot_venue_count      本轮成功取到报价的家数
"""
import os
import statistics
import sys
import time
from typing import Any, Dict, List

import requests

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import external_data

requests.Session.trust_env = False
HEADERS = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                         "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0 Safari/537.36"}
TIMEOUT = 12


def _get(url: str) -> Any:
    for attempt in range(2):
        try:
            r = requests.get(url, headers=HEADERS, timeout=TIMEOUT)
            r.raise_for_status()
            return r.json()
        except Exception:
            if attempt == 1:
                return None
            time.sleep(1)
    return None


def kraken(sym: str) -> float | None:
    pair = "XBTUSD" if sym == "BTC" else "ETHUSD"
    d = _get(f"https://api.kraken.com/0/public/Ticker?pair={pair}")
    if not d or d.get("error"):
        return None
    for v in (d.get("result") or {}).values():
        try:
            return float(v["c"][0])
        except (KeyError, ValueError, IndexError):
            return None
    return None


def bitstamp(sym: str) -> float | None:
    d = _get(f"https://www.bitstamp.net/api/v2/ticker/{sym.lower()}usd/")
    try:
        return float((d or {})["last"])
    except (KeyError, TypeError, ValueError):
        return None


def coinbase(sym: str) -> float | None:
    d = _get(f"https://api.exchange.coinbase.com/products/{sym}-USD/ticker")
    try:
        return float((d or {})["price"])
    except (KeyError, TypeError, ValueError):
        return None


def okx(sym: str) -> float | None:
    d = _get(f"https://www.okx.com/api/v5/market/ticker?instId={sym}-USDT")
    try:
        return float(d["data"][0]["last"])
    except (KeyError, IndexError, TypeError, ValueError):
        return None


VENUES = (("kraken", kraken), ("bitstamp", bitstamp),
          ("coinbase", coinbase), ("okx", okx))


def main() -> int:
    ts = int(time.time())
    total = 0
    for sym in ("BTC", "ETH"):
        quotes: Dict[str, float] = {}
        for name, fn in VENUES:
            px = fn(sym)
            if px and px > 0:
                quotes[name] = px
            time.sleep(0.4)

        if not quotes:
            print(f"[WARN] {sym} 四家报价全失败，本轮跳过", flush=True)
            continue

        for name, px in quotes.items():
            external_data.ingest_external(
                asset=sym, indicator=f"x_spot_{name}",
                records=[{"ts": ts, "value": round(px, 2)}],
                source="xvenue", meta={"unit": "USD", "sym": sym},
            )
            total += 1

        med = statistics.median(quotes.values())
        disp = (max(quotes.values()) - min(quotes.values())) / med * 100.0 if med else 0.0
        for ind, val, meta in (
            ("x_spot_median", round(med, 2), {"n": len(quotes)}),
            ("x_spot_dispersion_pct", round(disp, 4), {"venues": sorted(quotes)}),
            ("x_spot_venue_count", len(quotes), {"venues": sorted(quotes)}),
        ):
            external_data.ingest_external(
                asset=sym, indicator=ind, records=[{"ts": ts, "value": val}],
                source="xvenue", meta={"unit": "%" if "pct" in ind else "USD", **meta},
            )
            total += 1
        print(f"[{time.strftime('%H:%M:%S')}] {sym} 四所中位 {med:.2f} | "
              f"价差 {disp:.3f}% | 家数 {len(quotes)}", flush=True)

    print(f"共写 {total} 条", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
