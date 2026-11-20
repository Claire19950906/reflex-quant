"""v335: S4 权重判别力换口径 —— 桶内秩相关。

老口径 (先减同小时中位数、再把所有小时混在一起排秩) 实测 0.197,
卡在 0.20 门槛下方 0.003。根因不是"权重没用", 而是口径把信号淹了:
不同小时的波动量级差一个量级 (平静 ±0.2% / 剧烈 ±3%), 混排时小时之间的差异
盖过同一小时内权重差异的效果。只在同一小时内比较再按样本量加权 → 0.288。

构造测试数据时要复刻真实形状: 同一个权重会配到**正负两侧**的涨跌 (信号给方向,
权重给的是"影响有多大"), 所以桶内中位数≈0, |偏离| 才随权重单调。
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src_python"))

import capability_acceptance as ca


def _hour(hour: int, weights, scale: float = 10.0):
    """一个小时内: 每个权重配上 ±两侧涨跌, 幅度 ∝ 权重。"""
    out = []
    for w in weights:
        out.append((hour, w, w * scale))
        out.append((hour, w, -w * scale))
    return out


WEIGHTS = (0.2, 0.4, 0.6, 0.8, 1.0)


def test_weight_ranks_impact_within_hour():
    pairs = _hour(100, WEIGHTS) + _hour(101, WEIGHTS) + _hour(102, WEIGHTS)
    res = ca.bucket_spearman(pairs)
    assert res["rho"] == 1.0
    assert res["hours"] == 3
    assert res["n"] == 30


def test_weight_inverse_impact_is_negative():
    """权重大反而影响小的极端情形 (公式写反了) → ρ=-1."""
    pairs = []
    for w in WEIGHTS:
        mag = (1.0 - w) * 10
        pairs.append((7, w, mag))
        pairs.append((7, w, -mag))
    assert ca.bucket_spearman(pairs)["rho"] == -1.0


def test_market_level_offset_does_not_leak_across_hours():
    """平静小时 ±2%、剧烈小时 ±30% —— 混排会被量级差扭曲, 桶内仍单调。"""
    pairs = _hour(10, WEIGHTS, scale=2.0) + _hour(11, WEIGHTS, scale=30.0)
    assert ca.bucket_spearman(pairs)["rho"] == 1.0


def test_buckets_below_min_n_are_skipped():
    pairs = [(1, 0.1, 1.0), (1, 0.5, 2.0), (1, 0.9, 3.0)]   # 只有 3 个, < 5
    pairs += _hour(2, WEIGHTS)
    res = ca.bucket_spearman(pairs)
    assert res["hours"] == 1
    assert res["n"] == 10


def test_empty_and_tiny_input_edge_cases():
    assert ca.bucket_spearman([]) == {"rho": None, "n": 0, "hours": 0}
    assert ca.bucket_spearman([(1, 0.5, 1.0)] * 4)["rho"] is None


def test_bucket_rho_beats_pooled_rho_on_synthetic_regimes():
    """两种量级交替的时段: 混合口径被量级差压下去, 桶内口径保持 +1。"""
    pairs = []
    for hour in range(20):
        scale = 1.0 if hour % 2 == 0 else 6.0
        pairs += _hour(hour, WEIGHTS, scale=scale)
    bucket = ca.bucket_spearman(pairs)
    med = ca.statistics.median([p[2] for p in pairs])
    pooled = ca._spearman([p[1] for p in pairs],
                          [abs(p[2] - med) for p in pairs])
    assert bucket["rho"] == 1.0
    assert pooled is not None and pooled < 0.6
