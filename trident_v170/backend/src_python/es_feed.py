#!/usr/bin/env python3
"""E-mini S&P 500 期货 (ES) 实时价格 — yfinance `ES=F`。

yfinance 1.4.1 fast_info.last_price 在 ES=F / NQ=F 上偶尔抛 KeyError,
故加 _safe_fast_info() retry (与 us10y_feed 同思路)。

用途: 美股开盘前 ES 走势给 BTC/XAU 做引子。
"""
from __future__ import annotations

import time
from typing import Any, Dict, Optional

_CACHE: Dict[str, Any] = {}
_TTL = 60.0


def _yf_symbol() -> str:
    return "ES=F"


def _safe_fast_info(symbol: str, retries: int = 5):
    """带 retry 的 yfinance fast_info — yfinance 1.4.1 在部分 ticker 上偶发 KeyError。
    v189-3-fix: 加指数 backoff + 全部异常捕获."""
    import yfinance as yf
    last_err = None
    for i in range(retries):
        try:
            return yf.Ticker(symbol).fast_info
        except Exception as exc:
            last_err = exc
            time.sleep(0.5 * (i + 1))
    raise last_err  # type: ignore


def _fallback_history(symbol: str, periods=("30d", "60d", "1y")):
    """v189-3-fix: fast_info 永久坏时降级 history().iloc[-1].
    多 period fallback, 适应不同 ticker 的可用数据范围."""
    import yfinance as yf
    s = yf.Ticker(symbol)
    for p in periods:
        try:
            h = s.history(period=p, auto_adjust=False)
            if h is not None and len(h) > 0:
                last = h["Close"].iloc[-1]
                prev = h["Close"].iloc[-2] if len(h) >= 2 else last
                return {"last_price": float(last), "previous_close_price": float(prev)}
        except Exception:
            continue
    return None


def fetch_es() -> Optional[Dict[str, Any]]:
    """返回 ES 当前价 + change_pct。失败返回 error dict。"""
    now = time.time()
    cached = _CACHE.get("es")
    if cached and now - cached["ts"] < _TTL:
        return cached["data"]
    try:
        try:
            info = _safe_fast_info(_yf_symbol())
            price = float(getattr(info, "last_price", None) or 0) or None
            prev = float(getattr(info, "previous_close_price", None) or 0) or None
        except Exception:
            fb = _fallback_history(_yf_symbol())
            if fb is None:
                raise
            price = fb.get("last_price")
            prev = fb.get("previous_close_price")
        change_pct = round((price / prev - 1) * 100, 3) if (price and prev) else None
        data = {
            "symbol": _yf_symbol(),
            "asset_class": "us_index_futures",
            "asset_name": "E-mini S&P 500",
            "price": price,
            "previous_close": prev,
            "change_pct": change_pct,
            "ts": int(now),
        }
        _CACHE["es"] = {"ts": now, "data": data}
        return data
    except Exception as exc:
        return {"error": f"{type(exc).__name__}: {exc}",
                "symbol": _yf_symbol(), "ts": int(now)}


if __name__ == "__main__":
    print(fetch_es())


if __name__ == "__main__":
    import json
    print(json.dumps(fetch_es(), ensure_ascii=False, indent=2))