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
    # 能源：WTI / Brent 的期货+点差成本与 crypto 同档，不能落到 0
    "WTI":   {"fee_bps": 10.0, "slippage_bps": 5.0},
    "BRENT": {"fee_bps": 10.0, "slippage_bps": 5.0},
    "OIL":   {"fee_bps": 10.0, "slippage_bps": 5.0},
    "STOCK": {"fee_bps": 2.0,  "slippage_bps": 2.0},
    "FX":    {"fee_bps": 1.0,  "slippage_bps": 1.0},
}

# 资产别名归一化（quick_sim 里 asset 可能是 "BTC" 也可能是 "BTCUSDT"）
_ASSET_ALIASES: Final[dict[str, str]] = {
    "BTCUSDT": "BTC", "ETHUSDT": "ETH", "SOLUSDT": "SOL",
    "BNBUSDT": "BNB", "PAXGUSDT": "PAXG",
    "GOLD": "XAU",  # 双源旧名称兼容
    "XAUUSD": "XAU", "XAUUSDT": "XAU",
    "WTIUSD": "WTI", "USOIL": "WTI", "CL": "WTI",
    "BRENTUSD": "BRENT", "UKOIL": "BRENT",
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
        # env 写错（比如 "18bps" / 空串）不能让整条结算链崩掉：
        # 解析失败就当没设，退回默认费率并留一条 warning。
        try:
            f = float(env_fee) if env_fee is not None else _DEFAULT_BPS["fee_bps"]
            s = float(env_slip) if env_slip is not None else _DEFAULT_BPS["slippage_bps"]
            return f, s
        except (TypeError, ValueError):
            logger.warning("TRIDENT_FEE_BPS/SLIPPAGE_BPS 不是数字, 忽略: %r / %r",
                           env_fee, env_slip)
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


# ──────────────────────────────────────────────────────────────────────────
# v319+ 结算时点规则 (T+0 / T+1)
# ──────────────────────────────────────────────────────────────────────────
# 成本模型回答"交易一次要花多少钱", 但没回答"**什么时候能平仓**"。
#
# 为什么必须显式建模
#   A 股 / 股票是 T+1: 当天买入当天不能卖。如果回测/结算直接拿**当天**的收盘价
#   平仓, 就凭空多出一段实盘根本做不到的"当日买当日卖"收益 —— 回测胜率虚高,
#   实盘一上就穿仓。加密 (24x7) 与外汇才是 T+0。
#
# 口径 (asset class → 规则):
#   CRYPTO (BTC/ETH/SOL/BNB/PAXG) : T+0, 当日可平仓
#   METAL  (XAU/GOLD)              : T+0, 场外/期货均可当日平
#   ENERGY (WTI/BRENT/OIL)         : T+0
#   FX                            : T+0
#   EQUITY (STOCK/个股/ETF)        : T+1, 次一交易日才能平仓
#   未知品种                       : T+1 (保守: 宁可少算不可虚高)
#
# env 覆盖:
#   TRIDENT_SETTLEMENT_LAG_DAYS — 全局覆盖 lag_days (0=T+0, 1=T+1)
#   TRIDENT_SETTLEMENT_TZ      — 股票市场时区 (IANA 名, 默认 Asia/Shanghai)
_SETTLEMENT_RULES: Final[dict[str, dict]] = {
    "BTC":  {"lag_days": 0, "market": "crypto", "note": "24x7 现货/永续, 当日可平仓"},
    "ETH":  {"lag_days": 0, "market": "crypto", "note": "24x7 现货/永续, 当日可平仓"},
    "SOL":  {"lag_days": 0, "market": "crypto", "note": "24x7 现货/永续, 当日可平仓"},
    "BNB":  {"lag_days": 0, "market": "crypto", "note": "24x7 现货/永续, 当日可平仓"},
    "PAXG": {"lag_days": 0, "market": "crypto", "note": "代币化黄金 24x7, 当日可平仓"},
    "XAU":  {"lag_days": 0, "market": "metal", "note": "COMEX 黄金/场外现货, T+0"},
    "GOLD": {"lag_days": 0, "market": "metal", "note": "COMEX 黄金/场外现货, T+0"},
    "WTI":  {"lag_days": 0, "market": "energy", "note": "NYMEX 原油, T+0"},
    "BRENT": {"lag_days": 0, "market": "energy", "note": "ICE 原油, T+0"},
    "OIL":  {"lag_days": 0, "market": "energy", "note": "原油, T+0"},
    "FX":   {"lag_days": 0, "market": "fx", "note": "外汇, T+0"},
    "STOCK": {"lag_days": 1, "market": "equity", "note": "股票 T+1: 当日买入次日才可卖"},
}

# 未知品种的保守默认: T+1 (假设最严的交收约束)
_DEFAULT_LAG_DAYS: Final[int] = 1
_DEFAULT_EQUITY_TZ: Final[str] = "Asia/Shanghai"

# 股票开市时间 (当地时间的小时/分钟) —— 只用于算"最早可平仓时点"
_EQUITY_SESSION_OPEN: Final[tuple[int, int]] = (9, 30)


def _settlement_lag_override() -> int | None:
    raw = os.environ.get("TRIDENT_SETTLEMENT_LAG_DAYS")
    if raw is None or not str(raw).strip():
        return None
    try:
        return max(0, int(float(str(raw).strip())))
    except (TypeError, ValueError):
        logger.warning("TRIDENT_SETTLEMENT_LAG_DAYS 不是数字, 忽略: %r", raw)
        return None


def settlement_rule(asset: str) -> dict:
    """资产 → 结算时点规则 (T+0 / T+1)。"""
    norm = _normalize_asset(asset)
    rule = dict(_SETTLEMENT_RULES.get(norm, {
        "lag_days": _DEFAULT_LAG_DAYS,
        "market": "unknown",
        "note": "未知品种, 按最严的 T+1 保守处理",
    }))
    override = _settlement_lag_override()
    if override is not None and rule["lag_days"] != override:
        rule["lag_days"] = override
        rule["note"] = f"{rule['note']} (env 覆盖 lag_days={override})"
    rule["t_plus"] = "T+0" if rule["lag_days"] == 0 else "T+1"
    rule["normalized_asset"] = norm
    return rule


def settlement_lag_days(asset: str) -> int:
    """最早可平仓的**日历日**偏移: 0 = T+0 (当日可平), 1 = T+1 (次日才可平)。"""
    return int(settlement_rule(asset)["lag_days"])


def _equity_tz():
    """股票市场时区 (zoneinfo), 失败回退 UTC+8 固定偏移。"""
    name = (os.environ.get("TRIDENT_SETTLEMENT_TZ") or _DEFAULT_EQUITY_TZ).strip()
    try:
        from zoneinfo import ZoneInfo
        return ZoneInfo(name)
    except Exception:  # pragma: no cover - 只有非法 tz 名才会走
        logger.warning("非法 TRIDENT_SETTLEMENT_TZ=%r, 回退 UTC+8", name)
        from datetime import timezone, timedelta
        return timezone(timedelta(hours=8))


def earliest_exit_ts(asset: str, entry_ts: int) -> int:
    """最早允许的平仓时点 (epoch 秒)。

    · T+0 → 原样返回 entry_ts (24x7 品种随时能平)
    · T+1 → 次一**交易日**的开盘时刻 (跳过周末; 开市 09:30 本地时间)
    """
    lag = settlement_lag_days(asset)
    if lag <= 0:
        return int(entry_ts)
    from datetime import datetime, timedelta
    tz = _equity_tz()
    dt = datetime.fromtimestamp(int(entry_ts), tz)
    # 次一交易日: 先 +1 天, 再跳过周末
    cand = (dt + timedelta(days=1)).replace(
        hour=_EQUITY_SESSION_OPEN[0], minute=_EQUITY_SESSION_OPEN[1],
        second=0, microsecond=0,
    )
    while cand.weekday() >= 5:  # 5=周六 6=周日
        cand += timedelta(days=1)
    return int(cand.timestamp())


def is_exit_allowed(asset: str, entry_ts: int, exit_ts: int) -> tuple[bool, str]:
    """这次平仓在交收规则上**允许**吗? → (允许?, 原因)。"""
    lag = settlement_lag_days(asset)
    if lag <= 0:
        return True, "T+0: 同日可平仓"
    earliest = earliest_exit_ts(asset, entry_ts)
    if int(exit_ts) < earliest:
        return False, (
            f"T+1: {asset} 最早可平仓时点 {earliest} (epoch), "
            f"本次平仓 {int(exit_ts)} 早于它"
        )
    return True, f"T+1: 平仓时点 {int(exit_ts)} ≥ 最早可平仓 {earliest}"


def effective_entry_price(price: float, asset: str, side: str = "BUY") -> float:
    """理论价 → **可成交价** (含不利滑点): BUY 买得更贵, SELL 卖得更便宜。"""
    try:
        p = float(price)
    except (TypeError, ValueError):
        return price
    _, slip = _class_bps(asset)
    frac = slip / 10_000.0
    mult = (1.0 + frac) if str(side).strip().upper() == "BUY" else (1.0 - frac)
    return p * mult


def effective_exit_price(price: float, asset: str, side: str = "BUY") -> float:
    """平仓成交价 (含不利滑点): BUY 平仓是卖 (价更低), SELL 平仓是买 (价更高)。"""
    return effective_entry_price(
        price, asset, "SELL" if str(side).strip().upper() == "BUY" else "BUY"
    )


def settlement_policy(asset: str) -> dict:
    """一次说清: 这笔交易的**成本口径** + **结算时点口径**。

    给前端/审计用 —— 不含绕过开关 (TRIDENT_DISABLE_COSTS 只影响 apply_costs)。
    """
    f, s = _class_bps(asset)
    rule = settlement_rule(asset)
    return {
        "asset": asset,
        "normalized_asset": rule["normalized_asset"],
        "fee_bps_per_side": f,
        "slippage_bps_per_side": s,
        "round_trip_bps": 2.0 * (f + s),
        "round_trip_pct": 2.0 * (f + s) / 100.0,
        "t_plus": rule["t_plus"],
        "lag_days": rule["lag_days"],
        "market": rule["market"],
        "settlement_note": rule["note"],
    }

