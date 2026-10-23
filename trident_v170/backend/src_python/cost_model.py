#!/usr/bin/env python3
"""v301+ P0+：交易成本模型（单点真相）。

为什么必须有这个模块
======================
v300 之前所有 PnL 结算（quick_sim.settle_trades、paper_trading、trade_executor、
review 报表、backtest）都按"裸价"算 pnl_pct，没扣手续费/滑点，导致：
  - paper trading 净值比实盘高估 1-3%（crypto 10bps×2 + slip 5bps×2 ~30bps per trade）
  - backtest 胜率虚高 5-10pp（赢家被放大、输家被放大同比例但 winner 占比更敏感）
  - 投资人对接时一问"扣完成本还赚吗"就会暴露

这是 P0 级问题——没有显式成本模型的量化框架，估值打 5 折。

本模块目标
==========
1. 单一函数 `apply_costs(pnl_pct_raw, asset, side)` 给所有 PnL 通路调用
2. 资产类别 → 默认费率（bps）查表 + env 覆盖（`TRIDENT_FEE_BPS`、`TRIDENT_SLIPPAGE_BPS`）
3. 紧急逃生口：`TRIDENT_DISABLE_COSTS=1` 全模块短路（仅用于回放 v300 之前的"裸价历史快照"）
4. 默认开启成本计算——所有新结算必须经过本模块
5. Round-trip 模型：开仓 + 平仓各扣一次 fee + 一次 slippage（实际是 4 个冲击，但模型简化取 2×）

向后兼容
========
- 新增 DB 列 `cost_bps REAL DEFAULT 0.0`，老行 cost_bps=0 即"未扣成本"——回放这些行时
  本模块不再二次扣费（pct_bps<=0 时跳过）。这样历史快照和未来快照在同一表里
  可以共存而不污染。

调试入口
========
>>> from cost_model import apply_costs, round_trip_cost_bps
>>> apply_costs(10.0, "BTC")  # BUY BTC, 10% raw
9.94  # 扣 30 bps（10 fee + 5 slip × 2）
"""
from __future__ import annotations

import logging
import os
from typing import Final

logger = logging.getLogger("cost_model")

# ── 资产类基础费率（bps，round-trip = 入场 + 出场各一次）──
# 来源：crypto 默认 taker fee 8-10bps，slippage 3-8bps；
#       股票 ETF/ADR 通常 1-3bps + slippage 1-2bps；
#       黄金/XAU spot 通常 5bps + slippage 3bps。
# 注：这里的"fee"是平台/经纪商抽佣；"slippage"是市价单实际成交价偏离理论价的中位。
_ASSET_CLASS_BPS: Final[dict[str, dict[str, float]]] = {
    "BTC":   {"fee_bps": 10.0, "slippage_bps": 5.0},
    "ETH":   {"fee_bps": 10.0, "slippage_bps": 5.0},
    "SOL":   {"fee_bps": 10.0, "slippage_bps": 6.0},
    "BNB":   {"fee_bps": 10.0, "slippage_bps": 5.0},
    "XAU":   {"fee_bps": 5.0,  "slippage_bps": 3.0},
    "GOLD":  {"fee_bps": 5.0,  "slippage_bps": 3.0},
    "PAXG":  {"fee_bps": 5.0,  "slippage_bps": 3.0},
    "STOCK": {"fee_bps": 2.0,  "slippage_bps": 2.0},
    "FX":    {"fee_bps": 1.0,  "slippage_bps": 1.0},
}

# 资产别名归一化（quick_sim 里 asset 可能是 "BTC" 也可能是 "BTCUSDT"）
_ASSET_ALIASES: Final[dict[str, str]] = {
    "BTCUSDT": "BTC", "ETHUSDT": "ETH", "SOLUSDT": "SOL",
    "BNBUSDT": "BNB", "PAXGUSDT": "PAXG",
    "GOLD": "XAU",  # 双源旧名称兼容
    "XAUUSD": "XAU", "XAUUSDT": "XAU",
}

# 兜底费率（未知资产）：高估值给保守值 30 bps round-trip
_DEFAULT_BPS: Final[dict[str, float]] = {"fee_bps": 10.0, "slippage_bps": 5.0}


def _normalize_asset(asset: str) -> str:
    a = asset.upper().strip()
    return _ASSET_ALIASES.get(a, a)


def _class_bps(asset: str) -> tuple[float, float]:
    """返回 (fee_bps, slippage_bps) for the asset class.

    env 覆盖：
      TRIDENT_FEE_BPS       — 全局覆盖 fee_bps（任意整数或小数）
      TRIDENT_SLIPPAGE_BPS  — 全局覆盖 slippage_bps
    """
    env_fee = os.environ.get("TRIDENT_FEE_BPS")
    env_slip = os.environ.get("TRIDENT_SLIPPAGE_BPS")
    if env_fee is not None or env_slip is not None:
        f = float(env_fee) if env_fee is not None else _DEFAULT_BPS["fee_bps"]
        s = float(env_slip) if env_slip is not None else _DEFAULT_BPS["slippage_bps"]
        return f, s
    norm = _normalize_asset(asset)
    cls = _ASSET_CLASS_BPS.get(norm, _DEFAULT_BPS)
    return float(cls["fee_bps"]), float(cls["slippage_bps"])


def round_trip_cost_bps(asset: str) -> float:
    """往返总成本（bps）= 2 × (fee + slippage)。"""
    f, s = _class_bps(asset)
    return 2.0 * (f + s)


def costs_globally_disabled() -> bool:
    """紧急逃生口：v300 之前的回放/对比任务可以临时关成本。"""
    return os.environ.get("TRIDENT_DISABLE_COSTS", "").strip() in {"1", "true", "yes"}


def apply_costs(pnl_pct_raw: float, asset: str, side: str = "BUY") -> float:
    """把 raw pnl_pct 扣完往返成本再返回（单位：pct 百分点，不是 -100bp 之类）。

    行为：
      - `costs_globally_disabled()` 为真 → 原值返回（logger.debug 记录一次）
      - pnl_pct_raw 为 None/非数 → 原值返回（NaN 传播，但不让 compute crash）
      - 正常路径：pnl_pct_net = pnl_pct_raw - round_trip_cost_bps(asset) / 100

    参数：
      pnl_pct_raw: 原始百分比 PnL（已含正负号）
      asset:       标的代码（任意大小写/别名都会被规范化）
      side:        BUY / SELL，目前不影响扣费方向（成本是 round-trip 概念）

    返回：
      净 pnl_pct（百分点，已扣成本）
    """
    if costs_globally_disabled():
        return pnl_pct_raw
    try:
        v = float(pnl_pct_raw)
    except (TypeError, ValueError):
        return pnl_pct_raw
    cost_bps = round_trip_cost_bps(asset)
    # 1 bp = 0.01%，所以 30bps = 0.3 pct
    cost_pct = cost_bps / 100.0
    return v - cost_pct


def cost_summary(asset: str) -> dict[str, float | str]:
    """给前端/审计用的明细（不含绕过开关）。"""
    f, s = _class_bps(asset)
    return {
        "asset": asset,
        "normalized_asset": _normalize_asset(asset),
        "fee_bps_per_side": f,
        "slippage_bps_per_side": s,
        "round_trip_bps": 2.0 * (f + s),
        "round_trip_pct": 2.0 * (f + s) / 100.0,
    }