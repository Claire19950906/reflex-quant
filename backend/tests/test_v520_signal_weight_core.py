# -*- coding: utf-8 -*-
"""信号权重七维乘子与阈值分层的契约测试（全离线，不碰生产库）。

为什么单独写这一组：
`signal_weight_evaluator.compose_weight` 的结果会写进 `signal_weight_history`，
再被前端 K 线标记深浅、信号卡排序、验收脚本的分桶统计读取。七个乘子里只要有
一个方向写反，**前端看起来一切正常**——只是深浅和排序全错了，没有任何报错。

这里锁的是每条乘子的**方向与边界**，不是具体浮点值：
  · base：sentiment_score 的口径是 -1~1、0 表示"无方向"，绝不能像旧实现那样
    把 0 当成"最强信号"（那条 bug 让 98.7% 的权重并列）
  · consensus：单个评审不能给自己背书；同一模型问两次不能算两票
  · source：样本 < 5 不校准（宁可不学，也不能拿 2 个样本改权重）
  · reaction：0.2% ≤ |pct| < 0.3% 且缩量的区间必须有返回值（旧实现隐式返回
    None，直接让连乘 TypeError）
  · regime：库里实际产出的 `volatile_high` / `mean_revert` / `volatile_low`
    必须映射到乘子，不能全部落到 1.0（那等于这维不存在）
  · time：未来时点（时钟漂移）按 1.0 处理，不能出现负年龄放大权重
  · tier：阈值改动会影响前端分层，三档必须单调且边界可断言
"""
from __future__ import annotations

import json
import os
import sqlite3
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src_python"))

from signals import signal_weight_evaluator as swe  # noqa: E402


# ── 1. compose_weight ──────────────────────────────────────────────────────

def test_compose_weight_stays_in_unit_interval_and_never_divides_by_zero():
    assert swe.compose_weight(0.0, 0.0)["weight"] == 0.0        # raw=0 不能除零
    r = swe.compose_weight(1.0, 1.0)
    assert 0.0 < r["weight"] < 1.0
    # 极端输入也不许越界（前端直接当不透明度用）
    assert swe.compose_weight(10, 10, 10, 10, 10, 10, 10)["weight"] <= 1.0


def test_compose_weight_is_monotonic_in_impact():
    lo = swe.compose_weight(0.8, 0.5)["weight"]
    hi = swe.compose_weight(0.8, 1.4)["weight"]
    assert hi > lo          # 影响力维必须真正参与排序


def test_compose_weight_profiles_differ_and_unknown_falls_back():
    legacy = swe.compose_weight(0.8, 1.0, cons=0.6, src=0.5, react=0.7,
                                reg=0.7, tm=1.0, profile="legacy")["weight"]
    led = swe.compose_weight(0.8, 1.0, cons=0.6, src=0.5, react=0.7,
                             reg=0.7, tm=1.0, profile="impact_led")["weight"]
    assert led > legacy                      # 减维后不再被质量维稀释
    unknown = swe.compose_weight(0.8, 1.0, profile="whatever")["profile"]
    assert unknown == "legacy"               # 未知档位退回现网行为


# ── 2. base_mult ──────────────────────────────────────────────────────────

def test_base_mult_zero_sentiment_is_neutral_not_max():
    # 旧 bug：abs(0 - 0.5) * 2 == 1.0，把"无方向"当成"最强"
    neutral = swe._base_mult(0, None, None)
    strong = swe._base_mult(1.0, 100, 1.0)
    assert neutral < strong
    assert 0.4 <= neutral <= 1.0


def test_base_mult_bounds_and_direction_sign():
    assert swe._base_mult(-1.0, 100, 1.0) >= swe._base_mult(0, 100, 1.0) >= 0.4
    assert swe._base_mult(1.0, 999, 9.9) <= 1.0          # 上限，不再靠 min(1.0) 截断
    assert swe._base_mult("bad", "bad", "bad") >= 0.4    # 脏值不崩


# ── 3. model_family / consensus ───────────────────────────────────────────

def test_model_family_strips_provider_prefix():
    assert swe._model_family("minimax/MiniMax-M3") == swe._model_family("MiniMax-M3")
    assert swe._model_family("") == ""
    assert swe._model_family(None) == ""


def test_consensus_single_reviewer_proves_nothing():
    assert swe._consensus_mult("") == 1.0
    assert swe._consensus_mult(
        json.dumps([{"model_id": "MiniMax-M3", "action": "BUY"}]), main_action="BUY") == 1.0
    # 同一模型换个皮问两次，也只能算一票
    twins = json.dumps([{"model_id": "MiniMax-M3", "action": "BUY"},
                        {"model_id": "minimax/MiniMax-M3", "action": "BUY"}])
    assert swe._consensus_mult(twins, main_action="BUY") == 1.0


def test_consensus_agreement_and_disagreement_directions():
    agree = ["AGREE", "all_agree"]
    assert all(swe._consensus_mult(a) == 1.3 for a in agree)
    assert swe._consensus_mult("MIXED") == 0.6
    assert swe._consensus_mult("CONFLICT") == 0.6
    assert swe._consensus_mult("{not json") == 1.0

    votes = json.dumps([{"model_id": "M2", "action": "BUY"},
                        {"model_id": "M3", "action": "BUY"}])
    assert swe._consensus_mult(votes, main_action="BUY") == 1.3
    against = json.dumps([{"model_id": "M2", "action": "SELL"},
                          {"model_id": "M3", "action": "SELL"}])
    # 一致地反对 ≠ 一致地同意：主方向 BUY 时只能给 0.3
    assert swe._consensus_mult(against, main_action="BUY") == 0.3


# ── 4. source ─────────────────────────────────────────────────────────────

def test_source_mult_refuses_to_learn_from_tiny_samples():
    assert swe._source_mult_from_cache(None) == 1.0
    assert swe._source_mult_from_cache({"sample_n": 4, "hit_rate": 1.0}) == 1.0
    assert swe._source_mult_from_cache({"sample_n": 5, "hit_rate": 1.0}) == 1.5
    assert swe._source_mult_from_cache({"sample_n": 50, "hit_rate": 0.0}) == 0.5
    assert swe._source_mult_from_cache({"sample_n": "x"}) == 1.0


# ── 5. reaction ───────────────────────────────────────────────────────────

def test_reaction_mult_never_returns_none_in_the_dead_zone():
    # |pct| 落在 0.2~0.3 且缩量：旧实现没有 return，隐式 None → 连乘 TypeError
    zone = {"triggered": True, "aligned": True, "pct_change": 0.25, "vol_ratio": 1.0}
    v = swe._reaction_mult(zone)
    assert isinstance(v, float) and 0.5 <= v <= 1.5


def test_reaction_mult_direction_between_aligned_and_against():
    assert swe._reaction_mult(None) == 1.0
    assert swe._reaction_mult({"triggered": False}) == 1.0
    against = {"triggered": True, "aligned": False, "pct_change": 0.4}
    aligned = {"triggered": True, "aligned": True, "pct_change": 0.6,
               "vol_ratio": 1.8}
    assert swe._reaction_mult(against) < swe._reaction_mult(aligned)
    assert swe._reaction_mult(aligned) == 1.4
    dirty = {"triggered": True, "aligned": True, "pct_change": "x", "vol_ratio": "y"}
    assert swe._reaction_mult(dirty) == 0.9


# ── 6. regime ─────────────────────────────────────────────────────────────

def test_regime_mult_maps_labels_actually_produced_by_classifier():
    assert swe._regime_mult("trending_up") == 1.2
    assert swe._regime_mult("sideways") == 0.85
    assert swe._regime_mult("mean_revert") == 0.85      # 原来落到 1.0
    assert swe._regime_mult("volatile_high") == 0.7     # 原来落到 1.0
    assert swe._regime_mult("volatile_low") == 0.95     # 原来落到 1.0
    assert swe._regime_mult("") == 1.0


# ── 7. time ───────────────────────────────────────────────────────────────

def test_time_mult_decays_by_horizon_and_shrugs_off_clock_drift():
    now = 1_700_000_000
    assert swe._time_mult(None, now_ts=now) == 1.0
    assert swe._time_mult(now + 3600, now_ts=now) == 1.0     # 未来时点不放大
    assert swe._time_mult(now - 3 * 3600, now_ts=now, expected_horizon="intraday") == 0.7
    assert swe._time_mult(now - 30 * 3600, now_ts=now, expected_horizon="1-3d") == 0.65
    assert swe._time_mult(now - 168 * 3600, now_ts=now) == 0.5
    assert swe._time_mult(now - 600, now_ts=now) == 1.0


# ── 8. tier / aligned / schema helpers ────────────────────────────────────

def test_to_tier_boundaries_are_exact():
    assert swe._to_tier(0.58) == "strong"
    assert swe._to_tier(0.579) == "medium"
    assert swe._to_tier(0.50) == "medium"
    assert swe._to_tier(0.42) == "weak"
    assert swe._to_tier(0.41) == "ignored"


def test_check_aligned_hold_is_never_contradicted():
    assert swe._check_aligned("bearish", "HOLD") is True    # HOLD 没有方向主张
    assert swe._check_aligned("bullish", "BUY") is True
    assert swe._check_aligned("bullish", "SELL") is False
    assert swe._check_aligned("", "SELL") is True
    assert swe._check_aligned("neutral", "SELL") is True


def test_is_missing_column_covers_both_sqlite_wordings():
    assert swe._is_missing_column(Exception("no such column: impact_score"))
    assert swe._is_missing_column(Exception("table ai_decisions has no column named x"))
    assert not swe._is_missing_column(Exception("disk I/O error"))


def test_select_expr_uses_null_placeholder_for_absent_columns():
    conn = sqlite3.connect(":memory:")
    conn.execute("CREATE TABLE t (id INTEGER, target_asset TEXT)")
    expr = swe._select_expr(conn, "t", ["id", "target_asset", "impact_score"])
    assert "NULL AS impact_score" in expr
    assert "id" in expr and "target_asset" in expr
    assert swe._select_expr(conn, "missing_table", ["id"]) == ""
    conn.close()
