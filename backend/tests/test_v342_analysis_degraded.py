# -*- coding: utf-8 -*-
"""v342 — "没分析" ≠ "判断为观望"（classify_analysis_degraded）

现场背景：289/291 条 HOLD 里绝大多数是降级兜底产物，混在一起会：
  · 让胜率/方向准确率被"假 HOLD"拖平
  · 让界面显示"AI 一直在观望"，实际是"AI 根本没跑完"
只做记账分类，不改任何动作与分数。
"""
from __future__ import annotations

import os
import sys

SRC = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "src_python"))
if SRC not in sys.path:
    sys.path.insert(0, SRC)

from engine.ai_worker import classify_analysis_degraded


def test_target_none_is_degraded():
    assert classify_analysis_degraded(
        "[总框架] 目标资产 NONE（market_category=）；该标的**不在**核心监控清单"
    ) == (1, "target_none")


def test_empty_llm_output_is_degraded():
    assert classify_analysis_degraded(
        "[总框架] 目标资产 XAU；本次 **LLM 未返回可用分析**（空输出或解析前被截断）"
    )[0] == 1


def test_server_fallback_is_degraded():
    assert classify_analysis_degraded("[总框架] 服务端回灌的核心驱动是宏观流动性")[0] == 1


def test_real_analysis_not_degraded():
    assert classify_analysis_degraded(
        "[总框架] 目标资产 WTI；性质：未明；事件类型：市场事件"
    ) == (0, None)


def test_source_degraded_reason():
    assert classify_analysis_degraded(
        "[总框架] 目标资产 WTI；[因素定位] x",
        "LLM 源降级：空输出/截断（非无方向判断）",
    )[0] == 1


def test_exception_fallback_is_degraded():
    # 模型抛异常后拼出的兜底文案（历史 F12 脏行：reasoning="…异常降级: RuntimeError"）
    flag, why = classify_analysis_degraded("MiniMax-M3 异常降级: RuntimeError")
    assert flag == 1
    assert why == "llm_empty"


def test_exception_marker_in_no_trade_reason_flags():
    assert classify_analysis_degraded(
        "[总框架] 目标资产 BTC", "MiniMax-M3 异常降级: RuntimeError"
    )[0] == 1
