#!/usr/bin/env python3
"""第二家交易所的持仓与主动买卖（OKX + Bitfinex）。

为什么需要：
  · 币安那套指标（资金费率、多空比、主动买卖比、未平仓）是单一交易所口径，
    而单所数据很容易被"某一家的做市商结构"带偏。
  · 这里用 OKX 的合约主动买/卖成交量和 Bitfinex 的长/空持仓，做同一维度的
    第二、第三家交叉：两个所的多空结构背离时，往往比单所数值更有信息量。

写入 external_indicators asset=BTC/ETH，source=okx / bitfinex：
  okx_taker_buy / okx_taker_sell   合约主动买、主动卖成交量（币）
  okx_taker_buy_ratio              主动买 / (主动买+主动卖)
  bfx_long_btc / bfx_short_btc     Bitfinex 多头、空头持仓规模
                                  （单位是币本位数量，实测 BTC 多头 91298 → 约 63 亿美元，
                                    接口返回的就是币数，不是美元，不能标 USD）
  bfx_long_short_ratio             多头 / 空头
"""
import os
import sys
import time
from typing import Any, Dict, List, Optional

import requests

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import external_data

requests.Session.trust_env = False
HEADERS = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                         "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0 Safari/537.36"}
TIMEOUT = 15


def _get(url: str, tries: int = 3) -> Any:
    for attempt in range(tries):
        try:
            r = requests.get(url, headers=HEADERS, timeout=TIMEOUT)
            r.raise_for_status()
            return r.json()
        except Exception:
            if attempt == tries - 1:
                return None
            time.sleep(2 * (attempt + 1))
    return None


def okx_taker(sym: str) -> Optional[Dict[str, float]]:
    url = (f"https://www.okx.com/api/v5/rubik/stat/taker-volume"
           f"?ccy={sym}&instType=CONTRACTS&period=5m")
    d = _get(url)
    try:
        rows: List[List[str]] = d["data"]
        # 每行 [ts_ms, sellVol, buyVol]
        buy = float(rows[0][2])
        sell = float(rows[0][1])
        return {"buy": buy, "sell": sell,
                "ratio": buy / (buy + sell) if (buy + sell) else 0.0}
    except (KeyError, IndexError, TypeError, ValueError):
        return None


def bitfinex(sym: str) -> Optional[Dict[str, float]]:
    pair = "tBTCUSD" if sym == "BTC" else "tETHUSD"
    out: Dict[str, float] = {}
    for side in ("long", "short"):
        d = _get(f"https://api-pub.bitfinex.com/v2/stats1/pos.size:1m:{pair}:{side}/last")
        try:
            out[side] = abs(float(d[1]))
        except (KeyError, IndexError, TypeError, ValueError):
            return None
    if out.get("short"):
        out["ratio"] = out["long"] / out["short"]
    return out


def main() -> int:
    ts = int(time.time())
    written = 0

    for sym in ("BTC", "ETH"):
        t = okx_taker(sym)
        if t:
            for ind, key, unit in (("okx_taker_buy", "buy", "币"),
                                   ("okx_taker_sell", "sell", "币"),
                                   ("okx_taker_buy_ratio", "ratio", "0-1")):
                external_data.ingest_external(
                    asset=sym, indicator=ind,
                    records=[{"ts": ts, "value": round(t[key], 6)}],
                    source="okx", meta={"unit": unit, "venue": "OKX SWAP 5m"})
                written += 1
            print(f"[{time.strftime('%H:%M:%S')}] OKX {sym} 主动买 {t['buy']:.2f} / "
                  f"卖 {t['sell']:.2f} → 买占比 {t['ratio']*100:.1f}%", flush=True)
        else:
            print(f"[WARN] OKX {sym} 主动买卖量取不到", flush=True)

        b = bitfinex(sym)
        if b:
            for ind, key, unit in (("bfx_long_btc", "long", "BTC"),
                                   ("bfx_short_btc", "short", "BTC"),
                                   ("bfx_long_short_ratio", "ratio", "倍")):
                external_data.ingest_external(
                    asset=sym, indicator=ind,
                    records=[{"ts": ts, "value": round(b[key], 2)}],
                    source="bitfinex", meta={"unit": unit,
                                             "pair": "tBTCUSD" if sym == "BTC" else "tETHUSD"})
                written += 1
            print(f"[{time.strftime('%H:%M:%S')}] Bitfinex {sym} 多 {b['long']:,.0f} / "
                  f"空 {b['short']:,.0f} → 多空比 {b['ratio']:.2f}", flush=True)
        else:
            print(f"[WARN] Bitfinex {sym} 持仓取不到", flush=True)

    print(f"共写 {written} 条", flush=True)
    return 0 if written else 1


if __name__ == "__main__":
    sys.exit(main())
