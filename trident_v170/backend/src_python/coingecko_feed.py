#!/usr/bin/env python3
"""CoinGecko 市场宽度/广度接入（免费、免 key、直连）。

端点：
  /coins/markets?per_page=250  头部 250 币 24h 涨跌幅 → 涨/跌家数（市场宽度）
  /global                      ETH 市占率 + 全市场 24h 涨跌

与 CoinPaprika 互补：CoinPaprika 给 BTC 市占率 / 总市值 / 成交量（资金轮动），
CoinGecko 给涨跌家数（市场宽度）+ ETH 市占率 + 全市场 24h 涨跌（健康度）。
免费公共 API 限速约 10-30 次/分钟，本层每次构建仅拉 2 个端点，远低于限速。
"""

from __future__ import annotations

import json
import time
import urllib.request
from typing import Any, Callable, Dict, List, Optional

import source_status

HttpGet = Callable[[str, int], Any]
_TIMEOUT = 15
_RETRIES = 3

_GLOBAL_URL = "https://api.coingecko.com/api/v3/global"
_MARKETS_URL = (
    "https://api.coingecko.com/api/v3/coins/markets"
    "?vs_currency=usd&order=market_cap_desc&per_page=250&page=1&price_change_percentage=24h"
)


def _default_get(url: str, timeout: int = _TIMEOUT) -> Any:
    last_exc: Optional[Exception] = None
    for attempt in range(_RETRIES):
        try:
            req = urllib.request.Request(
                url,
                headers={"User-Agent": "TridentAgentMVP/1.0", "Accept": "application/json"},
            )
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                raw = resp.read()
            data = json.loads(raw.decode("utf-8"))
            source_status.report("coingecko", True)
            return data
        except Exception as exc:
            last_exc = exc
            time.sleep(0.6 * (attempt + 1))
    source_status.report("coingecko", False, error=f"{type(last_exc).__name__}: {last_exc}")
    assert last_exc is not None
    raise last_exc


def _num(value: Any) -> Optional[float]:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if number == number else None


def _metric(
    metric_key: str,
    value: Optional[float],
    unit: str,
    *,
    extra: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    payload = extra or {}
    return {
        "category": "breadth",
        "source": "coingecko",
        "metric_key": metric_key,
        "value": value,
        "unit": unit,
        "status": "ok" if value is not None else "unavailable",
        "payload": payload,
    }


def fetch_snapshot(get: HttpGet = _default_get) -> List[Dict[str, Any]]:
    """拉涨跌家数 + ETH 市占率 + 全市场 24h 涨跌。失败标 unavailable。"""
    rows: List[Dict[str, Any]] = []

    # 1) 市场宽度：头部 250 币 24h 涨跌家数
    try:
        markets = get(_MARKETS_URL, _TIMEOUT)
        if not isinstance(markets, list):
            raise ValueError("非数组")
        adv = dec = 0
        for coin in markets:
            if not isinstance(coin, dict):
                continue
            p = _num(coin.get("price_change_percentage_24h"))
            if p is None:
                continue
            if p > 0:
                adv += 1
            elif p < 0:
                dec += 1
        extra: Dict[str, Any] = {"scope": "头部250币", "label": "加密"}
        rows.append(_metric("crypto.advancers", float(adv), "个", extra=extra))
        rows.append(_metric("crypto.decliners", float(dec), "个", extra=extra))
    except Exception as exc:
        extra = {"note": f"{type(exc).__name__}", "label": "加密"}
        rows.append(_metric("crypto.advancers", None, "个", extra=extra))
        rows.append(_metric("crypto.decliners", None, "个", extra=extra))

    # 2) 全局：ETH 市占率 + 全市场 24h 涨跌
    try:
        body = get(_GLOBAL_URL, _TIMEOUT)
        data = body.get("data") if isinstance(body, dict) else None
        if not isinstance(data, dict):
            raise ValueError("非对象")
        pct = data.get("market_cap_percentage") or {}
        eth = _num(pct.get("eth")) if isinstance(pct, dict) else None
        chg = _num(data.get("market_cap_change_percentage_24h_usd"))
        extra = {"label": "加密"}
        rows.append(_metric("crypto.eth_dominance", eth, "%", extra=extra))
        rows.append(_metric("crypto.market_chg_24h", chg, "%", extra=extra))
    except Exception as exc:
        extra = {"note": f"{type(exc).__name__}", "label": "加密"}
        rows.append(_metric("crypto.eth_dominance", None, "%", extra=extra))
        rows.append(_metric("crypto.market_chg_24h", None, "%", extra=extra))

    return rows