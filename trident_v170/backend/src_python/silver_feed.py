#!/usr/bin/env python3
"""白银实时价格 — 免费、零延迟、yfinance (SI=F 白银期货 / SLV ETF)。

黄金-白银比 (Gold/Silver Ratio) 套利核心信号。
"""
from __future__ import annotations

import time
from typing import Any, Dict, Optional

_CACHE: Dict[str, Any] = {}
_TTL = 60.0


def fetch_silver() -> Optional[Dict[str, Any]]:
    """白银期货 SI=F 价 + SLV ETF 价 + 隐含金/银比 (需金价)。"""
    now = time.time()
    cached = _CACHE.get("silver")
    if cached and now - cached["ts"] < _TTL:
        return cached["data"]
    out: Dict[str, Any] = {"ts": int(now)}
    try:
        import yfinance as yf
        si = yf.Ticker("SI=F").fast_info
        out["futures"] = {
            "symbol": "SI=F",
            "price": float(getattr(si, "last_price", None) or 0) or None,
            "previous_close": float(getattr(si, "previous_close_price", None) or 0) or None,
        }
        slv = yf.Ticker("SLV").fast_info
        out["slv_etf"] = {
            "symbol": "SLV",
            "price": float(getattr(slv, "last_price", None) or 0) or None,
        }
        gld = yf.Ticker("GLD").fast_info
        out["gld_etf"] = {
            "symbol": "GLD",
            "price": float(getattr(gld, "last_price", None) or 0) or None,
        }
        # 隐含 Gold/Silver Ratio (近似 = GLD/SLV × 14.58; 1 oz SLV ≈ 0.09 oz 银)
        gld_p = out["gld_etf"]["price"]
        slv_p = out["slv_etf"]["price"]
        if gld_p and slv_p:
            # 更准确: SI/F (白银美元/oz) + GC=F (黄金美元/oz). 退而求其次 GLD/SLV 比 × 65(经验)
            out["approx_gold_silver_ratio"] = round((gld_p / slv_p) * 8.5, 2)
    except Exception as exc:
        out["error"] = f"{type(exc).__name__}: {exc}"
    _CACHE["silver"] = {"ts": now, "data": out}
    return out


if __name__ == "__main__":
    print(fetch_silver())