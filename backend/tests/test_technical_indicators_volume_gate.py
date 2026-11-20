"""technical_indicators 的补充用例：成交量覆盖率门 + 没人测过的几个函数。

2026-11-20: 原 `test_technical_indicators.py` 已覆盖 RSI/MACD/BOLL/ATR/吞没 等主干，
但 v480 那次"成交量覆盖率门"没有用例守着——而那正是最近一次静默错数的来源：

  · `volumes()` 用 `_clean` 压缩空值，返回稀疏序列；OBV 按 `range(1, len(candles))`
    索引 → XAU（2623 根、只有 120 个有效量）直接 IndexError；
  · MFI / vol_zscore 却会用 120 个跨十年的稀疏点算出"看起来正常"的数
    （实测 XAU MFI=24.14，被下游读成"超卖"）。

静默给错数比崩更贵。所以这里钉住：覆盖率不足 80% 时，量价指标一律不给数。
"""

import pytest

import technical_indicators as ti


def _c(o, h, l, c, v=None, ts=0):
    return [ts, o, h, l, c, v]


def _flat(n=100, price=100.0, vol=10.0):
    return [_c(price, price + 0.5, price - 0.5, price, vol, i) for i in range(n)]


def _trend(n=100, start=100.0, step=1.0, vol=10.0):
    return [_c(start + i * step, start + i * step + 1, start + i * step - 1,
               start + i * step + 0.5, vol, i) for i in range(n)]


# ── 取样工具 ──────────────────────────────────────────────────────────────

def test_f_clean_helpers():
    assert ti._f(None) is None and ti._f("") is None
    assert ti._f("1.5") == 1.5 and ti._f("abc") is None
    assert ti._clean([1, None, "2", "x"]) == [1.0, 2.0]


def test_extractors_ignore_dict_candles_but_ohlcv_handles_them():
    # 位置取值函数（closes/highs/lows/volumes）只认 list/tuple；
    # 需要吃 dict/对象的路径统一走 _ohlcv()，别指望这里静默兼容。
    candles = [_c(10, 12, 9, 11, 5), {"high": 20, "low": 18, "close": 19, "volume": 7}]
    assert ti.closes(candles) == [11.0]
    assert ti.highs(candles) == [12.0]
    assert ti.volumes(candles) == [5.0]
    assert ti._ohlcv(candles[1]) == (20.0, 18.0, 19.0, 7.0)


def test_volumes_full_keeps_length_and_maps_zero_to_none():
    candles = [_c(10, 11, 9, 10, None), _c(10, 11, 9, 10, 0), _c(10, 11, 9, 10, 5)]
    assert ti.volumes_full(candles) == [None, None, 5.0]


# ── 覆盖率门 ──────────────────────────────────────────────────────────────

def test_volume_availability_blocks_sparse_volume():
    candles = _flat(100, vol=None)
    for i in range(5):
        candles[i] = _c(100, 100.5, 99.5, 100, 10.0, i)
    avail = ti.volume_availability(candles)
    assert avail["available"] is False
    assert avail["n_valid"] == 5 and avail["n"] == 100
    assert avail["coverage_pct"] == 5.0
    assert "不计算" in (avail["note"] or "")


def test_volume_availability_accepts_dense_volume():
    avail = ti.volume_availability(_flat(100, vol=10.0))
    assert avail["available"] is True
    assert avail["coverage_pct"] == 100.0
    assert avail["note"] is None


def test_volume_availability_empty_input():
    assert ti.volume_availability([])["available"] is False


def test_pipeline_skip_reason_only_gates_volume_indicators():
    sparse = _flat(100, vol=None)
    reason = ti.pipeline_skip_reason("obv", sparse)
    assert reason is not None and reason["skipped"] == "no_volume"
    assert ti.pipeline_skip_reason("obv", _flat(100, vol=10.0)) is None
    assert ti.pipeline_skip_reason("rsi", sparse) is None      # 价格类不受影响


# ── 量价指标在门内外的行为 ────────────────────────────────────────────────

def test_obv_length_matches_candles_even_with_sparse_volume():
    # 旧实现这里会 IndexError
    candles = _flat(60, vol=None)
    for i in range(6):
        candles[i] = _c(100 + i, 101, 99, 100 + i, 10.0, i)
    obv = ti.obv(candles)
    assert len(obv) == 60
    assert all(v is None for v in obv)


def test_obv_accumulates_in_dense_volume():
    obv = ti.obv(_trend(10, step=1.0, vol=100.0))
    assert len(obv) == 10
    assert obv[0] == 0.0
    assert obv[-1] > 0


def test_obv_empty_returns_empty():
    assert ti.obv([]) == []


def test_mfi_and_vol_zscore_refuse_sparse_volume():
    sparse = _flat(60, vol=None)
    assert all(v is None for v in ti.mfi(sparse))
    assert ti.vol_zscore(sparse) is None


def test_mfi_bounds_in_dense_volume():
    vals = ti.mfi(_trend(60), 14)
    assert len(vals) == 60
    assert all(0 <= v <= 100 for v in vals if v is not None)


def test_vwap_refuses_sparse_volume():
    assert all(v is None for v in ti.vwap(_flat(60, vol=None)))


# ── 之前没人测的几个 ─────────────────────────────────────────────────────

def test_candle_pattern_doji_hammer_shooting_star():
    assert ti.candle_pattern(_c(10, 11, 9, 10.05)) == "doji"
    assert ti.candle_pattern(_c(10.5, 10.8, 9.0, 10.7)) == "hammer"   # 下影 1.5 ≥ 2×实体 0.2
    assert ti.candle_pattern(_c(10.0, 12.6, 9.9, 10.6)) == "shooting_star"  # 上影 2.0 ≥ 2×实体
    assert ti.candle_pattern(_c(10, 10, 10, 10)) is None       # h==l 无区间
    assert ti.candle_pattern("not a candle") is None


def test_atr_break_needs_history_and_threshold():
    assert ti.atr_break(_flat(20)) is False                     # 样本不足
    calm = _flat(60)
    assert ti.atr_break(calm) is False                          # 无扩张
    jumpy = _flat(40) + [_c(100, 140, 60, 100, 10.0, i) for i in range(20)]
    assert ti.atr_break(jumpy) is True                          # 末段波动放大


def test_ema_and_sma_edge_cases():
    assert ti.ema([], 12) == []
    assert ti.ema([5.0, 6.0, 7.0], 2)[0] == 5.0                 # 首值 = 输入首值
    assert ti.sma([1, 2], 5) == [None, None]
    assert ti.sma([1, 2], 0) == [None, None]
    # 常数序列的均线必须等于常数（最容易抓出手滑的恒等式）
    assert ti.sma([10.0] * 30, 20)[-1] == pytest.approx(10.0)


def test_ma_stack_short_series_flags_none():
    assert ti.ma_stack([1.0] * 10)["bull_aligned"] is None


def test_bollinger_state_constant_zero_bandwidth():
    st = ti.bollinger_state([10.0] * 30)
    assert st["percent_b"] == 0.5            # up==lo → 0.5，不除零
    assert st["bandwidth"] == pytest.approx(0.0)


def test_macd_zero_cross_short_and_flat():
    assert ti.macd_zero_cross([1.0]) is None
    assert ti.macd_zero_cross([10.0] * 60) is None
