#!/usr/bin/env python3
"""美股板块 ETF + 大盘 + VIX + SOXS 实时价格 — 免费、零延迟、yfinance。

覆盖:
  - 大盘: SPY / QQQ / IWM
  - 波动率: ^VIX (CBOE Volatility Index)
  - 行业: XLE(能源) / XLF(金融) / XLK(科技) / XLV(医疗) / XLY(可选消费) / XLP(必需消费)
  - 半导体: SMH / SOXS (3x 反向半导体)

延迟: yfinance 实时报价 < 1 分钟,对板块轮动判断够用。
"""
from __future__ import annotations

import time
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Dict

_CACHE: Dict[str, Any] = {}
_TTL = 60.0

# (sector_id, ticker, description_zh)
SECTOR_ETFS = (
    ("SPY",  "SPY",  "标普500 ETF"),
    ("QQQ",  "QQQ",  "纳斯达克100 ETF"),
    ("IWM",  "IWM",  "罗素2000 ETF"),
    ("VIX",  "^VIX", "CBOE 波动率"),
    ("XLE",  "XLE",  "能源板块 ETF"),
    ("XLF",  "XLF",  "金融板块 ETF"),
    ("XLK",  "XLK",  "科技板块 ETF"),
    ("XLV",  "XLV",  "医疗板块 ETF"),
    ("XLY",  "XLY",  "可选消费 ETF"),
    ("XLP",  "XLP",  "必需消费 ETF"),
    ("SMH",  "SMH",  "半导体 ETF"),
    ("SOXS", "SOXS", "3x 半导体反向"),
)


def _fetch_one(sid: str, sym: str, desc: str) -> Dict[str, Any]:
    try:
        import yfinance as yf
        info = yf.Ticker(sym).fast_info
        price = float(getattr(info, "last_price", None) or 0) or None
        prev = float(getattr(info, "previous_close_price", None) or 0) or None
        change_pct = None
        if price and prev:
            change_pct = round((price / prev - 1) * 100, 3)
        return {"id": sid, "symbol": sym, "name": desc,
                "price": price, "previous_close": prev,
                "change_pct": change_pct}
    except Exception as exc:
        return {"id": sid, "symbol": sym, "name": desc,
                "error": f"{type(exc).__name__}: {exc}"}


def fetch_sector_etfs() -> Dict[str, Any]:
    """并行拉所有板块 ETF。"""
    now = time.time()
    cached = _CACHE.get("sector_etfs")
    if cached and now - cached["ts"] < _TTL:
        return cached["data"]
    out = {"ts": int(now), "etfs": []}
    with ThreadPoolExecutor(max_workers=8) as ex:
        futs = [ex.submit(_fetch_one, s, sym, d) for s, sym, d in SECTOR_ETFS]
        for f in futs:
            out["etfs"].append(f.result())
    _CACHE["sector_etfs"] = {"ts": now, "data": out}
    return out


def fetch_one(sid: str) -> Dict[str, Any] | None:
    for s, sym, desc in SECTOR_ETFS:
        if s == sid:
            return _fetch_one(s, sym, desc)
    return None


if __name__ == "__main__":
    import json
    print(json.dumps(fetch_sector_etfs(), ensure_ascii=False, indent=2))