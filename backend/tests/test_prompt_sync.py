"""阶段4 防回归测试：双轨主 prompt（_SYSTEM_PROMPT / _JSON_PROMPT_FORCE）一致性。

背景：系统有两套主 prompt：
  - _SYSTEM_PROMPT    —— JSON 模式通道（aiping.cn / MiniMax 备用 full_prompt=True）
  - _JSON_PROMPT_FORCE —— 非 JSON 模式通道（OpenRouter / nassaapi / xinshu 等主通道）

阶段3 的④⑤步改动（[水位博弈]持仓拥挤度 / [叙事逻辑]黄金资金坐实）此前只进了
_SYSTEM_PROMPT，导致非 JSON 通道不生效。v170 改造后双通道已废弃（_JSON_PROMPT_FORCE
=_SYSTEM_PROMPT 复用同一份），本测试改为断言关键标记在统一 prompt 中**存在**（≥1），
防止指引被误删；字段数文案无旧残留（17 个字段 / 14 字段）。
不 import ai_worker（避免其重依赖与模块级副作用），只做源码级字符串断言。
"""
import os
import pathlib
import sys

sys.path.insert(0, os.path.dirname(__file__))
import _prompt_src

SRC = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "src_python"))
AI_WORKER = os.path.join(SRC, "engine", "ai_worker.py")


def _src() -> str:
    return _prompt_src.read_prompt_source()


def test_both_prompts_carry_stage3_crowding_guidance():
    src = _src()
    # [水位博弈] 持仓拥挤度指引：统一 prompt 中必须存在（v170 双通道已废弃，≥1 即可）
    assert src.count("持仓拥挤度") >= 1


def test_both_prompts_carry_stage3_funding_confirmation():
    src = _src()
    # [叙事逻辑] 黄金资金坐实核实：统一 prompt 中必须存在
    # v310: v3xx 把 prompt 的 [叙事逻辑] 段重写成 [驱动力]/[水位博弈]/[跨资产资金],
    # 原句 "黄金主线须核实资金" 随之消失; 同一要求的现表述是"盘面/资金证据"印证。
    # 两个表述任一存在即算通过 (目的是防止这段指引被整段删掉)。
    assert (src.count("黄金主线须核实资金") + src.count("盘面/资金证据")) >= 1


def test_no_stale_field_count_text():
    src = _src()
    assert "17 个字段" not in src
    assert "14 字段" not in src


def test_field_count_is_20():
    src = _src()
    # 统一主 prompt 结尾是 20 个字段（v170 双通道已废弃）
    assert src.count("20 个字段") >= 1