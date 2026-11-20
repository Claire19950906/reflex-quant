#!/usr/bin/env python3
"""交易质量双维度纯函数库 —— 反思评估维度（买点识别准确性 + K线走势吻合度）。

设计约束（对齐 technical_indicators / abnormal_detector）：
  - 纯函数、无 I/O、无第三方依赖，便于离线单测；
  - 输入缺失 / 区间无波动 / action 未知 → 返回 None，绝不抛异常、绝不造数；
  - 阈值/口径可配置（此模块无阈值，仅做归一化打分）。

两个维度（均归一化到 0-1，越高越好）：
  entry_accuracy  买点识别准确性：入场价在持仓区间 [min, max] 中是否靠近理想端点。
                  BUY 想买低 → 越靠近 min 越准（1.0=买在最低点）；
                  SELL 想卖高 → 越靠近 max 越准（1.0=卖在最高点）。
  trend_fit       K线走势吻合度：持仓期间 K 线走向是否支持 AI 给的方向。
                  BUY 看涨 → 向上空间 / (向上+向下) 越大越吻合；
                  SELL 看跌 → 向下空间 / (向上+向下) 越大越吻合。
"""

from __future__ import annotations

from typing import Any, Dict, Optional


def _f(v: Any) -> Optional[float]:
    if v is None or v == "":
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def entry_accuracy(action: Any, entry_price: Any, min_price: Any,
                   max_price: Any) -> Optional[float]:
    """买点识别准确性 0-1；区间无波动（max==min）或数据缺失返回 None。"""
    action = str(action or "").strip().upper()
    e, lo, hi = _f(entry_price), _f(min_price), _f(max_price)
    if e is None or lo is None or hi is None:
        return None
    span = hi - lo
    if span <= 0:
        return None
    if action == "BUY":
        score = (hi - e) / span   # 越靠近区间低点越准
    elif action == "SELL":
        score = (e - lo) / span   # 越靠近区间高点越准
    else:
        return None
    return round(max(0.0, min(1.0, score)), 4)


def trend_fit(action: Any, entry_price: Any, min_price: Any,
              max_price: Any) -> Optional[float]:
    """K线走势吻合度 0-1；区间无波动或数据缺失返回 None。

    以持仓区间 [min, max] 相对入场价的向上/向下空间占比衡量方向吻合：
      up = max - entry  （向上空间），dn = entry - min（向下空间）。
    BUY 看涨：score = up / (up+dn)；SELL 看跌：score = dn / (up+dn)。
    """
    action = str(action or "").strip().upper()
    e, lo, hi = _f(entry_price), _f(min_price), _f(max_price)
    if e is None or lo is None or hi is None:
        return None
    up = hi - e
    dn = e - lo
    total = up + dn
    if total <= 0:
        return None
    if action == "BUY":
        score = up / total
    elif action == "SELL":
        score = dn / total
    else:
        return None
    return round(max(0.0, min(1.0, score)), 4)


def trade_quality(action: Any, entry_price: Any, min_price: Any,
                  max_price: Any) -> Dict[str, Optional[float]]:
    """一次算出两个质量维度。"""
    return {
        "entry_accuracy": entry_accuracy(action, entry_price, min_price, max_price),
        "trend_fit": trend_fit(action, entry_price, min_price, max_price),
    }