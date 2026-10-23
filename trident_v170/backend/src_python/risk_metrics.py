"""v176 — Portfolio-level 风险指标（drawdown / sharpe / winrate）。

输出用于：
  · /api/decisions/:id 全文溯源面板附加"组合当前风险状态"
  · weekly review 聚合（history_review 接入 compute_portfolio_metrics）
  · LLM prompt 上下文（"当前组合最大回撤 X% / 夏普 Y" 让 AI 决策时考虑风险敞口）

设计原则：
  · 纯函数 + 数据驱动，方便单测
  · 输入: 排序后的 pnl_pct 序列（每笔已结算 trades 的收益率）
  · 输出: {max_drawdown, current_drawdown, sharpe, winrate, total_pnl, n_trades, n_wins, n_losses}
"""
from __future__ import annotations

import math
from typing import Any, Dict, Sequence


def compute_drawdown(pnl_series: Sequence[float]) -> Dict[str, float]:
    """最大回撤 + 当前回撤（基于累计 PnL 曲线）。

    pnl_series: 每笔已结算 trades 的 pnl_pct（如 +0.02 = +2%, -0.01 = -1%）
      返回 {"max_drawdown": -0.15, "current_drawdown": -0.03, "peak_pnl": 0.18, "trough_pnl": 0.03}
      注意：回撤是**正数损失**（即 max_drawdown=0.03 = 跌 3%）。
    """
    if not pnl_series:
        return {
            "max_drawdown": 0.0,
            "current_drawdown": 0.0,
            "peak_pnl": 0.0,
            "trough_pnl": 0.0,
        }
    cum: List[float] = []
    s = 0.0
    for p in pnl_series:
        s += float(p or 0.0)
        cum.append(s)
    peak = cum[0]
    max_dd = 0.0  # 最大回撤（正数表示损失幅度）
    cur_dd = 0.0  # 当前回撤（基于最新 cum vs peak）
    for c in cum:
        if c > peak:
            peak = c
        dd = peak - c
        if dd > max_dd:
            max_dd = dd
    cur_dd = max(peak - cum[-1], 0.0)
    return {
        "max_drawdown": round(max_dd, 6),
        "current_drawdown": round(cur_dd, 6),
        "peak_pnl": round(peak, 6),
        "trough_pnl": round(cum[-1] - max_dd, 6) if cum else 0.0,
    }


def compute_sharpe(
    pnl_series: Sequence[float],
    *,
    periods_per_year: float = 252.0,
    risk_free_per_period: float = 0.0,
) -> float:
    """年化夏普比率 = (mean - rf) / std * sqrt(periods_per_year)。

    默认按交易日 252 年化（美股/加密 24-7 也可用 365）。<2 笔时 std=0 → 返回 -1 表示无意义。
    """
    n = len(pnl_series)
    if n < 2:
        return -1.0
    vals = [float(x or 0.0) for x in pnl_series]
    mean = sum(vals) / n
    var = sum((v - mean) ** 2 for v in vals) / (n - 1)
    std = math.sqrt(var) if var > 0 else 0.0
    if std <= 0:
        return -1.0
    sharpe = (mean - float(risk_free_per_period)) / std * math.sqrt(periods_per_year)
    return round(sharpe, 4)


def compute_winrate(pnl_series: Sequence[float]) -> Dict[str, float]:
    """胜率 + 总盈亏 + n_trades/n_wins/n_losses。

    分母：有明确胜负的（pnl > 0 为 WIN, pnl < 0 为 LOSS；pnl = 0 不计入分母）
    """
    if not pnl_series:
        return {
            "winrate": 0.0,
            "total_pnl": 0.0,
            "n_trades": 0,
            "n_wins": 0,
            "n_losses": 0,
            "n_breakeven": 0,
        }
    n_wins = sum(1 for p in pnl_series if float(p or 0.0) > 0)
    n_losses = sum(1 for p in pnl_series if float(p or 0.0) < 0)
    n_breakeven = sum(1 for p in pnl_series if float(p or 0.0) == 0)
    decided = n_wins + n_losses
    winrate = (n_wins / decided) if decided else 0.0
    total = sum(float(p or 0.0) for p in pnl_series)
    return {
        "winrate": round(winrate, 4),
        "total_pnl": round(total, 6),
        "n_trades": len(pnl_series),
        "n_wins": n_wins,
        "n_losses": n_losses,
        "n_breakeven": n_breakeven,
    }


def compute_portfolio_metrics(
    pnl_series: Sequence[float],
    *,
    periods_per_year: float = 252.0,
    risk_free_per_period: float = 0.0,
) -> Dict[str, Any]:
    """聚合：drawdown + sharpe + winrate 一并返回。

    单笔笔反思/复盘都用这个，避免分别调三次。"""
    return {
        **compute_drawdown(pnl_series),
        **compute_winrate(pnl_series),
        "sharpe": compute_sharpe(
            pnl_series,
            periods_per_year=periods_per_year,
            risk_free_per_period=risk_free_per_period,
        ),
    }
