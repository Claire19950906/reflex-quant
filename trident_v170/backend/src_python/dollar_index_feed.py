#!/usr/bin/env python3
"""美元指数 (DXY) 实时价格 — 免费、零延迟、yfinance 拉取。

DXY = U.S. Dollar Index (USDX) futures，yfinance ticker `DX-Y.NYB`。
缓存 TTL 60s（金融数据本身 15min 延迟但不影响套利节奏，仅作背景对照）。
"""
from __future__ import annotations

import time
from typing import Any, Dict, Optional

import http_retry

_CACHE: Dict[str, Any] = {}
_TTL = 60.0
_TIMEOUT = 4.0


def _yf_symbol() -> str:
    return "DX-Y.NYB"


def _safe_fast_info(sym: str, retries: int = 3) -> Optional[Any]:
    """v189-3-fix: yfinance fast_info 在某些字段缺失时会抛 KeyError, 用 retry 兜底.
    es_feed/nq_feed/us10y_feed 都用同样模式. DXY 之前没加所以报 KeyError."""
    import yfinance as yf
    last_exc = None
    for i in range(retries):
        try:
            return yf.Ticker(sym).fast_info
        except Exception as exc:
            last_exc = exc
            time.sleep(0.5 * (i + 1))
    raise last_exc


def _fallback_history(sym: str, periods=("30d", "60d", "1y")):
    """v189-3-fix: fast_info 失败时降级 history() — DX-Y.NYB 等 ticker 上偶发 metadata bug.
    多 period fallback."""
    import yfinance as yf
    s = yf.Ticker(sym)
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


def fetch_dxy() -> Optional[Dict[str, Any]]:
    """返回 DXY 当前价 + change_pct。无数据 None。
    v189-3-fix: 直接用 history() 避免 fast_info metadata bug."""
    now = time.time()
    cached = _CACHE.get("dxy")
    if cached and now - cached["ts"] < _TTL:
        return cached["data"]
    try:
        # DXY ticker 上 fast_info 经常坏, 直接用 history() 更可靠
        fb = _fallback_history(_yf_symbol())
        if fb is None:
            # 最后兜底再试 fast_info
            info = _safe_fast_info(_yf_symbol())
            price = float(getattr(info, "last_price", None) or 0) or None
            prev = float(getattr(info, "previous_close_price", None) or 0) or None
        else:
            price = fb.get("last_price")
            prev = fb.get("previous_close_price")
        change_pct = None
        if price and prev:
            change_pct = round((price / prev - 1) * 100, 3)
        data = {
            "symbol": _yf_symbol(),
            "price": price,
            "previous_close": prev,
            "change_pct": change_pct,
            "ts": int(now),
        }
        _CACHE["dxy"] = {"ts": now, "data": data}
        return data
    except Exception as exc:
        return {"error": f"{type(exc).__name__}: {exc}", "ts": int(now)}


if __name__ == "__main__":
    print(fetch_dxy())