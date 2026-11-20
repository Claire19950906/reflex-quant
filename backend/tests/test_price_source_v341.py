# -*- coding: utf-8 -*-
"""v341: 取价的两条口径 —— 指标名归一 + 分辨率归一。

导致的问题都是实的：
  · 只认 close/price 时，NG('hf_fut_price')、DXY('DXY')、SGE_GOLD
    ('sge_au9999_cny_g') 全被判"指标不明确" → 一行异动都不出；
  · 认出来之后还有第二层坑：NG 那序列是**每 90 秒一根**，
    直接当日线用，k=1 就变成"90 秒涨跌"，1d/3d/5d 口径全废。
"""
from __future__ import annotations

import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src_python"))

import backfill_critical_moves as bcm


@pytest.mark.parametrize("avail,expect", [
    ({"hf_fut_price": 862, "hf_fut_chg_pct": 862}, "hf_fut_price"),
    ({"sge_au9999_cny_g": 400, "sge_au9999_high": 400, "sge_gold_premium_pct": 400},
     "sge_au9999_cny_g"),
    ({"DXY": 29}, "DXY"),
    ({"VIX": 30}, "VIX"),
])
def test_picks_price_like_indicator(avail, expect):
    assert bcm._pick_price_indicator(avail) == expect


@pytest.mark.parametrize("avail", [
    {"cm_supply": 2039, "sc_mcap_usd": 2039},          # 稳定币市值，不是价格
    {"sc_change_24h": 900, "sc_total_mcap": 900},       # 变化率/存量
    {"insider_buy_shares_30d": 4},                      # 内部人股数
    {"cp_total_mcap_usd": 80, "stable_supply_total": 29},
])
def test_refuses_non_price_indicators(avail):
    assert bcm._pick_price_indicator(avail) is None


def test_keeps_daily_series_as_is():
    prices = [(1_700_000_000 + i * 86400, 100.0 + i) for i in range(10)]
    assert bcm._to_daily(prices) == prices


def test_collapses_intraday_to_daily():
    """90 秒一根 → 每个自然日只留最后一根。"""
    t0 = 1_700_006_400            # 某天 00:00 UTC
    prices = [(t0 + i * 90, 100.0 + i) for i in range(5)]
    prices += [(t0 + 86400 + i * 90, 200.0 + i) for i in range(3)]
    got = bcm._to_daily(prices)
    assert len(got) == 2
    assert got[0][1] == 104.0      # 第一天最后一根
    assert got[1][1] == 202.0      # 第二天最后一根


def test_short_series_untouched():
    prices = [(1_700_000_000, 10.0), (1_700_000_090, 10.1)]
    assert bcm._to_daily(prices) == prices
