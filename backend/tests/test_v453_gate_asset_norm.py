# -*- coding: utf-8 -*-
"""v453 P0-3：门卫主品种归一 + 股票桶降级。

实测背景（2026-11-18）：门卫判「可交易」的 921 条里，`gate_main_asset` 写的是
`个股`243 / `原油`58 / `黄金`51 / `美股`35 / `加密主流`17 —— 下游 v451 只认
XAU/BTC/WTI/ETH/SOL 这类代码，于是中文名与桶标签整批走完主分析仍判 NONE。
"""

from __future__ import annotations

import re

import realtime_filter as rf


def _result(main_asset="", assets="", verdict="可交易"):
    return {
        "gate": {"verdict": verdict, "main_asset": main_asset, "assets": assets, "strength": 60},
        "gate_verdict": verdict,
        "is_noise": 0,
        "status": "PENDING",
    }


def test_gold_and_oil_names_normalized():
    out = rf._finalize_gate_assets(_result("黄金"), "金价创阶段新高", "现货黄金上涨")
    assert out["gate_verdict"] == "可交易"
    assert out["gate"]["main_asset"] == "XAU"

    out = rf._finalize_gate_assets(_result("原油"), "OPEC 减产", "原油期货走高")
    assert out["gate"]["main_asset"] == "WTI"


def test_english_alias_normalized():
    out = rf._finalize_gate_assets(_result("GOLD"), "Gold rallies", "gold price up")
    assert out["gate"]["main_asset"] == "XAU"


def test_stock_bucket_dowgraded_to_excluded():
    out = rf._finalize_gate_assets(
        _result("个股"),
        "某公司财报超预期，股价涨停",
        "公司发布三季报，净利润同比增长，股价涨停。",
    )
    assert out["gate_verdict"] == "排除"
    assert out["status"] == "FILTERED"
    assert out["is_noise"] == 1


def test_stock_bucket_with_macro_hint_still_analyzed():
    out = rf._finalize_gate_assets(
        _result("个股"),
        "美联储官员讲话",
        "美联储主席鲍威尔表示可能需要进一步加息，市场风险偏好下降。",
    )
    assert out["gate_verdict"] == "可交易"


def test_crypto_class_word_maps_to_btc():
    out = rf._finalize_gate_assets(
        _result("加密主流"),
        "Coinbase 开放现货杠杆",
        "Coinbase 向美国合资格用户开放现货杠杆交易，最高 10 倍。",
    )
    assert out["gate_verdict"] == "可交易"
    assert out["gate"]["main_asset"] == "BTC"


def test_text_alias_beats_empty_main():
    out = rf._finalize_gate_assets(_result(""), "白银大涨", "现货白银日内涨幅扩大至 1%。")
    assert out["gate"]["main_asset"] == "XAG"


def test_non_tradeable_verdict_untouched():
    out = rf._finalize_gate_assets(_result("个股", verdict="排除"), "财报", "净利润")
    assert out["gate_verdict"] == "排除"
    assert out["gate"]["main_asset"] == "个股"


def test_no_gate_untouched():
    out = rf._finalize_gate_assets({"gate": None, "gate_verdict": "可交易"}, "标题", "正文")
    assert out["gate"] is None
