#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""v501：归因 → 可执行结论的行为测试（attribution_actionable 从 0% 起步）。

这块是"AI 到底说了什么有用的话"的分水岭：
97% 的归因写着"观望"，看上去像 AI 什么也没说，
但驱动里其实已经写清楚了 —— 这里的规则决定哪些能当"现在怎么办"用。
规则一旦放宽，面板就会开始替 AI 编动作，所以每条边界都要钉住。
"""
import json
import sqlite3
import sys
from pathlib import Path

import pytest

BACKEND = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND / "src_python"))

import attribution_actionable as aa


# ── 结论本身就带动作 ───────────────────────────────────────────
@pytest.mark.parametrize("text", ["顺势做多 BTC", "减仓 30%", "跌破 MA20 止损离场", "逢低布局"])
def test_concrete_conclusion_is_actionable(text):
    r = aa.derive_action(text, "[]")
    assert r["is_actionable"] == 1
    assert r["action_source"] == "ai_conclusion"
    assert r["action_text"] == text[:160]


def test_pure_wait_is_not_actionable_without_drivers():
    r = aa.derive_action("观望", "[]")
    assert r["is_actionable"] == 0
    assert r["action_source"] == "insufficient_evidence"


def test_wait_with_conditional_action_still_counts():
    # "观望突破后顺势" 带条件动作，属于可执行，不是纯观望
    r = aa.derive_action("观望突破后顺势", "[]")
    assert r["is_actionable"] == 1
    assert r["action_source"] == "ai_conclusion"


# ── 从驱动里推：只认非推测的 medium/high ───────────────────────
def test_low_confidence_drivers_do_not_generate_actions():
    drivers = [{"cause": "量能萎缩", "confidence": "low"}]
    r = aa.derive_action("观望", json.dumps(drivers))
    assert r["is_actionable"] == 0
    assert r["action_source"] == "insufficient_evidence"


def test_speculative_driver_is_ignored_even_if_high():
    drivers = [{"cause": "可能有大户在出货", "confidence": "high", "speculative": True}]
    r = aa.derive_action("观望", drivers)
    assert r["is_actionable"] == 0


def test_solid_driver_becomes_derived_action():
    drivers = [{"cause": "量能Z异常为负并伴随MA5/MA20死叉, 表明缩量下跌", "confidence": "high"}]
    r = aa.derive_action("观望", drivers)
    assert r["is_actionable"] == 1
    assert r["action_source"] == "derived_from_drivers"
    assert "触发条件" in r["action_text"]
    assert "顺势偏空" in r["action_text"]


def test_bullish_driver_direction():
    r = aa.derive_action("观望", [{"cause": "买盘净买入放量突破前高", "confidence": "medium"}])
    assert "顺势偏多" in r["action_text"]


def test_neutral_driver_asks_for_confirmation():
    r = aa.derive_action("观望", [{"cause": "成交量温和变化", "confidence": "medium"}])
    assert "确认后跟随" in r["action_text"]


def test_highest_confidence_driver_wins():
    drivers = [
        {"cause": "缩量下跌承压", "confidence": "medium"},
        {"cause": "买盘净买入拉升突破", "confidence": "high"},
    ]
    r = aa.derive_action("观望", drivers)
    assert "顺势偏多" in r["action_text"]


def test_action_text_is_truncated_to_160():
    long_cause = "买盘" + "净买入" * 100
    r = aa.derive_action("观望", [{"cause": long_cause, "confidence": "high"}])
    assert len(r["action_text"]) <= 160


# ── 脏输入不能炸 ───────────────────────────────────────────────
@pytest.mark.parametrize("raw", ["", None, "not json", "{}", '{"a":1}', 123])
def test_bad_drivers_input_is_tolerated(raw):
    r = aa.derive_action("观望", raw)
    assert r["is_actionable"] == 0


def test_driver_without_cause_falls_back_to_neutral():
    r = aa.derive_action("观望", [{"confidence": "high"}])
    assert r["is_actionable"] == 1
    assert "确认后跟随" in r["action_text"]


def test_confidence_rank_mapping():
    assert aa._confidence_rank("high") == 3
    assert aa._confidence_rank("高") == 3
    assert aa._confidence_rank("medium") == 2
    assert aa._confidence_rank("低") == 1
    assert aa._confidence_rank(None) == 0


# ── 落库：幂等 + 统计口径 ─────────────────────────────────────
@pytest.fixture()
def conn():
    c = sqlite3.connect(":memory:")
    c.executescript("""
        CREATE TABLE abnormal_attributions(
          id INTEGER PRIMARY KEY, actionable TEXT, drivers_json TEXT, severity_hint TEXT);
    """)
    yield c
    c.close()


def test_ensure_columns_adds_three_columns(conn):
    aa.ensure_columns(conn)
    cols = {r[1] for r in conn.execute("PRAGMA table_info(abnormal_attributions)")}
    assert {"is_actionable", "action_text", "action_source"} <= cols


def test_ensure_columns_is_idempotent(conn):
    aa.ensure_columns(conn)
    aa.ensure_columns(conn)   # 第二次不应抛"列已存在"
    assert True


def test_backfill_counts_and_is_idempotent(conn):
    aa.ensure_columns(conn)
    conn.execute("""INSERT INTO abnormal_attributions(id, actionable, drivers_json, severity_hint)
                    VALUES(1,'减仓 20%',NULL,NULL)""")
    conn.execute("""INSERT INTO abnormal_attributions(id, actionable, drivers_json, severity_hint)
                    VALUES(2,'观望','[{"cause":"缩量下跌承压","confidence":"high"}]',NULL)""")
    conn.execute("""INSERT INTO abnormal_attributions(id, actionable, drivers_json, severity_hint)
                    VALUES(3,'观望','[]',NULL)""")
    conn.commit()
    r1 = aa.backfill(conn)
    assert r1["rows"] == 3 and r1["processed"] == 3
    assert r1["actionable"] == 2                       # 1 是原话动作，2 是推导动作
    assert r1["by_source"]["ai_conclusion"] == 1
    assert r1["by_source"]["derived_from_drivers"] == 1
    assert r1["by_source"]["insufficient_evidence"] == 1
    r2 = aa.backfill(conn)                              # 幂等：结果不漂
    assert r2["actionable"] == 2
    assert r2["by_source"] == r1["by_source"]
