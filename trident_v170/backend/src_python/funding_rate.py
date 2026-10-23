"""
v302+ P1-D: Binance 永续合约资金费率 (Funding Rate) 数据源 + 衍生指标.

数据流: GET https://fapi.binance.com/fapi/v1/fundingRate?symbol=X&limit=N
- 每 8h 结算一次 (00:00, 08:00, 16:00 UTC)
- rate > 0  ⇒ 多头支付空头 (贪婪, 杠杆堆积)
- rate < 0  ⇒ 空头支付多头 (恐慌, 空头挤压信号)
- annualized = rate × 3 × 365 (3 期/天 × 365 天)
- 典型区间: ±0.01% (8h) ≈ ±11% APR
- 极值:  |rate| > 0.03% (8h) ≈ >30% APR  ⇒  regime 信号

模块依赖: orderbook_depth._http_get_json (复用 requests+retry 处理 TLS 抖动)
"""
from __future__ import annotations
import statistics
import time
from typing import Any

from orderbook_depth import _http_get_json

BINANCE_FAPI_FUNDING = "https://fapi.binance.com/fapi/v1/fundingRate"
SUPPORTED_SYMBOLS = ("BTCUSDT", "ETHUSDT", "SOLUSDT", "BNBUSDT")
CACHE_TTL_S = 60
HISTORY_LIMIT = 90       # 90 笔 = 30 天 × 3 笔/天
FUNDING_PERIODS_PER_YEAR = 3 * 365  # 8h × 3/day × 365
RATE_LONG_PAY = 0.0001   # 0.01% / 8h ≈ 11% APR
RATE_EXTREME = 0.0003    # 0.03% / 8h ≈ 33% APR (regime 触发)
RATE_NEGATIVE_FLIP = -0.00005  # 24h 资金费率由正转负阈值

_cache: dict = {}


def _fetch_history(symbol: str, limit: int = HISTORY_LIMIT) -> list[dict]:
    url = f"{BINANCE_FAPI_FUNDING}?symbol={symbol}&limit={limit}"
    raw = _http_get_json(url)
    # 排序: fundingTime 升序
    raw_sorted = sorted(raw, key=lambda r: int(r["fundingTime"]))
    return [
        {
            "ts": int(r["fundingTime"]) // 1000,
            "rate": float(r["fundingRate"]),
            "mark_price": float(r["markPrice"]) if r.get("markPrice") else None,
        }
        for r in raw_sorted
    ]


def _compute_metrics(history: list[dict], symbol: str) -> dict[str, Any]:
    if not history:
        return {"ok": False, "symbol": symbol, "error": "empty_history"}
    n = len(history)
    rates = [h["rate"] for h in history]
    current = rates[-1]
    last_ts = history[-1]["ts"]
    last_mark = history[-1]["mark_price"]
    # 24h = 最近 3 笔 (0/1/2)
    avg_24h = statistics.mean(rates[-3:]) if n >= 3 else current
    # 7d = 最近 21 笔
    avg_7d = statistics.mean(rates[-21:]) if n >= 21 else statistics.mean(rates)
    # 30d = 全部
    avg_30d = statistics.mean(rates)
    stdev_30d = statistics.pstdev(rates) if n >= 2 else 0.0
    min_30d = min(rates)
    max_30d = max(rates)
    trend_24h_vs_7d = avg_24h - avg_7d
    annualized_current = current * FUNDING_PERIODS_PER_YEAR * 100  # %
    annualized_24h = avg_24h * FUNDING_PERIODS_PER_YEAR * 100
    annualized_7d = avg_7d * FUNDING_PERIODS_PER_YEAR * 100
    annualized_30d = avg_30d * FUNDING_PERIODS_PER_YEAR * 100
    # 信号分级
    if current >= RATE_EXTREME:
        signal = "long_pay_extreme"  # 极度贪婪
        signal_label = "多头付费极端"
    elif current >= RATE_LONG_PAY:
        signal = "long_pay"
        signal_label = "多头付费"
    elif current <= -RATE_EXTREME:
        signal = "short_pay_extreme"  # 极度恐慌
        signal_label = "空头付费极端"
    elif current <= -RATE_LONG_PAY:
        signal = "short_pay"
        signal_label = "空头付费"
    else:
        signal = "neutral"
        signal_label = "中性"
    # 多空翻转 (最近 24h 费率从正变负)
    flip_to_negative = avg_24h <= RATE_NEGATIVE_FLIP and avg_7d > RATE_NEGATIVE_FLIP
    # 持续 7d 同号
    consecutive_sign = (
        "all_long_pay" if all(r > 0 for r in rates[-21:]) else
        "all_short_pay" if all(r < 0 for r in rates[-21:]) else
        "mixed"
    )
    return {
        "ok": True,
        "symbol": symbol,
        "ts": last_ts,
        "mark_price": last_mark,
        "n_periods": n,
        "current_rate": current,
        "avg_24h_rate": avg_24h,
        "avg_7d_rate": avg_7d,
        "avg_30d_rate": avg_30d,
        "stdev_30d_rate": stdev_30d,
        "min_30d_rate": min_30d,
        "max_30d_rate": max_30d,
        "trend_24h_vs_7d": trend_24h_vs_7d,
        "annualized_current_pct": annualized_current,
        "annualized_24h_pct": annualized_24h,
        "annualized_7d_pct": annualized_7d,
        "annualized_30d_pct": annualized_30d,
        "signal": signal,
        "signal_label": signal_label,
        "flip_to_negative_24h": flip_to_negative,
        "consecutive_sign_7d": consecutive_sign,
        "history": history,  # 全量 (前端画时间序列)
    }


def get_funding_metrics(symbol: str, force_refresh: bool = False) -> dict[str, Any]:
    if symbol not in SUPPORTED_SYMBOLS:
        return {"ok": False, "symbol": symbol, "error": f"unsupported_symbol: {symbol}"}
    now = time.time()
    cached = _cache.get(symbol)
    if cached and not force_refresh and (now - cached["fetched_at"]) < CACHE_TTL_S:
        out = dict(cached["data"])
        out["cached"] = True
        out["cache_age_s"] = int(now - cached["fetched_at"])
        return out
    try:
        history = _fetch_history(symbol)
        metrics = _compute_metrics(history, symbol)
        if metrics.get("ok"):
            _cache[symbol] = {
                "data": metrics,
                "fetched_at": now,
            }
        metrics["cached"] = False
        return metrics
    except Exception as e:
        if cached:
            out = dict(cached["data"])
            out["cached"] = True
            out["stale"] = True
            out["fetch_error"] = f"{type(e).__name__}: {e}"
            return out
        return {"ok": False, "symbol": symbol, "error": f"fetch_failed: {type(e).__name__}: {e}"}


def get_all_funding() -> dict[str, Any]:
    return {s: get_funding_metrics(s) for s in SUPPORTED_SYMBOLS}


if __name__ == "__main__":
    import json
    import sys
    sym = sys.argv[1] if len(sys.argv) > 1 else "BTCUSDT"
    m = get_funding_metrics(sym)
    # CLI 输出不带 history 数组
    if m.get("ok"):
        m.pop("history", None)
    print(json.dumps(m, ensure_ascii=False, indent=2))
