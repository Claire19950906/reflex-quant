#!/usr/bin/env python3
"""美国国债收益率实时 — 免费、零延迟、yfinance 拉取。

覆盖: ^IRX (13周) / ^FVX (5年) / ^TNX (10年) / ^TYX (30年)。
黄金/风险资产最大联动因子。
"""
from __future__ import annotations

import time
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Dict, Optional

_CACHE: Dict[str, Any] = {}
_TTL = 60.0

YIELDS = (
    ("US13W_3M",  "^IRX"),
    ("US5Y",      "^FVX"),
    ("US10Y",     "^TNX"),
    ("US30Y",     "^TYX"),
)


def _fetch_one(name: str, symbol: str) -> Dict[str, Any]:
    try:
        import yfinance as yf
        s = yf.Ticker(symbol)
        info = s.fast_info
        price = float(getattr(info, "last_price", None) or 0) or None
        prev = float(getattr(info, "previous_close_price", None) or 0) or None
        change = None
        if price is not None and prev:
            change = round(price - prev, 3)
        return {"name": name, "symbol": symbol, "yield_pct": price,
                "previous_close": prev, "change_pct": change}
    except Exception as exc:
        return {"name": name, "symbol": symbol, "error": f"{type(exc).__name__}: {exc}"}


def fetch_yields() -> Dict[str, Any]:
    """并行拉所有期限收益率。"""
    now = time.time()
    cached = _CACHE.get("yields")
    if cached and now - cached["ts"] < _TTL:
        return cached["data"]
    out = {"ts": int(now), "yields": []}
    with ThreadPoolExecutor(max_workers=4) as ex:
        futures = [ex.submit(_fetch_one, n, s) for n, s in YIELDS]
        for f in futures:
            out["yields"].append(f.result())
    _CACHE["yields"] = {"ts": now, "data": out}
    return out


def fetch_one(name: str) -> Optional[Dict[str, Any]]:
    """单期限收益率。name: US10Y / US30Y / US5Y / US13W_3M"""
    sym_map = dict(YIELDS)
    if name not in sym_map:
        return None
    return _fetch_one(name, sym_map[name])


if __name__ == "__main__":
    import json
    print(json.dumps(fetch_yields(), ensure_ascii=False, indent=2))