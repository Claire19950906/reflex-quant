"""v412：风险档位（仓位系数 + 止损 ATR 倍数）——执行建议，不参与方向分。

依据：v411 实测「5 根内最大不利偏移 ÷ 当时 ATR%」的中位比值 —— 日线 ≈0.42~0.52、
4h ≈0.48~1.32。所以止损放在 1.5 倍 ATR 以内会被正常波动打掉，档位必须是 ATR 倍数。
"""
import json
import os
import sqlite3
import sys

SRC = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "src_python"))
if SRC not in sys.path:
    sys.path.insert(0, SRC)

import technical_state as ts
import decision_guard


def _state(**blocks):
    base = {"trend": {}, "volatility": {}, "value_area": {}, "leverage": {},
            "flow": {}, "positioning": {}, "_meta": {"timeframe": "4h"}}
    base.update(blocks)
    return base


# ---------------------------------------------------------------------------
# 档位判定
# ---------------------------------------------------------------------------

def test_calm_state_is_low_risk():
    """v414：4h 的 ATR 1.0% 以下 = 低波动档；日内档位不缩仓位。"""
    st = _state(volatility={"atr_pct": 0.8, "bb_width_percentile": 40.0,
                            "realized_vol_percentile": 45.0},
                trend={"adx": 15.0}, value_area={"position": "in_value"},
                leverage={"label": None, "crowding": None})
    r = ts.risk_profile(st, "4h")
    assert r["available"] and r["level"] == "低"
    assert r["size_factor"] == 1.0 and r["stop_atr_multiple"] == 2.0
    assert r["size_basis"] == "uniform_intraday"


def test_extreme_state_high_volatility_but_no_size_cut_on_4h():
    """v414：4h ATR≥2% 记为高波动档，但**仓位不缩**（实测 4h 缩仓无依据）。"""
    st = _state(volatility={"atr_pct": 2.4, "bb_width_percentile": 80.3,
                            "realized_vol_percentile": 84.8},
                trend={"adx": 59.1}, value_area={"position": "below_value"},
                leverage={"label": "减仓下跌", "crowding": "空头拥挤"})
    r = ts.risk_profile(st, "4h")
    assert r["level"] == "高"
    assert r["size_factor"] == 1.0 and r["stop_atr_multiple"] == 2.0
    assert len(r["drivers"]) >= 4
    assert "4h" in r["validation_note"]


def test_daily_mid_band():
    """v414：日线 1.5% ≤ ATR < 3% = 中档 → 0.8×、2 倍 ATR。"""
    st = _state(volatility={"atr_pct": 2.2}, trend={"adx": 25.0},
                value_area={"position": "in_value"}, leverage={})
    r = ts.risk_profile(st, "1d")
    assert r["level"] == "中"
    assert r["size_factor"] == 0.8 and r["stop_atr_multiple"] == 2.0
    assert r["size_basis"] == "atr_band_daily"


def test_adverse_hint_uses_measured_multiple_per_timeframe():
    """日线实测 0.45 倍 ATR、4h 0.9 倍 —— 同为 ATR 2%，两者的"常见逆风"不该一样。"""
    st = _state(volatility={"atr_pct": 2.0, "bb_width_percentile": 40.0})
    daily = ts.risk_profile(st, "1d")
    h4 = ts.risk_profile(st, "4h")
    assert daily["expected_adverse_pct"] == round(2.0 * 0.45, 2)
    assert h4["expected_adverse_pct"] == round(2.0 * 0.9, 2)


def test_error_state_has_no_risk_profile():
    r = ts.risk_profile({"error": "no candles"})
    assert r["available"] is False


def test_stop_is_atr_multiple_not_fixed_percent():
    """日线高档：止损 3 × ATR（跟着波动走），不是固定百分比。"""
    st = _state(volatility={"atr_pct": 4.0},
                trend={"adx": 40.0}, value_area={"position": "below_value"},
                leverage={"label": "减仓下跌"})
    r = ts.risk_profile(st, "1d")
    assert r["level"] == "高"
    assert r["stop_atr_multiple"] == 3.0
    assert r["size_factor"] == 0.5


# ---------------------------------------------------------------------------
# 接进 build_state / 闸门
# ---------------------------------------------------------------------------

def _mk_db(tmp_path):
    db = tmp_path / "mini.db"
    conn = sqlite3.connect(db)
    conn.executescript("""
        CREATE TABLE kline_bars (symbol TEXT, timeframe TEXT, ts INTEGER,
            open REAL, high REAL, low REAL, close REAL, volume REAL);
        CREATE TABLE external_indicators (asset TEXT, indicator TEXT, ts INTEGER, value REAL);
        CREATE TABLE macro_snapshots (metric_key TEXT, ts INTEGER, value REAL);
    """)
    now = 1_760_000_000_000
    step = 14400 * 1000
    px = 100.0
    for i in range(240):
        px *= 0.99 if i > 200 else 1.0
        conn.execute("INSERT INTO kline_bars VALUES ('BTC','4h',?,?,?,?,?,?)",
                     (now - (239 - i) * step, px * 1.002, px * 1.01, px * 0.99, px, 100.0))
    conn.commit()
    return conn


def test_build_state_carries_risk(tmp_path):
    state = ts.build_state(_mk_db(tmp_path), "BTC", "4h", bars=240)
    assert "risk" in state and state["risk"]["available"]
    assert state["risk"]["level"] in ("低", "中", "高")


def test_guard_exposes_risk_plan_without_touching_score():
    """同样的输入，带不带风险档位，final_score 必须一致——风险档位不许改分。"""
    ctx_plain = json.dumps({"assets": {"BTC": {"status": "ok", "change_24h_pct": 1.0,
                                               "stats_7d": {"atr_pct": 3.0}}}})
    high_risk = {"level": "高", "size_factor": 0.5, "stop_atr_multiple": 3.0,
                 "atr_pct": 5.0, "expected_adverse_pct": 4.5,
                 "drivers": ["波动扩张"], "note": "高风险"}
    ctx_risk = json.dumps({"assets": {"BTC": {"status": "ok", "change_24h_pct": 1.0,
                                              "stats_7d": {"atr_pct": 3.0},
                                              "technical_state": {"available": True,
                                                                  "bias": 0.0, "coverage": 0.8,
                                                                  "usable": True, "risk": high_risk}}}})
    row_plain = {"sentiment_score": 0.5, "target_asset": "BTC", "decision_context": ctx_plain}
    row_risk = {"sentiment_score": 0.5, "target_asset": "BTC", "decision_context": ctx_risk}
    a = decision_guard.evaluate_decision(row_plain)
    b = decision_guard.evaluate_decision(row_risk)
    assert a["final_score"] == b["final_score"]
    assert b["risk_plan"]["available"] is True
    assert b["risk_plan"]["level"] == "高"
    assert b["risk_plan"]["size_factor"] == 0.5
    assert b["risk_plan"]["stop_pct_hint"] == 15.0       # 5.0% ATR × 3.0


def test_guard_risk_plan_absent_when_state_has_none():
    out = decision_guard.evaluate_decision({
        "sentiment_score": 0.3, "target_asset": "BTC",
        "decision_context": json.dumps({"assets": {"BTC": {"status": "ok"}}}),
    })
    assert out["risk_plan"]["available"] is False


# ---------------------------------------------------------------------------
# HOLD 的"不参与理由"（v412 第二轮）
# ---------------------------------------------------------------------------

def _ctx_with_risk(level="高", adverse=1.78):
    return json.dumps({"assets": {"BTC": {
        "status": "ok", "change_24h_pct": -5.0, "stats_7d": {"atr_pct": 1.98},
        "technical_state": {
            "available": True, "bias": -0.33, "coverage": 0.82, "usable": True,
            "readout": "强趋势·空头主导",
            "risk": {"level": level, "size_factor": 0.5, "stop_atr_multiple": 3.0,
                     "atr_pct": 1.98, "expected_adverse_pct": adverse,
                     "drivers": ["波动扩张"], "note": "高风险"},
        },
    }}})


def test_hold_reason_says_odds_are_unfavorable():
    """预期空间打不过常见逆风 → 这必须是"不参与"的第一条理由。"""
    out = decision_guard.evaluate_decision({
        "sentiment_score": 0.15, "target_asset": "BTC", "suggested_action": "BUY",
        "decision_context": _ctx_with_risk(),
        "impact_curve": json.dumps({"1d": 1.2}), "expected_horizon": "1-3d",
    })
    assert out["action"] == "HOLD"
    reason = out["hold_reason"]
    assert reason["applicable"] is True
    assert reason["headline"].startswith("HOLD：")
    codes = [r["code"] for r in reason["reasons"]]
    assert "odds_unfavorable" in codes
    assert "赔率不合算" in reason["headline"]


def test_hold_reason_flags_gap_to_threshold():
    out = decision_guard.evaluate_decision({
        "sentiment_score": 0.0, "target_asset": "BTC",
        "decision_context": json.dumps({"assets": {"BTC": {"status": "ok"}}}),
    })
    reason = out["hold_reason"]
    assert reason["applicable"] is True
    assert reason["nearest_direction"] in ("BUY", "SELL")
    assert reason["gap_to_threshold"] > 0
    assert any(r["code"] == "score_below_threshold" for r in reason["reasons"])
    assert reason["check_next"]


def test_hold_reason_absent_when_action_is_buy_or_sell():
    ctx = json.dumps({"assets": {"BTC": {"status": "ok", "change_24h_pct": 2.0,
                                        "stats_7d": {"atr_pct": 1.0}}}})
    out = decision_guard.evaluate_decision({
        "sentiment_score": 0.95, "target_asset": "BTC", "decision_context": ctx,
    })
    assert out["action"] in ("BUY", "SELL")
    assert out["hold_reason"] == {"applicable": False}


# ---------------------------------------------------------------------------
# v414：档位由 ATR 绝对百分比决定（打分只留作解释；日内不缩仓位）
# ---------------------------------------------------------------------------

def test_score_does_not_determine_level():
    """打分再高，只要 ATR 在低档区间，仓位就不该被砍（打分是滞后指标）。"""
    st = _state(volatility={"atr_pct": 1.2, "bb_width_percentile": 95.0,
                            "realized_vol_percentile": 95.0},
                trend={"adx": 60.0}, value_area={"position": "below_value"},
                leverage={"label": "减仓下跌", "crowding": "空头拥挤"})
    r = ts.risk_profile(st, "1d")
    assert r["score"] >= 5          # 打分照样说"这局面看起来险"
    assert r["level"] == "低"       # 但仓位只跟波动尺度走
    assert r["size_factor"] == 1.0


def test_daily_high_band_cuts_size():
    st = _state(volatility={"atr_pct": 4.0}, trend={"adx": 20.0},
                value_area={"position": "in_value"}, leverage={})
    r = ts.risk_profile(st, "1d")
    assert r["level"] == "高"
    assert r["size_factor"] == 0.5 and r["stop_atr_multiple"] == 3.0


def test_intraday_never_cuts_size():
    """4h 上 ATR 分档实测无区分力 → 任何档位都不缩仓，只把止损放 2 倍 ATR。"""
    for atr in (0.6, 1.5, 3.5):
        r = ts.risk_profile(_state(volatility={"atr_pct": atr}), "4h")
        assert r["size_factor"] == 1.0
        assert r["stop_atr_multiple"] == 2.0
        assert r["size_basis"] == "uniform_intraday"


def test_risk_carries_validation_scope():
    """档位必须随身带"哪个周期验过"，否则下游会把 4h 档位当已验证口径用。"""
    r = ts.risk_profile(_state(volatility={"atr_pct": 3.0}), "4h")
    assert r["validated_timeframes"] == ["1d"]
    assert "4h" in r["validation_note"]


def test_missing_atr_does_not_invent_a_band():
    r = ts.risk_profile(_state(volatility={}), "4h")
    assert r["level"] == "低"
    assert r["size_basis"] == "fallback"
    assert r["atr_pct"] is None


def test_equal_risk_size_scales_with_atr_vs_median():
    """v415：ATR 比自身常态贵 → 等风险仓位收；便宜 → 放；夹在 0.5~1.5。"""
    st = _state(volatility={"atr_pct": 3.0, "atr_pct_median": 1.5})
    r = ts.risk_profile(st, "1h")
    assert r["equal_risk_size"] == 0.5            # 2.0 倍波动 → 被夹到下限
    assert r["atr_vs_median"] == 2.0
    assert "近 60 根中位" in r["equal_risk_note"]

    st2 = _state(volatility={"atr_pct": 0.5, "atr_pct_median": 1.0})
    r2 = ts.risk_profile(st2, "1h")
    assert r2["equal_risk_size"] == 1.5           # 便宜一倍 → 夹到上限


def test_equal_risk_size_never_overrides_size_factor():
    """等风险只是资金口径建议，不能偷偷改掉 size_factor（那是另一套已验证口径）。"""
    r = ts.risk_profile(_state(volatility={"atr_pct": 3.0, "atr_pct_median": 1.0}), "4h")
    assert r["size_factor"] == 1.0
    assert r["equal_risk_size"] is not None


def test_missing_atr_median_leaves_equal_risk_empty():
    r = ts.risk_profile(_state(volatility={"atr_pct": 1.2}), "1h")
    assert r["equal_risk_size"] is None
    assert r["equal_risk_note"] is None


def test_one_hour_adverse_multiple_registered():
    """1h 的实测倍数（0.55 ATR）必须落地，否则 1h 读数会借用 4h 的 0.9。"""
    r = ts.risk_profile(_state(volatility={"atr_pct": 2.0}), "1h")
    assert r["expected_adverse_pct"] == 1.1


def test_position_pct_from_stop_and_risk_budget():
    """v417：仓位 = 风险预算 / 止损距离 × 已验证的档位系数。"""
    # 日内：ATR 1.9% → 止损 2×ATR=3.8% → 1%/3.8%≈26.3%，日内系数 1.0
    r = ts.risk_profile(_state(volatility={"atr_pct": 1.9}), "4h")
    assert r["stop_pct"] == 3.8
    assert r["position_pct"] == 26.3
    assert r["max_risk_pct"] == 1.0


def test_position_pct_applies_daily_size_factor():
    """日线高档 0.5× 必须同时压仓位，否则"档位"和"风险预算"各说一套。"""
    high = ts.risk_profile(_state(volatility={"atr_pct": 4.0}), "1d")
    assert high["level"] == "高" and high["size_factor"] == 0.5
    assert high["stop_pct"] == 12.0
    assert high["position_pct"] == 4.2          # (1/12)*100*0.5


def test_position_pct_stays_within_bounds():
    """止损很窄时敞口也不能超过 100%（不加杠杆），很宽时不低于 1%。"""
    tiny = ts.risk_profile(_state(volatility={"atr_pct": 0.01}), "4h")
    assert tiny["position_pct"] == 100.0
    wide = ts.risk_profile(_state(volatility={"atr_pct": 60.0}), "1d")
    assert wide["position_pct"] == 1.0


def test_equal_risk_position_uses_unscaled_base():
    """等风险敞口按未乘 size_factor 的基准再叠等风险系数（日内 size_factor 恒 1.0）。"""
    r = ts.risk_profile(_state(volatility={"atr_pct": 3.0, "atr_pct_median": 1.5}), "1h")
    assert r["stop_pct"] == 6.0
    assert r["position_pct"] == 16.7
    # equal_size=0.5 → 8.35 → 8.3 或 8.4（四舍五入到 1 位）
    assert r["position_pct_equal_risk"] in (8.3, 8.4)


def _risk(tf, atr=2.0):
    return ts.risk_profile(
        {"volatility": {"atr_pct": atr, "atr_pct_percentile": 50},
         "trend": {}, "value_area": {}, "leverage": {}, "positioning": {}},
        tf)


def test_v418_odds_typical_by_timeframe():
    assert _risk("1h")["odds_typical"] == 0.70
    assert _risk("1d")["odds_typical"] == 1.32
    # 4h 样本不足 → 不给假数
    assert _risk("4h")["odds_typical"] is None


def test_v418_require_space_is_adverse_over_odds():
    r = _risk("1d", atr=2.0)
    assert r["require_space_pct"] == round(r["expected_adverse_pct"] / 1.32, 2)
    assert r["odds_note"] and "1.32" in r["odds_note"]


def test_v418_intraday_requires_more_space_than_adverse():
    r = _risk("1h", atr=1.0)
    assert r["require_space_pct"] > r["expected_adverse_pct"]


def test_v418_4h_falls_back_without_odds():
    # 4h 没有可靠赔率 → 不套赔率门槛，退回「逆风线」（v420 起再与成本地板取大）
    r = _risk("4h", atr=2.0)
    assert r["odds_typical"] is None
    assert r["require_basis"] == "adverse"
    assert r["require_space_pct"] == max(r["expected_adverse_pct"], r["round_trip_cost_pct"])
    assert r["odds_note"] and "样本不足" in r["odds_note"]


def test_v419_min_hold_bars():
    assert _risk("1d")["min_hold_bars"] == 3
    # 日内没有正赔率的持有期 → 不给数，也不假装"拿久点就行"
    assert _risk("1h")["min_hold_bars"] is None
    assert _risk("4h")["min_hold_bars"] is None


def test_v419_holding_note_says_what_the_curve_measured():
    assert "拿久不会变好" in _risk("1h")["holding_note"] and "**" not in _risk("1h")["holding_note"]
    assert "3 根" in _risk("1d")["holding_note"]


def test_v420_cost_floor_binds_when_odds_requirement_is_low():
    # 止损空间很窄（ATR 0.1%）时，赔率要求 0.06/0.7≈0.09% 低于成本 0.30% → 抬到成本线
    r = _risk("1h", atr=0.10)
    assert r["require_basis"] == "cost"
    assert r["require_space_pct"] == r["round_trip_cost_pct"] == 0.30


def test_v420_odds_bound_wins_when_above_cost():
    r = _risk("1h", atr=0.85)
    assert r["require_basis"] == "odds"
    assert r["require_space_pct"] > r["round_trip_cost_pct"]


def test_v420_cost_constant_matches_measurement():
    # 30bp 来自 news_replay 2028 笔 cost_bps 中位，不是拍的
    assert _risk("1d", atr=1.8)["round_trip_cost_pct"] == 0.30
