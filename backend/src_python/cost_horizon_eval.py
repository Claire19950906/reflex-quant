# -*- coding: utf-8 -*-
"""成本–持有期匹配评估（v423，只读）。

为什么要有这个：
  已有 2030 笔已结算模拟交易的实测是「20 分钟持有、0.30% 往返成本」，
  毛收益 ≈ 0（胜率 48%），净收益 -0.301%/笔 —— 等于每笔先固定亏一个成本。
  20 分钟窗口的平均绝对波动只有 0.168%，**比成本还小**。

  所以"能不能赚钱"不该再靠加信号回答，而应该问：
  同样的信号，换持有期、换成本档，净期望是正还是负？

本模块就是回答这个：拿历史信号的入场点，用真实 K 线在多个持有期上重新结算，
再叠加不同成本档，输出每格的净期望、胜率、盈亏比、t 值。

**只用库里的 K 线，不发网络请求、不写库。**
"""
from __future__ import annotations

import math
import sqlite3
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

BUY_ACTIONS = {"BUY", "LONG"}
SELL_ACTIONS = {"SELL", "SHORT"}

# 持有期（秒）与对应要用哪条 K 线。顺序即前端展示顺序。
HORIZONS: List[Tuple[str, int, str]] = [
    ("20m", 1_200, "5m"),
    ("1h", 3_600, "15m"),
    ("4h", 14_400, "1h"),
    ("1d", 86_400, "4h"),
    ("3d", 259_200, "1d"),
]

# 成本档（基点，往返）。30bps = 现货吃单双边；10bps/5bps = 合约/做市；
# 2bps 是"只做主流币永续、吃返佣"的理想档，用来判断"成本是不是唯一问题"。
COST_BPS_TIERS: List[int] = [30, 10, 5, 2, 0]

# 价格容差：允许 2 个周期的空洞（周末/停盘），超过就跳过该笔，不硬凑
TOL_MULT = 2

_TF_SECONDS = {"1m": 60, "5m": 300, "15m": 900, "1h": 3600, "4h": 14_400, "1d": 86_400}


def gross_pct(action: str, entry: float, exit_: float) -> Optional[float]:
    """毛收益百分比（未扣成本）。方向不认识返回 None，不默认按多头算。"""
    a = (action or "").upper()
    if not entry or entry <= 0 or not exit_ or exit_ <= 0:
        return None
    if a in BUY_ACTIONS:
        return (exit_ / entry - 1.0) * 100.0
    if a in SELL_ACTIONS:
        return (entry / exit_ - 1.0) * 100.0
    return None


def exit_at(bars: Sequence[Tuple[int, float]], target_ts: int,
            interval_s: int, tol_mult: int = TOL_MULT) -> Optional[float]:
    """取「目标时刻及之前最近一根」的收盘价。

    bars: [(ts 秒, close), ...] 升序。
    返回值：找到就用 close；目标时刻落在数据尾部之外（还没到那么久）或
    中间空洞超过 tol_mult 个周期 → None（跳过，不拿更早的价冒充）。
    """
    if not bars:
        return None
    last_ts = bars[-1][0]
    if target_ts > last_ts:
        return None                      # 持有期还没走完
    lo, hi = 0, len(bars) - 1
    idx = -1
    while lo <= hi:                      # 找最后一个 ts <= target
        mid = (lo + hi) // 2
        if bars[mid][0] <= target_ts:
            idx = mid
            lo = mid + 1
        else:
            hi = mid - 1
    if idx < 0:
        return None
    if target_ts - bars[idx][0] > interval_s * tol_mult:
        return None                      # 空洞太大
    return bars[idx][1]


def stats(xs: Iterable[float]) -> Dict[str, Any]:
    """净收益序列 → 可判断"值不值得做"的一组统计。"""
    xs = [x for x in xs if x is not None]
    n = len(xs)
    if n == 0:
        return {"n": 0, "avg": None, "median": None, "win_rate": None,
                "profit_factor": None, "t_stat": None, "best": None, "worst": None}
    avg = sum(xs) / n
    srt = sorted(xs)
    med = srt[n // 2] if n % 2 else (srt[n // 2 - 1] + srt[n // 2]) / 2.0
    wins = [x for x in xs if x > 0]
    losses = [x for x in xs if x <= 0]
    gain = sum(wins)
    loss = abs(sum(losses))
    pf = (gain / loss) if loss > 0 else (None if gain == 0 else float("inf"))
    var = sum((x - avg) ** 2 for x in xs) / (n - 1) if n > 1 else 0.0
    sd = math.sqrt(var)
    t = (avg / (sd / math.sqrt(n))) if (n > 1 and sd > 0) else None
    return {
        "n": n,
        "avg": round(avg, 4),
        "median": round(med, 4),
        "win_rate": round(len(wins) / n, 4),
        "profit_factor": (round(pf, 3) if pf is not None and pf != float("inf") else pf),
        "t_stat": round(t, 2) if t is not None else None,
        "best": round(max(xs), 3),
        "worst": round(min(xs), 3),
    }


def verdict(s: Dict[str, Any], min_n: int = 30) -> str:
    """把统计翻译成一句人话。样本不足时不许下结论 —— 这是本仓库的老毛病。"""
    n = s.get("n") or 0
    if n < min_n:
        return "样本不足（%d<%d），不下结论" % (n, min_n)
    avg = s.get("avg") or 0.0
    t = s.get("t_stat")
    if avg > 0 and (t is not None and t >= 2.0):
        return "净期望为正且统计显著（t=%.2f）" % t
    if avg > 0:
        return "净期望为正但不显著（t=%s），可能只是运气" % (t if t is not None else "—")
    if avg < 0 and (t is not None and t <= -2.0):
        return "净期望为负且显著 —— 这套结构在亏钱，别再往里加信号"
    return "净期望为负但不显著"


def load_bars(conn: sqlite3.Connection, symbol: str, tf: str,
              since_ts: int) -> List[Tuple[int, float]]:
    cur = conn.execute(
        """
        SELECT ts, close FROM kline_bars
        WHERE symbol = ? AND timeframe = ? AND ts >= ?
        ORDER BY ts ASC
        """,
        (symbol, tf, since_ts * 1000),
    )
    rows = cur.fetchall()
    cur.close()
    out: List[Tuple[int, float]] = []
    for ts, close in rows:
        t = int(ts)
        t = t // 1000 if t > 10_000_000_000 else t
        if close:
            out.append((t, float(close)))
    return out


def load_entries(conn: sqlite3.Connection, since_ts: int, limit: int = 5000,
                 asset: Optional[str] = None) -> List[Dict[str, Any]]:
    """历史信号入场点。用 t2_sim_trades（它有真实入场价与方向）。"""
    sql = """
        SELECT asset, action, entry_ts, entry_price, confidence
        FROM t2_sim_trades
        WHERE settled = 1 AND entry_ts >= ?
    """
    args: List[Any] = [since_ts]
    if asset:
        sql += " AND asset = ?"
        args.append(asset.upper())
    sql += " ORDER BY entry_ts ASC LIMIT ?"
    args.append(limit)
    cur = conn.execute(sql, tuple(args))
    rows = cur.fetchall()
    cur.close()
    out = []
    for r in rows:
        action = str(r[1] or "").upper()
        if action not in BUY_ACTIONS | SELL_ACTIONS:
            continue
        out.append({
            "asset": str(r[0] or "").upper(),
            "action": action,
            "entry_ts": int(r[2] or 0),
            "entry_price": float(r[3] or 0),
        })
    return [e for e in out if e["entry_ts"] and e["entry_price"] > 0]


def evaluate(entries: Sequence[Dict[str, Any]],
             bars_by_asset: Dict[str, Dict[str, List[Tuple[int, float]]]],
             horizons: Sequence[Tuple[str, int, str]] = HORIZONS,
             cost_tiers: Sequence[int] = COST_BPS_TIERS) -> Dict[str, Any]:
    """矩阵结算：每个 (持有期 × 成本) 一个格子。

    返回 {cells: [...], by_asset: [...], coverage: {...}}
    每个格子同时给"毛"和"净"，并给出需要多少胜率/多大波动才能覆盖成本。
    """
    cells: List[Dict[str, Any]] = []
    # 先把每笔在每个持有期的毛收益算出来，复用给所有成本档
    per_h: Dict[Tuple[str, str], List[float]] = {}
    per_h_abs: Dict[Tuple[str, str], List[float]] = {}
    asset_h: Dict[Tuple[str, str, str], List[float]] = {}
    skipped: Dict[str, int] = {}

    for label, sec, tf in horizons:
        interval = _TF_SECONDS.get(tf, sec)
        for e in entries:
            bars = (bars_by_asset.get(e["asset"]) or {}).get(tf) or []
            px = exit_at(bars, e["entry_ts"] + sec, interval)
            if px is None:
                skipped[label] = skipped.get(label, 0) + 1
                continue
            g = gross_pct(e["action"], e["entry_price"], px)
            if g is None:
                skipped[label] = skipped.get(label, 0) + 1
                continue
            per_h.setdefault((label, tf), []).append(g)
            per_h_abs.setdefault((label, tf), []).append(abs(g))
            asset_h.setdefault((e["asset"], label, tf), []).append(g)

    for label, sec, tf in horizons:
        gross_list = per_h.get((label, tf), [])
        if not gross_list:
            continue
        avg_abs = sum(per_h_abs[(label, tf)]) / len(per_h_abs[(label, tf)])
        for bps in cost_tiers:
            cost_pct = bps / 100.0
            net = [g - cost_pct for g in gross_list]
            s = stats(net)
            cells.append({
                "horizon": label,
                "tf": tf,
                "cost_bps": bps,
                "cost_pct": cost_pct,
                "gross": stats(gross_list),
                "net": s,
                "avg_abs_move": round(avg_abs, 4),
                # 成本/波动 = 你要"跨越成本"的倍数；>1 表示这一档天生做不了
                "cost_to_move": round(cost_pct / avg_abs, 3) if avg_abs else None,
                "verdict": verdict(s),
                "profitable": bool(s["avg"] is not None and s["avg"] > 0),
            })

    # 分品种：只在"最优持有期"上展开，否则表格会炸
    by_asset: List[Dict[str, Any]] = []
    best = None
    for c in cells:
        v = c["net"]["avg"]
        if v is None:
            continue
        if best is None or v > best["net"]["avg"]:
            best = c
    if best:
        for (asset, label, tf), gl in sorted(asset_h.items()):
            if label != best["horizon"]:
                continue
            s = stats([g - best["cost_pct"] for g in gl])
            by_asset.append({
                "asset": asset, "horizon": label, "cost_bps": best["cost_bps"],
                "net": s, "verdict": verdict(s),
            })
        by_asset.sort(key=lambda x: -((x["net"]["avg"] or -99)))

    return {
        "entries": len(entries),
        "skipped": skipped,
        "cells": cells,
        "by_asset": by_asset,
        "best_cell": best,
        "note": ("每笔只用库内真实 K 线结算；目标时刻之后没有 K 线或空洞超过 "
                 "%d 个周期的跳过，不拿旧价冒充。" % TOL_MULT),
    }
