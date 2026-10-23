"""技术指标纯函数库 —— 单元测试（行情归因 Phase 1，离线）。

覆盖：RSI / 背离 / EMA / SMA / MACD / BOLL / ATR / 量价 / OI变化率 / 相对强弱 / 快照。
"""

from __future__ import annotations

import os
import sys

SRC = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "src_python"))
if SRC not in sys.path:
    sys.path.insert(0, SRC)

import technical_indicators as ti


def _candle(ts, o, h, l, c, v):
    """构造一根蜡烛 [ts_ms, open, high, low, close, volume]。"""
    return [ts, o, h, l, c, v]


# ---------------------------------------------------------------------------
# RSI
# ---------------------------------------------------------------------------

def test_rsi_all_up_reaches_100():
    closes = [float(i) for i in range(1, 40)]
    r = ti.rsi(closes)
    assert r[-1] == 100.0


def test_rsi_all_down_reaches_0():
    closes = [float(40 - i) for i in range(40)]
    r = ti.rsi(closes)
    assert r[-1] == 0.0


def test_rsi_insufficient_returns_all_none():
    r = ti.rsi([1.0, 2.0, 3.0], period=14)
    assert r == [None, None, None]


# ---------------------------------------------------------------------------
# 均线 / 平滑
# ---------------------------------------------------------------------------

def test_ema_constant():
    assert ti.ema([5.0] * 10, 12) == [5.0] * 10


def test_sma_constant():
    s = ti.sma([5.0] * 10, 3)
    assert s[:2] == [None, None]
    assert all(x == 5.0 for x in s[2:])


def test_ma_stack():
    assert set(ti.ma_stack([1.0] * 5).keys()) >= {"ma5", "ma10", "ma20", "ma60", "bull_aligned"}


# ---------------------------------------------------------------------------
# MACD
# ---------------------------------------------------------------------------

def test_macd_constant_is_zero():
    m = ti.macd([5.0] * 30)
    assert m["hist"] == [0.0] * 30


def test_macd_cross_none_when_flat():
    assert ti.macd_cross([5.0] * 30) is None


def test_macd_cross_none_on_too_short():
    assert ti.macd_cross([5.0]) is None
    assert ti.macd_cross([]) is None


# ---------------------------------------------------------------------------
# BOLL
# ---------------------------------------------------------------------------

def test_bollinger_constant_flat():
    b = ti.bollinger([5.0] * 25, period=20)
    assert b["upper"][-1] == 5.0
    assert b["mid"][-1] == 5.0
    assert b["lower"][-1] == 5.0


def test_bollinger_state_percent_b():
    st = ti.bollinger_state([5.0] * 25, period=20)
    assert st["upper"] == st["lower"] == st["mid"] == 5.0
    assert st["percent_b"] == 0.5


# ---------------------------------------------------------------------------
# ATR
# ---------------------------------------------------------------------------

def test_atr_constant_true_range():
    candles = [_candle(i, 10, 12, 10, 11, 100) for i in range(30)]
    a = ti.atr(candles, period=14)
    assert a[12] is None
    assert a[13] == 2.0
    assert a[-1] == 2.0


# ---------------------------------------------------------------------------
# 量价关系
# ---------------------------------------------------------------------------

def test_volume_regime_fangliang_shangzhang():
    candles = [_candle(i, 10, 11, 9, 10 + i, 100) for i in range(6)]
    candles.append(_candle(6, 16, 17, 15, 18, 800))
    r = ti.volume_regime(candles)
    assert r["regime"] == "放量上涨"
    assert r["price_up"] is True


def test_volume_regime_suoliang_xiadie():
    candles = [_candle(i, 20, 21, 19, 20 - i, 100) for i in range(6)]
    candles.append(_candle(6, 10, 11, 9, 9, 50))
    r = ti.volume_regime(candles)
    assert r["regime"] == "缩量下跌"
    assert r["divergence"] == "buy"


def test_volume_regime_insufficient():
    r = ti.volume_regime([_candle(0, 10, 11, 9, 10, 100)])
    assert r["regime"] == "样本不足"


# ---------------------------------------------------------------------------
# 持仓量 / 相对强弱
# ---------------------------------------------------------------------------

def test_oi_change_pct():
    assert ti.oi_change_pct(200, 100) == 100.0
    assert ti.oi_change_pct(100, 200) == -50.0
    assert ti.oi_change_pct(None, 100) is None
    assert ti.oi_change_pct(100, 0) is None


def test_relative_strength():
    assert ti.relative_strength(2000, 100) == {"rs": 20.0, "rs_pct": None}
    rs = ti.relative_strength(2000, 100, prev_asset=1000, prev_base=100)
    assert rs["rs"] == 20.0
    assert rs["rs_pct"] == 100.0


# ---------------------------------------------------------------------------
# RSI 背离
# ---------------------------------------------------------------------------

def test_rsi_divergence_bearish():
    # 100→114 连涨(RSI=100) → 回调两拍 → 末根 +3 创新高，但 RSI 未创新高 → 顶背离
    closes = list(range(100, 115))
    closes = closes + [113, 112, 112, 115]
    assert ti.rsi_divergence(closes) == "bearish"


def test_rsi_divergence_bullish():
    # 115→101 连跌(RSI=0) → 反弹两拍 → 末根 -3 创新低，但 RSI 未创新低 → 底背离
    closes = list(range(115, 101, -1))
    closes = closes + [102, 103, 103, 100]
    assert ti.rsi_divergence(closes) == "bullish"


def test_rsi_divergence_none():
    assert ti.rsi_divergence([float(i) for i in range(1, 40)]) == "none"


# ---------------------------------------------------------------------------
# 快照
# ---------------------------------------------------------------------------

def test_snapshot_shape():
    candles = [_candle(i, 10, 11, 9, 10 + i * 0.5, 100) for i in range(40)]
    s = ti.snapshot(candles, oi_now=200, oi_prev=100,
                    asset_price=2000, base_price=100, prev_asset=1000, prev_base=100)
    assert set(s.keys()) == {
        "rsi", "rsi_divergence", "macd_hist", "macd_cross", "bollinger",
        "atr", "ma_stack", "volume_regime", "oi_change_pct", "relative_strength",
    }
    assert s["rsi"] == 100.0
    assert s["oi_change_pct"] == 100.0
    assert s["relative_strength"]["rs_pct"] == 100.0
    assert s["bollinger"] is not None


# ---------------------------------------------------------------------------
# Q5 新增 4 个异常识别（macd_cross 已覆盖于 test_macd_cross_*）
# ---------------------------------------------------------------------------

def test_macd_zero_cross_above_when_rising_from_negative():
    """末根巨涨 + 前段在零下平台 → MACD 末根零轴上穿"""
    closes = [100.0 - i * 0.5 for i in range(50)]
    closes += [75.0 + i * 0.05 for i in range(30)]
    closes += [75.0 - i * 0.05 for i in range(20)]
    closes += [75.0]
    closes += [90.0]   # 末根巨涨，MACD 末根跳上零轴
    assert ti.macd_zero_cross(closes) == "above_zero"


def test_macd_zero_cross_below_when_falling_from_positive():
    """镜像：先涨 → 微振荡 → 末根大跌穿零"""
    closes = [50.0 + i * 0.5 for i in range(50)]
    closes += [74.5 + (i % 2) * 0.1 for i in range(30)]
    closes += [74.5]
    closes += [50.0]   # 末根大跌
    assert ti.macd_zero_cross(closes) == "below_zero"


def test_macd_zero_cross_none_when_flat():
    closes = [10.0] * 40
    assert ti.macd_zero_cross(closes) is None


def test_rsi_blunting_bearish_when_steady_overbought():
    """价格连涨 → RSI 持续 ≥70 → 钝化看跌"""
    closes = [10.0 + 0.5 * i for i in range(20)]  # 单边上涨，RSI≈100
    assert ti.rsi_blunting(closes, lookback=5, overbought=70.0) == "bearish"


def test_rsi_blunting_bullish_when_steady_oversold():
    closes = [30.0 - 0.5 * i for i in range(20)]  # 单边下跌，RSI≈0
    assert ti.rsi_blunting(closes, lookback=5, oversold=30.0) == "bullish"


def test_rsi_blunting_none_when_alternating():
    """锯齿走势 → RSI 反复穿越 → 不构成钝化"""
    closes = [10.0 + (1.0 if i % 2 == 0 else -1.0) for i in range(20)]
    assert ti.rsi_blunting(closes, lookback=5) is None


def test_rsi_blunting_insufficient_returns_none():
    closes = [float(i) for i in range(10)]
    assert ti.rsi_blunting(closes, lookback=5) is None


def test_vol_break_bullish_when_volume_surge_and_price_breakout():
    """前 20 根窄幅 + 量有起伏 + 末根放量创高 → bullish_break"""
    # 量要有起伏（否则 std=0 → 永不触发），价格末根突破前 20 根最高
    candles = [_candle(i, 100, 101, 99, 100, 100 + i * 5) for i in range(20)]
    # 前 20 根最高 close = 100，high = 101
    candles.append(_candle(20, 102, 115, 101, 113, 5000))  # 末根突破前高 + 巨量
    assert ti.vol_break(candles, z_threshold=2.0) == "bullish_break"


def test_vol_break_bearish_when_volume_surge_and_price_breakdown():
    candles = [_candle(i, 110, 111, 109, 110, 100 + i * 5) for i in range(20)]
    candles.append(_candle(20, 108, 109, 99, 100, 5000))
    assert ti.vol_break(candles, z_threshold=2.0) == "bearish_break"


def test_vol_break_none_when_volume_only_no_price_break():
    """仅量能放大，价格未突破 → 返回 None（调用方不投票）"""
    # 前 20 根 close 都在 [99, 100]，末根 close=99.5 显然不破前 20 根最高 close=100
    candles = [_candle(i, 100, 101, 99, 100 - i * 0.05, 100 + i * 5) for i in range(20)]
    candles.append(_candle(20, 99, 100, 98.5, 99.5, 5000))   # 末根 close 99.5 < 前 20 根最高 100
    assert ti.vol_break(candles, z_threshold=2.0) is None


def test_vol_break_insufficient_returns_none():
    candles = [_candle(0, 100, 101, 99, 100, 100)]
    assert ti.vol_break(candles) is None


def test_engulfing_bullish_when_current_bull_engulfs_prev_bear():
    """前阴后阳 + 当前实体严格包覆前一根 → 看涨吞没"""
    # 前阴：o=100, c=98 → body [98, 100]
    # 后阳：o=95, c=101 → body [95, 101]，严格 > [98, 100]
    candles = [
        _candle(0, 100, 100, 96, 98, 100),    # 阴线（实体 98~100）
        _candle(1, 95, 102, 94, 101, 100),    # 阳线（实体 95~101，严格包覆 98~100）
    ]
    assert ti.engulfing(candles) == "bullish"


def test_engulfing_bearish_when_current_bear_engulfs_prev_bull():
    # 前阳：o=95, c=98 → body [95, 98]
    # 后阴：o=101, c=94 → body [94, 101]，严格 > [95, 98]
    candles = [
        _candle(0, 95, 100, 94, 98, 100),     # 阳线（实体 95~98）
        _candle(1, 101, 106, 93, 94, 100),    # 阴线（实体 94~101，严格包覆 95~98）
    ]
    assert ti.engulfing(candles) == "bearish"


def test_engulfing_none_when_same_direction():
    candles = [
        _candle(0, 100, 101, 99, 100.5, 100),  # 阳
        _candle(1, 100, 105, 99, 104, 100),    # 阳（更大）—— 同向不构成吞没
    ]
    assert ti.engulfing(candles) is None


def test_engulfing_none_when_not_engulfed():
    """当前实体没有完全包覆前一根 → 不构成吞没"""
    candles = [
        _candle(0, 100, 100, 90, 91, 100),    # 阴线 90~100
        _candle(1, 95, 100, 94, 99, 100),     # 阳线 95~99（前一根范围 90~100，未包覆）
    ]
    assert ti.engulfing(candles) is None


def test_engulfing_none_on_too_short():
    assert ti.engulfing([_candle(0, 100, 101, 99, 100, 100)]) is None
    assert ti.engulfing([]) is None