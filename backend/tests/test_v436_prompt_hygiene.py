#!/usr/bin/env python3
"""v436 回归：主分析 prompt 卫生（非主分析指令不入、品种规则去重）。

2026-11-17 实测两处浪费：
1. ai_prompts 里 4 条启用条目全是**别的子助手**的 prompt（反思/HOLD 判定/因果抽取/
   经验提炼），却带着「优先级最高，必须遵守」被拼进主分析 system prompt（663 字符噪音）；
2. ai_asset_rules 24 条启用规则只对应 9 个品种 —— DXY/EURUSD/SOL/US10Y/USDJPY
   各重复 4 遍，注入块 1,098 字符里约 700 是同一句话重复。
"""
import os
import sys

import pytest

SRC = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "src_python"))
if SRC not in sys.path:
    sys.path.insert(0, SRC)

import ai_prompts as ap


def test_subagent_prompt_detected():
    for name in ("v186_reflection_prompt", "v186_hold_prompt",
                 "v186_causal_extraction", "v186_insight_extraction"):
        assert ap._is_subagent_prompt(name), name


def test_main_prompt_not_filtered():
    assert not ap._is_subagent_prompt("main_risk_rule")
    assert not ap._is_subagent_prompt("交易时段约束")


def _real_rules_block():
    """pytest 默认指向独立空库 → 没规则就跳过（生产库实测见报告附录 F）。"""
    ap._asset_cache["ts"] = 0.0
    block = ap.load_asset_rules_block(ttl=9999)
    if not block:
        pytest.skip("测试库无启用品种规则（隔离库），生产库实测在报告附录 F")
    return block


def test_asset_rules_deduplicated():
    block = _real_rules_block()
    for code in ("DXY", "EURUSD", "SOL", "US10Y", "USDJPY"):
        head = f"[{code} "
        assert block.count(head) <= 1, f"{code} 标题重复：{block.count(head)} 次"


def test_asset_rules_block_size_reduced():
    block = _real_rules_block()
    assert len(block) < 900, f"去重后应显著缩短，当前 {len(block)} 字符"
