# -*- coding: utf-8 -*-
"""v213 海力士 kind 分类回归测试。

修复背景：
  _classify_news_kind 原只把「海力士」新闻归到通用 `stock`，
  与 NVDA/TSLA/美光/三星混在一起，dashboard 无法单列、引擎无法按主体挑模板。
  v213 在 stock 之前加 `hynix` 分支，覆盖：
    · 中文「海力士」、「SK 海力士」
    · 英文 SK Hynix（含 0xA0 不间断空格变体）
    · ticker 000660.KS / KOSPI 000660

  反向回归：三星 HBM、美光、南亚 DRAM 都不应被误归 hynix。
"""
from __future__ import annotations

import os
import sys

SRC = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "src_python"))
if SRC not in sys.path:
    sys.path.insert(0, SRC)

import realtime_filter as rt


# ───── 正向用例：应归 hynix ─────

def test_classify_hynix_chinese_only():
    assert rt._classify_news_kind("海力士三季度利润创新高", "") == "hynix"


def test_classify_hynix_full_chinese():
    assert rt._classify_news_kind("SK海力士三季度利润创新高，HBM业务增长",
                                   "SK Hynix reported record Q3 profit") == "hynix"


def test_classify_hynix_english():
    assert rt._classify_news_kind("SK Hynix earnings beat expectations",
                                   "") == "hynix"


def test_classify_hynix_ticker_yahoo():
    # Yahoo Finance 写法
    assert rt._classify_news_kind("000660.KS 今日大涨", "") == "hynix"


def test_classify_hynix_kospi_ticker():
    assert rt._classify_news_kind("KOSPI 000660 业绩超预期", "") == "hynix"


def test_classify_hynix_hbm_context():
    # HBM 行业新闻只锁定海力士主体
    assert rt._classify_news_kind("海力士 HBM3E 产能扩张", "") == "hynix"


# ───── 反向用例：不应被误归 hynix ─────

def test_classify_samsung_hbm_stays_stock():
    # 三星也做 HBM，但不叫海力士
    assert rt._classify_news_kind("三星 HBM 产能扩张", "Samsung expands HBM") == "stock"


def test_classify_micron_stays_stock():
    assert rt._classify_news_kind("美光 Q2 财报超预期", "Micron earnings beat") == "stock"


def test_classify_nanya_dram_stays_stock():
    assert rt._classify_news_kind("南亚 DRAM 价格上涨", "Nanya DRAM prices rise") == "stock"


def test_classify_nvidia_stays_stock():
    # 老的 stock 分支不退化
    assert rt._classify_news_kind("英伟达 Q1 财报", "NVIDIA earnings") == "stock"


# ───── 老的 10 类回归 ─────

def test_classify_gold_intact():
    assert rt._classify_news_kind("黄金 ETF 持仓创新高", "Gold ETF holdings hit record") == "gold"


def test_classify_oil_intact():
    assert rt._classify_news_kind("WTI 原油库存大增", "Crude oil inventories surge") == "oil"


def test_classify_crypto_intact():
    assert rt._classify_news_kind("BTC 突破 70000 美元", "Bitcoin breaks 70K") == "crypto"


def test_classify_policy_intact():
    assert rt._classify_news_kind("美联储暗示 9 月降息", "Fed signals September rate cut") == "policy"


def test_classify_macro_intact():
    assert rt._classify_news_kind("CPI 数据低于预期", "Inflation data below expectations") == "macro"


def test_classify_geopolitical_intact():
    # 地缘优先级最高，应在 hynix 之前返回
    assert rt._classify_news_kind("台海局势紧张", "Taiwan Strait tensions") == "geopolitical"


# ───── 优先级测试 ─────

def test_hynix_overrides_stock_when_both_match():
    """标题同时含「海力士」和「股价」时，必须归 hynix（不被 stock 抢走）。"""
    # stock 分支有「股价」关键词；hynix 分支有「海力士」。
    # hynix 分支必须在 stock 之前。
    got = rt._classify_news_kind("海力士股价大涨", "SK Hynix stock price rises")
    assert got == "hynix", "hynix 分支必须早于 stock 返回；实际: %s" % got


def test_geopolitical_overrides_hynix():
    """朝鲜+海力士同现时，地缘（最高优先级）必须先返回。"""
    got = rt._classify_news_kind("朝鲜发射导弹 海力士 撤离", "North Korea missile, SK Hynix evacuates")
    assert got == "geopolitical", "geopolitical 优先 hynix；实际: %s" % got


# ───── 防御测试 ─────

def test_classify_empty_inputs_returns_other():
    assert rt._classify_news_kind("", "") == "other"


def test_classify_no_match_returns_other():
    assert rt._classify_news_kind("咖啡拉花比赛", "Latte art competition") == "other"