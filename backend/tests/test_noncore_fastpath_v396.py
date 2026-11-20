#!/usr/bin/env python3
"""v396.2：非核心新闻的"快速通道"（跳过 schema 补全 / 两步抢救）。

背景（实测 2026-11-16 20:40 日志）：
    一批 8 条，实际只落库 1-3 条；日志里每条 NONE 类新闻都会打
    "缺 P0-2 必填字段 [...] retrying" + 两步抢救 —— 每条多烧 3-5 次模型调用，
    单条 1-2 分钟，一批拖到 20 分钟。而这些新闻本身是
    "英伟达/亚马逊热议""CFTC 命令争议""美联储 50bp 讨论"，池里只有 BTC/XAU/WTI/ETH/SOL，
    **抢救不可能凭空造出标的**，最后判的还是 NONE。

口径（三重同时成立才算"天生非核心"）：
    1. 模型明确 target_asset=NONE/OTHER（不是字段缺失）
    2. 模型自己也没给 market_category
    3. 全文（模型看到的正文）一次都没出现 POOL_KEYWORDS

宁可多花 token（命中关键词照常抢救），不可误杀真信号。
开关 NEWS_NONCORE_FASTPATH=0 可回退旧行为。
"""
from __future__ import annotations

import sys
from pathlib import Path

SRC = Path(__file__).resolve().parents[1] / "src_python"
sys.path.insert(0, str(SRC))

from engine.ai_worker import declares_non_core_news, news_mentions_pool_asset


def _res(**kw):
    base = {"target_asset": "NONE", "market_category": "",
            "reasoning_path": "总框架：本条与 BTC/XAU/WTI 无直接标的关联。" * 6}
    base.update(kw)
    return base


def test_no_pool_keyword_detected():
    assert news_mentions_pool_asset("市场热议：英伟达、亚马逊、微软、OpenAI") is False


def test_pool_keyword_detected():
    assert news_mentions_pool_asset("比特币突破 10 万美元关口") is True
    # v420：宏观驱动词（美联储/降息/通胀…）是"影响品种的原因"，不是"提到了品种"，
    # 不计入品种关键词命中。
    assert news_mentions_pool_asset("美联储 12 月降息概率上升") is False


def test_declares_non_core_true():
    assert declares_non_core_news(_res(), "民主党参议员称 CFTC 命令必须撤销") is True


def test_not_non_core_when_pool_keyword_present():
    """新闻里提到比特币 → 即便模型判 NONE，也必须照常抢救（可能漏了间接影响）。"""
    assert declares_non_core_news(_res(), "分析师称比特币 ETF 资金流出") is False


def test_not_non_core_when_category_given():
    """模型给了分类但标的 NONE —— 说明它认为属于某类市场，不该跳过抢救。"""
    assert declares_non_core_news(_res(market_category="STOCK"), "某公司公告") is False


def test_not_non_core_without_reasoning():
    """没有真推演（reasoning_path 太短）的 NONE 仍按"没锁住"处理，照常抢救。"""
    assert declares_non_core_news(_res(reasoning_path=""), "某公司公告") is False


def test_not_non_core_when_asset_present():
    assert declares_non_core_news(_res(target_asset="BTC"), "无关文本") is False


def test_empty_text_is_conservative():
    """正文为空不敢断言 → 按"可能有"处理（不跳过抢救）。"""
    assert news_mentions_pool_asset("") is True
