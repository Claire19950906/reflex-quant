# -*- coding: utf-8 -*-
"""v381 · nlq 离线单测：意图识别 / 参数抽取 / 白名单查询 / 空库降级。

原则：全离线、:memory: db、不调 LLM（NLQ_USE_LLM 默认 0）。
"""
from __future__ import annotations

import nlq


def test_parse_performance_with_asset_and_days():
    p = nlq.parse_question("黄金最近 30 天表现怎么样")
    assert p["intent"] == "performance"
    assert p["asset"] == "XAU"
    assert p["days"] == 30


def test_parse_errors_intent():
    p = nlq.parse_question("系统最常犯哪些错")
    assert p["intent"] == "errors"


def test_parse_missed_intent():
    p = nlq.parse_question("漏检了多少信号")
    assert p["intent"] == "missed"


def test_parse_moves_intent_keeps_why_question():
    p = nlq.parse_question("比特币最近为什么跌")
    assert p["intent"] == "moves"
    assert p["asset"] == "BTC"


def test_parse_asset_only_defaults_to_performance():
    p = nlq.parse_question("以太坊怎么样")
    assert p["asset"] == "ETH"
    assert p["intent"] == "performance"


def test_parse_unknown():
    p = nlq.parse_question("今天天气如何")
    assert p["intent"] == "unknown"


def test_ask_unknown_gives_guidance(fresh_db):
    res = nlq.ask("今天天气如何", conn=fresh_db)
    assert res["ok"] is True
    assert res["intent"] == "unknown"
    assert "可以这样问" in res["answer"]


def test_ask_performance_empty_db(fresh_db):
    res = nlq.ask("最近 7 天命中率怎么样", conn=fresh_db)
    assert res["ok"] is True
    assert res["n"] == 0
    assert "没有已结算" in res["answer"]


def test_ask_performance_with_sample(fresh_db):
    fresh_db.execute(
        "INSERT INTO raw_news (id, source, content, timestamp) VALUES (1,'test','seed',datetime('now'))"
    )
    for pnl in (1.5, -2.0, 0.5):
        fresh_db.execute(
            """INSERT INTO ai_decisions (news_id, target_asset, suggested_action, settled,
                                          sentiment_score, reasoning, forward_pnl, created_at)
               VALUES (1,'XAU','BUY',1,0.0,'test',?,datetime('now'))""",
            (pnl,),
        )
    fresh_db.commit()
    res = nlq.ask("黄金最近 7 天命中率", conn=fresh_db)
    assert res["ok"] is True
    assert res["n"] == 3
    assert res["winrate"] == 66.7
    assert "3 笔" in res["answer"]


def test_ask_errors_empty(fresh_db):
    res = nlq.ask("最近都踩了什么坑", conn=fresh_db)
    assert res["ok"] is True
    assert res["intent"] == "errors"


def test_parse_decision_intent_and_id():
    p = nlq.parse_question("11555 这笔为什么亏")
    assert p["intent"] == "decision"
    assert p["decision_id"] == 11555


def test_ask_decision_without_id_gives_hint(fresh_db):
    res = nlq.ask("这笔为什么亏", conn=fresh_db)
    assert res["ok"] is True
    assert res["intent"] == "decision"
    assert "编号" in res["answer"]


def test_ask_decision_replay_missing(fresh_db):
    res = nlq.ask("#99999 这笔为什么亏", conn=fresh_db)
    assert res["ok"] is True
    assert "无法重放" in res["answer"]
