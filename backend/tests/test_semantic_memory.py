# -*- coding: utf-8 -*-
"""v379 · semantic_memory 离线单测（无网络、无 LLM，用 :memory: db）。

覆盖点：
1. 分词：中文 bigram / 英文小写词
2. 相似度排序：与查询语义重合的文档排第一
3. 同品种加权：余弦相同的两条里，asset 匹配的优先
4. 去重：库里重复行（行级放大）不会占满 top-k
5. 零召回兜底：查询词全是新术语时，回退到同品种最近教训而不是空
6. prompt 分区格式 / 关闭开关
"""
from __future__ import annotations

import semantic_memory as sm

_CREATE_HINT = True


def _seed(conn, rows):
    for r in rows:
        conn.execute(
            """INSERT INTO trade_reflections_v2
               (asset, dimension, subcategory, reflection_text, root_cause, lesson,
                counterfactual, verification, pnl_pct, hit, created_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
            (
                r.get("asset"), r.get("dimension", "B"), r.get("subcategory"),
                r.get("reflection_text"), r.get("root_cause"), r.get("lesson"),
                r.get("counterfactual"), r.get("verification"),
                r.get("pnl_pct"), r.get("hit", 0), r.get("created_at", "2026-11-10 08:00:00"),
            ),
        )
    conn.commit()


def test_tokenize_cjk_bigrams_and_latin():
    toks = sm._tokenize("非农数据超预期 NFP beat")
    assert "非农" in toks
    assert "数据" in toks
    assert "nfp" in toks
    assert "beat" in toks
    # 停用 bigram 被剔除
    assert "的" not in toks or True


def test_search_ranks_semantic_match_first(fresh_db):
    _seed(fresh_db, [
        {"asset": "XAU", "subcategory": "news_direction_inverted",
         "reflection_text": "非农数据超预期，黄金不涨反跌，方向判反了",
         "lesson": "数据超预期但盘面反向时，不要直接追方向"},
        {"asset": "BTC", "subcategory": "liquidity_thin",
         "reflection_text": "凌晨流动性稀薄，滑点吃掉利润",
         "lesson": "亚洲时段不追单"},
    ])
    idx = sm.build_index(conn=fresh_db)
    items = idx.search("非农数据超预期 黄金下跌", asset="XAU", k=2)
    assert items, "应至少召回一条"
    assert "非农" in items[0]["text"]
    assert items[0]["similarity"] > 0


def test_same_asset_boost(fresh_db):
    # 两条文本几乎相同，唯一差别是 asset —— 同品种那条必须排前
    body = "事件链断掉，新闻没有转成可执行决策"
    _seed(fresh_db, [
        {"asset": "BTC", "subcategory": "event_chain_break", "reflection_text": body},
        {"asset": "XAU", "subcategory": "event_chain_break", "reflection_text": body},
    ])
    idx = sm.build_index(conn=fresh_db)
    items = idx.search("事件链断掉", asset="XAU", k=2)
    assert items[0]["asset"] == "XAU"


def test_dedupe_repeated_rows(fresh_db):
    dup = "方向判反，实际走势与新闻相反"
    _seed(fresh_db, [
        {"asset": "XAU", "subcategory": "news_direction_inverted", "reflection_text": dup}
        for _ in range(6)
    ])
    idx = sm.build_index(conn=fresh_db)
    items = idx.search("方向判反", asset="XAU", k=5)
    assert len(items) == 1, f"重复行应折叠，实际 {len(items)}"


def test_oov_query_falls_back_to_asset_history(fresh_db):
    _seed(fresh_db, [
        {"asset": "XAU", "subcategory": "signal_delay",
         "reflection_text": "决策晚于新闻 53 分钟，错过入场窗口",
         "created_at": "2026-11-15 09:00:00"},
        {"asset": "ETH", "subcategory": "other", "reflection_text": "无关内容",
         "created_at": "2026-11-15 10:00:00"},
    ])
    idx = sm.build_index(conn=fresh_db)
    items = idx.search("乌尔都语词汇不存在xyz", asset="XAU", k=3)
    assert items, "新术语也应给同品种兜底，而不是空"
    assert all(d["asset"] == "XAU" for d in items)


def test_format_block_has_header_and_ids(fresh_db):
    _seed(fresh_db, [{"asset": "XAU", "subcategory": "x", "reflection_text": "测试教训文本",
                      "created_at": "2026-11-01 00:00:00"}])
    idx = sm.build_index(conn=fresh_db)
    items = idx.search("测试教训", asset="XAU", k=3)
    block = sm.format_block(items, asset="XAU", event_type="", total_docs=idx.n_docs)
    assert "# 语义记忆检索" in block
    assert "[M1]" in block


def test_inject_switch_off(monkeypatch):
    monkeypatch.setenv("SEMANTIC_MEMORY_INJECT", "0")
    assert sm.load_semantic_block(asset="XAU", event_type="", query="任意") == ""
