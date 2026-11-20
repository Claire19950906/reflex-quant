# -*- coding: utf-8 -*-
"""v341: 外部指标的坐标系 —— 分清"品种价格"和"市场聚合量"。

背景：`external_indicators` 的 asset 列有时不是品种而是抽屉：
    '*'      + 'GLD'                → 其实是 GLD 这只能源/黄金 ETF 的价格
    'FX'     + 'fx_eurusd'          → 汇率，品种是 EURUSD
    'CRYPTO' + 'cp_total_mcap_usd'  → 全市场市值，没有品种
    'STABLE' + 'sc_usdt_mcap'       → 稳定币存量，不是价格
    'US_MACRO' + 'us_dgs10_calc'    → 10Y 收益率，是水平值不是价格
不分开，按 asset 聚合时这些会被当成"一个品种的行情"。
"""
from __future__ import annotations

import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src_python"))

import series_namespace as sn


@pytest.mark.parametrize("asset,indicator,ns,series,kind", [
    ("*", "GLD", "asset", "GLD", "price"),
    ("*", "TLT", "asset", "TLT", "price"),
    ("*", "^GSPC", "asset", "SPX", "price"),
    ("*", "^TNX", "macro", "UST10Y", "yield"),      # 10Y 是收益率，别当价格
    ("*", "DX-Y.NYB", "asset", "DXY", "price"),
    ("*", "xau_xag_ratio", "market", "ratio:xau_xag_ratio", "ratio"),
    ("*", "fed_total_assets", "macro", "fed_total_assets", "macro"),
    ("FX", "fx_eurusd", "asset", "EURUSD", "price"),
    ("FX", "dxy_ecb_computed", "asset", "DXY", "price"),
    ("CRYPTO", "cp_total_mcap_usd", "market", "cp_total_mcap_usd", "aggregate"),
    ("STABLE", "sc_usdt_mcap", "supply", "sc_usdt_mcap", "supply"),
    ("US_MACRO", "us_dgs10_calc", "macro", "us_dgs10_calc", "yield"),
    ("US_MACRO", "us_vix_cboe", "asset", "VIX", "price"),
    ("US_MACRO", "us_vix_change", "macro", "us_vix_change", "change"),
    ("fred", "VIX", "asset", "VIX", "price"),
])
def test_resolve_known_drawers(asset, indicator, ns, series, kind):
    got = sn.resolve(asset, indicator)
    assert (got.namespace, got.series, got.kind) == (ns, series, kind)


def test_plain_asset_is_price():
    for a, ind in [("BTC", "close"), ("NVDA", "close"), ("000660.KS", "close")]:
        got = sn.resolve(a, ind)
        assert got.namespace == "asset" and got.kind == "price"


def test_supply_never_looks_like_price():
    """稳定币存量/流通量：asset 正常也要判 supply。"""
    for a, ind in [("USDT", "cm_supply"), ("USDC", "sc_mcap_usd"),
                   ("BTC", "cm_supply")]:
        assert sn.resolve(a, ind).kind == "supply"


def test_is_drawer():
    assert sn.is_drawer("*") is True
    assert sn.is_drawer("CRYPTO") is True
    assert sn.is_drawer("BTC") is False
