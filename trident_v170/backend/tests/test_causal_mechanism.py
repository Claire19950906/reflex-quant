# -*- coding: utf-8 -*-
"""因果机理三件套回归测试（全离线，不联网、不调 LLM）。

覆盖本轮落地：
  · event_struct.extract_events 事件原子抽取（加息/OPEC 减产、量级解析、方向）
  · db.migrate 后 causal_chains 具备 mechanism/precondition/invalidation 三列
  · causal_chains.add_chain/list_chains 对三字段的持久化往返
  · review_agent.counterfactual_evidence 反共识/因果链段提取
"""
import os
import sqlite3
import sys

SRC = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "src_python"))
if SRC not in sys.path:
    sys.path.insert(0, SRC)

import event_struct as es
import db
import causal_chains as cc
import review_agent as r


def _mem_db():
    conn = sqlite3.connect(":memory:")
    db.migrate(conn)
    return conn


# ---------------------------------------------------------------------------
# ② 事件结构化
# ---------------------------------------------------------------------------

def test_extract_events_rate_hike():
    evs = es.extract_events("美联储宣布加息 25 个基点")
    assert evs, "应命中加息事件"
    e = evs[0]
    assert e["event_type"] == "加息"
    assert e["actor"] == "美联储"
    assert e["magnitude_value"] == 25.0
    assert e["magnitude_unit"] == "bp"
    assert "XAU" in e["target_assets"] and "BTC" in e["target_assets"]


def test_extract_events_opec_cut():
    evs = es.extract_events("OPEC 决定每日减产 50 万桶")
    e = evs[0]
    assert e["event_type"] == "OPEC减产"
    assert e["magnitude_value"] == 50.0
    assert e["magnitude_unit"] == "万桶"
    assert e["target_assets"] == ["WTI"]


def test_extract_events_no_match_returns_empty():
    assert es.extract_events("今天天气晴朗适合出行") == []
    assert es.extract_events("") == []


def test_direction_for_rule_bias():
    assert es.direction_for("加息", "XAU") == "利空"
    assert es.direction_for("OPEC减产", "WTI") == "利多"
    # 原油库存方向取决于增/减，规则不应抢答
    assert es.direction_for("原油库存", "WTI") is None


def test_to_prompt_block_empty_and_filled():
    assert es.to_prompt_block([]) == ""
    block = es.to_prompt_block(es.extract_events("美联储加息25个基点"))
    assert "加息" in block and "25bp" in block


# ---------------------------------------------------------------------------
# ① 因果链库三字段（schema + CRUD 往返）
# ---------------------------------------------------------------------------

def test_causal_chains_schema_has_mechanism_cols():
    conn = _mem_db()
    try:
        cols = {row[1] for row in conn.execute("PRAGMA table_info(causal_chains)")}
        assert {"mechanism", "precondition", "invalidation"} <= cols
    finally:
        conn.close()


def test_add_chain_persists_mechanism_triple():
    conn = _mem_db()
    try:
        res = cc.add_chain(
            "加息", "黄金承压", name="加息链",
            mechanism="持有黄金机会成本上升，资金转向美债",
            precondition="非金融危机环境",
            invalidation="金融危机时避险压倒利率逻辑",
            conn=conn,
        )
        assert res["ok"]
        chains = cc.list_chains(conn)
        assert chains[0]["mechanism"] == "持有黄金机会成本上升，资金转向美债"
        assert chains[0]["precondition"] == "非金融危机环境"
        assert chains[0]["invalidation"] == "金融危机时避险压倒利率逻辑"
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# ③ 反共识段 vs 结算结果（归因证据提取）
# ---------------------------------------------------------------------------

def test_counterfactual_evidence_extracts_stages():
    t = {"reasoning": "[反共识结论] 价格已 price in\n[因果链分析] 因：加息；果：黄金跌"}
    out = r.counterfactual_evidence(t)
    assert "反共识" in out and "因果链" in out


def test_stage_text_missing_or_none():
    assert r._stage_text(None, "反共识结论") == ""
    assert r._stage_text("[驱动力] 资金面", "反共识结论") == ""