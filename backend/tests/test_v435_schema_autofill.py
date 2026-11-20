#!/usr/bin/env python3
"""v435 P0-2 回归：结构性字段本地推导（不为此类缺口发 LLM 请求）。

依据 2026-11-17 全天日志里「缺 P0-2 必填字段」的字段分布：
fund_driver / consensus_vs_us / asset_classification 各 464 次、asset_nature 399 次，
这些都不是模型的判断，而是"描述/分类"字段 —— 本地能定就不该再花一次请求。
"""
import os
import sys

SRC = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "src_python"))
if SRC not in sys.path:
    sys.path.insert(0, SRC)

from engine import ai_worker as w


def test_asset_classification_from_market_category():
    r = {"target_asset": "BTC", "market_category": "CRYPTO", "sentiment_score": 0.6,
         "reasoning": "比特币突破关键阻力，链上资金流入明显"}
    filled = w._autofill_soft_fields(r)
    assert r["asset_classification"] == "高Beta"
    assert "asset_classification" in filled


def test_gold_and_oil_override_market_category():
    r = {"target_asset": "XAU", "market_category": "MACRO"}
    w._autofill_soft_fields(r)
    assert r["asset_classification"] == "避险/抗通胀"
    r2 = {"target_asset": "WTI", "market_category": "MACRO"}
    w._autofill_soft_fields(r2)
    assert r2["asset_classification"] == "供给驱动"


def test_asset_nature_skipped_for_none_target():
    r = {"target_asset": "NONE", "market_category": "MACRO"}
    w._autofill_soft_fields(r)
    assert not r.get("asset_nature"), "无标的时不能凭空造资产性质"


def test_asset_nature_built_for_real_target():
    r = {"target_asset": "ETH", "market_category": "CRYPTO"}
    w._autofill_soft_fields(r)
    assert r["asset_nature"].startswith("ETH")
    assert len(r["asset_nature"]) <= 40


def test_fund_driver_from_reasoning():
    r = {"target_asset": "ETH", "market_category": "CRYPTO",
         "reasoning": "以太坊ETF净流入放大，链上质押量创新高，短线偏多。" * 3}
    w._autofill_soft_fields(r)
    assert r["fund_driver"]
    assert len(r["fund_driver"]) <= 60


def test_consensus_neutral_vs_directional():
    r = {"sentiment_score": 0.01}
    w._autofill_soft_fields(r)
    assert r["consensus_vs_us"] == "一致"
    r2 = {"sentiment_score": -0.4}
    w._autofill_soft_fields(r2)
    assert r2["consensus_vs_us"] == "反共识"


def test_verification_marks_autofill_honestly():
    r = {"target_asset": "BTC", "market_category": "CRYPTO", "sentiment_score": 0.2,
         "reasoning": "突破后回踩确认"}
    w._autofill_soft_fields(r)
    assert r["verification"].startswith("pass")
    assert "本地补全" in r["verification"]
    assert r["_schema_autofill"], "补过的字段必须留痕，能被统计区分"


def test_does_not_overwrite_model_output():
    r = {
        "target_asset": "BTC", "market_category": "CRYPTO",
        "asset_classification": "高Beta", "asset_nature": "加密龙头，风险偏好代理",
        "fund_driver": "ETF 资金流入", "consensus_vs_us": "反共识",
        "verification": "warn | 分数与方向不一致",
        "sentiment_score": 0.3,
    }
    filled = w._autofill_soft_fields(r)
    assert filled == []
    assert r["asset_nature"] == "加密龙头，风险偏好代理"
    assert r["verification"].startswith("warn")
