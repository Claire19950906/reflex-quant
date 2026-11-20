#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""v420：快路径 `_noncore_fast` 为什么从来没命中过。

实测（2026-11-17）：今日 21 条 target_asset=NONE 的决策里
  MACRO 18 条 / 空 3 条 —— 而 `declares_non_core_news` 要求 market_category
  **完全为空**才有资格走快路径 → 日志里 `_noncore_fast` 命中 **0 次**。
`MACRO` 是"宏观"这个合法且正确的非品种分类，用它否定快路径是逻辑反了。
"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src_python"))

from engine.ai_worker import (
    declares_non_core_news,
    _is_specific_asset_category,
)

_LONG_REASONING = "水位博弈：当前宏观水位中性偏紧。" * 12  # >100 字


def _none_result(market_category: str):
    return {
        "target_asset": "NONE",
        "market_category": market_category,
        "reasoning_path": _LONG_REASONING,
    }


def test_macro_category_still_qualifies_for_fastpath():
    """实测最常见的形态：NONE + MACRO + 无品种词 → 应当命快路径。"""
    assert declares_non_core_news(
        _none_result("MACRO"), "美联储官员表示通胀仍需观察，措辞偏鹰") is True


def test_empty_category_qualifies():
    assert declares_non_core_news(
        _none_result(""), "某国央行行长发表讲话，强调政策独立性") is True


def test_specific_asset_category_blocks_fastpath():
    """给了品种级分类却又不填 target_asset → 可能真有标的没锁住，必须抢救。"""
    assert declares_non_core_news(
        _none_result("XAU"), "黄金价格出现异动但未说明原因") is False


def test_news_mentioning_pool_asset_blocks_fastpath():
    """正文提到池内品种 → 不能判天生非核心（防误杀真信号）。"""
    assert declares_non_core_news(
        _none_result("MACRO"), "比特币与黄金同步走强，市场风险偏好回升") is False


def test_non_none_target_never_fastpath():
    r = _none_result("MACRO")
    r["target_asset"] = "XAU"
    assert declares_non_core_news(r, "无关文本") is False


def test_short_reasoning_blocks_fastpath():
    """没有真推演的 NONE 仍要照常抢救。"""
    r = _none_result("MACRO")
    r["reasoning_path"] = "无"
    assert declares_non_core_news(r, "无关文本") is False


def test_category_classifier():
    assert _is_specific_asset_category("MACRO") is False
    assert _is_specific_asset_category("宏观") is False
    assert _is_specific_asset_category("") is False
    assert _is_specific_asset_category("XAU") is True
    assert _is_specific_asset_category("US_STOCK") is True
    assert _is_specific_asset_category("BTC") is True
