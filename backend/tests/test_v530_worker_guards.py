# -*- coding: utf-8 -*-
"""worker_helpers 的判定护栏契约测试（全离线）。

这里每一条都对应一次真实事故：
  · 未闭合 `<think>` 被"从标签删到文末"→ 1455 字连 JSON 一起削成 0 字
  · `child_count` 被写成 'OIL|WTI|BUY' → int() 抛错炸掉整批 16 条落库
  · completeness 扣到 0 后再标缺失字段，`or 1.0` 把它重置回 1.0 重新起扣
"""
from __future__ import annotations

import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src_python"))

from engine import worker_helpers as wh  # noqa: E402


# ── think 块 ──────────────────────────────────────────────────────────────

def test_unclosed_think_keeps_json_after_tag():
    raw = "<think>这里是一大段推理，没有闭合标签…" + '{"target_asset": "XAU", "action": "BUY"}'
    out = wh.strip_think_blocks(raw)
    assert out.startswith('{"target_asset"')       # JSON 必须留下来
    assert "推理" not in out


def test_closed_think_block_is_removed_entirely():
    raw = "<think>推理</think>  {\"action\": \"SELL\"}"
    assert wh.strip_think_blocks(raw) == '{"action": "SELL"}'
    assert wh.strip_think_blocks("") == ""


def test_has_unclosed_think_only_when_open_without_close():
    assert wh.has_unclosed_think("<think>abc") is True
    assert wh.has_unclosed_think("<think>a</think>") is False
    assert wh.has_unclosed_think("plain") is False


def test_is_empty_llm_output_ignores_think_and_code_fence():
    assert wh.is_empty_llm_output(None) is True
    assert wh.is_empty_llm_output("   ") is True
    assert wh.is_empty_llm_output("<think>只有思维链</think>") is True
    assert wh.is_empty_llm_output("```json\n{\"a\":1}\n```" + "x" * 50) is False


# ── 脏计数 ────────────────────────────────────────────────────────────────

def test_safe_child_count_never_raises_on_dirty_values():
    assert wh._safe_child_count("OIL|WTI|BUY") == 0
    assert wh._safe_child_count(None) == 0
    assert wh._safe_child_count("3") == 3
    assert wh._safe_child_count(2.9) == 2


# ── 分数与方向 ────────────────────────────────────────────────────────────

def test_reconcile_score_action_hold_is_never_promoted():
    assert wh.reconcile_score_action(0.9, "HOLD") == (0.0, "HOLD")
    assert wh.reconcile_score_action(-0.9, "hold") == (0.0, "HOLD")
    assert wh.reconcile_score_action(0.9, "WEIRD") == (0.0, "HOLD")


def test_reconcile_score_action_flips_sign_to_match_direction():
    score, action = wh.reconcile_score_action(-0.6, "BUY")
    assert action == "BUY" and score > 0
    score, action = wh.reconcile_score_action(0.6, "SELL")
    assert action == "SELL" and score < 0
    # 接近 0 时给同向弱分，便于聚合而不是消失
    assert wh.reconcile_score_action(0.0, "BUY")[0] > 0
    assert wh.reconcile_score_action(0.0, "SELL")[0] < 0
    # 越界分数先夹到 ±1
    assert wh.reconcile_score_action(5.0, "BUY")[0] == 1.0


def test_direction_consistent_rules():
    assert wh.direction_consistent("HOLD", 0.5) is True
    assert wh.direction_consistent("BUY", 0.3) is True
    assert wh.direction_consistent("BUY", -0.3) is False
    assert wh.direction_consistent("SELL", -0.3) is True
    assert wh.direction_consistent("OTHER", 0.0) is False


# ── field_sources / completeness ──────────────────────────────────────────

def test_mark_field_source_writes_json_and_count_sources():
    result = {}
    wh._mark_field_source(result, "reasoning", "llm")
    wh._mark_field_source(result, "action", "rule_v231")
    wh._mark_field_source(result, "asset_nature", "business_knowledge_v2")
    wh._mark_field_source(result, "risk", "missing")
    src = json.loads(result["field_sources"])
    assert src["reasoning"] == "llm" and src["action"] == "rule_v231"
    counts = wh._count_sources(src)
    assert counts["llm"] == 1 and counts["rule"] == 1
    assert counts["business_kb"] == 1 and counts["missing"] == 1


def test_mark_field_missing_does_not_reset_zero_completeness():
    result = {"completeness_score": 0.0}
    wh._mark_field_missing(result, "reasoning")
    assert result["completeness_score"] == 0.0     # 0 是合法值，不许被 or 1.0 重置
    wh._mark_field_missing(result, "reasoning")     # 重复标不重复记
    assert json.loads(result["missing_fields"]) == ["reasoning"]
    fresh = {}
    wh._mark_field_missing(fresh, "reasoning")
    assert fresh["completeness_score"] < 1.0


# ── 占位名 / 骨感输出 ─────────────────────────────────────────────────────

def test_looks_placeholder_rejects_template_noise_and_directions():
    assert wh._looks_placeholder("") is True
    for bad in ("N/A", "NA", "BUY", "SELL", "因素名", "待定", "未填", "服务端兜底"):
        assert wh._looks_placeholder(bad) is True, bad
    assert wh._looks_placeholder("美联储缩表预期") is False


def test_is_skeleton_result_detects_empty_body_json():
    assert wh.is_skeleton_result(None) is True
    skeleton = {"target_asset": "XAU", "suggested_action": "HOLD", "reasoning": "无"}
    assert wh.is_skeleton_result(skeleton) is True
    full = {"reasoning": "实际利率下行叠加避险买盘，" * 3,
            "reasoning_path": "通胀回落 → 降息预期 → 金价上行",
            "asset_nature": "黄金对实际利率高度敏感",
            "asset_reasoning": "持仓成本下降推动配置需求"}
    assert wh.is_skeleton_result(full) is False
