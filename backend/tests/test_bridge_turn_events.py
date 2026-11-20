# -*- coding: utf-8 -*-
"""tests/test_bridge_turn_events.py — 转折/形态事件的定向与佐证口径回归。

依据 reports/indicator_ledger/factor_effectiveness.md（逐点事件口径，5 日前向）：
  - ma_golden / engulfing_bull 偏多但超额近 0 → 不单独定向，仅给 BUY 轻量加分；
  - ma_death / engulfing_bear / MACD 零轴穿越 噪声/反向 → HOLD 背景；
  - 有效极值因子（kdj_oversold 等）仍可单独出 BUY/SELL。
"""
from __future__ import annotations

import sys
from pathlib import Path

SRC = Path(__file__).resolve().parents[1] / "src_python"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

import tech_signal_bridge as bridge


def test_turn_bull_alone_is_hold():
    """金叉/看涨吞没单独出现不直出 BUY。"""
    for k in ("ma_golden", "engulfing_bull"):
        action, _reason, strength = bridge.decide_direction([{"k": k, "w": 0.5}], 0.1)
        assert action == "HOLD", f"{k} 不应单独定向"
        assert strength == 0.0


def test_turn_bear_alone_is_hold():
    """死叉/看跌吞没/MACD 零轴穿越单独出现不直出 SELL。"""
    for k in ("ma_death", "engulfing_bear", "macd_above_zero", "macd_below_zero"):
        action, _reason, _strength = bridge.decide_direction([{"k": k, "w": 0.5}], 0.2)
        assert action == "HOLD", f"{k} 不应单独定向"


def test_turn_bull_boosts_buy_but_does_not_trigger():
    """有效 BUY 因子 + 偏多转折 → BUY，强度带转折佐证加分。"""
    base_action, base_reason, base_strength = bridge.decide_direction(
        [{"k": "kdj_oversold", "w": 0.5}], -0.3)
    boosted_action, boosted_reason, boosted_strength = bridge.decide_direction(
        [{"k": "kdj_oversold", "w": 0.5}, {"k": "ma_golden", "w": 0.5}], -0.3)
    assert base_action == "BUY"
    assert boosted_action == "BUY"
    assert boosted_strength > base_strength
    assert "转折形态偏多佐证" in boosted_reason


def test_turn_bear_does_not_boost_buy():
    """偏空转折配 BUY 不加重（只作背景，逆势不额外减分也不加）。"""
    base_action, _, base_strength = bridge.decide_direction(
        [{"k": "kdj_oversold", "w": 0.5}], -0.3)
    mixed_action, mixed_reason, mixed_strength = bridge.decide_direction(
        [{"k": "kdj_oversold", "w": 0.5}, {"k": "ma_death", "w": 0.5}], -0.3)
    assert base_action == mixed_action == "BUY"
    assert mixed_strength == base_strength
    assert "转折形态" not in mixed_reason
