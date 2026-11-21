# -*- coding: utf-8 -*-
"""review_agent 的归因层/子层/窗口/参数校验契约测试（全离线）。

反思报告的"层"和"子层"是给人看的分类：分错层，人就去修错地方。
两条最容易被改坏的规则：
  · 未知 layer 的权重必须落到最低（999），不能因为 dict 少一个 key 就变成最高
  · LLM 返回的 improvement_suggestion 必须**白名单 + 夹紧**后才落库；
    一个没在白名单里的参数名进了配置，等于让模型直接改生产行为
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src_python"))

from review import review_agent as ra  # noqa: E402


# ── 可观测性 / 分层 ───────────────────────────────────────────────────────

def test_reason_observability_unknown_defaults_to_llm():
    assert ra.reason_observability(25) == "auto"
    assert ra.reason_observability(5) == "hidden"
    assert ra.reason_observability(22) == "hidden"
    assert ra.reason_observability(999) == "llm"     # 未知不硬判


def test_layer_weight_unknown_never_outranks_known_layers():
    assert ra.layer_weight("event_logic") == 1
    assert ra.layer_weight("data") == 2
    assert ra.layer_weight("gate") == 8
    assert ra.layer_weight("no_such_layer") == 999
    assert ra.layer_weight("") == 999


def test_layer_name_and_ordering():
    assert ra.layer_name("timing") == "时机层"
    assert ra.layer_name("custom") == "custom"
    assert ra.layer_name("") == "—"
    ordered = ra.list_layers_ordered()
    assert ordered[0] == ("event_logic", "事件逻辑链层")
    weights = [ra.layer_weight(k) for k, _ in ordered]
    assert weights == sorted(weights)


def test_reason_layer_maps_known_codes_and_defaults_to_judgment():
    assert ra.reason_layer(1) == "data"
    assert ra.reason_layer(2) == "timing"
    assert ra.reason_layer(15) == "execution"
    assert ra.reason_layer(9999) == "judgment"


# ── 子层校验回退 ──────────────────────────────────────────────────────────

def test_parse_sublayer_falls_back_and_clears_l2_on_l1_fallback():
    codes = sorted(ra._SUBLAYER_TWO_LEVEL.keys())
    assert codes, "子层枚举不能为空"
    code = codes[0]
    layers = ra._get_sublayer_levels(code)
    l1, l2s = layers[0]
    # 合法 l1 + 合法 l2 → 原样保留
    assert ra.parse_sublayer(code, f"{l1}:{l2s[0][0]}") == (l1, l2s[0][0])
    # l1 非法 → 回退第一个 l1，且 l2 清空（不许留半截误导）
    assert ra.parse_sublayer(code, f"garbage:whatever") == (l1, "")
    # 只给合法 l1 → 取该 l1 下第一个 l2
    assert ra.parse_sublayer(code, l1) == (l1, l2s[0][0])
    # 没有该 code 的子层枚举 → 空
    assert ra.parse_sublayer(0, "x") == ("", "")


def test_format_sublayer_and_gate_variants():
    assert ra.format_sublayer(1, "a", "b") == "a:b"
    assert ra.format_sublayer(1, "a") == "a"
    assert ra.format_sublayer(1, "") == ""
    assert ra.parse_gate_sublayer("gate_miss:focus_no_signal_but_real") == (
        "gate_miss", "focus_no_signal_but_real")
    assert ra.parse_gate_sublayer("nonsense") == ("gate_miss", "")
    assert ra.parse_gate_sublayer("nonsense:whatever") == ("gate_miss", "")


# ── improvement_suggestion 的落库前校验 ──────────────────────────────────

def test_improvement_suggestion_rejects_non_dict_and_unknown_param():
    assert ra._validate_improvement_suggestion(None)["primary_param"] == {}
    out = ra._validate_improvement_suggestion({"primary_param": {"key": "not_a_real_key",
                                                                 "direction": "up"}})
    assert out["primary_param"] == {}          # 白名单外一律丢弃


def test_improvement_suggestion_clamps_and_truncates():
    key = ra.v182_param_keys()[0]
    out = ra._validate_improvement_suggestion({
        "primary_param": {"key": key, "direction": "UP", "magnitude": 9.0,
                          "confidence": -1, "rationale": "x" * 400},
    })
    assert out["primary_param"]["direction"] == "up"
    assert out["primary_param"]["magnitude"] == 0.5        # 夹到上限
    assert out["primary_param"]["confidence"] == 0.0       # 夹到下限
    assert len(out["primary_param"]["rationale"]) == 200


def test_improvement_suggestion_keeps_at_most_two_secondary_params():
    key = ra.v182_param_keys()[0]
    sec = [{"key": key, "direction": "down", "magnitude": 0.1, "confidence": 0.5}] * 5
    out = ra._validate_improvement_suggestion({"secondary_params": sec})
    assert len(out["secondary_params"]) == 2
    bad = ra._validate_improvement_suggestion({"affected_dimension": "made_up"})
    assert bad["affected_dimension"] == ""


# ── 窗口 WHERE 口径 ───────────────────────────────────────────────────────

def test_window_where_builds_balanced_clauses():
    assert ra._window_where_for_decisions(None, None) == ""
    only_lo = ra._window_where_for_decisions(100, None)
    assert only_lo.startswith("AND ") and only_lo.count("datetime(") == 2
    both = ra._window_where_for_decisions(100, 200)
    assert both.count("datetime(?") == 2 and both.count(" AND ") == 1
    # quick_sim 用 qt.created_at，不许和 decisions 混用
    assert "qt.created_at" in ra._window_where_for_quick_sim(100, 200)


def test_resolve_window_clause_prefers_explicit_window_over_scope():
    where, params = ra._resolve_window_clause("today", 100, 200, "decisions")
    assert where.count("datetime(?") == 2 and params == (100, 200)
    where_scope, params_scope = ra._resolve_window_clause("today", None, None, "decisions")
    assert where_scope == ra._SCOPE_WHERE["today"] and params_scope == ()
    # 只给 hi 时参数里只有 hi，顺序不能错
    where_hi, params_hi = ra._resolve_window_clause("all", None, 200, "decisions")
    assert params_hi == (200,) and "<= datetime" in where_hi
