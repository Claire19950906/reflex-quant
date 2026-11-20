# -*- coding: utf-8 -*-
"""v342 — 门卫规则兜底（rule_gate_fallback）

现场背景：MINIMAX_GATE_KEY 与主分析 key 实测是同一个，gate 与 main 抢同一份配额；
配额打满时 gate 全池不可用，新闻只能冻结等回放（实测断流数小时）。
规则兜底只放行"高权重发言 + 关键宏观主题"，其余继续冻结。
"""
from __future__ import annotations

import os
import sys

SRC = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "src_python"))
if SRC not in sys.path:
    sys.path.insert(0, SRC)

from realtime_filter import rule_gate_fallback


def test_authority_plus_topic_passes():
    r = rule_gate_fallback("美联储主席鲍威尔：通胀仍然偏高，不排除再加息")
    assert r["pass"] is True
    assert r["reason"] == "authority+topic"


def test_analyst_opinion_does_not_pass():
    r = rule_gate_fallback("某分析师观点：看好黄金反弹")
    assert r["pass"] is False


def test_opec_supply_news_passes():
    r = rule_gate_fallback("OPEC 宣布减产，原油库存下降")
    assert r["pass"] is True


def test_company_pr_does_not_pass():
    r = rule_gate_fallback("某科技公司发布新品")
    assert r["pass"] is False


def test_two_assets_two_topics_pass_without_authority():
    r = rule_gate_fallback("黄金与原油齐涨：避险 + 减产双重驱动")
    assert r["pass"] is True
    assert r["reason"] == "asset+topic>=2"
