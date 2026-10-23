# -*- coding: utf-8 -*-
"""trade_quality —— 反思双质量维度（买点识别准确性 + K线走势吻合度）单测。

数学口径（0-1，越高越好）：
  entry_accuracy：BUY 越靠近区间低点越准（1=买在最低点）；SELL 越靠近高点越准。
  trend_fit：     BUY 向上空间占区间比；SELL 向下空间占区间比。
"""

from __future__ import annotations

import os
import sys

SRC = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "src_python"))
if SRC not in sys.path:
    sys.path.insert(0, SRC)

import trade_quality as tq


def test_entry_accuracy_buy_mid():
    assert tq.entry_accuracy("BUY", 100, 90, 110) == 0.5


def test_entry_accuracy_buy_buys_low():
    assert tq.entry_accuracy("BUY", 90, 90, 110) == 1.0


def test_entry_accuracy_buy_buys_high():
    assert tq.entry_accuracy("BUY", 110, 90, 110) == 0.0


def test_entry_accuracy_sell_mid():
    assert tq.entry_accuracy("SELL", 100, 90, 110) == 0.5


def test_entry_accuracy_sell_sells_high():
    assert tq.entry_accuracy("SELL", 110, 90, 110) == 1.0


def test_entry_accuracy_sell_sells_low():
    assert tq.entry_accuracy("SELL", 90, 90, 110) == 0.0


def test_trend_fit_buy_mostly_up():
    # BUY: entry=100, max=130, min=90 → up=30, dn=10 → 0.75
    assert tq.trend_fit("BUY", 100, 90, 130) == 0.75


def test_trend_fit_buy_mostly_down():
    # BUY: entry=100, max=105, min=80 → up=5, dn=20 → 0.2
    assert tq.trend_fit("BUY", 100, 80, 105) == 0.2


def test_trend_fit_sell_mostly_down():
    # SELL: entry=100, min=90, max=130 → dn=10, up=30 → 10/40 = 0.25
    assert tq.trend_fit("SELL", 100, 90, 130) == 0.25


def test_trend_fit_sell_down_wins():
    # SELL: entry=100, min=80, max=105 → dn=20, up=5 → dn/(up+dn)=0.8（向下占优=吻合看跌）
    assert tq.trend_fit("SELL", 100, 80, 105) == 0.8


def test_zero_span_returns_none():
    assert tq.entry_accuracy("BUY", 100, 100, 100) is None
    assert tq.trend_fit("BUY", 100, 100, 100) is None


def test_missing_price_returns_none():
    assert tq.entry_accuracy("BUY", None, 90, 110) is None
    assert tq.trend_fit("SELL", 100, None, 110) is None


def test_unknown_action_returns_none():
    assert tq.entry_accuracy("HOLD", 100, 90, 110) is None
    assert tq.trend_fit("", 100, 90, 110) is None


def test_clamped_within_zero_one():
    # entry 越界也只是理论值，clamp 防御：不输出区间外的分
    assert 0.0 <= tq.entry_accuracy("BUY", 999, 90, 110) <= 1.0
    assert 0.0 <= tq.trend_fit("BUY", 0, 90, 130) <= 1.0


def test_trade_quality_shape():
    q = tq.trade_quality("BUY", 90, 90, 110)
    assert q == {"entry_accuracy": 1.0, "trend_fit": 1.0}