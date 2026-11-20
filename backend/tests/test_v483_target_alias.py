#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""v483：target_asset=NONE 的唯一具体品种别名回退。"""
from __future__ import annotations

import sys
from pathlib import Path

_HERE = Path(__file__).resolve().parent
_SRC = _HERE.parent / "src_python"
sys.path.insert(0, str(_SRC))

from target_resolver import resolve_specific_one
import engine.ai_worker as ai_worker


def test_unique_specific_alias():
    assert resolve_specific_one("英伟达发布新一代 GPU") == "US_STOCK"
    assert resolve_specific_one("现货黄金突破前高") == "XAU"
    assert resolve_specific_one("白银价格大涨") == "XAG"
    assert resolve_specific_one("WTI 原油库存下降") == "WTI"


def test_ambiguous_or_aggregate_not_guessed():
    assert resolve_specific_one("黄金和原油同时上涨") is None
    assert resolve_specific_one("英伟达和特斯拉一起涨") is None
    assert resolve_specific_one("美股三大指数收涨") is None
    assert resolve_specific_one("今天市场没有明确标的") is None


def test_presanitize_fills_none_target():
    r = {"target_asset": "NONE", "suggested_action": "HOLD", "reasoning": "英伟达发布新芯片"}
    out = ai_worker._presanitize_for_schema(r, "英伟达发布新一代 GPU，股价盘后上涨")
    assert out["target_asset"] == "US_STOCK"
    assert out["asset_route"] == "alias_fallback"


def test_presanitize_keeps_none_when_ambiguous():
    r = {"target_asset": "NONE", "suggested_action": "HOLD", "reasoning": "黄金和原油同涨"}
    out = ai_worker._presanitize_for_schema(r, "黄金和原油同时上涨，市场避险情绪升温")
    assert out["target_asset"] == "NONE"


def test_xag_and_copper_are_valid_target_enum():
    for ta in ("XAG", "COPPER", "BRENT"):
        out = ai_worker._presanitize_for_schema({"target_asset": ta}, "")
        assert out["target_asset"] == ta
