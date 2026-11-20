#!/usr/bin/env python3
"""
策略回测 — 只回放本平台已结算真实信号。

数据源：`quick_sim_trades` JOIN `ai_decisions`（P1 改造）：
      qs.settled=1 且 qs.entry_price>0、qs.pnl_pct 非空的行。
决策上下文（suggested_action/target_asset/news_id 等）从 ai_decisions 取。
盈亏使用 quick_sim_trades 已写入的真实 exit/entry，不再生成模拟价格路径。
仓位：notional_usdt × leverage，与交易模块口径一致。
"""

from __future__ import annotations

import sqlite3
from typing import Any, Dict, List, Optional

import db
import strategy_store

STRENGTH_RANK = {"weak": 1, "medium": 2, "strong": 3}


def load_settled_trades(
    asset: str = "",
    limit: int = 500,
    connection: Optional[sqlite3.Connection] = None,
) -> List[Dict[str, Any]]:
    """P1: 结算字段改读 quick_sim_trades（结算唯一权威）；决策上下文仍来自 ai_decisions。

    SELECT 仍以 ai_decisions 行为主线（防 JOIN 双计），用 EXISTS 校验有结算。
    """
    owned = connection is None
    connection = connection or db.get_connection()
    connection.row_factory = sqlite3.Row
    try:
        clauses = [
            "qs.settled = 1",
            "qs.entry_price IS NOT NULL AND qs.entry_price > 0",
            "qs.pnl_pct IS NOT NULL",
            "UPPER(ad.suggested_action) IN ('BUY', 'SELL')",
        ]
        params: List[Any] = []
        if asset:
            clauses.append("UPPER(ad.target_asset) = ?")
            params.append(asset.upper())
        rows = connection.execute(
            f"""
            SELECT ad.id, ad.news_id, ad.created_at, qs.entry_ts AS entry_time,
                   qs.entry_price, qs.exit_price,
                   qs.pnl_pct AS forward_pnl, qs.mfe_pct, qs.mae_pct,
                   qs.verdict AS is_correct,
                   UPPER(ad.suggested_action) AS action, UPPER(ad.target_asset) AS asset,
                   ad.sentiment_score, ad.event_strength, ad.direct_catalyst, ad.market_confirmation
            FROM ai_decisions ad
            JOIN quick_sim_trades qs ON qs.decision_id = ad.id
            WHERE {" AND ".join(clauses)}
            ORDER BY COALESCE(qs.entry_ts, ad.created_at) ASC, qs.id ASC
            LIMIT ?
            """,
            params + [max(1, min(int(limit), 2000))],
        ).fetchall()
        return [dict(row) for row in rows]
    finally:
        if owned:
            connection.close()


def apply_filter(trade: Dict[str, Any], params: Dict[str, Any]) -> bool:
    action = str(trade.get("action") or "").upper()
    if action not in {"BUY", "SELL"}:
        return False
    score = abs(float(trade.get("sentiment_score") or 0.0))
    if score < float(params["signal_threshold"]):
        return False
    asset_filter = str(params.get("asset_filter") or "").upper()
    if asset_filter and str(trade.get("asset") or "").upper() != asset_filter:
        return False
    min_strength = str(params.get("min_event_strength") or "").lower()
    if min_strength:
        actual = str(trade.get("event_strength") or "medium").lower()
        if STRENGTH_RANK.get(actual, 2) < STRENGTH_RANK.get(min_strength, 0):
            return False
    if params.get("require_direct_catalyst") and not int(trade.get("direct_catalyst") or 0):
        return False
    return True


def _pnl_usdt(trade: Dict[str, Any], params: Dict[str, Any]) -> float:
    notional = float(params["notional_usdt"])
    leverage = float(params["leverage"])
    pnl_pct = float(trade.get("forward_pnl") or 0.0)
    return notional * leverage * (pnl_pct / 100.0)


def _max_drawdown(equity: List[float]) -> float:
    peak = equity[0] if equity else 0.0
    worst = 0.0
    for value in equity:
        peak = max(peak, value)
        worst = min(worst, value - peak)
    return worst


def run_backtest(
    params: Dict[str, Any],
    *,
    asset: str = "",
    limit: int = 500,
    connection: Optional[sqlite3.Connection] = None,
) -> Dict[str, Any]:
    clamped = strategy_store.clamp_params(params)
    trades = load_settled_trades(asset=asset or clamped.get("asset_filter") or "", limit=limit, connection=connection)
    equity = 0.0
    curve: List[Dict[str, Any]] = []
    taken_rows: List[Dict[str, Any]] = []
    skipped = 0

    for trade in trades:
        if not apply_filter(trade, clamped):
            skipped += 1
            continue
        pnl = _pnl_usdt(trade, clamped)
        equity += pnl
        row = {
            "id": trade["id"],
            "news_id": trade["news_id"],
            "asset": trade["asset"],
            "action": trade["action"],
            "entry_time": trade.get("entry_time") or trade.get("created_at"),
            "entry_price": trade["entry_price"],
            "exit_price": trade["exit_price"],
            "sentiment_score": trade["sentiment_score"],
            "event_strength": trade.get("event_strength"),
            "forward_pnl_pct": trade["forward_pnl"],
            "pnl_usdt": round(pnl, 4),
            "is_correct": trade.get("is_correct") or "",
            "equity_usdt": round(equity, 4),
        }
        taken_rows.append(row)
        curve.append({"t": row["entry_time"], "equity": row["equity_usdt"], "id": trade["id"]})

    wins = [row for row in taken_rows if row["pnl_usdt"] > 0]
    losses = [row for row in taken_rows if row["pnl_usdt"] < 0]
    flats = [row for row in taken_rows if row["pnl_usdt"] == 0]
    # 胜率分母 = 有明确胜负的样本（WIN+LOSS），平局(flats)不计入分母
    decided = len(wins) + len(losses)
    winrate = (len(wins) / decided) if decided else None
    avg_win = (sum(row["pnl_usdt"] for row in wins) / len(wins)) if wins else 0.0
    avg_loss = (sum(row["pnl_usdt"] for row in losses) / len(losses)) if losses else 0.0
    profit_factor = None
    loss_abs = abs(sum(row["pnl_usdt"] for row in losses))
    if loss_abs > 0:
        profit_factor = sum(row["pnl_usdt"] for row in wins) / loss_abs
    elif wins:
        profit_factor = float("inf")

    by_asset: Dict[str, Dict[str, Any]] = {}
    for row in taken_rows:
        bucket = by_asset.setdefault(row["asset"], {"asset": row["asset"], "taken": 0, "wins": 0, "losses": 0, "pnl_usdt": 0.0})
        bucket["taken"] += 1
        bucket["pnl_usdt"] += row["pnl_usdt"]
        if row["pnl_usdt"] > 0:
            bucket["wins"] += 1
        elif row["pnl_usdt"] < 0:
            bucket["losses"] += 1
    for bucket in by_asset.values():
        bucket["pnl_usdt"] = round(bucket["pnl_usdt"], 4)
        _decided = bucket["wins"] + bucket["losses"]
        bucket["winrate"] = round(bucket["wins"] / _decided, 4) if _decided else None

    return {
        "data_source": "quick_sim_trades.settled (P1: JOIN ai_decisions 取决策上下文)",
        "honest": True,
        "note": "盈亏来自已结算信号的真实 entry/exit（pnl_pct），未使用模拟 K 线。",
        "params": clamped,
        "sample_size": len(trades),
        "taken": len(taken_rows),
        "skipped": skipped,
        "wins": len(wins),
        "losses": len(losses),
        "flats": len(flats),
        "winrate": None if winrate is None else round(winrate, 4),
        "total_pnl_usdt": round(equity, 4),
        "avg_pnl_usdt": round(equity / len(taken_rows), 4) if taken_rows else 0.0,
        "avg_win_usdt": round(avg_win, 4),
        "avg_loss_usdt": round(avg_loss, 4),
        "profit_factor": None if profit_factor is None else (None if profit_factor == float("inf") else round(profit_factor, 4)),
        "max_drawdown_usdt": round(_max_drawdown([0.0] + [row["equity_usdt"] for row in taken_rows]), 4),
        "by_asset": sorted(by_asset.values(), key=lambda item: item["pnl_usdt"]),
        "equity_curve": curve,
        "trades": taken_rows[-80:],
        "insufficient_sample": len(trades) < 8,
    }


def optimize_params(current: Dict[str, Any], report: Dict[str, Any]) -> Dict[str, Any]:
    """规则优化：样本少/回撤大就收紧，胜率高就略放宽。不编造行情。"""
    next_params = dict(strategy_store.clamp_params(current))
    taken = int(report.get("taken") or 0)
    winrate = report.get("winrate")
    drawdown = float(report.get("max_drawdown_usdt") or 0.0)
    notes: List[str] = []

    if taken < 8:
        next_params["signal_threshold"] = min(0.9, next_params["signal_threshold"] + 0.05)
        next_params["notional_usdt"] = max(5.0, next_params["notional_usdt"] * 0.75)
        notes.append("已结算样本不足 8 条，提高阈值并降低名义本金")
    elif winrate is not None and winrate < 0.45:
        next_params["signal_threshold"] = min(0.9, next_params["signal_threshold"] + 0.08)
        next_params["require_direct_catalyst"] = True
        notes.append("胜率低于 45%，提高阈值并只保留直接催化剂")
    elif winrate is not None and winrate >= 0.58 and drawdown > -next_params["notional_usdt"]:
        next_params["signal_threshold"] = max(0.3, next_params["signal_threshold"] - 0.04)
        next_params["notional_usdt"] = min(500.0, next_params["notional_usdt"] * 1.1)
        notes.append("胜率稳定且回撤可控，略微放宽阈值并提高名义本金")
    else:
        next_params["trailing_callback_rate"] = min(5.0, next_params["trailing_callback_rate"] + 0.1)
        notes.append("胜率中性，略放宽追踪止损以减少被洗")

    if drawdown < -next_params["notional_usdt"] * next_params["leverage"] * 0.3:
        next_params["leverage"] = max(1, int(next_params["leverage"]) - 1)
        notes.append("回撤超过名义本金×杠杆的 30%，降低杠杆")

    return {"params": strategy_store.clamp_params(next_params), "notes": notes}
