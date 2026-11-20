#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
free_alt_sources.py —— 不依赖代理的免费替代源（无 key）

背景：CoinGecko / FRED / OKX 在本机不走代理就连不上，Clash 一关这几路数据就停。
这一轮实测找了三家**直连可用、免 key**的替代源，先把最容易断的几路补上：

  1) DefiLlama 稳定币  https://stablecoins.llama.fi/stablecoins?includePrices=false
     → 替代 CoinGecko 的稳定币总供应（原来 11-12 就断了）
  2) CoinPaprika 全局   https://api.coinpaprika.com/v1/global
     → 市值 / BTC 占比的独立交叉验证
  3) er-api 汇率       https://open.er-api.com/v6/latest/USD
     → frankfurter 连不上时的汇率备胎

约定：拿不到就跳过并打印原因，**不写字、不补零**。
"""
import json
import subprocess
import sys
import time
from typing import Any, Dict, List, Optional, Tuple

import db
import external_data

# rc=35（TLS 握手被打断）在这台机器上是常态，实测三家源都是间歇性的，
# 所以统一交给 curl 自己重试，别在 Python 里写循环。
CURL = ["curl", "-sL", "-m", "20", "--noproxy", "*", "--compressed",
        "--retry", "4", "--retry-delay", "1", "--retry-all-errors",
        "--retry-max-time", "60",
        "-A", "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7)"]


def _get(url: str) -> Optional[Any]:
    try:
        out = subprocess.run(CURL + ["-w", "\n__HTTP:%{http_code}", url],
                             capture_output=True, text=True, timeout=40)
        body = out.stdout.rsplit("\n__HTTP:", 1)
        code = body[1].strip() if len(body) == 2 else "?"
        payload = body[0].strip()
        if not payload:
            print(f"  [WARN] {url.split('/')[2]} HTTP {code} 空响应 rc={out.returncode} "
                  f"err={out.stderr.strip()[:80]}", flush=True)
            return None
        return json.loads(payload)
    except Exception as exc:
        print(f"  [WARN] {url.split('/')[2]} 失败: {type(exc).__name__}", flush=True)
        return None


def _ingest(asset: str, indicator: str, value: float, source: str,
            meta: Optional[Dict[str, Any]] = None) -> None:
    external_data.ingest_external(
        asset, indicator, [{"ts": int(time.time()), "value": float(value)}],
        source=source, meta=meta or {})


def collect_stablecoins() -> int:
    """DefiLlama: 稳定币总供应 + USDT / USDC 单币市值。"""
    data = _get("https://stablecoins.llama.fi/stablecoins?includePrices=false")
    if not data or "peggedAssets" not in data:
        print("  [SKIP] DefiLlama 无数据")
        return 0
    rows = 0
    total = 0.0
    per: Dict[str, float] = {}
    for a in data["peggedAssets"]:
        sym = (a.get("symbol") or "").upper()
        circ = (a.get("circulating") or {})
        v = circ.get("peggedUSD") if isinstance(circ, dict) else None
        if v is None:
            continue
        total += float(v)
        if sym in ("USDT", "USDC", "DAI", "USDE", "FDUSD"):
            per[sym] = float(v)
    if total <= 0:
        print("  [SKIP] DefiLlama 总量为 0")
        return 0
    _ingest("CRYPTO", "stable_supply_total", total, "defillama",
            {"unit": "USD", "n_coins": len(data["peggedAssets"])})
    rows += 1
    for sym, v in per.items():
        _ingest(sym, "sc_mcap_usd", v, "defillama", {"unit": "USD"})
        rows += 1
    print(f"  稳定币总量 {total/1e9:.2f}B（{len(per)} 个单币）")
    return rows


def collect_global() -> int:
    """CoinPaprika: 总市值 / BTC 占比 / 24h 量，给 CoinGecko 做交叉验证。"""
    data = _get("https://api.coinpaprika.com/v1/global")
    if not data:
        print("  [SKIP] CoinPaprika 无数据")
        return 0
    rows = 0
    mcap = data.get("market_cap_usd")
    if mcap:
        _ingest("CRYPTO", "cp_total_mcap_usd", mcap, "coinpaprika", {"unit": "USD"})
        rows += 1
    dom = data.get("bitcoin_dominance_percentage")
    if dom:
        _ingest("BTC", "cp_btc_dominance", dom, "coinpaprika", {"unit": "pct"})
        rows += 1
    vol = data.get("volume_24h_usd")
    if vol:
        _ingest("CRYPTO", "cp_volume_24h_usd", vol, "coinpaprika", {"unit": "USD"})
        rows += 1
    print(f"  市值 {float(mcap or 0)/1e12:.2f}T | BTC 占比 {dom}%")
    return rows


def collect_fx() -> int:
    """er-api: 美元兑主要货币。仅当本地已有值明显过期时才写，避免与主源打架。"""
    data = _get("https://open.er-api.com/v6/latest/USD")
    rates = (data or {}).get("rates") or {}
    if not rates:
        print("  [SKIP] er-api 无汇率")
        return 0
    want: List[Tuple[str, str]] = [("CNY", "fx_usdcny"), ("EUR", "fx_eurusd"),
                                   ("JPY", "fx_usdjpy")]
    rows = 0
    for code, ind in want:
        v = rates.get(code)
        if not v:
            continue
        if code == "EUR":
            v = 1.0 / float(v)          # 接口是 USD→EUR，库里要 EUR/USD
        _ingest("FX", ind, v, "erapi", {"unit": "rate", "quote": f"USD/{code}"})
        rows += 1
    print(f"  汇率 USD/CNY={rates.get('CNY')} EUR/USD={1/float(rates.get('EUR',1)):.4f}")
    return rows


def collect_mexc_funding() -> int:
    """MEXC 资金费率：OKX 连不上时的替代（本机直连 200，无需代理）。"""
    data = _get("https://api.mexc.com/api/v1/contract/funding_rate?symbol=BTC_USDT")
    rows = 0
    items = (data or {}).get("data") or []
    if isinstance(items, dict):
        items = [items]
    for it in items:
        sym = str(it.get("symbol") or "")
        rate = it.get("fundingRate")
        if not sym or rate is None:
            continue
        coin = sym.split("_")[0]
        _ingest(coin, "mexc_funding_rate", float(rate), "mexc",
                {"unit": "rate", "symbol": sym})
        rows += 1
        if rows >= 5:
            break
    if rows:
        print(f"  MEXC 资金费率 {rows} 个合约")
    else:
        print("  [SKIP] MEXC 无资金费率")
    return rows


def main(argv=None) -> int:
    print(f"[{time.strftime('%H:%M:%S')}] 免费替代源采集")
    total = 0
    for fn in (collect_stablecoins, collect_global, collect_fx, collect_mexc_funding):
        try:
            total += fn()
        except Exception as exc:
            print(f"  [ERROR] {fn.__name__}: {type(exc).__name__}: {exc}")
    print(f"  共写 {total} 条")
    return 0


if __name__ == "__main__":
    sys.exit(main())
