# -*- coding: utf-8 -*-
"""v342 — 降级文案跟着品种走（rerender_degrade_text_for_asset）

真实 bug（实测数据）：
  168 条核心品种（XAU/WTI/BTC）决策里有 93 条（55%）的 reasoning_path 写着
  「目标资产 NONE … 该标的**不在**核心监控清单」。
  根因：降级兜底按 NONE 写了文案 → ASSETFIX/MAINFIX 改了 target_asset
  → 文案没跟着改 → 记录自相矛盾。
  后果不是分数错，而是**归因错**：反思据此把「LLM 空输出」学成「品种不在监控范围」。

钉死的四件事：
  1. 核心品种被提升后，文案重写成 in-scope 版本（含"该品种在核心监控清单内"）
  2. 品种本就不在核心范围（NVDA）→ 重写为"确实不在核心监控清单"，不伪装
  3. 不是降级文案（正常八段）→ 一个字都不动
  4. 只改文案，suggested_action / sentiment_score 原样保留
"""
from __future__ import annotations

import os
import sys

SRC = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "src_python"))
if SRC not in sys.path:
    sys.path.insert(0, SRC)

from engine.ai_worker import (
    asset_in_core_scope,
    degraded_reasoning_template,
    rerender_degrade_text_for_asset,
)


def _stale_result(asset="NONE"):
    """复现线上那条矛盾记录：文案按 NONE 写，实际品种是 XAU。"""
    return {
        "target_asset": asset,
        "market_category": "",
        "sentiment_score": 0.0,
        "suggested_action": "HOLD",
        "reasoning": "[副品种 XAU] 该标的**不在**核心监控清单（BTC/XAU/WTI/ETH/SOL），按非交易信号处理",
        "reasoning_path": degraded_reasoning_template(
            asset="NONE", market_category="", action="HOLD", score_s="+0.00",
            summary_line="NONE 不在核心监控清单", in_scope=False,
        ),
    }


def test_core_asset_promotion_rewrites_text():
    r = _stale_result("XAU")
    assert rerender_degrade_text_for_asset(r, "XAU") is True
    assert "目标资产 XAU" in r["reasoning_path"]
    assert "目标资产 NONE" not in r["reasoning_path"]
    assert "不在核心监控清单" not in r["reasoning_path"]
    assert "核心监控清单内" in r["reasoning_path"]
    assert "分析降级" in r["reasoning_path"]
    # reasoning 那一句也要改，且不再说"不在清单"
    assert "不在核心监控清单" not in r["reasoning"]
    assert "核心监控清单内" in r["reasoning"]


def test_out_of_scope_asset_says_it_honestly():
    r = _stale_result("NVDA")
    assert asset_in_core_scope("NVDA") is False
    assert rerender_degrade_text_for_asset(r, "NVDA") is True
    assert "目标资产 NVDA" in r["reasoning_path"]
    assert "不在" in r["reasoning_path"]          # 确实不在范围，就该这么说
    assert "确实不在核心监控清单" in r["reasoning"]


def test_normal_analysis_is_untouched():
    r = {
        "target_asset": "XAU",
        "reasoning": "黄金突破",
        "reasoning_path": "[总框架] 目标资产 XAU；[因素定位] 实际八段推演……",
        "suggested_action": "BUY",
        "sentiment_score": 0.42,
    }
    before = dict(r)
    assert rerender_degrade_text_for_asset(r, "XAU") is False
    assert r == before


def test_judgment_fields_never_touched():
    r = _stale_result("XAU")
    r["suggested_action"] = "SELL"
    r["sentiment_score"] = -0.31
    rerender_degrade_text_for_asset(r, "XAU")
    assert r["suggested_action"] == "SELL"
    assert r["sentiment_score"] == -0.31
