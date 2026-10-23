#!/usr/bin/env python3
"""美股基本面/估值层 — FMP(/stable/) 主力 + Alpha Vantage 兜底，内置限时缓存。

免费配额（务必 TTL 缓存，防高频烧份额）：
  - FMP 免费 250 次/日：关注标的 × 2 端点（quote + ratios-ttm）= 10 次/轮，
    TTL 默认 6h → 约 40 次/日，留足余量。
  - Alpha Vantage 免费 25 次/日：FMP 失败标的逐只用 AV OVERVIEW 补齐（≤5 次/轮），
    配合 6h 缓存最坏 ~20 次/日，留余量。

标的：config.EQUITY_TICKERS（默认加密高相关 NVDA/MSTR/COIN/MARA/RIOT）。
估值/基本面为慢变量，标注 freshness=delayed → 仅作背景/估值对照，勿当即时信号。
"""

from __future__ import annotations

import json
import threading
import time
import urllib.request
from typing import Any, Callable, Dict, List, Optional, Tuple

import config
import source_status

HttpGet = Callable[[str, int], Any]
_TIMEOUT = 15
_RETRIES = 3
_AV_TTL = 43200  # 12h，Alpha Vantage 配额极紧

# 进程内缓存：key -> (expire_ts, rows)
_cache: Dict[str, Tuple[float, List[Dict[str, Any]]]] = {}
_cache_lock = threading.Lock()

_UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
       "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36")


def _default_get(url: str, timeout: int = _TIMEOUT) -> Any:
    last: Optional[Exception] = None
    for attempt in range(_RETRIES):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": _UA, "Accept": "application/json"})
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                raw = resp.read()
            return json.loads(raw.decode("utf-8", "replace"))
        except Exception as exc:
            last = exc
            time.sleep(0.6 * (attempt + 1))
    assert last is not None
    raise last


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
    label: str,
    extra: Optional[Dict[str, Any]] = None,
    source: str = "fmp",
) -> Dict[str, Any]:
    payload = dict(extra or {})
    payload["label"] = label
    return {
        "category": "equity",
        "source": source,
        "metric_key": metric_key,
        "value": value,
        "unit": unit,
        "status": "ok" if value is not None else "unavailable",
        "payload": payload,
    }


def _set_cache(key: str, rows: List[Dict[str, Any]], ttl: float) -> None:
    with _cache_lock:
        _cache[key] = (time.time() + ttl, list(rows))


def _unavailable_all() -> List[Dict[str, Any]]:
    tickers = config.EQUITY_TICKERS or ("NVDA",)
    rows: List[Dict[str, Any]] = []
    for ticker in tickers:
        rows.extend(_unavailable_one(ticker))
    return rows


def _unavailable_one(ticker: str) -> List[Dict[str, Any]]:
    rows: List[Dict[str, Any]] = []
    for key, unit in (("eq.mktcap", "十亿美元"), ("eq.pe", "x"), ("eq.peg", "x"),
                      ("eq.net_margin", "%"), ("eq.debt_to_equity", "x")):
        rows.append(_metric(key, None, unit, ticker, {"freshness": "delayed", "note": "源不可用"}))
    return rows


def _fmp_one(get: HttpGet, ticker: str) -> List[Dict[str, Any]]:
    """拉单只股票的估值/基本面（quote 市值 + ratios-ttm 估值）。失败标 unavailable。"""
    quote: Any = None
    ratios: Any = None
    try:
        q = get(f"{config.FMP_BASE_URL}/stable/quote?symbol={ticker}&apikey={config.FMP_API_KEY}", _TIMEOUT)
        quote = q[0] if isinstance(q, list) and q else None
    except Exception:
        quote = None
    try:
        r = get(f"{config.FMP_BASE_URL}/stable/ratios-ttm?symbol={ticker}&apikey={config.FMP_API_KEY}", _TIMEOUT)
        ratios = r[0] if isinstance(r, list) and r else None
    except Exception:
        ratios = None

    mktcap = _num(quote.get("marketCap")) if isinstance(quote, dict) else None
    pe = _num(ratios.get("priceToEarningsRatioTTM")) if isinstance(ratios, dict) else None
    peg = _num(ratios.get("priceToEarningsGrowthRatioTTM")) if isinstance(ratios, dict) else None
    nm = _num(ratios.get("netProfitMarginTTM")) if isinstance(ratios, dict) else None
    de = _num(ratios.get("debtToEquityRatioTTM")) if isinstance(ratios, dict) else None

    extra: Dict[str, Any] = {"freshness": "delayed", "scope": "基本面/估值"}
    return [
        _metric("eq.mktcap", (mktcap / 1e9) if mktcap is not None else None, "十亿美元", ticker, extra),
        _metric("eq.pe", pe, "x", ticker, extra),
        _metric("eq.peg", peg, "x", ticker, extra),
        _metric("eq.net_margin", (nm * 100.0) if nm is not None else None, "%", ticker, extra),
        _metric("eq.debt_to_equity", de, "x", ticker, extra),
    ]


def _av_fallback(get: HttpGet, ticker: str) -> Optional[List[Dict[str, Any]]]:
    """Alpha Vantage OVERVIEW 补齐单只标的（FMP 该标的失败时调用一次）。"""
    try:
        body = get(
            f"https://www.alphavantage.co/query?function=OVERVIEW&symbol={ticker}"
            f"&apikey={config.ALPHAVANTAGE_API_KEY}",
            _TIMEOUT,
        )
    except Exception:
        return None
    if not isinstance(body, dict) or (not body.get("Symbol") and not body.get("PERatio")):
        return None
    pe = _num(body.get("PERatio"))
    peg = _num(body.get("PEGRatio"))
    nm = _num(body.get("ProfitMargin"))
    mktcap = _num(body.get("MarketCapitalization"))
    extra: Dict[str, Any] = {"freshness": "delayed", "scope": "估值(AV)"}
    return [
        _metric("eq.mktcap", (mktcap / 1e9) if mktcap is not None else None, "十亿美元", ticker, extra, source="alphavantage"),
        _metric("eq.pe", pe, "x", ticker, extra, source="alphavantage"),
        _metric("eq.peg", peg, "x", ticker, extra, source="alphavantage"),
        _metric("eq.net_margin", (nm * 100.0) if nm is not None else None, "%", ticker, extra, source="alphavantage"),
        _metric("eq.debt_to_equity", None, "x", ticker, extra, source="alphavantage"),
    ]


def fetch_snapshot(get: HttpGet = _default_get) -> List[Dict[str, Any]]:
    """美股基本面层（带 TTL 缓存）。FMP 主力 + Alpha Vantage 逐标的补齐。

    FMP 免费 key 直连不稳定（间歇被墙），对 FMP 失败的标的用 AV OVERVIEW 补齐
    （AV 免 key、国内直连可达；免费 25 次/日，配合 6h 缓存最坏 ~20 次/日，留余量）。
    """
    now = time.time()
    key = "equity"
    with _cache_lock:
        cached = _cache.get(key)
        if cached and cached[0] > now:
            return list(cached[1])

    if not config.FMP_API_KEY and not config.ALPHAVANTAGE_API_KEY:
        source_status.report("fmp", False, error="未配置 FMP_API_KEY / ALPHAVANTAGE_API_KEY")
        rows = _unavailable_all()
        _set_cache(key, rows, config.EQUITY_CACHE_TTL)
        return rows

    tickers = config.EQUITY_TICKERS or ("NVDA",)
    rows: List[Dict[str, Any]] = []
    fmp_ok = 0
    av_ok = 0
    for ticker in tickers:
        piece = _fmp_one(get, ticker) if config.FMP_API_KEY else None
        if piece and any(r["status"] == "ok" for r in piece):
            fmp_ok += 1
            rows.extend(piece)
            continue
        # FMP 该标的失败/未配 key → AV 逐标的补齐
        av = _av_fallback(get, ticker) if config.ALPHAVANTAGE_API_KEY else None
        if av and any(r["status"] == "ok" for r in av):
            av_ok += 1
            rows.extend(av)
        elif piece:
            rows.extend(piece)
        else:
            rows.extend(_unavailable_one(ticker))

    if fmp_ok:
        source_status.report("fmp", True, detail=f"美股基本面 {fmp_ok}/{len(tickers)} 只(FMP)")
    else:
        source_status.report("fmp", False, error="FMP 无成功标的" if config.FMP_API_KEY else "未配置 FMP_API_KEY")
    if av_ok:
        source_status.report("alphavantage", True, detail=f"{av_ok} 只(AV 补齐)")

    _set_cache(key, rows, config.EQUITY_CACHE_TTL)
    return rows