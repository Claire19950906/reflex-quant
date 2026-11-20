# -*- coding: utf-8 -*-
"""v424：结构性优势体检的口径测试。

锁住三件最容易出错的事：
  1. walk-forward 分位：阈值只能用"当下之前"的数据算，不能偷看未来；
  2. 重叠样本：同一持有期内必须只留一个信号，否则 t 值虚高；
  3. 置换检验：样本数 < pool 时不能给 p，宁可返回 None 也不编一个数。
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src_python"))

import structural_edge as se


def _series(vals, start=1_000_000, step=28_800):
    return [(start + i * step, float(v)) for i, v in enumerate(vals)]


def test_quantile_basic():
    xs = [1, 2, 3, 4, 5]
    assert se._quantile(xs, 0.0) == 1
    assert se._quantile(xs, 1.0) == 5
    assert se._quantile(xs, 0.5) == 3
    assert se._quantile([], 0.5) == 0.0


def test_walk_forward_uses_past_only():
    # 前 40 个点全是 0.0，第 41 个点突然拉到 10 → 应该触发"高"信号；
    # 但触发器只能用前 40 个点的历史（全 0，无区分度）→ 不该出信号。
    vals = [0.0] * 40 + [10.0]
    sig = se.signals_walk_forward(_series(vals), 0.2, 0.8, 40, True)
    assert sig == []


def test_walk_forward_detects_high_and_low():
    base = [0.0, 1.0, 2.0] * 14      # 42 个点，均匀分布
    vals = base + [5.0] + base[:6] + [-5.0]
    sig = se.signals_walk_forward(_series(vals), 0.2, 0.8, 40, True)
    assert any(s["side"] == "high" and s["direction"] == "SELL" for s in sig)
    assert any(s["side"] == "low" and s["direction"] == "BUY" for s in sig)


def test_non_contrarian_flips_direction():
    base = [0.0, 1.0, 2.0] * 14
    vals = base + [5.0]
    contra = se.signals_walk_forward(_series(vals), 0.2, 0.8, 40, True)
    trend = se.signals_walk_forward(_series(vals), 0.2, 0.8, 40, False)
    assert contra[-1]["direction"] == "SELL"
    assert trend[-1]["direction"] == "BUY"


def test_dedupe_within_horizon():
    ev = [{"ts": 0}, {"ts": 3_600}, {"ts": 90_000}, {"ts": 95_000}, {"ts": 200_000}]
    kept = se.dedupe_within_horizon(ev, 86_400)
    assert [k["ts"] for k in kept] == [0, 90_000, 200_000]


def test_timing_p_none_when_pool_too_small():
    assert se.timing_p([1.0], 5, 1.0) is None
    assert se.timing_p([], 5, 1.0) is None
    assert se.timing_p([1.0, 2.0], 0, 1.0) is None
    assert se.timing_p([1.0, 2.0], 2, None) is None


def test_timing_p_extreme_values():
    # 决定性检验：真实值远高于 pool 里的每个值 → p 应该很小；远低 → p 接近 1
    pool = [0.0, 0.5, 1.0, 1.5]
    assert se.timing_p(pool, 2, 10.0, k=200) == 0.0
    assert se.timing_p(pool, 2, -10.0, k=200) == 1.0


def test_baseline_pool_covers_all_bars():
    # 早先把 bars 按"秒"步进，导致 pool 只剩 1 个样本 —— 这条防止再犯
    bars = [(1_000_000 + i * 14_400, 100.0 + i) for i in range(20)]
    pool = se._baseline_pool(bars, 86_400, "BUY", 14_400)
    assert len(pool) > 10
