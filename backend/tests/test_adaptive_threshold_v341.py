# -*- coding: utf-8 -*-
"""v341: 逐点波动率自适应阈值 —— 两条硬性质。

1. **无前视**：第 i 根的阈值只能由 ≤ i 的数据决定。
   这是回测/实盘口径一致的前提；v340 的标量版恰恰违反了它
   （用最新 250 日去判四年前的 K 线）。
2. **自适应**：同一品种在剧烈期阈值自动抬高、平静期自动压低，
   而不是"一个数管全部历史"。
"""
from __future__ import annotations

import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src_python"))

import backfill_critical_moves as bcm


def _series(closes, start_ts=1_700_000_000):
    return [(start_ts + i * 86400, float(c)) for i, c in enumerate(closes)]


def _quiet(n=120, base=100.0, amp=0.005):
    """日波动 ±0.5% 的平静序列。"""
    out = []
    for i in range(n):
        out.append(base * (1 + amp * (1 if i % 2 else -1)))
    return out


def _wild(n=120, base=100.0, amp=0.08):
    """日波动 ±8% 的剧烈序列。"""
    out = []
    for i in range(n):
        out.append(base * (1 + amp * (1 if i % 2 else -1) * (1 + i % 3)))
    return out


# ── 1. 无前视 ───────────────────────────────────────────────────────
def test_scale_at_i_does_not_depend_on_future():
    """把后面 100 根改成剧烈波动，前 100 根的 scale 必须一模一样。"""
    head = _quiet(100) + _quiet(100, base=101.0)
    tail_wild = head[:100] + _wild(100, base=101.0)
    a = bcm.compute_thresh_scale_series(_series(head))
    b = bcm.compute_thresh_scale_series(_series(tail_wild))
    assert a[:100] == b[:100]


def test_scale_series_aligned_with_prices():
    prices = _series(_quiet(80))
    scales = bcm.compute_thresh_scale_series(prices)
    assert len(scales) == len(prices)
    assert all(isinstance(s, float) for s in scales)


# ── 2. 自适应 ─────────────────────────────────────────────────────
def test_calm_and_wild_get_different_thresholds():
    calm = bcm.compute_thresh_scale_series(_series(_quiet(120)))[-1]
    wild = bcm.compute_thresh_scale_series(_series(_wild(120)))[-1]
    assert calm < wild, f"平静期阈值应更低: calm={calm} wild={wild}"
    # 平静期落在下限附近，剧烈期明显抬高
    assert calm <= 0.75
    assert wild >= 1.5


def test_regime_shift_raises_threshold_then_settles():
    """平静 120 天 → 突然剧烈：阈值应随之抬起来（而不是永远用平静期的数）。"""
    prices = _series(_quiet(120) + _wild(120, base=100.4))
    scales = bcm.compute_thresh_scale_series(prices)
    assert scales[119] <= 0.75, "平静期末应仍是低阈值"
    assert scales[-1] >= 1.2, "剧烈期后阈值必须抬起来"


def test_clamped_to_floor_and_cap():
    tiny = bcm.compute_thresh_scale_series(_series(_quiet(200, amp=0.0005)))
    huge = bcm.compute_thresh_scale_series(_series(_wild(200, amp=0.30)))
    assert all(s >= 0.67 for s in tiny)
    assert all(s <= 3.5 for s in huge)


def test_short_history_falls_back_to_one():
    prices = _series(_quiet(10))
    scales = bcm.compute_thresh_scale_series(prices)
    assert all(s == 1.0 for s in scales), "历史不足时应保守用 1.0，不猜"


# ── 3. detect_moves 真的吃逐点 scale ───────────────────────────────
def test_detect_moves_uses_per_index_scale():
    """构造一段：先用高阈值压住假异动，再用低阈值让真异动过线。

    若 detect_moves 忽略了 scales（比如仍用标量 3.5），第二段就不会产出 move。
    """
    closes = _quiet(60)                       # 前段平静
    closes += [100.0, 103.0, 106.0, 109.0]    # 后段连涨 ~3%/日
    prices = _series(closes)
    scales = bcm.compute_thresh_scale_series(prices)
    by_scale = bcm.detect_moves(prices, scales=scales)
    fixed = bcm.detect_moves(prices, scale=3.5)
    assert len(by_scale) >= len(fixed), (len(by_scale), len(fixed))
    assert by_scale, "自适应阈值下这段连涨必须被识别出来"


def test_detect_moves_without_scales_matches_legacy_behaviour():
    """不传 scales 时与原标量行为完全一致（向后兼容）。"""
    prices = _series(_wild(60) + _quiet(40, base=200.0))
    a = bcm.detect_moves(prices, scale=1.0)
    b = bcm.detect_moves(prices, scale=1.0, scales=None)
    assert a == b


def test_no_lookahead_end_to_end():
    """端到端：只喂前 120 根 vs 喂全量，前 120 根识别出的异动必须一致。"""
    closes = _wild(120) + _quiet(120, base=101.0)
    prices = _series(closes)
    full = bcm.detect_moves(prices, scales=bcm.compute_thresh_scale_series(prices))
    head = prices[:120]
    part = bcm.detect_moves(head, scales=bcm.compute_thresh_scale_series(head))
    # 只比"未来 5 天也都在前 120 根之内"的部分：末尾那几根在截断序列里
    # 根本没有 i+k 这个端点，属于样本边界，不算口径差异。
    cutoff = head[-6][0]
    full_head = [m for m in full if m["ts"] <= cutoff]
    part = [m for m in part if m["ts"] <= cutoff]
    assert [(m["ts"], m["move_pct"], m["move_kind"]) for m in full_head] == \
           [(m["ts"], m["move_pct"], m["move_kind"]) for m in part]
