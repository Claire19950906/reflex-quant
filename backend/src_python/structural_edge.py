# -*- coding: utf-8 -*-
"""结构性优势检验（v424，只读）。

v423 已经证明：新闻情绪类信号在 20m–3d 上毛胜率 42–49%，没有优势。
本模块换一个方向：**不用"理解新闻"，用"谁被迫交易"** —— 资金费率（funding）、
持仓量（OI）、多空比这类数据，天然带均值回归属性。

做法与 cost_horizon_eval 完全同口径：
  指标序列 → 取分位极端 → 生成方向（默认反向：多头拥挤就做空）
  → 用真实 K 线结算 → 输出毛收益、胜率、t 值、扣费后净收益。

**关键防自欺措施**（这是本仓库的老毛病）：
1. `dedupe_within_horizon`：同一持有期内只取第一个信号，否则重叠样本会把 t 值吹大；
2. 分位阈值用**前一段**数据算（walk-forward），不是拿全样本算完再回测自己；
3. 样本 <30 或 t<2 一律不许说"有优势"。
"""
from __future__ import annotations

import sqlite3
from typing import Any, Dict, List, Optional, Sequence, Tuple

import cost_horizon_eval as ce

# 指标 → 允许的持有期（秒）与结算用 K 线
_KLINE_FOR = {28_800: ("1h", 3_600), 86_400: ("4h", 14_400), 259_200: ("1d", 86_400)}

# 成本只有一个来源：cost_model.round_trip_cost_bps（BTC/ETH 30bps、SOL 32bps、XAU 16bps …）。
# v424 初版在这里自建了一张 5bps 的小表，导致 /edge 的"净收益"比全站其它结算乐观 6 倍——
# 同一事实两处来源就是这个仓库最常犯的病，已删除，统一走 cost_model。
try:
    import cost_model as _cost
except Exception:  # pragma: no cover - 分析脚本在断依赖时仍要能跑（保守值）
    _cost = None


def asset_cost_bps(asset: str) -> float:
    if _cost is not None:
        try:
            return float(_cost.round_trip_cost_bps(asset))
        except Exception:
            pass
    return 30.0


# flow_snapshots 的写入方 market_flow.py 已从仓库消失（2026-11-17 起该表全线停更），
# 但实时费率一直由 binance_flow/okx_flow/bybit_flow 写进 external_indicators。
# 所以指标读取改成"优先读活的那个，再回落老表"——否则分析页会拿两周前的死数据下结论。
_LIVE_MAP = {
    "funding": ("bn_funding_rate", "okx_funding_rate", "bybit_funding_rate"),
    "lsratio": ("bn_long_short_ratio",),
    "oi": ("bn_open_interest",),
}
_BUCKET_S = 8 * 3_600   # 费率本身 8 小时变一次，5 分钟采样下采样到 8h 桶，取桶内最后一个值


def _live_series(conn: sqlite3.Connection, kind: str, asset: str,
                 since_ts: int) -> List[Tuple[int, float]]:
    for indicator in _LIVE_MAP.get(kind, ()): 
        try:
            cur = conn.execute(
                "SELECT ts, value FROM external_indicators WHERE indicator=? AND asset=? "
                "AND ts>=? ORDER BY ts", (indicator, asset.upper(), since_ts),
            )
            rows = cur.fetchall()
            cur.close()
        except sqlite3.Error:
            continue
        if len(rows) < 10:
            continue
        buckets: Dict[int, float] = {}
        for ts, v in rows:
            if v is None:
                continue
            t = int(ts) // 1000 if int(ts) > 10_000_000_000 else int(ts)
            buckets[t - (t % _BUCKET_S)] = float(v)   # 同桶覆盖 → 桶内最后一笔
        return sorted(buckets.items())
    return []


def load_series(conn: sqlite3.Connection, kind: str, asset: str,
                since_ts: int, prefer_live: bool = True) -> List[Tuple[int, float]]:
    """取一条指标序列。ts 统一成秒（库里有的表是毫秒，统一归一，别信字段名）。

    优先 external_indicators（活的），没有才回落 flow_snapshots（可能已停更）。
    """
    if prefer_live:
        live = _live_series(conn, kind, asset, since_ts)
        if len(live) >= 10:
            return live
    cur = conn.execute(
        "SELECT ts, value FROM flow_snapshots WHERE kind=? AND asset=? AND ts>=? ORDER BY ts",
        (kind, asset.upper(), since_ts),
    )
    rows = cur.fetchall()
    cur.close()
    out: List[Tuple[int, float]] = []
    for ts, v in rows:
        t = int(ts)
        t = t // 1000 if t > 10_000_000_000 else t
        if v is None:
            continue
        out.append((t, float(v)))
    return out


def _quantile(xs: Sequence[float], q: float) -> float:
    s = sorted(xs)
    if not s:
        return 0.0
    pos = max(0.0, min(1.0, q)) * (len(s) - 1)
    lo = int(pos)
    hi = min(lo + 1, len(s) - 1)
    frac = pos - lo
    return s[lo] * (1 - frac) + s[hi] * frac


def signals_walk_forward(series: Sequence[Tuple[int, float]], q_low: float = 0.2,
                         q_high: float = 0.8, min_history: int = 40,
                         contrarian: bool = True) -> List[Dict[str, Any]]:
    """用"当下之前的历史"定分位阈值，再判断当前值是否极端。

    这样阈值不会偷看未来；但代价是前 min_history 个点没有信号。
    """
    if len(series) < min_history + 1:
        return []
    out: List[Dict[str, Any]] = []
    vals: List[float] = []
    for i, (ts, v) in enumerate(series):
        if i < min_history:
            vals.append(v)
            continue
        lo = _quantile(vals, q_low)
        hi = _quantile(vals, q_high)
        # 阈值必须有区分度，否则"极端"是假的
        if hi - lo <= 1e-12:
            vals.append(v)
            continue
        if v >= hi:
            direction = "SELL" if contrarian else "BUY"     # 多头拥挤 → 反向做空
            out.append({"ts": ts, "value": v, "direction": direction,
                        "threshold": hi, "side": "high"})
        elif v <= lo:
            direction = "BUY" if contrarian else "SELL"
            out.append({"ts": ts, "value": v, "direction": direction,
                        "threshold": lo, "side": "low"})
        vals.append(v)
    return out


def dedupe_within_horizon(events: Sequence[Dict[str, Any]],
                          horizon_s: int) -> List[Dict[str, Any]]:
    """重叠样本会把 t 值吹大。同一持有期内只留第一个信号。"""
    kept: List[Dict[str, Any]] = []
    last_ts = None
    for e in sorted(events, key=lambda x: x["ts"]):
        if last_ts is not None and e["ts"] - last_ts < horizon_s:
            continue
        kept.append(e)
        last_ts = e["ts"]
    return kept


def settle(asset: str, events: Sequence[Dict[str, Any]],
           bars: Sequence[Tuple[int, float]], horizon_s: int,
           interval_s: int) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    for e in events:
        entry = ce.exit_at(bars, e["ts"], interval_s)   # 入场用信号时刻(及之前)的收盘价
        if entry is None:
            continue
        px = ce.exit_at(bars, e["ts"] + horizon_s, interval_s)
        if px is None:
            continue
        g = ce.gross_pct(e["direction"], entry, px)
        if g is None:
            continue
        out.append({**e, "asset": asset, "entry": entry, "exit": px, "gross": g})
    return out


def evaluate_indicator(conn: sqlite3.Connection, kind: str,
                       assets: Sequence[str], since_ts: int,
                       horizons_s: Sequence[int] = (28_800, 86_400, 259_200),
                       q_low: float = 0.2, q_high: float = 0.8,
                       contrarian: bool = True,
                       min_history: int = 40) -> Dict[str, Any]:
    """一个指标 × 一组品种 × 一组持有期的优势检验。"""
    rows: List[Dict[str, Any]] = []
    bars_cache: Dict[Tuple[str, str], List[Tuple[int, float]]] = {}
    for asset in assets:
        series = load_series(conn, kind, asset, since_ts)
        if len(series) < min_history + 1:
            continue
        raw_events = signals_walk_forward(series, q_low, q_high, min_history, contrarian)
        for h in horizons_s:
            tf, interval = _KLINE_FOR[h]
            key = (asset, tf)
            if key not in bars_cache:
                bars_cache[key] = ce.load_bars(conn, asset, tf, since_ts - 7 * 86_400)
            events = dedupe_within_horizon(raw_events, h)
            settled = settle(asset, events, bars_cache[key], h, interval)
            if not settled:
                continue
            gross = [s["gross"] for s in settled]
            cost_pct = asset_cost_bps(asset) / 100.0
            net = [g - cost_pct for g in gross]
            st_g = ce.stats(gross)
            st_n = ce.stats(net)
            rows.append({
                "kind": kind, "asset": asset.upper(), "horizon_s": h,
                "horizon": {28_800: "8h", 86_400: "1d", 259_200: "3d"}.get(h, "%ds" % h),
                "cost_bps": asset_cost_bps(asset),
                "signals_raw": len(raw_events),
                "signals_after_dedupe": len(events),
                "n": st_n["n"],
                "gross": st_g, "net": st_n,
                "verdict": ce.verdict(st_n),
                "has_edge": bool(st_n["n"] >= 30 and (st_n["t_stat"] or 0) >= 2.0
                                 and (st_n["avg"] or 0) > 0),
            })
    return {"kind": kind, "contrarian": contrarian, "rows": rows,
            "has_any_edge": any(r["has_edge"] for r in rows)}


def _baseline_pool(bars: Sequence[Tuple[int, float]], horizon_s: int,
                   direction: str, interval_s: int) -> List[float]:
    """同期"随便一个时点"按同方向做的结果集合 —— 用来回答"是不是只是踩到了趋势"。"""
    pool: List[float] = []
    # bars 本身已经是按 interval_s 排列的，逐根遍历即可；这里不能按"秒"步进，
    # 否则会每 4 小时才取一根，pool 只剩 1 个样本，置换检验失效（v424 修）。
    for i in range(len(bars)):
        px = ce.exit_at(bars, bars[i][0] + horizon_s, interval_s)
        if px is None:
            continue
        g = ce.gross_pct(direction, bars[i][1], px)
        if g is not None:
            pool.append(g)
    return pool


def timing_p(pool: Sequence[float], n: int, observed_avg: Optional[float],
             k: int = 2000, seed: int = 7) -> Optional[float]:
    """置换检验：从同期所有时点里随机抽 n 个，看真实信号的平均收益能排到第几。

    p 小 = "挑时点"本身有信息，而不是这段行情本来是单边。
    """
    if not pool or not n or observed_avg is None or len(pool) < n:
        return None
    import random as _random
    rnd = _random.Random(seed)
    better = 0
    for _ in range(k):
        avg = sum(rnd.sample(pool, n)) / n
        if avg >= observed_avg:
            better += 1
    return round(better / k, 4)


def evaluate_with_baseline(conn: sqlite3.Connection, kind: str, assets: Sequence[str],
                           since_ts: int, horizons_s: Sequence[int] = (86_400,),
                           q_low: float = 0.2, q_high: float = 0.8,
                           contrarian: bool = True, k: int = 2000) -> Dict[str, Any]:
    """按"方向"拆行做体检：同一指标的高低两侧，本来就是两套不同的策略，混在一起会互相稀释。

    每行给：样本、毛收益、无条件基线（同期同方向随便时点）、扣费净收益、t 值、时机置换 p。
    只有 净>0 且 t>=2 且 p<0.05 三样同时满足，才算"时点有信息"。
    """
    rows: List[Dict[str, Any]] = []
    bars_cache: Dict[Tuple[str, str], List[Tuple[int, float]]] = {}
    pools: Dict[Tuple[str, int, str], List[float]] = {}
    for asset in assets:
        series = load_series(conn, kind, asset, since_ts)
        if len(series) < 41:
            continue
        raw = signals_walk_forward(series, q_low, q_high, 40, contrarian)
        for h in horizons_s:
            tf, interval = _KLINE_FOR[h]
            key = (asset, tf)
            if key not in bars_cache:
                bars_cache[key] = ce.load_bars(conn, asset, tf, since_ts - 7 * 86_400)
            bars = bars_cache[key]
            settled = settle(asset, dedupe_within_horizon(raw, h), bars, h, interval)
            if not settled:
                continue
            for direction in ("BUY", "SELL"):
                group = [x["gross"] for x in settled if x["direction"] == direction]
                if not group:
                    continue
                pk = (asset, h, direction)
                if pk not in pools:
                    pools[pk] = _baseline_pool(bars, h, direction, interval)
                pool = pools[pk]
                base = round(sum(pool) / len(pool), 4) if pool else None
                avg_gross = sum(group) / len(group)
                cost_pct = asset_cost_bps(asset) / 100.0
                st_g = ce.stats(group)
                st_n = ce.stats([g - cost_pct for g in group])
                p = timing_p(pool, len(group), avg_gross, k)
                has_edge = bool(
                    st_n["n"] >= 30
                    and (st_n["t_stat"] or -99) >= 2.0
                    and (st_n["avg"] or -99) > 0
                    and p is not None and p < 0.05
                )
                rows.append({
                    "kind": kind, "asset": asset.upper(), "horizon_s": h,
                    "horizon": {28_800: "8h", 86_400: "1d", 259_200: "3d"}.get(h, "%ds" % h),
                    "direction": direction,
                    "cost_bps": asset_cost_bps(asset),
                    "raw_signals": len(raw),
                    "n": st_n["n"],
                    "gross": st_g, "net": st_n,
                    "baseline_avg": base,
                    "excess_gross": round(avg_gross - base, 4) if base is not None else None,
                    "timing_p": p,
                    "verdict": ce.verdict(st_n),
                    "has_edge": has_edge,
                })
    rows.sort(key=lambda r: -((r["net"]["t_stat"] or -99)))
    return {"kind": kind, "contrarian": contrarian, "rows": rows,
            "has_any_edge": any(r["has_edge"] for r in rows),
            "min_n": 30, "min_t": 2.0, "max_p": 0.05}


def summarize(rep: Dict[str, Any]) -> Dict[str, Any]:
    """只挑最好的几行，别让用户看 30 行噪声。"""
    rows = rep.get("rows") or []
    best = max(rows, key=lambda r: (r["net"]["t_stat"] or -99)) if rows else None
    return {"rows": len(rows), "best": best, "has_any_edge": rep.get("has_any_edge")}
