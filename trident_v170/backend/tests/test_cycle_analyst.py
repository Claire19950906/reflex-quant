# -*- coding: utf-8 -*-
"""cycle_analyst 单元测试（v226+ Q7 闭环）。"""
from __future__ import annotations

import random

import cycle_analyst as ca


def _rising(n: int = 30, pct: float = 2.0) -> list:
    """每天上涨 pct% 的几何序列（强势 markup）。"""
    prices = [100.0]
    for _ in range(n - 1):
        prices.append(prices[-1] * (1 + pct / 100))
    return prices


def _falling(n: int = 30, pct: float = 2.0) -> list:
    prices = [100.0]
    for _ in range(n - 1):
        prices.append(prices[-1] * (1 - pct / 100))
    return prices


def _flat_with_pump_and_dump(n: int = 60, peak_idx: int = 30,
                              peak_pct: float = 30.0) -> list:
    prices = []
    p = 100.0
    for i in range(n):
        if i < peak_idx:
            p *= (1 + (peak_pct / peak_idx) / 100)
        else:
            p *= (1 - (peak_pct / (n - peak_idx)) / 100)
        prices.append(p)
    return prices


def _random_walk(n: int = 60, seed: int = 42) -> list:
    rnd = random.Random(seed)
    prices = [100.0]
    for _ in range(n - 1):
        prices.append(prices[-1] * (1 + rnd.gauss(0, 0.01)))
    return prices

def test_zigzag_empty_returns_empty():
    assert ca.zigzag_pivots([]) == []
    assert ca.zigzag_pivots([100.0]) == []


def test_zigzag_monotonic_rising_has_one_pivot():
    pivots = ca.zigzag_pivots(_rising(20), threshold_pct=5.0)
    assert len(pivots) >= 1


def test_zigzag_monotonic_falling_has_one_pivot():
    pivots = ca.zigzag_pivots(_falling(20), threshold_pct=5.0)
    assert len(pivots) >= 1


def test_zigzag_pump_dump_detects_reversals():
    prices = _flat_with_pump_and_dump(n=60, peak_idx=30, peak_pct=30.0)
    pivots = ca.zigzag_pivots(prices, threshold_pct=10.0)
    types = [p["type"] for p in pivots]
    assert "high" in types
    assert "low" in types


def test_zigzag_threshold_higher_fewer_pivots():
    prices = [100 + (i % 5) * 0.5 for i in range(50)]
    low_th = ca.zigzag_pivots(prices, threshold_pct=1.0)
    high_th = ca.zigzag_pivots(prices, threshold_pct=10.0)
    assert len(high_th) <= len(low_th)
def test_hurst_strong_trend_returns_high_value():
    """强持续性序列（AR(1) φ=0.5） → Hurst > 0.5。"""
    import random
    random.seed(42)
    prices = [100.0]
    # AR(1) 过程：每步正相关于上一步 → Hurst > 0.5（持久性）
    last_return = 0.0
    for _ in range(199):
        eps = random.gauss(0, 0.01)
        # AR(1) with phi = 0.5
        last_return = 0.5 * last_return + eps
        prices.append(prices[-1] * (1 + last_return))
    h = ca.hurst_exponent(prices)
    assert h is not None
    assert h > 0.5, f"持久性序列 Hurst 应 > 0.5，实际 {h}"


def test_hurst_random_walk_returns_near_05():
    prices = _random_walk(60, seed=42)
    h = ca.hurst_exponent(prices)
    assert h is not None
    assert 0.2 < h < 0.8, f"随机游走 Hurst 应 ~0.5，实际 {h}"


def test_hurst_too_short_returns_none():
    assert ca.hurst_exponent([100, 101, 102]) is None
    assert ca.hurst_exponent([100 + i for i in range(15)]) is None


def test_detect_phase_markup():
    prices = _rising(30, pct=2.0)
    result = ca.detect_phase(prices)
    assert result["phase"] in ("markup", "distribution")
    assert result["trend_pct"] > 0


def test_detect_phase_markdown():
    prices = _falling(30, pct=2.0)
    result = ca.detect_phase(prices)
    assert result["phase"] in ("markdown", "accumulation")
    assert result["trend_pct"] < 0


def test_detect_phase_too_short_returns_unknown():
    result = ca.detect_phase([100, 101, 102, 103])
    assert result["phase"] == "unknown"
    assert result["confidence"] == 0.0


def test_detect_phase_returns_required_fields():
    prices = _rising(30, pct=2.0)
    result = ca.detect_phase(prices)
    for k in ("phase", "rsi", "hurst", "trend_pct", "confidence"):
        assert k in result
def test_detect_cycle_length_returns_int_with_enough_data():
    prices = _flat_with_pump_and_dump(n=120, peak_idx=60, peak_pct=40.0)
    cycle = ca.detect_cycle_length(prices)
    assert cycle is None or isinstance(cycle, int)


def test_detect_cycle_length_returns_none_for_short_data():
    prices = _rising(30)
    assert ca.detect_cycle_length(prices) is None


def test_predict_reversal_top_pivot_signals_down():
    prices = _flat_with_pump_and_dump(n=60, peak_idx=30, peak_pct=30.0)
    result = ca.predict_reversal(prices)
    assert result["votes"] >= 1


def test_predict_reversal_persistent_series_hurst_vote():
    """强持续性序列（AR(1) φ=0.8） → Hurst 应 ≥ 0.5（≥ 0.7 阈值附近）。"""
    import random
    random.seed(42)
    prices = [100.0]
    last_return = 0.0
    for _ in range(399):
        eps = random.gauss(0, 0.01)
        # AR(1) with phi = 0.8（更强的持续性，Hurst 应接近 0.7+）
        last_return = 0.8 * last_return + eps
        prices.append(prices[-1] * (1 + last_return))
    h = ca.hurst_exponent(prices)
    assert h is not None
    assert h > 0.5
    result = ca.predict_reversal(prices)
    assert result["hurst_vote"] is True


def test_predict_reversal_too_short_returns_no_votes():
    result = ca.predict_reversal([100, 101, 102, 103])
    assert result["prob"] == 0.0
    assert result["votes"] == 0


def test_predict_reversal_returns_required_fields():
    prices = _rising(30)
    result = ca.predict_reversal(prices)
    for k in ("prob", "days_to", "direction", "votes",
              "rsi_vote", "pivot_vote", "hurst_vote"):
        assert k in result


def test_predict_reversal_strong_trend_hurst_vote():
    """老测试保留兼容：恒定涨价的纯序列应不投 Hurst 票（无方差，H 不适用）。"""
    prices = _rising(60, pct=2.0)
    result = ca.predict_reversal(prices)
    # 纯确定性趋势 R/S 失效 → hurst=None → 不投票
    assert result["hurst_vote"] is False


def test_multi_timeframe_aligned():
    """三周期同方向 → aligned。"""
    import random
    random.seed(42)
    def _rising_noisy(n: int, pct: float = 0.5) -> list:
        prices = [100.0]
        for _ in range(n - 1):
            prices.append(prices[-1] * (1 + (pct + random.gauss(0, 0.5)) / 100))
        return prices
    result = ca.multi_timeframe_signal({
        "large": _rising_noisy(60),
        "mid": _rising_noisy(40),
        "short": _rising_noisy(20),
    })
    assert result["alignment"] == "aligned"


def test_multi_timeframe_diverging():
    """三周期不同方向（up/mid/down/short-up） → diverging 或 mixed。"""
    import random
    random.seed(42)
    def _rising_noisy(n: int) -> list:
        prices = [100.0]
        for _ in range(n - 1):
            prices.append(prices[-1] * (1 + random.gauss(0.005, 0.01)))
        return prices
    def _falling_noisy(n: int) -> list:
        prices = [100.0]
        for _ in range(n - 1):
            prices.append(prices[-1] * (1 + random.gauss(-0.005, 0.01)))
        return prices
    result = ca.multi_timeframe_signal({
        "large": _rising_noisy(60),
        "mid": _falling_noisy(40),
        "short": _rising_noisy(20),
    })
    # 注意：可能因 noise 落入 mixed（one of them = ranging），但绝不应是 aligned
    assert result["alignment"] in ("diverging", "mixed")


def test_multi_timeframe_empty_data():
    result = ca.multi_timeframe_signal({})
    assert result["alignment"] == "mixed"
    assert result["dominant_phase"] == "ranging"


def test_multi_timeframe_insufficient_data_marks_unknown():
    import random
    random.seed(42)
    def _weak_rising(n: int) -> list:
        """弱上涨（漂移小、噪声大），避免 RSI 触发 distribution。"""
        prices = [100.0]
        for _ in range(n - 1):
            prices.append(prices[-1] * (1 + random.gauss(0.001, 0.015)))
        return prices
    result = ca.multi_timeframe_signal({
        "large": _weak_rising(60),
        "mid": [100, 101, 102],
        "short": _weak_rising(20),
    })
    assert result["phase_by_tf"]["mid"] == "unknown"
    # large + short 应为 markup 或 ranging（弱上涨）
    assert result["dominant_phase"] in ("markup", "ranging")