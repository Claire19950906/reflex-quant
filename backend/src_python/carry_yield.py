#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""资金费率套息（v425）：唯一"不吃方向也能赚钱"的结构。

思路（cash-and-carry）
----------------------
永续合约每 8 小时按资金费率结算一次：费率为正 → 多头付钱给空头。
所以「做空永续 + 做多现货（或等值多头）」对冲掉价格方向后，只剩收费率这一条现金流。
这不是预测，不需要信号，赚的是杠杆拥挤度本身。

必须说清的三件事
----------------
1. 收益 = 费率均值 × 每天 3 次 × 365。费率可正可负，负的时候这个结构要倒贴。
2. 建仓+平仓两个腿都要成本，所以先算**回本天数**：成本 / 每天净收。
   只有回本天数明显小于计划持有期，这件事才划算。
3. 杠杆放大的是收益也是爆仓概率；这里按 1x 名义本金报数，另外给一列 ×3 只做参考。

口径：成本取 cost_model 的往返 bps（单腿口径 ×2 视作两腿，保守），不在这里自造。
"""
from __future__ import annotations

import sqlite3
import statistics
import time
from typing import Any, Dict, List, Optional, Sequence

import cost_model
import structural_edge as se

SETTLE_PER_DAY = 3          # 8 小时结算一次
DAYS_PER_YEAR = 365


def funding_stats(conn: sqlite3.Connection, asset: str, days: int,
                  now: Optional[int] = None) -> Optional[Dict[str, Any]]:
    now = now or int(time.time())
    series = se.load_series(conn, "funding", asset, now - days * 86_400)
    vals = [v for _, v in series if v is not None]
    if len(vals) < 5:
        return None
    mean = statistics.mean(vals)
    pos_share = sum(1 for v in vals if v > 0) / len(vals)
    ann_pct = mean * SETTLE_PER_DAY * DAYS_PER_YEAR * 100.0     # 年化 %
    daily_pct = ann_pct / DAYS_PER_YEAR                          # 每天净收 %
    cost_pct = cost_model.round_trip_cost_bps(asset) / 100.0     # 往返成本（百分点）
    breakeven_days = (cost_pct / daily_pct) if daily_pct > 0 else None
    return {
        "asset": asset,
        "days": days,
        "points": len(vals),
        "mean_per_settle_pct": round(mean * 100, 5),
        "positive_share": round(pos_share, 3),
        "ann_pct_1x": round(ann_pct, 2),
        "ann_pct_3x": round(ann_pct * 3, 2),
        "cost_bps": cost_model.round_trip_cost_bps(asset),
        "breakeven_days": round(breakeven_days, 1) if breakeven_days else None,
        "verdict": _verdict(ann_pct, pos_share, breakeven_days),
    }


def _verdict(ann_pct: float, positive_share: float, breakeven_days: Optional[float]) -> str:
    if ann_pct <= 0:
        return "这段窗口费率不付钱 —— 套息不成立"
    if breakeven_days is None:
        return "无法回本"
    if breakeven_days > 60:
        return f"回本 {breakeven_days:.0f} 天，太慢 —— 不值得占资金"
    if positive_share < 0.6:
        return f"正费率只占 {positive_share:.0%}，收租不稳（会倒贴）"
    return f"回本 {breakeven_days:.0f} 天、正费率 {positive_share:.0%} —— 可做"


def evaluate(conn: sqlite3.Connection, assets: Sequence[str] = ("BTC", "ETH", "SOL"),
             windows: Sequence[int] = (30, 90, 320),
             now: Optional[int] = None) -> Dict[str, Any]:
    rows: List[Dict[str, Any]] = []
    for a in assets:
        for d in windows:
            st = funding_stats(conn, a, d, now)
            if st:
                rows.append(st)
    best = max(rows, key=lambda r: r["ann_pct_1x"]) if rows else None
    return {
        "rows": rows,
        "best": best,
        "note": ("套息赚的是杠杆拥挤度的租金，不是方向。年化个位数、回本十几天，"
                 "适合当底仓现金流，不适合当「主力赚钱手段」。"),
    }
