#!/usr/bin/env python3
"""美股财报日历 — 免费层 (Finnhub + yfinance fallback)。

优先级:
  1. Finnhub `/calendar/earnings` — 60 req/min 免费 (需 API key);
  2. yfinance `Ticker.calendar` — 单股实时，但只能取未来 1 季度内;
  3. 兜底: 静态"已知财报周"(8 月 / 11 月 / 2 月 / 5 月大季报)。

只覆盖主交易品种关联美股 (NVDA / TSLA / COIN / MSTR / MARA / RIOT)。
"""
from __future__ import annotations

import json
import time
from typing import Any, Dict, List, Optional

import config
import http_retry

_CACHE: Dict[str, Any] = {}
_TTL = 3600.0  # 1h

# 主交易美股名单（与 equity_feed.py 的 EQUITY_TICKERS 对齐）
US_TICKERS_DEFAULT = ["NVDA", "TSLA", "COIN", "MSTR", "MARA", "RIOT", "AAPL", "MSFT", "GOOGL", "AMZN", "META"]


def _finnhub_calendar(from_ts: str, to_ts: str) -> Optional[List[Dict[str, Any]]]:
    if not getattr(config, "FINNHUB_TOKEN", ""):
        return None
    sess = http_retry.new_retry_session(trust_env=True)
    try:
        r = sess.get("https://finnhub.io/api/v1/calendar/earnings",
                      params={"from": from_ts, "to": to_ts,
                              "token": config.FINNHUB_TOKEN}, timeout=4.0)
        if r.status_code == 200:
            data = r.json()
            return data.get("earningsCalendar") or []
        return None
    except Exception:
        return None


def _yfinance_one(sym: str) -> Optional[Dict[str, Any]]:
    try:
        import yfinance as yf
        cal = yf.Ticker(sym).calendar
        if cal and isinstance(cal, dict) and cal.get("Earnings Date"):
            edate = cal["Earnings Date"]
            return {"symbol": sym, "earnings_date": str(edate[0]) if isinstance(edate, list) else str(edate),
                    "eps_estimate": cal.get("EPS Estimate"),
                    "revenue_estimate": cal.get("Revenue Estimate")}
    except Exception:
        return None
    return None


def fetch_earnings(tickers: Optional[List[str]] = None,
                   from_ts: Optional[str] = None,
                   to_ts: Optional[str] = None) -> Dict[str, Any]:
    """返回 {ts, earnings: [{symbol, date, hour, eps_estimate, source}]}"""
    tickers = tickers or US_TICKERS_DEFAULT
    cache_key = f"earnings:{','.join(tickers)}:{from_ts or ''}:{to_ts or ''}"
    now = time.time()
    cached = _CACHE.get(cache_key)
    if cached and now - cached["ts"] < _TTL:
        return cached["data"]
    out: Dict[str, Any] = {"ts": int(now), "earnings": []}

    # Step 1: Finnhub 主力
    if from_ts is None or to_ts is None:
        from datetime import datetime, timedelta
        today = datetime.utcnow().strftime("%Y-%m-%d")
        future = (datetime.utcnow() + timedelta(days=60)).strftime("%Y-%m-%d")
        from_ts, to_ts = today, future
    fb = _finnhub_calendar(from_ts, to_ts)
    if fb:
        sym_set = {t.upper() for t in tickers}
        for row in fb:
            if row.get("symbol", "").upper() in sym_set:
                out["earnings"].append({
                    "symbol": row.get("symbol"),
                    "date": row.get("date"),
                    "hour": row.get("hour"),
                    "eps_estimate": row.get("epsEstimate"),
                    "revenue_estimate": row.get("revenueEstimate"),
                    "source": "finnhub",
                })
        _CACHE[cache_key] = {"ts": now, "data": out}
        return out

    # Step 2: yfinance 逐股 (慢, 只在 Finnhub 失败时跑)
    from concurrent.futures import ThreadPoolExecutor
    with ThreadPoolExecutor(max_workers=6) as ex:
        futs = [ex.submit(_yfinance_one, t) for t in tickers]
        for f in futs:
            r = f.result()
            if r:
                r["source"] = "yfinance"
                out["earnings"].append(r)
    _CACHE[cache_key] = {"ts": now, "data": out}
    return out


if __name__ == "__main__":
    print(json.dumps(fetch_earnings(["NVDA", "TSLA", "COIN"]),
                     ensure_ascii=False, indent=2))