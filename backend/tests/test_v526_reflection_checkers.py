# -*- coding: utf-8 -*-
"""反思校验器的纯函数契约测试（全离线）。

反思层把"这条决策哪里错了"写成错误码给人看。错误码分错，整份周报的结论就跑偏。
两条最容易出事的规则在这里锁死：
  · 缺硬数据必须拆出**可修/不可修**（本地无源 vs 断档 vs 符号缺失 vs 休市）
    —— 全塞进一个 E001，人就永远不知道该去修什么
  · 方向解析必须容忍历史脏格式（JSON / Python repr / 被截断的 repr）
    —— 否则"有因子"的方向单会被判成"没有因子方向"
"""
from __future__ import annotations

import os
import sys
from datetime import datetime, timedelta

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src_python"))

from review import reflection_checkers as rc  # noqa: E402


def test_compute_weight_is_severity_plus_layer():
    assert rc._compute_weight(3, "data") == 30 + rc._LAYER_WEIGHT["data"]
    assert rc._compute_weight(0, "no_such_layer") == rc._DEFAULT_LAYER_WEIGHT


def test_safe_json_loads_returns_default_instead_of_raising():
    assert rc._safe_json_loads(None, {"d": 1}) == {"d": 1}
    assert rc._safe_json_loads("", []) == []
    assert rc._safe_json_loads("{bad", "fallback") == "fallback"
    assert rc._safe_json_loads('{"a": 1}', None) == {"a": 1}


def test_parse_factor_ranking_accepts_json_repr_and_truncated_history():
    assert rc._parse_factor_ranking('[{"factor": "ust2y", "direction": "up"}]') == [
        {"factor": "ust2y", "direction": "up"}]
    # 历史行是 str(list) 落的（单引号，不是 JSON）
    assert rc._parse_factor_ranking("[{'factor': 'dxy', 'direction': 'down'}]") == [
        {"factor": "dxy", "direction": "down"}]
    # 更老的行被 str(list)[:500] 从尾部截断（对象本身完整，只丢了外层 ]）→
    # literal_eval 报错，按 {} 对象片段抢救，两条都要救回来
    truncated = "[{'factor': 'cpi', 'direction': 'up'}, {'factor': 'oil', 'direction': 'down'}"
    assert rc._parse_factor_ranking(truncated) == [
        {"factor": "cpi", "direction": "up"}, {"factor": "oil", "direction": "down"}]
    assert rc._parse_factor_ranking("") == []


def test_direction_to_sign_covers_chinese_and_english():
    assert rc._direction_to_sign("看多") == 1
    assert rc._direction_to_sign("上涨") == 1
    assert rc._direction_to_sign("看空") == -1
    assert rc._direction_to_sign("bullish") == 1
    assert rc._direction_to_sign("bearish") == -1
    assert rc._direction_to_sign("") == 0


def test_action_to_sign_only_buy_sell():
    assert rc._action_to_sign("BUY") == 1
    assert rc._action_to_sign("SELL") == -1
    assert rc._action_to_sign("HOLD") == 0
    assert rc._action_to_sign("") == 0


def test_count_evidence_ignores_placeholders_and_lines_without_values():
    assert rc._count_evidence("") == 0
    assert rc._count_evidence("N/A") == 0
    assert rc._count_evidence("AMBIGUOUS") == 0
    assert rc._count_evidence("ust2y=4.2|dxy=104") == 2
    assert rc._count_evidence("a=1;b=2\nc=3") == 3
    assert rc._count_evidence("无等号的描述") == 0


def test_classify_missing_hard_evidence_splits_fixable_from_structural():
    code, sub, note = rc._classify_missing_hard_evidence("missing:no_local_feed")
    assert code == rc.E001g_NO_LOCAL_FEED and "结构性" in note
    code, sub, _ = rc._classify_missing_hard_evidence("missing:feed_stale")
    assert code == rc.E001h_FEED_STALE and sub == "missing_feed_stale"
    code, _, _ = rc._classify_missing_hard_evidence("missing:unknown_symbol")
    assert code == rc.E001i_UNKNOWN_SYMBOL
    code, _, note = rc._classify_missing_hard_evidence("missing:market_closed")
    assert code == rc.E001j_MARKET_CLOSED and "休市" in note
    # 未标注不硬猜：回落 E001a
    code, sub, _ = rc._classify_missing_hard_evidence("")
    assert code == rc.E001a_MISSING_HARD_EVIDENCE and sub == "missing_hard_evidence"


def test_ts_age_minutes_parses_iso_and_returns_none_on_placeholders():
    now = datetime(2026, 11, 21, 10, 0, 0)
    assert rc._ts_age_minutes(None, now) is None
    assert rc._ts_age_minutes("N/A", now) is None
    assert rc._ts_age_minutes("not-a-time", now) is None
    ts = (now - timedelta(minutes=45)).strftime("%Y-%m-%dT%H:%M:%SZ")
    assert rc._ts_age_minutes(ts, now) == 45.0
