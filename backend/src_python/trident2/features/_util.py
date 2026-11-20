# -*- coding: utf-8 -*-
"""功能实现公共小工具（纯函数，无副作用）。"""
from __future__ import annotations

import math
from typing import List, Optional, Sequence


def mean(xs: Sequence[float]) -> Optional[float]:
    vals = [float(x) for x in xs if x is not None]
    if not vals:
        return None
    return sum(vals) / len(vals)


def stdev(xs: Sequence[float]) -> Optional[float]:
    vals = [float(x) for x in xs if x is not None]
    if len(vals) < 2:
        return None
    m = sum(vals) / len(vals)
    var = sum((v - m) ** 2 for v in vals) / (len(vals) - 1)
    return math.sqrt(var)


def sharpe(returns: Sequence[float], periods_per_year: int = 252) -> Optional[float]:
    vals = [float(x) for x in returns if x is not None]
    if len(vals) < 5:
        return None
    sd = stdev(vals)
    if not sd or sd == 0:
        return None
    return mean(vals) / sd * math.sqrt(periods_per_year)


def quantile(xs: Sequence[float], q: float) -> Optional[float]:
    vals = sorted(float(x) for x in xs if x is not None)
    if not vals:
        return None
    if len(vals) == 1:
        return vals[0]
    pos = (len(vals) - 1) * min(max(q, 0.0), 1.0)
    lo, hi = int(math.floor(pos)), int(math.ceil(pos))
    if lo == hi:
        return vals[lo]
    return vals[lo] + (vals[hi] - vals[lo]) * (pos - lo)


def zscore(value: float, history: Sequence[float]) -> Optional[float]:
    vals = [float(x) for x in history if x is not None]
    if len(vals) < 5:
        return None
    sd = stdev(vals)
    if not sd:
        return None
    return (value - mean(vals)) / sd


def pct_change(new: Optional[float], old: Optional[float]) -> Optional[float]:
    if new is None or old is None or old == 0:
        return None
    return (float(new) - float(old)) / abs(float(old)) * 100.0


def correlation(xs: Sequence[float], ys: Sequence[float]) -> Optional[float]:
    n = min(len(xs), len(ys))
    if n < 5:
        return None
    x = [float(v) for v in xs[-n:]]
    y = [float(v) for v in ys[-n:]]
    mx, my = sum(x) / n, sum(y) / n
    sx = math.sqrt(sum((v - mx) ** 2 for v in x))
    sy = math.sqrt(sum((v - my) ** 2 for v in y))
    if sx == 0 or sy == 0:
        return None
    return sum((x[i] - mx) * (y[i] - my) for i in range(n)) / (sx * sy)


def atr(bars: Sequence[dict], period: int = 14) -> Optional[float]:
    if len(bars) < period + 1:
        return None
    trs = []
    for i in range(len(bars) - period, len(bars)):
        b, prev = bars[i], bars[i - 1]
        if b["high"] is None or b["low"] is None or prev["close"] is None:
            return None
        trs.append(max(b["high"] - b["low"],
                       abs(b["high"] - prev["close"]),
                       abs(b["low"] - prev["close"])))
    return sum(trs) / len(trs)


def realized_vol(closes: Sequence[float], window: int = 20, periods_per_year: int = 252) -> Optional[float]:
    if len(closes) < window + 1:
        return None
    rets = []
    for i in range(len(closes) - window, len(closes)):
        prev, cur = closes[i - 1], closes[i]
        if prev and cur and prev > 0 and cur > 0:
            rets.append(math.log(cur / prev))
    sd = stdev(rets)
    if sd is None:
        return None
    return sd * math.sqrt(periods_per_year)


def max_drawdown(equity: Sequence[float]) -> Optional[float]:
    vals = [float(x) for x in equity if x is not None]
    if len(vals) < 2:
        return None
    peak = vals[0]
    mdd = 0.0
    for v in vals:
        peak = max(peak, v)
        if peak > 0:
            mdd = min(mdd, (v - peak) / peak * 100.0)
    return mdd


def bucketize_confidence(pairs: Sequence[tuple], edges: Sequence[float] = (0.5, 0.6, 0.7, 0.8, 0.9)) -> List[dict]:
    """把 (confidence, hit) 分桶，返回每桶样本量与命中率（F59）。"""
    buckets = []
    bounds = list(edges) + [1.000001]
    for i in range(len(bounds) - 1):
        lo, hi = bounds[i], bounds[i + 1]
        rows = [(c, h) for c, h in pairs if c is not None and lo <= float(c) < hi]
        hit = sum(1 for _, h in rows if h)
        buckets.append({
            "range": f"{lo:.2f}-{min(hi, 1.0):.2f}",
            "n": len(rows),
            "hit_rate": (hit / len(rows)) if rows else None,
            "mean_confidence": mean([c for c, _ in rows]) if rows else None,
        })
    return buckets
