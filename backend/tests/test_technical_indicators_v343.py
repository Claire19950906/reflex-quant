"""技术指标库 v343 增补 —— 趋势强度 / 筹码结构 / 博弈结构 / 波动情绪 / 状态读数。

每个函数验两件事：
  1. 性质（取值范围、单调性、方向）——不依赖具体实现细节；
  2. 边界（样本不足返回 None、绝不抛异常）——这是全库的硬约束。
"""

from __future__ import annotations

import os
import sys

SRC = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "src_python"))
if SRC not in sys.path:
    sys.path.insert(0, SRC)

import technical_indicators as ti


def _mk(closes, vol=1.0, spread=0.01, start_ts=1_700_000_000_000):
    """由收盘序列构造标准蜡烛 [ts, o, h, l, c, v]。"""
    out = []
    prev = closes[0]
    for i, c in enumerate(closes):
        o = prev
        out.append([start_ts + i * 3_600_000, o, max(o, c) + spread,
                    min(o, c) - spread, c, vol])
        prev = c
    return out


def _trend_closes(n=200, step=1.0, noise=0.5, base=100.0):
    closes = []
    for i in range(n):
        closes.append(base + i * step + (noise if i % 2 == 0 else -noise))
    return closes


# ---------------------------------------------------------------------------
# 统计工具
# ---------------------------------------------------------------------------

def test_percentile_rank_basic():
    hist = [float(i) for i in range(101)]
    assert ti.percentile_rank(50, hist) == 50.0
    assert ti.percentile_rank(0, hist) == 0.0
    assert ti.percentile_rank(100, hist) == 100.0


def test_percentile_rank_insufficient_and_none():
    assert ti.percentile_rank(5, []) is None
    assert ti.percentile_rank(5, [1.0]) is None
    assert ti.percentile_rank(None, [1.0, 2.0]) is None


def test_rsi_reference_value_from_wilder():
    """对照 Wilder《New Concepts》原始算例的首个 RSI 值（70.53）。

    原作者在表格里把平均涨跌幅四舍五入到三位小数，所以严格实现会得到 70.46；
    容差 0.2 足以拦住平滑方式写错（那类错误会让结果偏离几个点以上），
    又不会把一个正确的实现判成失败。
    """
    closes = [44.34, 44.09, 44.15, 43.61, 44.33, 44.83, 45.10, 45.42, 45.84, 46.08,
              45.89, 46.03, 45.61, 46.28, 46.28, 46.00, 46.03, 46.41, 46.22, 45.64]
    r = ti.rsi(closes, 14)
    assert abs(r[14] - 70.53) < 0.2
    assert abs(r[15] - 66.32) < 0.2


# ---------------------------------------------------------------------------
# 趋势强度
# ---------------------------------------------------------------------------

def test_adx_insufficient_returns_none():
    out = ti.adx(_mk([1.0, 2.0, 3.0]), period=14)
    assert out["adx"] == [None, None, None]
    assert out["plus_di"] == [None, None, None]


def test_adx_strong_uptrend_is_high_and_di_direction():
    closes = _trend_closes(n=200, step=1.0, noise=0.3)
    out = ti.adx(_mk(closes), period=14)
    adx_last = out["adx"][-1]
    assert adx_last is not None
    assert 25 <= adx_last <= 100
    assert out["plus_di"][-1] > out["minus_di"][-1]


def test_adx_strong_downtrend_di_flips():
    closes = _trend_closes(n=200, step=-1.0, noise=0.3)
    out = ti.adx(_mk(closes), period=14)
    assert out["minus_di"][-1] > out["plus_di"][-1]
    assert out["adx"][-1] > 25


def test_adx_flat_market_has_no_trend():
    closes = [100.0 + (0.2 if i % 2 == 0 else -0.2) for i in range(200)]
    out = ti.adx(_mk(closes), period=14)
    assert out["adx"][-1] is not None
    assert out["adx"][-1] < 20


def test_adx_bounds():
    closes = _trend_closes(n=120, step=1.0, noise=0.8)
    out = ti.adx(_mk(closes), period=14)
    for name in ("adx", "plus_di", "minus_di"):
        for v in out[name]:
            if v is not None:
                assert 0 <= v <= 100


def test_aroon_rising_closes_up_is_100():
    closes = [100.0 + i for i in range(40)]
    out = ti.aroon(_mk(closes), period=25)
    assert out["up"][-1] == 100.0
    assert out["down"][-1] < 20.0


def test_aroon_insufficient():
    out = ti.aroon(_mk([1.0, 2.0, 3.0]), period=25)
    assert out["up"] == [None, None, None]


def test_supertrend_direction_follows_trend():
    up = ti.supertrend(_mk(_trend_closes(120, 1.0, 0.4)), period=10, multiplier=3.0)
    down = ti.supertrend(_mk(_trend_closes(120, -1.0, 0.4)), period=10, multiplier=3.0)
    assert up["direction"][-1] == 1
    assert down["direction"][-1] == -1
    assert up["line"][-1] is not None


def test_supertrend_insufficient():
    out = ti.supertrend(_mk([1.0, 2.0]), period=10)
    assert out["direction"] == [None, None]


def test_donchian_position_extremes():
    rising = ti.donchian(_mk([100.0 + i for i in range(30)]), period=20)
    falling = ti.donchian(_mk([100.0 - i for i in range(30)]), period=20)
    # 通道上轨取 high、下轨取 low，收盘价永远贴不到极端，故用 0.99/0.01 而非等值
    assert rising["position"][-1] > 0.99
    assert falling["position"][-1] < 0.01
    assert falling["upper"][-1] > falling["lower"][-1]


def test_donchian_insufficient():
    out = ti.donchian(_mk([1.0, 2.0]), period=20)
    assert out["position"] == [None, None]


def test_ma_slope_sign_and_flat():
    up = ti.ma_slope([100.0 + i for i in range(40)])
    flat = ti.ma_slope([100.0 for _ in range(40)])
    assert up > 0
    assert abs(flat) < 1e-9
    assert ti.ma_slope([100.0, 101.0]) is None


# ---------------------------------------------------------------------------
# 筹码结构
# ---------------------------------------------------------------------------

def test_volume_profile_poc_tracks_volume_concentration():
    # 30 根在 100 附近量 1，10 根在 105 附近量 50 → POC 必须落在 105 一带
    candles = []
    ts = 1_700_000_000_000
    for i in range(30):
        p = 100.0 + (i % 3) * 0.1
        candles.append([ts + i * 3_600_000, p, p, p, p, 1.0])
    for i in range(10):
        p = 105.0 + (i % 3) * 0.1
        candles.append([ts + (30 + i) * 3_600_000, p, p, p, p, 50.0])
    candles.append([ts + 40 * 3_600_000, 110.0, 110.0, 110.0, 110.0, 1.0])
    vp = ti.volume_profile(candles, lookback=41, bins=24)
    assert vp["poc"] is not None
    assert 104.0 <= vp["poc"] <= 106.0
    assert vp["val"] <= vp["poc"] <= vp["vah"]
    assert vp["position"] == "above_value"


def test_volume_profile_insufficient():
    vp = ti.volume_profile(_mk([1.0, 2.0, 3.0]))
    assert vp["poc"] is None and vp["position"] is None


def test_swing_structure_higher_highs_higher_lows():
    closes = ([100, 101, 102, 103, 102, 101, 100, 101, 102, 103, 104, 105, 104, 103, 102,
               103, 104, 105, 106, 107, 106, 105, 104, 105, 106, 107, 108, 109])
    sw = ti.swing_structure(_mk([float(x) for x in closes]))
    assert sw["structure"] == "HH_HL"
    assert sw["trend"] == "up"


def test_swing_structure_lower_highs_lower_lows():
    closes = [float(250 - x) for x in
              [100, 101, 102, 103, 102, 101, 100, 101, 102, 103, 104, 105, 104, 103, 102,
               103, 104, 105, 106, 107, 106, 105, 104, 105, 106, 107, 108, 109]]
    sw = ti.swing_structure(_mk(closes))
    assert sw["structure"] == "LH_LL"
    assert sw["trend"] == "down"


def test_swing_structure_too_few_swings():
    sw = ti.swing_structure(_mk([100.0, 101.0, 102.0]))
    assert sw["structure"] is None


# ---------------------------------------------------------------------------
# 博弈结构
# ---------------------------------------------------------------------------

def test_oi_price_quadrant_four_cases():
    assert ti.oi_price_quadrant(2.0, 6.0)["label"] == "增仓上涨"
    assert ti.oi_price_quadrant(2.0, -6.0)["label"] == "减仓上涨"
    assert ti.oi_price_quadrant(-2.0, 6.0)["label"] == "增仓下跌"
    assert ti.oi_price_quadrant(-2.0, -6.0)["label"] == "减仓下跌"
    assert ti.oi_price_quadrant(0.1, 0.2)["label"] == "双向观望"
    assert ti.oi_price_quadrant(None, 5.0)["label"] is None


def test_oi_price_quadrant_bias_semantics():
    # 减仓上涨是"弱多"（空头回补），不能和增仓上涨同权
    assert ti.oi_price_quadrant(2.0, 6.0)["bias"] == "bull"
    assert ti.oi_price_quadrant(2.0, -6.0)["bias"] == "bull_weak"
    assert ti.oi_price_quadrant(-2.0, -6.0)["bias"] == "bear_weak"


def test_positioning_divergence_retail_chasing():
    out = ti.positioning_divergence(1.2, 2.6)
    assert out["label"] is not None
    assert out["bias"] == "bear"       # 散户追多、大户克制 → 反向提示
    assert out["gap_pct"] > 0


def test_positioning_divergence_whale_long():
    out = ti.positioning_divergence(2.2, 1.3)
    assert out["bias"] == "bull"
    assert ti.positioning_divergence(None, 1.3)["label"] is None


def test_taker_pressure_levels():
    assert ti.taker_pressure(70.0, 30.0)["label"] == "强主动买"
    assert ti.taker_pressure(55.0, 45.0)["label"] == "主动买占优"
    assert ti.taker_pressure(50.0, 50.0)["label"] == "主动成交均衡"
    assert ti.taker_pressure(45.0, 55.0)["label"] == "主动卖占优"
    assert ti.taker_pressure(30.0, 70.0)["label"] == "强主动卖"
    assert ti.taker_pressure(0.0, 0.0)["label"] is None


def test_cvd_price_divergence_cases():
    up_cvd_down = ti.cvd_price_divergence([10, 8, 6, 4], [100, 102, 104, 106])
    assert up_cvd_down["divergence"] == "bearish"
    down_cvd_up = ti.cvd_price_divergence([4, 6, 8, 10], [106, 104, 102, 100])
    assert down_cvd_up["divergence"] == "bullish"
    aligned = ti.cvd_price_divergence([4, 6, 8, 10], [100, 102, 104, 106])
    assert aligned["divergence"] == "confirmed"


def test_cvd_price_divergence_flat_and_misaligned():
    assert ti.cvd_price_divergence([1, 2], [100, 100.05])["divergence"] is None  # 幅度不足
    assert ti.cvd_price_divergence([1, 2, 3], [100, 101])["divergence"] is None  # 长度不一致


# ---------------------------------------------------------------------------
# 波动 / 情绪
# ---------------------------------------------------------------------------

def test_realized_vol_constant_and_insufficient():
    assert ti.realized_vol(_mk([100.0] * 30))[-1] == 0.0
    assert ti.realized_vol(_mk([100.0, 101.0]))[-1] is None


def test_realized_vol_scales_with_volatility():
    calm = ti.realized_vol(_mk([100.0 + (0.5 if i % 2 == 0 else -0.5) for i in range(60)]))[-1]
    wild = ti.realized_vol(_mk([100.0 + (5.0 if i % 2 == 0 else -5.0) for i in range(60)]))[-1]
    assert wild > calm > 0


def test_bollinger_width_flat_is_zero():
    assert ti.bollinger_width([100.0] * 30)[-1] == 0.0
    assert ti.bollinger_width([100.0, 101.0])[-1] is None


def test_rsi_stretch_counts_consecutive_bars():
    up = ti.rsi_stretch([100.0 + i for i in range(60)])
    assert up["side"] == "overbought"
    assert up["bars"] >= 1
    down = ti.rsi_stretch([100.0 - i for i in range(60)])
    assert down["side"] == "oversold"


# ---------------------------------------------------------------------------
# 市场状态聚合
# ---------------------------------------------------------------------------

def _state_candles():
    return _mk(_trend_closes(160, 1.0, 0.4, base=100.0))


def test_market_state_full_readout():
    st = ti.market_state(
        _state_candles(), price_change_pct=3.0, oi_change_pct=8.0, funding_rate=0.03,
        funding_history=[0.005] * 90 + [0.03] * 10,
        top_long_short_ratio=1.4, retail_long_short_ratio=2.6,
        taker_buy=55.0, taker_sell=45.0,
        cvd=[10, 12, 14, 16, 18], interval_sec=14_400,
    )
    assert st["trend"]["stage"] is not None
    assert st["leverage"]["label"] == "增仓上涨"
    assert st["flow"]["taker"]["label"] == "主动买占优"
    assert st["positioning"]["bias"] == "bear"
    assert st["readout"] and "增仓上涨" in st["readout"]
    assert st["missing"] == []


def test_market_state_missing_dimensions_not_fabricated():
    st = ti.market_state(_state_candles())
    # 没有 OI / 资金费率 / 主动成交 / 持仓数据时，维度进 missing，读数为 None 而不是编一个
    assert "leverage(OI)" in st["missing"]
    assert "taker" in st["missing"]
    assert "positioning" in st["missing"]
    assert st["leverage"]["label"] is None
    assert st["flow"]["taker"]["label"] is None


def test_market_state_price_change_derived_from_candles():
    st = ti.market_state(_state_candles(), oi_change_pct=5.0)
    assert st["leverage"]["price_change_window"].endswith("根")
    assert st["leverage"]["price_change_pct"] is not None


def test_market_state_retail_only_positioning():
    # 黄金这类只有券商客户多空比的标的：给读数但标注"缺大户对照"，不冒充完整分歧判断
    st = ti.market_state(_state_candles(), retail_long_short_ratio=0.72)
    assert any("仅散户多空比" in x for x in [st["readout"] or ""])
    assert "positioning(top)" in st["missing"]


def test_market_state_insufficient_returns_error():
    assert "error" in ti.market_state(_mk([100.0]))


def test_market_state_value_area_and_volatility_present():
    st = ti.market_state(_state_candles())
    assert st["value_area"]["position"] in ("above_value", "in_value", "below_value")
    assert st["volatility"]["state"] in ("收缩", "常态", "扩张")


# ---------------------------------------------------------------------------
# 注册表
# ---------------------------------------------------------------------------

def test_new_indicators_registered():
    names = {x["name"] for x in ti.list_indicators()}
    for n in ("adx", "aroon", "supertrend", "donchian", "ma_slope", "volume_profile",
              "swing_structure", "realized_vol", "bollinger_width", "rsi_stretch",
              "percentile_rank", "oi_price_quadrant", "positioning_divergence",
              "taker_pressure", "cvd_price_divergence", "market_state"):
        assert n in names


def test_pipeline_runs_market_state():
    out = ti.IndicatorPipeline().add("market_state").run(_state_candles())
    assert "readout" in out["market_state"]
