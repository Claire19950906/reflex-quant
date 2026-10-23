"""
P1-C 挂单量 (Order Book Depth) 数据源
======================================
从 Binance 公共 API 拉取 BTCUSDT / ETHUSDT 深度, 计算 7 类挂单量指标:
  1. total_bid_qty / total_ask_qty   - 前 N 档累计挂单量 (基础货币)
  2. bid_ask_ratio                    - bid/ask 力量比 (>1 偏多, <1 偏空)
  3. mid_price                        - (best_bid + best_ask) / 2
  4. spread_bps                       - 价差 (基点, 1bp = 0.01%)
  5. large_bid_qty / large_ask_qty   - 单笔 > THRESHOLD_USD 的大单累计
  6. large_order_imbalance            - (large_bid - large_ask) / (large_bid + large_ask) ∈ [-1, 1]
  7. depth_50bps_pct                  - 距离中间价 ±50bps 范围内的挂单量占比 (近端流动性)

缓存: in-memory TTL=60s (避免 rate limit)
"""
from __future__ import annotations
import json
import time
import urllib.request
import urllib.error
import ssl
from typing import Any

BINANCE_DEPTH_URL = "https://api.binance.com/api/v3/depth"
SUPPORTED_SYMBOLS = ("BTCUSDT", "ETHUSDT")
CACHE_TTL_S = 60
DEPTH_LIMIT = 100
LARGE_ORDER_USD_THRESHOLD = 50_000  # $50K — 平衡的 BTC 大单阈值 (实测 100K 时买盘为 0)
NEAR_TOLERANCE_BPS = 50
REQUEST_TIMEOUT = 8
MAX_RETRIES = 3

_cache: dict = {}


def _http_get_json(url: str, timeout: int = REQUEST_TIMEOUT) -> dict:
    """使用 requests 库 (处理 TLS 重试更稳定) + urllib fallback."""
    try:
        import requests
        from requests.adapters import HTTPAdapter
        from urllib3.util.retry import Retry
        session = requests.Session()
        retries = Retry(
            total=MAX_RETRIES, backoff_factor=0.3,
            status_forcelist=[429, 500, 502, 503, 504],
            allowed_methods=["GET"],
        )
        adapter = HTTPAdapter(max_retries=retries)
        session.mount("https://", adapter)
        session.mount("http://", adapter)
        r = session.get(url, timeout=timeout, headers={"User-Agent": "trident-bridge/1.0"})
        r.raise_for_status()
        return r.json()
    except ImportError:
        pass
    # fallback: urllib
    req = urllib.request.Request(url, headers={"User-Agent": "trident-bridge/1.0"})
    ctx = ssl.create_default_context()
    last_err: Exception | None = None
    for attempt in range(MAX_RETRIES):
        try:
            with urllib.request.urlopen(req, timeout=timeout, context=ctx) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except (urllib.error.URLError, ssl.SSLError) as e:
            last_err = e
            time.sleep(0.3 * (attempt + 1))
    raise last_err  # type: ignore[misc]


def _fetch_raw_depth(symbol: str) -> dict:
    url = f"{BINANCE_DEPTH_URL}?symbol={symbol}&limit={DEPTH_LIMIT}"
    return _http_get_json(url)


def _compute_metrics(raw: dict, symbol: str) -> dict:
    bids = [(float(p), float(q)) for p, q in raw.get("bids", [])]
    asks = [(float(p), float(q)) for p, q in raw.get("asks", [])]
    if not bids or not asks:
        return {"ok": False, "error": "empty_orderbook", "symbol": symbol}

    best_bid, _ = bids[0]
    best_ask, _ = asks[0]
    mid_price = (best_bid + best_ask) / 2
    spread = best_ask - best_bid
    spread_bps = (spread / mid_price) * 10_000 if mid_price else 0.0

    total_bid_qty = sum(q for _, q in bids)
    total_ask_qty = sum(q for _, q in asks)
    total_bid_usd = sum(p * q for p, q in bids)
    total_ask_usd = sum(p * q for p, q in asks)
    bid_ask_ratio = (total_bid_qty / total_ask_qty) if total_ask_qty else 0.0

    large_bids = [(p, q) for p, q in bids if p * q >= LARGE_ORDER_USD_THRESHOLD]
    large_asks = [(p, q) for p, q in asks if p * q >= LARGE_ORDER_USD_THRESHOLD]
    large_bid_qty = sum(q for _, q in large_bids)
    large_ask_qty = sum(q for _, q in large_asks)
    large_bid_usd = sum(p * q for p, q in large_bids)
    large_ask_usd = sum(p * q for p, q in large_asks)
    large_total_usd = large_bid_usd + large_ask_usd
    large_order_imbalance = (
        (large_bid_usd - large_ask_usd) / large_total_usd if large_total_usd else 0.0
    )

    near_bid_floor = mid_price * (1 - NEAR_TOLERANCE_BPS / 10_000)
    near_ask_ceiling = mid_price * (1 + NEAR_TOLERANCE_BPS / 10_000)
    near_bid_usd = sum(p * q for p, q in bids if p >= near_bid_floor)
    near_ask_usd = sum(p * q for p, q in asks if p <= near_ask_ceiling)
    near_total = near_bid_usd + near_ask_usd
    near_bid_pct = (near_bid_usd / near_total * 100) if near_total else 0.0
    near_ask_pct = (near_ask_usd / near_total * 100) if near_total else 0.0

    return {
        "ok": True,
        "symbol": symbol,
        "ts": int(time.time()),
        "best_bid": best_bid,
        "best_ask": best_ask,
        "mid_price": mid_price,
        "spread_bps": round(spread_bps, 3),
        "total_bid_qty": round(total_bid_qty, 6),
        "total_ask_qty": round(total_ask_qty, 6),
        "total_bid_usd": round(total_bid_usd, 2),
        "total_ask_usd": round(total_ask_usd, 2),
        "bid_ask_ratio": round(bid_ask_ratio, 4),
        "large_bid_qty": round(large_bid_qty, 6),
        "large_ask_qty": round(large_ask_qty, 6),
        "large_bid_usd": round(large_bid_usd, 2),
        "large_ask_usd": round(large_ask_usd, 2),
        "large_order_imbalance": round(large_order_imbalance, 4),
        "n_large_bids": len(large_bids),
        "n_large_asks": len(large_asks),
        "near_50bps_bid_usd": round(near_bid_usd, 2),
        "near_50bps_ask_usd": round(near_ask_usd, 2),
        "near_50bps_bid_pct": round(near_bid_pct, 1),
        "near_50bps_ask_pct": round(near_ask_pct, 1),
        "n_levels": len(bids),
    }


def fetch_orderbook_metrics(symbol: str, force_refresh: bool = False) -> dict:
    """获取挂单量指标 (带 60s in-memory 缓存)."""
    symbol = symbol.upper()
    if symbol not in SUPPORTED_SYMBOLS:
        return {"ok": False, "error": f"unsupported_symbol:{symbol}",
                "supported": list(SUPPORTED_SYMBOLS)}

    now = time.time()
    if not force_refresh and symbol in _cache:
        ts, cached = _cache[symbol]
        if now - ts < CACHE_TTL_S:
            return {**cached, "cached": True, "cache_age_s": int(now - ts)}

    try:
        raw = _fetch_raw_depth(symbol)
        metrics = _compute_metrics(raw, symbol)
    except (urllib.error.URLError, ssl.SSLError, json.JSONDecodeError, KeyError, TimeoutError) as e:
        if symbol in _cache:
            ts, cached = _cache[symbol]
            return {**cached, "cached": True, "stale": True,
                    "cache_age_s": int(now - ts), "fetch_error": str(e)}
        return {"ok": False, "symbol": symbol, "error": f"fetch_failed:{e}"}

    _cache[symbol] = (now, metrics)
    return {**metrics, "cached": False}


if __name__ == "__main__":
    import sys
    sym = sys.argv[1] if len(sys.argv) > 1 else "BTCUSDT"
    print(json.dumps(fetch_orderbook_metrics(sym, force_refresh=True), indent=2, ensure_ascii=False))
