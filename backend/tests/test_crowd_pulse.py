# -*- coding: utf-8 -*-
"""Crowd Pulse 回归测试（v316）。

覆盖：
  P0  解析器：喂**真实抓下来的结构样本**，断言解析结果正确
      —— 重点是「解析失败必须返回空、绝不用占位数据填充」这条底线。
  P1  热度归一：跨源量纲天差地别（同花顺 50 万级 / 百度 0~1 / gnews 0），
      必须归一到可比尺度，否则榜单退化成单源榜。
  P2  权威度：跨平台共现必须比单平台高（同一家平台的两张榜只算一次）。
  P3  落库 → 建榜 → 建画像 端到端（内存库，离线）。
  P4  人群画像 9 个维度都要有真实数据。

全部离线：无网络、无 LLM、不碰生产库（用 :memory:）。
"""
from __future__ import annotations

import json
import os
import sqlite3
import sys

import pytest

SRC = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "src_python"))
if SRC not in sys.path:
    sys.path.insert(0, SRC)

import db
import crowd_pulse as cp
import crowd_sources as cs


# ─────────────────── 真实结构样本（2026-10-30 实抓） ───────────────────

BAIDU_SAMPLE = {
    "success": True,
    "data": {
        "cards": [{
            "component": "tabTextList",
            "content": [{
                "content": [
                    {"isTop": True, "word": "党的二十届五中全会公报一图速览",
                     "url": "https://m.baidu.com/s?word=a"},
                    {"index": 1, "word": "三只松鼠日赚不足1万元",
                     "url": "https://m.baidu.com/s?word=b", "hotTag": "1"},
                    {"index": 2, "word": "证监会：全力稳市场稳预期",
                     "url": "https://m.baidu.com/s?word=c"},
                ]
            }]
        }],
        "currentBoard": {"text": "财经榜", "rule": {"content": [
            {"type": "title", "text": "一、榜单介绍"}]}},
    },
}

THS_SAMPLE = {
    "status_code": 0,
    "data": {"stock_list": [
        {"market": 33, "code": "300502", "name": "新易盛", "rate": "418618.0",
         "rise_and_fall": -3.57, "order": 1,
         "tag": {"concept_tag": ["共封装光学(CPO)", "5G"],
                 "popularity_tag": "持续上榜"},
         "hot_rank_chg": 0},
    ]},
    "status_msg": "success",
}

COINGECKO_SAMPLE = {
    "coins": [{"item": {"id": "hyperliquid", "name": "Hyperliquid", "symbol": "HYPE",
                        "market_cap_rank": 10, "slug": "hyperliquid",
                        "data": {"price_change_percentage_24h": -2.5}}}],
    "rwas": [{"id": "gold", "name": "Gold", "symbol": "XAU", "market_cap_rank": 1,
              "slug": "gold", "data": {"price_change_percentage_24h": -0.9}}],
    "categories": [],
}

GNEWS_SAMPLE = """<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0"><channel>
<item><title>美股三大指数集体低开 谷歌股价重挫逾10% - 东方财富</title>
<link>https://news.google.com/x/1</link>
<pubDate>Fri, 30 Oct 2026 02:54:04 GMT</pubDate>
<source url="https://finance.eastmoney.com">东方财富</source></item>
</channel></rss>"""


# ─────────────────── P0 解析器 ───────────────────

def test_parse_baidu_hot_reads_real_path():
    """百度真实榜单在 data.cards[0].content[0].content（不是 currentBoard.rule）。"""
    rows = cs.parse_baidu_hot(BAIDU_SAMPLE)
    assert len(rows) == 3
    words = [r.title for r in rows]
    assert "证监会：全力稳市场稳预期" in words
    assert "一、榜单介绍" not in words, "榜单说明文字不能当成词条"
    assert rows[0].source == "baidu_finance", "currentBoard.text 含「财经」→ 判为财经榜"
    # 置顶条目（无 index）不能与 index=1 撞号
    ranks = [r.rank_no for r in rows]
    assert ranks.count(0) == 1, "置顶条目应给 rank=0（标记为 top_pinned）"
    assert len(set(r for r in ranks if r > 0)) == len([r for r in ranks if r > 0])


def test_parse_baidu_hot_heat_basis_recorded():
    rows = cs.parse_baidu_hot(BAIDU_SAMPLE)
    by_title = {r.title: r for r in rows}
    pinned = by_title["党的二十届五中全会公报一图速览"]
    assert pinned.meta["heat_basis"] == "top_pinned"
    normal = by_title["证监会：全力稳市场稳预期"]
    assert normal.meta["heat_basis"] == "rank_proxy"
    assert normal.heat == pytest.approx(1.0 / 2)


def test_parse_ths_hot_extracts_concepts():
    rows = cs.parse_ths_hot(THS_SAMPLE)
    assert len(rows) == 1
    r = rows[0]
    assert r.title == "300502 新易盛"
    assert r.meta["market"] == "深A"
    assert r.meta["concepts"] == ["共封装光学(CPO)", "5G"]
    assert r.meta["popularity"] == "持续上榜"
    assert r.rank_no == 1


def test_parse_coingecko_trending_includes_rwas_gold():
    rows = cs.parse_coingecko_trending(COINGECKO_SAMPLE)
    syms = {r.meta["symbol"] for r in rows}
    assert "HYPE" in syms
    assert "XAU" in syms, "rwas 里的黄金必须进来（对应本系统 XAU 资产）"


def test_parse_gnews_extracts_channel_and_date():
    rows = cs.parse_gnews(GNEWS_SAMPLE)
    assert len(rows) == 1
    assert rows[0].author == "东方财富"
    assert rows[0].published_at.startswith("2026-10-30 02:54")


def test_parsers_return_empty_on_garbage():
    """底线：解析不了就返回空，绝不返回占位/编造条目。"""
    assert cs.parse_baidu_hot(None) == []
    assert cs.parse_baidu_hot({}) == []
    assert cs.parse_baidu_hot({"data": {}}) == []
    assert cs.parse_ths_hot(None) == []
    assert cs.parse_ths_hot({"data": {"stock_list": "not-a-list"}}) == []
    assert cs.parse_coingecko_trending(None) == []
    assert cs.parse_gnews("") == []
    assert cs.parse_gnews("<not xml") == []


# ─────────────────── P1 热度归一 ───────────────────

def _mk(source, external_id, heat, rank_no=0, title="x", kind="theme",
        concepts=None, author="", collected_at="2026-10-30T10:00:00",
        market=""):
    return cs.RawItem(
        source=source, external_id=external_id, kind=kind, title=title,
        url="https://example.invalid/%s" % external_id, author=author,
        heat=heat, rank_no=rank_no, collected_at=collected_at,
        meta={"concepts": concepts or [], "market": market})


def test_heat_normalized_across_sources():
    """同花顺 50 万级 / 百度 0~1 / gnews 0 → 归一后同一名次的热度可比。"""
    items = [
        _mk("ths_hot", "ths:1", 500000.0, 1),
        _mk("ths_hot", "ths:2", 100000.0, 2),
        _mk("baidu_hot", "bd:1", 1.0, 1),
        _mk("baidu_hot", "bd:2", 0.5, 2),
        _mk("gnews", "gn:1", 0.0, 0),
        _mk("gnews", "gn:2", 0.0, 0),
    ]
    m = cp._normalize_heat_by_source(items)
    by_ext = {it.external_id: m[id(it)] for it in items}
    assert by_ext["ths:1"] == pytest.approx(by_ext["bd:1"]), "两个源的第 1 名热度应相等"
    assert by_ext["ths:2"] == pytest.approx(by_ext["bd:2"])
    assert by_ext["gn:1"] == pytest.approx(1.0), "无 rank 的源按出现顺序，第 1 名=1.0"
    assert by_ext["gn:2"] == pytest.approx(0.5)


# ─────────────────── P2 权威度 ───────────────────

def test_authority_score_rewards_cross_platform():
    one = cp.authority_score(10.0, 1, 1, 0.0, 0.5)
    two = cp.authority_score(10.0, 2, 1, 0.0, 0.5)
    assert two > one, "跨平台共现必须显著加权（权威度的核心）"


def test_authority_explain_mentions_platforms():
    txt = cp.authority_explain(10.0, 2, 3, 0.25, 0.5, ["baidu", "gnews"])
    assert "跨平台共现 2 个平台" in txt
    assert "baidu" in txt and "gnews" in txt
    assert "动能" in txt and "媒体权重" in txt


def test_platform_mapping_collapses_baidu_boards():
    """百度实时榜与百度财经榜是同一平台 → 交叉验证只能算 1 次。"""
    assert cp.platform_of("baidu_hot") == "baidu"
    assert cp.platform_of("baidu_finance") == "baidu"
    assert cp.platform_of("ths_hot") == "ths"


# ─────────────────── P3 端到端（内存库） ───────────────────

@pytest.fixture
def mem_conn():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = OFF")
    db.migrate(conn)
    yield conn
    conn.close()


def _seed(conn, items):
    return cp.persist_items(conn, items)


def test_end_to_end_rank_and_profile(mem_conn):
    items = [
        _mk("baidu_hot", "bd:a", 1.0, 1, title="证监会：全力稳市场稳预期"),
        _mk("baidu_finance", "bd:a", 0.5, 2, title="证监会：全力稳市场稳预期"),
        _mk("gnews", "gn:a", 0.0, 0, title="证监会：全力稳市场稳预期",
            author="第一财经"),
        _mk("ths_hot", "ths:a", 500000.0, 1, title="300502 新易盛",
            concepts=["共封装光学(CPO)"], kind="asset"),
    ]
    w = _seed(mem_conn, items)
    assert w["items"] == 4

    n = cp.build_monthly_rankings(mem_conn, "2026-10")
    assert n >= 2
    rows = cp.monthly_ranking(mem_conn, month="2026-10")
    top = rows[0]
    assert top["display_name"] == "证监会：全力稳市场稳预期"
    # 百度实时+财经+媒体 = 2 个独立平台（baidu, gnews），不是 3 个源
    assert top["source_count"] == 2
    assert top["explain"], "每条排名必须带可追问的解释"

    segs = cp.build_audience_segments(mem_conn, "2026-10")
    assert segs > 0
    prof = cp.audience_profile(mem_conn, month="2026-10")
    assert prof["dimensions"], "人群画像必须有维度数据"
    for dim, _ in cp.AUDIENCE_DIMENSIONS:
        assert dim in prof["dimensions"], "维度 %s 不该缺失" % dim
    assert cp.build_source_authority(mem_conn, "2026-10") >= 1


def test_heatmap_share_is_not_single_source_monopoly(mem_conn):
    """回归：平台维度的 share 不能退化成「一个源占 100%」。"""
    items = [
        _mk("ths_hot", "t%d" % i, 500000.0 - i, i, title="股票%d" % i,
            concepts=["人形机器人"], kind="asset") for i in range(1, 20)
    ] + [
        _mk("baidu_hot", "b%d" % i, 1.0 / i, i, title="热点%d" % i) for i in range(1, 20)
    ]
    cp.persist_items(mem_conn, items)
    cp.build_audience_segments(mem_conn, "2026-10")
    prof = cp.audience_profile(mem_conn, month="2026-10", dimension="platform")
    rows = prof["dimensions"]["platform"]
    shares = [r["share"] for r in rows]
    assert max(shares) < 0.999, "热度占比被单一源独占 = 归一化失效"


def test_no_fabrication_empty_input(mem_conn):
    """空输入 → 空库，不产生任何条目/榜单/画像。"""
    w = cp.persist_items(mem_conn, [])
    assert w["items"] == 0 and w["daily"] == 0
    assert cp.build_monthly_rankings(mem_conn, "2026-10") == 0
    assert cp.build_audience_segments(mem_conn, "2026-10") == 0
    assert cp.monthly_ranking(mem_conn, month="2026-10") == []


def test_normalize_item_tags():
    it = _mk("ths_hot", "x", 1.0, 1, title="688836 宇树科技",
             concepts=["人形机器人"], kind="asset", market="深A")
    tags = cp.normalize_item(it, heat_pct=0.95)
    assert tags["platform"] == "A股散户"
    assert tags["market"] == "A股", "同花顺 market=深A 必须归一到 A股（否则维度碎裂）"
    assert "科技/AI" in tags["themes"]
    assert tags["influence_tier"] == "头部关注(top10%)"
    assert tags["time_window"] == "早盘(09-11:30)"


def test_source_market_map_collapses_a_share_boards():
    assert cp.SOURCE_MARKET_MAP["深A"] == "A股"
    assert cp.SOURCE_MARKET_MAP["创业板"] == "A股"
    assert cp.SOURCE_MARKET_MAP["科创板"] == "A股"
    assert cp.SOURCE_MARKET_MAP["港股"] == "港股"
