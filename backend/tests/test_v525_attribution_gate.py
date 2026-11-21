# -*- coding: utf-8 -*-
"""归因闸门（闸二/闸三）与噪声源治理的契约测试（全离线）。

这一层决定"一条新闻能不能解释一次异动"。它有三类历史污染，源码注释里都记着：
  · 快涨用 5 天窗口 → 时间上不成立；慢跌挂"跨链桥恢复" → 巧合当因果
  · 行情播报（"SOL 跌破 95 美元，24 小时跌幅 0.84%"）被当成原因 → 循环论证
  · 来源不分好坏 → 噪音源靠篇数挤掉真证据（同一套打分在不同源命中率差 6 倍）

这里的断言直接对着这些反例写，不是对着"代码长什么样"写。
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src_python"))

import calc_event_move_attribution as attr  # noqa: E402


def _ins(move_pct=0.0, score=0.0, match_type="ASSET"):
    ins = [None] * 18
    ins[4] = score
    ins[11] = move_pct
    ins[16] = match_type
    return tuple(ins)


# ── 闸三分流 ──────────────────────────────────────────────────────────────

def test_route_insert_ai_reject_never_enters_any_table():
    assert attr.route_insert(_ins(move_pct=5.0), "reject", ai_online=True) == "skip"


def test_route_insert_small_move_and_ai_confirmed_go_main():
    assert attr.route_insert(_ins(move_pct=0.01), None, ai_online=False) == "main"
    assert attr.route_insert(_ins(move_pct=5.0), "confirm", ai_online=False) == "main"
    assert attr.route_insert(_ins(move_pct=5.0), "partial", ai_online=True) == "main"


def test_route_insert_big_move_without_ai_queues_unless_degraded_pass():
    big = attr.BIG_MOVE_ABS_PCT + 1.0
    assert attr.route_insert(_ins(move_pct=big), None, ai_online=True) == "pending"
    # 降级放行只对"直接命中本标的 + 分数过线"开
    assert attr.route_insert(_ins(move_pct=big, score=attr.DEGRADE_MIN_SCORE,
                                  match_type="ASSET"), None, ai_online=False) == "main"
    assert attr.route_insert(_ins(move_pct=big, score=attr.DEGRADE_MIN_SCORE,
                                  match_type="CROSS"), None, ai_online=False) == "pending"
    assert attr.route_insert(_ins(move_pct=big, score=0.1,
                                  match_type="ASSET"), None, ai_online=False) == "pending"


# ── 窗口按形态自适应 ──────────────────────────────────────────────────────

def test_window_and_lag_share_the_same_shape_rules():
    assert attr._window_for_kind("fast_rise", 0) == attr.MOVE_WINDOW_FAST_SEC
    assert attr._window_for_kind("slow_fall", 0) == attr.MOVE_WINDOW_SLOW_SEC
    # 形态未知但持续 <=180 分钟 → 当快形态；更久 → 默认
    assert attr._window_for_kind("", 120) == attr.MOVE_WINDOW_FAST_SEC
    assert attr._window_for_kind("", 181) == attr.MOVE_WINDOW_DEFAULT_SEC
    assert attr._lag_max_for_kind("slow_fall", 0) == attr.LAG_MAX_SLOW_SEC
    assert attr._lag_max_for_kind("fast_rise", 0) == attr.LAG_MAX_FAST_SEC
    # 两者必须同口径，不许"窗口 3 天、闸门只放 6 小时"
    assert attr._lag_max_for_kind("", 120) == attr.LAG_MAX_FAST_SEC


def test_slow_shape_rejects_cross_matches_only():
    assert attr._slow_weak_blocked("cross", True) is True
    assert attr._slow_weak_blocked("asset", True) is False
    assert attr._slow_weak_blocked("cross", False) is False   # 快形态不限制 cross


# ── 标题清洗与方向一致性 ─────────────────────────────────────────────────

def test_strip_hash_prefix_keeps_first_line_content():
    assert attr._strip_hash_prefix("[hash:9f3a] 霍尔木兹海峡封锁") == "霍尔木兹海峡封锁"
    assert attr._strip_hash_prefix("  [hash:AB12]   多  空格  ") == "多 空格"
    assert attr._strip_hash_prefix("[hash:9f3a]") == ""
    assert attr._strip_hash_prefix(None) == ""


def test_source_trust_normalises_aliases_and_zeroes_noise():
    assert attr._source_trust("eastmoney") == 1.2
    assert attr._source_trust("东方财富") == 1.2          # 别名归一，同一家媒体不许拆两次
    assert attr._source_trust("WEB:Blogs") == 0.0         # 博客聚合与事件驱动无关
    assert attr._source_trust("jin10_calendar") == 0.0
    assert attr._source_trust("") == 0.9                  # 空来源略低于中性
    assert attr._source_trust("some_new_feed") == 1.0


def test_headline_direction_and_contradiction_block():
    assert attr._headline_direction("SOL 涨超 5%") == 1
    assert attr._headline_direction("SOL 跌破 95 美元") == -1
    assert attr._headline_direction("纳指跌、芯片涨") == 0     # 方向混杂不判
    assert attr._headline_direction("") == 0
    # 利好解释下跌 / 利空解释上涨 → 循环论证
    assert attr._direction_blocked("SOL 跌破 95 美元", 1.57) is True
    assert attr._direction_blocked("SOL 涨超 5%", 1.57) is False
    assert attr._direction_blocked("纳指跌、芯片涨", -2.0) is False


def test_price_echo_detection_needs_verb_and_price_mark():
    assert attr._is_price_echo("SOL 跌破 95 美元，24 小时跌幅 0.84%") is True
    assert attr._is_price_echo("BNB 跌破 700 美元") is True
    assert attr._is_price_echo("SOL 上涨 5%") is False         # 无价格标记
    assert attr._is_price_echo("黄金价格创新高") is False
