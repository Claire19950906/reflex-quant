#!/usr/bin/env python3
"""美国 10 年期国债收益率 (US10Y) — yfinance 拉取 `^TNX`（CBOE 10Y）。

^TNX 是 10 倍报价（即 5.16 = 实际 0.516% 收益？不对：^TNX 报价 1 单位 = 0.01%
   —— 所以 5.16 表示 0.0516 = 5.16% 的收益率。但实际显示值如 5.16 = 5.16%。

缓存 TTL 60s。用途: US10Y 上行 = 美元走强 + 风险资产承压; 下行 = 流动性宽松。
"""
from __future__ import annotations

import time
from typing import Any, Dict, Optional

_CACHE: Dict[str, Any] = {}
_TTL = 60.0


def _yf_symbol() -> str:
    return "^TNX"


def _safe_fast_info(symbol: str, retries: int = 5):
    """带 retry + 软失败的 yfinance fast_info 读取。

    yfinance 1.4.1 对 ^TNX / ^FVX 等部分 ticker 第一次访问 history metadata
    时会因 KeyError: 'exchangeTimezoneName' 崩。重试一次能绕过。
    v189-3-fix: retries=5 + 指数 backoff + 全部异常捕获.
    """
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
    """v189-3-fix: fast_info 连续失败时降级到 history().iloc[-1].
    返回 dict 模拟 fast_info 字段 (last_price/previous_close_price).
    用于 ^TNX/^FVX 等 yfinance 1.4.1 metadata bug 的 ticker.
    多 period fallback: ^FVX 等 ticker 5d 没数据但 30d 有."""
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


def fetch_us10y() -> Optional[Dict[str, Any]]:
    """返回 US10Y 当前收益率(%) + change_bp。
    v189-3-fix: 直接 history() 优先, fast_info 兜底 (^TNX 上 fast_info metadata bug 高发)."""
    now = time.time()
    cached = _CACHE.get("us10y")
    if cached and now - cached["ts"] < _TTL:
        return cached["data"]
    try:
        # ^TNX fast_info 经常坏, history() 更可靠
        fb = _fallback_history(_yf_symbol())
        if fb is None:
            info = _safe_fast_info(_yf_symbol())
            price = float(getattr(info, "last_price", None) or 0) or None
            prev = float(getattr(info, "previous_close_price", None) or 0) or None
        else:
            price = fb.get("last_price")
            prev = fb.get("previous_close_price")
        change_bp = None
        change_pct = None
        if price and prev:
            change_bp = round((price - prev) * 100, 1)
            change_pct = round((price / prev - 1) * 100, 3)
        data = {
            "symbol": _yf_symbol(),
            "asset_class": "us_treasury_yield",
            "asset_name": "US 10Y Treasury Yield",
            "yield_pct": price,
            "previous_close": prev,
            "change_bp": change_bp,
            "change_pct": change_pct,
            "ts": int(now),
        }
        _CACHE["us10y"] = {"ts": now, "data": data}
        return data
    except Exception as exc:
        return {"error": f"{type(exc).__name__}: {exc}",
                "symbol": _yf_symbol(), "ts": int(now)}


def fetch_us5y() -> Optional[Dict[str, Any]]:
    """5 年期国债 (^FVX) — 短端利率，曲线斜率 = US10Y - US5Y。
    v189-3-fix: 直接 history() 优先."""
    now = time.time()
    cached = _CACHE.get("us5y")
    if cached and now - cached["ts"] < _TTL:
        return cached["data"]
    try:
        fb = _fallback_history("^FVX")
        if fb is None:
            info = _safe_fast_info("^FVX")
            price = float(getattr(info, "last_price", None) or 0) or None
            prev = float(getattr(info, "previous_close_price", None) or 0) or None
        else:
            price = fb.get("last_price")
            prev = fb.get("previous_close_price")
        change_bp = round((price - prev) * 100, 1) if (price and prev) else None
        data = {
            "symbol": "^FVX",
            "asset_class": "us_treasury_yield",
            "asset_name": "US 5Y Treasury Yield",
            "yield_pct": price,
            "previous_close": prev,
            "change_bp": change_bp,
            "ts": int(now),
        }
        _CACHE["us5y"] = {"ts": now, "data": data}
        return data
    except Exception as exc:
        return {"error": f"{type(exc).__name__}: {exc}",
                "symbol": "^FVX", "ts": int(now)}


def fetch_yield_curve_spread() -> Optional[Dict[str, Any]]:
    """10Y-5Y 利差(basis points)。曲线倒挂 = 衰退信号。"""
    a10 = fetch_us10y()
    a5 = fetch_us5y()
    if not a10 or not a5 or a10.get("yield_pct") is None or a5.get("yield_pct") is None:
        return None
    spread_bp = round((a10["yield_pct"] - a5["yield_pct"]) * 100, 1)
    return {
        "spread_bp": spread_bp,
        "inverted": spread_bp < 0,
        "ten_y": a10["yield_pct"],
        "five_y": a5["yield_pct"],
        "ts": int(time.time()),
    }


if __name__ == "__main__":
    import json
    print("US10Y:", json.dumps(fetch_us10y(), ensure_ascii=False, indent=2))
    print("US5Y:", json.dumps(fetch_us5y(), ensure_ascii=False, indent=2))
    print("Spread:", json.dumps(fetch_yield_curve_spread(), ensure_ascii=False, indent=2))