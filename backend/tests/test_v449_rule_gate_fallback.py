# -*- coding: utf-8 -*-
"""v449 —— 门卫降级为"规则为主 + LLM 抽样复核"的回归用例。

背景（2026-11-18 实测）：门卫 LLM `http_error/net` 占 50%，116/132 条待分析
卡在 `gate_verdict='未判定'`，随后按 gate_down 归档 → 一整天几乎不出决策。
现在 LLM 不可用时由规则给出真判决（可交易 / 无信号），不再一律"未判定"。
"""
from __future__ import annotations

import os
import sys

import pytest

SRC = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "src_python"))
if SRC not in sys.path:
    sys.path.insert(0, SRC)

import realtime_filter as rf


@pytest.mark.parametrize("title", [
    "美联储宣布降息 25 个基点",
    "SEC 批准比特币现货 ETF 上市",
    "比特币突破 12 万美元，创历史新高",
    "OPEC+ 宣布原油减产 200 万桶/日",
])
def test_rule_verdict_tradeable(title):
    rv = rf.rule_verdict(title, "")
    # v450（2026-11-19）收紧：规则命中只代表「值得进主分析」，判定如实写「未判定」，
    # 不再伪造「可交易」（否则门卫抖动期 648 条"可交易"只有 6 条真过门）。
    # 但强事件必须仍然被判为可放行（未判定/可交易都算），绝不能落到「无信号」。
    assert rv["verdict"] in ("未判定", "可交易"), (title, rv)
    assert rv["verdict"] != "无信号", (title, rv)
    assert rv["reason"].startswith("rule:"), (title, rv)


@pytest.mark.parametrize("title", [
    "某项目 AMA 活动预告",
    "今天天气不错，适合出门",
    "招聘：诚聘资深后端工程师",
])
def test_rule_verdict_no_signal(title):
    rv = rf.rule_verdict(title, "")
    assert rv["verdict"] == "无信号", (title, rv)


def test_rule_verdict_shape():
    rv = rf.rule_verdict("美联储宣布降息", "")
    assert set(rv) >= {"verdict", "reason", "rule"}
    assert rv["reason"].startswith("rule:")


def test_rule_verdict_asset_hint_macro_to_pool():
    """宏观/地缘新闻必须落到池内资产，否则主分析一律 routing_miss。"""
    cases = {
        "乌克兰军方称袭击了俄罗斯的炼油厂": "WTI",
        "美联储沃勒：通胀仍高于目标": "XAU",
        "比特币突破 12 万美元": "BTC",
    }
    for title, asset in cases.items():
        rv = rf.rule_verdict(title, "")
        # v450：判定标签不再伪造「可交易」，但资产指向必须仍然落到池内。
        assert rv["verdict"] in ("未判定", "可交易"), (title, rv)
        assert rv.get("main_asset") == asset, (title, rv)
        assert asset in (rv.get("assets") or []), (title, rv)
