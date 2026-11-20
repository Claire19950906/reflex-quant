"""market_snapshot 纯计算层用例（盘面数字 + 趋势判定）。

这些函数产出的是首页／信号台直接显示给用户的数字：7 日涨跌、ATR、RSI、ADX、VWAP、
量能比。它们错的方向只有两种，但都很难在界面上看出来：
- **样本不足时造数**（比如 8 根日线硬算 RSI14），看起来有值、其实是假的；
- **阈值口径漂移**（ret > 3% 才算 Bull），改一行判定，历史回看全部变形。

所以这里重点不是"函数能跑"，而是三条口径：样本不足一律 None / 趋势档位边界 /
TR 与 Wilder 平滑的算法本身。
"""

import pytest

import market_snapshot as ms


def _k(o, h, l, c, v=100.0):
    return [0, o, h, l, c, v]


def _series(closes, vol=100.0):
    return [_k(c, c + 0.5, c - 0.5, c, vol) for c in closes]


def _osc(n):
    """交替涨跌的 K 线：ADX 需要"有方向动作"才能算，纯平盘会直接返回 None。"""
    return [_k(100, 102, 99, 101) if i % 2 else _k(100, 101, 99, 100) for i in range(n)]


# ── 基础清洗 ──────────────────────────────────────────────────────────────

@pytest.mark.parametrize("val,expect", [(1.5, 1.5), ("2.5", 2.5), (3, 3.0)])
def test_safe_float_accepts_numbers_and_numeric_strings(val, expect):
    assert ms._safe_float(val) == pytest.approx(expect)


def test_safe_float_falls_back_on_garbage():
    assert ms._safe_float(None, -1.0) == -1.0
    assert ms._safe_float("abc", 7.0) == 7.0
    assert ms._safe_float(object(), 0.0) == 0.0


def test_extract_price_prefers_last_then_close():
    assert ms._extract_price({"last": 101.5, "close": 99.0}) == pytest.approx(101.5)
    assert ms._extract_price({"close": "99.2"}) == pytest.approx(99.2)
    assert ms._extract_price({"info": {"lastPrice": "88.8"}}) == pytest.approx(88.8)


def test_extract_price_falls_back_to_bid_ask_mid():
    # 没有成交价时才用买卖中间价，保留 4 位
    assert ms._extract_price({"bid": 100.0, "ask": 100.5}) == pytest.approx(100.25)
    assert ms._extract_price({}) is None


def test_basis_bps_sign_and_guard():
    # 升水为正、贴水为负，单位 bp
    assert ms._basis_bps({"mark": 101.0, "index": 100.0}) == pytest.approx(100.0)
    assert ms._basis_bps({"mark": 99.0, "index": 100.0}) == pytest.approx(-100.0)
    assert ms._basis_bps({"mark": 100.0}) is None
    assert ms._basis_bps({"mark": 100.0, "index": 0}) is None      # 除零保护


# ── 趋势判定 ──────────────────────────────────────────────────────────────

def test_period_stats_needs_two_candles():
    assert ms._compute_period_stats([])["trend"] == "Unknown"
    assert ms._compute_period_stats([_k(1, 1, 1, 1)])["trend"] == "Unknown"


def test_strong_bull_requires_consecutive_up_and_positive_return():
    s = ms._compute_period_stats(_series([100, 101, 102, 103, 104, 105]), consistency=5)
    assert s["trend"] == "Strong Bull" and s["trend_strength"] == "high"
    assert s["n_up"] == 5 and s["n_down"] == 0


def test_strong_bear_symmetry():
    s = ms._compute_period_stats(_series([105, 104, 103, 102, 101, 100]), consistency=5)
    assert s["trend"] == "Strong Bear" and s["trend_strength"] == "high"
    assert s["n_down"] == 5


def test_consecutive_up_but_net_down_is_not_strong_bull():
    # 前段大跌、尾段连涨 5 根，净涨幅仍为负 → 不能叫 Strong Bull
    s = ms._compute_period_stats(_series([200, 100, 101, 102, 103, 104]), consistency=5)
    assert s["trend"] != "Strong Bull"


def test_trend_threshold_bands():
    # +3% 以上 Bull、-3% 以下 Bear、1% 以内 Ranging，其余 Mild
    assert ms._compute_period_stats(_series([100, 100.5, 104]))["trend"] == "Bull"
    assert ms._compute_period_stats(_series([100, 99.5, 96]))["trend"] == "Bear"
    assert ms._compute_period_stats(_series([100, 100.2, 100.5]))["trend"] == "Ranging"
    assert ms._compute_period_stats(_series([100, 100.4, 102]))["trend"] == "Mild Bull"
    assert ms._compute_period_stats(_series([100, 99.6, 98]))["trend"] == "Mild Bear"


def test_period_stats_range_atr_and_return_fields():
    candles = [_k(100, 102, 98, 100), _k(100, 104, 100, 103)]
    s = ms._compute_period_stats(candles)
    assert s["return_pct"] == pytest.approx(3.0)
    assert s["return_str"] == "+3.00%"
    assert s["highest"] == pytest.approx(104) and s["lowest"] == pytest.approx(98)
    assert s["range_pct"] == pytest.approx(round((104 - 98) / 103 * 100, 2))
    # 第一根 TR = 4，第二根 TR = max(4, |104-100|, |100-100|) = 4
    assert s["atr_value"] == pytest.approx(4.0)


def test_zero_or_negative_prior_price_is_not_used():
    s = ms._compute_period_stats([_k(0, 0, 0, 0), _k(1, 2, 1, 2)])
    assert s["trend"] == "Unknown"          # 除零 → 空结构，不许算出 inf/NaN


# ── 技术指标：样本不足一律 None ───────────────────────────────────────────

def test_rsi_needs_15_closes():
    assert ms._rsi14([float(i) for i in range(14)]) is None
    assert ms._rsi14([float(i) for i in range(15)]) is not None


def test_rsi_extremes():
    assert ms._rsi14([float(i) for i in range(1, 40)]) == pytest.approx(100.0)   # 只涨
    assert ms._rsi14([10.0] * 30) == pytest.approx(50.0)                          # 全平


def test_rsi_down_only_approaches_zero():
    v = ms._rsi14([float(60 - i) for i in range(40)])
    assert v is not None and v < 5.0         # 单边下跌，RSI 贴 0


def test_adx_needs_29_candles():
    assert ms._adx14(_osc(28)) is None       # 少一根就不给数
    assert ms._adx14(_osc(29)) is not None


def test_adx_returns_none_when_no_directional_movement_at_all():
    # 完全平盘：没有 DM 就没有 DX，宁可 None 也不能编一个 0 出来
    assert ms._adx14(_series([100.0] * 40)) is None


def test_adx_single_direction_trend_is_extreme():
    assert ms._adx14(_series([100.0 + i for i in range(40)]) ) == pytest.approx(100.0)


def test_vwap_ignores_candles_without_volume():
    # volume 为 None 时按"无成交量"跳过，不能抛 TypeError 把整张快照打挂（v342）
    candles = [_k(1, 10, 8, 9, None), _k(1, 10, 8, 9, 0.0)]
    assert ms._window_vwap(candles) is None
    assert ms._window_vwap([_k(1, 10, 8, 9, 10.0)]) == pytest.approx(9.0)


def test_volume_ratio_window():
    candles = [_k(1, 1, 1, 1, 10.0) for _ in range(20)]
    candles.append(_k(1, 1, 1, 1, 30.0))
    assert ms._volume_ratio(candles) == pytest.approx(3.0)
    assert ms._volume_ratio(candles[:20]) is None              # 少一根就不给
    zero = [_k(1, 1, 1, 1, 0.0) for _ in range(21)]
    assert ms._volume_ratio(zero) is None                      # 前序全无量


def test_compute_indicators_shape_and_short_sample():
    short = ms._compute_indicators(_series([100.0] * 10))
    assert short["rsi"] is None and short["adx"] is None       # 不足样本 → None
    long = ms._compute_indicators(_series([100.0 + i for i in range(40)]))
    assert long["rsi"] == pytest.approx(100.0)
    assert long["vwap_pos"] in {"above", "below"}
    assert long["volume_ratio"] == pytest.approx(1.0)


def test_empty_indicators_when_no_candles():
    out = ms._compute_indicators([])
    assert out == {"rsi": None, "adx": None, "vwap": None, "vwap_pos": None, "volume_ratio": None}


# ── 7d 兼容层 ─────────────────────────────────────────────────────────────

def test_7d_stats_keeps_legacy_field_names():
    s = ms._compute_7d_stats(_series([100, 101, 102, 103, 104, 105]))
    assert s["price_7d_ago"] == pytest.approx(100)
    assert s["return_7d_pct"] == pytest.approx(5.0)
    assert s["n_up_days"] == 5
    assert s["trend"] == "Strong Bull"


def test_empty_7d_stats_never_fake_numbers():
    e = ms._empty_7d_stats()
    assert e["trend"] == "Unknown" and e["trend_strength"] == "none"
    assert e["return_7d_str"] == "N/A" and e["atr_str"] == "N/A"


# ── 展示格式 ──────────────────────────────────────────────────────────────

def test_format_helpers():
    assert ms._format_pct(1.5) == "+1.50%"
    assert ms._format_pct(-2.345, 1) == "-2.3%"
    assert ms._format_pct(0) == "+0.00%"                 # 0 也要带符号
    assert ms._format_price(12345.678) == "12,345.68"    # 千分位
    assert ms._format_price(99.5) == "99.50"
