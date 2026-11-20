# -*- coding: utf-8 -*-
"""v342 — 骨架输出判定（is_skeleton_result）

现场证据（2026-11-16 08:29 批次）：
    同一条新闻的 3 行（主品种 + 2 副品种）全部"LLM 未返回可用分析"，
    对应调用 tokens_out=33 —— 模型交了合法 JSON，但没有正文。
    `is_empty_llm_output` 只判原文长度，判不出来，于是重试从不触发。
"""
from __future__ import annotations

import os
import sys

SRC = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "src_python"))
if SRC not in sys.path:
    sys.path.insert(0, sys.path and SRC)

from engine.ai_worker import is_empty_llm_output, is_skeleton_result


def test_skeleton_target_only_is_detected():
    res = {"target_asset": "XAU", "suggested_action": "HOLD", "sentiment_score": 0.3}
    assert is_skeleton_result(res) is True
    # 原文并不空 —— 这正是漏洞：短 JSON 能过 is_empty_llm_output
    assert is_empty_llm_output('{"target_asset":"XAU","suggested_action":"HOLD"}') is False


def test_empty_string_fields_are_skeleton():
    res = {"target_asset": "WTI", "asset_nature": "", "reasoning": "",
           "reasoning_path": "", "asset_reasoning": ""}
    assert is_skeleton_result(res) is True


def test_real_hold_is_not_skeleton():
    res = {
        "target_asset": "WTI",
        "reasoning": "俄炼厂遇袭属供给端事件，但出口未断，属短期脉冲，观望等库存验证。",
        "asset_nature": "原油 供给端敏感",
        "reasoning_path": "[总框架] ... [总结] 观望",
        "asset_reasoning": "WTI(原油)：供给冲击但未落地",
    }
    assert is_skeleton_result(res) is False


def test_none_result_is_skeleton():
    assert is_skeleton_result(None) is True
    assert is_skeleton_result({}) is True
