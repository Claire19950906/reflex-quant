#!/usr/bin/env python3
"""永续合约资金费率 (perpetual futures funding rate) — OKX 公共 API。

OKX 公共 API 无需 API key, 即可拉取永续合约当前资金费率。
  - API: https://www.okx.com/api/v5/public/funding-rate?instId=BTC-USDT-SWAP
  - 缓存 TTL 60s

资金费率含义:
  - 正费率 (+0.01%/8h): 多头付空头, 市场偏多
  - 负费率 (-0.01%/8h): 空头付多头, 市场偏空
  - 极端费率 (±0.1%+): 套利机会 / 顶部信号

注: 仅拉 USDT 永续 (最常用)。
"""
from __future__ import annotations

import time
from typing import Any, Dict, List, Optional

import http_retry

_CACHE: Dict[str, Any] = {}
_TTL = 60.0
_SESSION = None


def _get_session():
    """延迟初始化共享 session（http_retry 唯一暴露的工厂）。"""
    global _SESSION
    if _SESSION is None:
        import http_retry as _hr
        _SESSION = _hr.new_retry_session()
    return _SESSION

# 默认拉取的永续品种
_DEFAULT_INSTRUMENTS = [
    "BTC-USDT-SWAP",
    "ETH-USDT-SWAP",
    "SOL-USDT-SWAP",
    "XAU-USDT-SWAP",  # OKX 现在有 XAU 永续
]

_OKX_URL = "https://www.okx.com/api/v5/public/funding-rate"


def _fetch_one(inst_id: str) -> Optional[Dict[str, Any]]:
    """拉单个永续合约当前费率。"""
    try:
        sess = _get_session()
        resp = sess.get(_OKX_URL, params={"instId": inst_id}, timeout=4.0)
        data = resp.json() if hasattr(resp, "json") else {}
        if data.get("code") == "0" and data.get("data"):
            d = data["data"][0]
            return {
                "instrument": inst_id,
                "funding_rate": float(d.get("fundingRate", 0)),
                "next_funding_time": d.get("nextFundingTime"),
                "interest_rate": float(d.get("interestRate", 0)),
                "max_funding_rate": float(d.get("maxFundingRate", 0)),
                "min_funding_rate": float(d.get("minFundingRate", 0)),
                "settle_funding_rate": float(d.get("settleFundingRate", 0)),
                "ts": int(time.time()),
            }
        return {"instrument": inst_id, "error": data.get("msg", "no data"),
                "ts": int(time.time())}
    except Exception as exc:
        return {"instrument": inst_id,
                "error": f"{type(exc).__name__}: {exc}",
                "ts": int(time.time())}


def fetch_funding_rates(
    instruments: Optional[List[str]] = None,
) -> Dict[str, Any]:
    """批量拉永续合约资金费率。

    返回 {ok, ts, items: [...]}，每个 item 含 funding_rate / next_funding_time。
    """
    insts = instruments or _DEFAULT_INSTRUMENTS
    now = time.time()
    cached = _CACHE.get("batch")
    if cached and now - cached["ts"] < _TTL and cached.get("insts") == insts:
        return cached["data"]
    items = [_fetch_one(i) for i in insts]
    out = {"ok": True, "ts": int(now), "items": items}
    # 极端费率告警
    extreme = [it for it in items
               if it.get("funding_rate") is not None
               and abs(it["funding_rate"]) >= 0.0005]  # ≥ 0.05%/8h
    out["extreme_count"] = len(extreme)
    out["extreme"] = [{"inst": e["instrument"],
                        "rate_pct": round(e["funding_rate"] * 100, 4)}
                       for e in extreme]
    _CACHE["batch"] = {"ts": now, "data": out, "insts": insts}
    return out


def fetch_btc_funding() -> Optional[Dict[str, Any]]:
    """单 BTC 永续 — 套利最常用。"""
    now = time.time()
    cached = _CACHE.get("btc")
    if cached and now - cached["ts"] < _TTL:
        return cached["data"]
    out = _fetch_one("BTC-USDT-SWAP")
    _CACHE["btc"] = {"ts": now, "data": out}
    return out


if __name__ == "__main__":
    import json
    print("BTC:", json.dumps(fetch_btc_funding(), ensure_ascii=False, indent=2))
    print()
    print("Batch:", json.dumps(fetch_funding_rates(), ensure_ascii=False, indent=2))