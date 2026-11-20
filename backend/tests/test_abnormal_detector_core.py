"""abnormal_detector 的核心判定用例（异动触发 + 因子家族投票）。

2026-11-20: 这个模块 904 条语句、基本没有单测。它是信号的第一道闸：
"这一根 K 线算不算异动" 判错，后面所有归因都在解释一个不存在的现象。

两组重点：
1. **阈值自适应**：3m 1.5%/1.4×、5m 1.2%/1.3×、10m 1.1%/2.0×、1h 2.0%/1.5×。
   同一个 1.6% 在 10m 算异动、在 1h 不算——写死一个阈值就会两种周期都错。
2. **按家族投票**（v318）：RSI/KDJ/MFI 是同一段价格算出来的，三个同时触发不等于
   "三份独立证据"。原实现按因子个数计票，同族能把方向灌满（线上实测三族命中 9.6 万次）。
   现在同族只算一票，平手时才用族内净票强度破平。
"""

import pytest

import abnormal_detector as ad


def _c(o, h, l, c, v=None, ts=0):
    return [ts, o, h, l, c, v]


# ── 基础换算 ──────────────────────────────────────────────────────────────

def test_pct_change_guard_conditions():
    assert ad._pct_change(100, 102) == pytest.approx(2.0)
    assert ad._pct_change(None, 102) is None
    assert ad._pct_change(0, 102) is None          # 除零保护
    assert ad._pct_change(100, None) is None


def test_candle_parts_pads_missing_columns():
    assert ad._candle_parts([1, 2, 3]) == [1.0, 2.0, 3.0, None, None, None]
    assert ad._candle_parts("not a list") is None


def test_vol_ratio_needs_history_and_positive_volume():
    candles = [[None, 1, 1, 1, 1, 10] for _ in range(5)]
    candles[-1] = [None, 1, 1, 1, 1, 30]
    assert ad._vol_ratio(candles, 4) == pytest.approx(3.0)
    assert ad._vol_ratio(candles, 1) is None                  # 前序不足 2 根
    zeros = [[None, 1, 1, 1, 1, 0] for _ in range(5)]
    assert ad._vol_ratio(zeros, 4) is None                    # 前序无量 → 不给数


# ── 阈值自适应 ────────────────────────────────────────────────────────────

@pytest.mark.parametrize("interval,pct,vol", [(180, 1.5, 1.4), (300, 1.2, 1.3),
                                              (600, 1.1, 2.0), (3600, 2.0, 1.5)])
def test_interval_thresholds_match_table(interval, pct, vol):
    got = ad._interval_thresholds(interval)
    assert got[0] == pytest.approx(pct)
    assert got[1] == pytest.approx(vol)


def test_interval_thresholds_fallback_to_3m():
    assert ad._interval_thresholds(12345)[0] == pytest.approx(1.5)


def test_same_move_is_abnormal_on_10m_but_not_on_1h():
    # 1.6% 在 10m（门槛 1.1%）算异动；在 1h（门槛 2.0%）不算
    assert ad._classify(1.6, 1.0, 600)["triggered"] is True
    assert ad._classify(1.6, 1.0, 3600)["triggered"] is False


def test_classify_three_trigger_types():
    assert ad._classify(1.6, 1.0, 180)["trigger_type"] == "pct"
    assert ad._classify(0.2, 2.0, 180)["trigger_type"] == "volume"
    both = ad._classify(1.6, 2.0, 180)
    assert both["trigger_type"] == "both" and both["pct_hit"] and both["vol_hit"]
    assert ad._classify(None, None, 180)["triggered"] is False


def test_classify_direction_uses_absolute_pct():
    # 跌 1.6% 一样算异动（方向由下游判，闸门只看幅度）
    assert ad._classify(-1.6, 1.0, 180)["pct_hit"] is True


def test_describe_labels():
    assert "涨跌 +1.60%" in ad._describe(0, 1.6, None, "pct")
    assert "量比 2.00×" in ad._describe(0, None, 2.0, "volume")
    assert "连摆" in ad._describe(0, 1.0, None, "swing")
    assert "4根连动" in ad._describe(0, 1.0, None, "cum4")
    assert ad._describe(0, None, None, "") == "未触发异动"


# ── 主检测 ────────────────────────────────────────────────────────────────

def test_detect_abnormal_insufficient_candles():
    out = ad.detect_abnormal([_c(100, 101, 99, 100.5)])
    assert out["triggered"] is False
    assert out["detail"] == "K 线样本不足"
    assert out["anchor_ts"] is None


def test_detect_abnormal_triggers_on_pct():
    candles = [_c(100, 100.4, 99.8, 100, 10, i) for i in range(20)]
    candles.append(_c(100, 102.5, 99.9, 102.4, 10, 20))     # 单根 +2.4%
    out = ad.detect_abnormal(candles, 180)
    assert out["triggered"] is True
    assert out["trigger_type"] == "pct"
    assert out["pct_change"] == pytest.approx(2.4, abs=0.01)
    assert out["anchor_ts"] == 20                            # 时间错位就会归因到错的事件


def test_detect_abnormal_not_triggered_stays_quiet():
    candles = [_c(100, 100.2, 99.9, 100.1, 10, i) for i in range(20)]
    out = ad.detect_abnormal(candles, 180)
    assert out["triggered"] is False and out["anchor_ts"] is None


# ── RSI 触发 ──────────────────────────────────────────────────────────────

def test_rsi_trip_directions():
    up = ad.rsi_trip([float(i) for i in range(1, 40)])
    assert up["triggered"] is True and up["direction"] == "bearish"   # 超买=动能衰竭
    down = ad.rsi_trip([float(40 - i) for i in range(40)])
    assert down["triggered"] is True and down["direction"] == "bullish"
    flat = ad.rsi_trip([10.0] * 30)
    assert flat["triggered"] is False and flat["direction"] is None


def test_rsi_trip_short_series_is_safe():
    out = ad.rsi_trip([1.0, 2.0])
    assert out == {"triggered": False, "rsi": None, "direction": None}


# ── 因子家族（v318 核心）─────────────────────────────────────────────────

@pytest.mark.parametrize("name,family", [
    ("RSI超买", "oscillator"), ("KDJ死叉", "oscillator"), ("MFI超卖", "oscillator"),
    ("MACD金叉", "trend"), ("MA5上穿", "trend"),
    ("布林上轨", "volatility"), ("ATR突破", "volatility"),
    ("量比放大", "volume"), ("放量突破", "volume"),
    ("看涨吞没", "candlestick"), ("十字星", "candlestick"),
    ("单根大阳", "price"),
])
def test_factor_family_mapping(name, family):
    assert ad.factor_family(name) == family


def test_factor_family_unknown_is_its_own_family():
    assert ad.factor_family("某个新因子") == "other:某个新因子"
    assert ad.factor_family("") == "other:"


def test_family_vote_beats_factor_count():
    # 旧口径：oscillator 三票 vs trend 一票 → bullish？现在同族只算一票
    out = ad.resolve_family_direction({"oscillator": -3, "trend": 1, "volume": 1})
    assert out["bull_votes"] == 2 and out["bear_votes"] == 1
    assert out["direction"] == "bullish"
    # 族内抵消记 mixed：算一份独立证据，但不参与方向
    out2 = ad.resolve_family_direction({"oscillator": 0, "trend": 1})
    assert out2["family_votes"]["oscillator"] == "mixed"
    assert out2["direction"] == "bullish"


def test_family_tie_breaks_by_net_strength():
    # RSI+KDJ+MFI+钝化 净 -4 vs MA 金叉 净 +1 → 仍判 bearish
    out = ad.resolve_family_direction({"oscillator": -4, "trend": 1})
    assert out["bull_votes"] == 1 and out["bear_votes"] == 1
    assert out["direction"] == "bearish"


def test_family_all_flat_is_undecided():
    out = ad.resolve_family_direction({"oscillator": 0, "trend": 0})
    assert out["direction"] is None
    assert out["families"] == ["oscillator", "trend"]


def test_attribution_hint_mentions_direction_label():
    hint = ad._attribution_hint(["RSI超买", "量比放大"], "bearish")
    assert "技术面异常签名已触发" in hint
    assert "RSI超买、量比放大" in hint
    assert "净信号" in hint
    assert "方向未明" in ad._attribution_hint(["X"], None)
