#!/usr/bin/env python3
"""v396 P3 回归：R11 降级禁建仓 / 池外个股预路由 / 啰嗦惩罚（不再按字数罚）。

守的三件事：
1. analysis_degraded=1 的"方向"不许变成仓位（实测 3 笔成交里 2 笔是降级信号）。
2. A 股个股公告在进 LLM 之前就被分流，不烧 token、不污染 HOLD 分母。
3. 推演写得长 ≠ 过度分析；只有重复啰嗦才扣分。
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

SRC = Path(__file__).resolve().parents[1] / "src_python"
sys.path.insert(0, str(SRC))

from feeds import news_router
import sample_quality_filter as sqf
from common import text_quality


# ─────────────────── R11 ───────────────────


def _decision(**kw):
    base = {
        "id": 1, "target_asset": "WTI", "evidence_action": "BUY",
        "evidence_confidence": 80.0,
        "trade_gate_reason": __import__("config").TRADE_GATE_PASSED_REASON,
        "status": "UNREAD", "analysis_degraded": 0, "analysis_degraded_reason": None,
    }
    base.update(kw)
    return base


def test_r11_blocks_degraded_decision():
    res = sqf.pre_trade_check(
        _decision(analysis_degraded=1, analysis_degraded_reason="server_fallback"),
        conn=None, r8_enabled=False, max_batch_siblings=0)
    assert res["is_valid"] is False
    assert any(r.startswith("R11") for r in res["reasons"]), res["reasons"]


def test_r11_passes_clean_decision():
    res = sqf.pre_trade_check(_decision(), conn=None, r8_enabled=False, max_batch_siblings=0)
    assert res["is_valid"] is True


def test_r11_ignores_missing_flag():
    d = _decision()
    d.pop("analysis_degraded")
    res = sqf.pre_trade_check(d, conn=None, r8_enabled=False, max_batch_siblings=0)
    assert res["is_valid"] is True  # 老库没这列 → 不拦（兼容）


# ─────────────────── 预路由 ───────────────────


@pytest.mark.parametrize("title,content", [
    ("【哈铁科技：董事、总经理于雷因工作调整离任】", "哈铁科技公告称，公司董事会收到辞职报告"),
    ("东材科技：拟以集中竞价交易方式回购股份", "回购金额不低于5000万元"),
    ("中国国航：10月客运运力投入同比下降2.2%", "平均客座率87.0%"),
    ("药康生物公告，拟以3.12亿元收购佰麦生物80%股权", "交易完成后成为控股子公司"),
])
def test_pool_out_for_stock_announcements(title, content):
    # v406 新增"非交易类主体"（辞职/回购…）口径，同一个"池外"结论会换理由串；
    # 这里只守"必须被拦下"，不再锁死理由前缀。
    reason = news_router.pool_out_reason(title, content)
    assert reason, f"股票公告必须判池外，实际={reason!r}"


@pytest.mark.parametrize("title,content", [
    ("黄金价格突破 2400 美元", "现货金创历史新高"),
    ("OPEC 宣布延长减产", "布伦特原油上涨 3%"),
    ("美联储 12 月降息概率升至 70%", "非农数据疲弱"),
    ("比特币 ETF 单周净流入 12 亿美元", "机构持续加仓"),
    ("爱奇艺发布财报，公司营收增长", "同时宣布与中东基金合作"),  # 含池外词但命中"中东" → 必须分析
])
def test_analyze_when_pool_keyword_present(title, content):
    assert news_router.pool_out_reason(title, content) == ""


def test_non_company_news_not_routed():
    # 没有公司行为标记 → 不做池外判定（宁可多花 token，不误杀）
    assert news_router.pool_out_reason("工信部就汽车企业账款支付调研作出回应", "") == ""


def test_pool_out_for_nonpool_ticker():
    # v400: 显式非池市场代码（[港股 00700]）即使没有公司行为词也要挡住，
    # 否则会走到主分析判 NONE，再记一条假 HOLD。
    # v424 扩池：美股/台股已纳入交易池，其代码标记放行、不再判池外。
    assert news_router.pool_out_reason("腾讯控股：拟回购 10 亿港元股份 [港股 00700]", "").startswith("池外个股")
    assert news_router.pool_out_reason(
        "市场热议：英伟达、亚马逊、微软、OpenAI和Anthropic将被白宫命名为“人工智能全国冠军” [美股 MSFT]", "") == ""
    assert news_router.pool_out_reason("台积电 10 月营收同比增长 15% [台股 2330]", "") == ""


@pytest.mark.parametrize("title", [
    "美股期货走高，市场等待 CPI 数据",
    "港股恒生指数收涨 1.2%，成交额创月内新高",
    "日经指数突破 4 万点，日元走弱",
])
def test_broad_market_news_still_analyzed(title):
    # 只有市场名、没有具体标的代码 → 可能是跨市场驱动，照常分析
    assert news_router.pool_out_reason(title, "") == ""


# ─────────────────── 啰嗦惩罚 ───────────────────

_LONG_STRUCTURED = """[总框架] 目标资产 WTI，market_category=OIL，本次事件为供给端冲击。
[因素定位] 驱动来自输油设施遇袭，短期减产 20 万桶/日，库存去化加速。
[驱动力] 供给收缩叠加低库存，价格弹性向上，炼厂利润承压。
[水位博弈] 持仓水位中性，多头拥挤度不高，回撤风险可控。
[跨资产资金] 美元指数走弱，黄金同步走强，风险偏好边际改善。
[反共识结论] 市场共识偏空，本次判断偏多，差异来自供给端定价。
[逻辑自检] 若 48 小时内复产，则供给冲击消失，结论失效。
[总结] WTI 短线偏多，失效条件为快速复产。"""


def test_long_structured_reasoning_not_penalized():
    factor, note = text_quality.overanalysis_penalty(_LONG_STRUCTURED, threshold=300)
    assert factor == 1.0
    assert note == ""


def test_repetitive_reasoning_penalized():
    rep = ("[总框架] 供给收缩推动价格上行，库存去化加速，价格弹性向上。" * 12)
    factor, note = text_quality.overanalysis_penalty(rep, threshold=300)
    assert factor < 1.0
    assert factor >= 0.75          # 上限 25%
    assert "啰嗦" in note


def test_short_reasoning_not_penalized():
    factor, note = text_quality.overanalysis_penalty("[总结] 无方向。", threshold=1800)
    assert factor == 1.0 and note == ""


def test_penalty_cap_is_25_percent():
    rep = ("同一句话反复说很多遍用来凑字数导致重复率极高。" * 40)
    factor, _ = text_quality.overanalysis_penalty(rep, threshold=100, base_pen=25.0)
    assert factor >= 0.75
