"""v343 技术状态模块测试：取数口径 / 合成偏置 / 闸门减分。

不连真实库：用 tmp_path 里最小 schema 的 sqlite 造假数据，
这样"毫秒→秒""同窗口比较""缺维度丢权重"这些口径能被真正验证，
而不是靠真实库里恰好有数据。
"""
import json
import math
import sqlite3
import sys
from pathlib import Path

import pytest

BACKEND = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND / "src_python"))

import technical_state as ts
import decision_guard as guard


# ---------------------------------------------------------------------------
# 最小库
# ---------------------------------------------------------------------------

def _mk_db(tmp_path, *, with_oi=True, with_taker=True, with_cvd=False,
           with_top=True, with_funding=True):
    db = tmp_path / "mini.db"
    conn = sqlite3.connect(db)
    conn.executescript("""
        CREATE TABLE kline_bars (symbol TEXT, timeframe TEXT, ts INTEGER,
            open REAL, high REAL, low REAL, close REAL, volume REAL);
        CREATE TABLE external_indicators (asset TEXT, indicator TEXT, ts INTEGER, value REAL);
        CREATE TABLE macro_snapshots (metric_key TEXT, ts INTEGER, value REAL);
    """)
    # 240 根 4h 下跌趋势蜡烛（毫秒时间戳，模拟真实表）
    now = 1_760_000_000_000
    step = 4 * 3600 * 1000
    price = 100000.0
    for i in range(240):
        price *= (1.0 - 0.004)
        ts_ms = now - (239 - i) * step
        o = price * 1.002
        h = price * 1.006
        l = price * 0.994
        conn.execute("INSERT INTO kline_bars VALUES ('BTC','4h',?,?,?,?,?,?)",
                     (ts_ms, o, h, l, price, 100.0 + i))
    # external_indicators / macro_snapshots 的 ts 是**秒**（与 kline_bars 的毫秒不同表不同口径）
    now_s = now // 1000
    if with_oi:
        # 3 天前的 OI 与当前 OI：验证"窗口自适应"会选中 3 日而不是 7 日
        conn.execute("INSERT INTO external_indicators VALUES ('BTC','bn_open_interest',?,?)",
                     (now_s - 3 * 86400, 1000.0))
        conn.execute("INSERT INTO external_indicators VALUES ('BTC','bn_open_interest',?,?)",
                     (now_s, 940.0))
    if with_funding:
        conn.execute("INSERT INTO external_indicators VALUES ('BTC','bn_funding_rate',?,?)",
                     (now_s, 0.0001))
    if with_taker:
        conn.execute("INSERT INTO external_indicators VALUES ('BTC','okx_taker_buy',?,?)",
                     (now_s, 30.0))
        conn.execute("INSERT INTO external_indicators VALUES ('BTC','okx_taker_sell',?,?)",
                     (now_s, 70.0))
    if with_top:
        conn.execute("INSERT INTO external_indicators VALUES ('BTC','bn_top_position_ratio',?,?)",
                     (now_s, 1.2))
        conn.execute("INSERT INTO external_indicators VALUES ('BTC','bn_long_short_ratio',?,?)",
                     (now_s, 2.5))
    if with_cvd:
        for k in range(40):
            conn.execute("INSERT INTO macro_snapshots VALUES ('BTC.cvd',?,?)",
                         (now_s - (39 - k) * 4 * 3600, -5.0))
    conn.commit()
    return conn, db


def test_build_state_converts_ms_and_reads_window(tmp_path):
    conn, _ = _mk_db(tmp_path)
    state = ts.build_state(conn, "BTC", "4h", 240)
    conn.close()
    assert "error" not in state
    # 时间戳出库即转秒：最后一根应与库里最大 ts(ms)/1000 对齐
    assert state["_meta"]["candle_last_ts"] < 10**12
    # OI 只有 3 日深度 → 窗口必须承认是 3 日，不能写成 7 日
    assert state["_meta"]["oi_window"] == "3 日"
    assert state["leverage"]["oi_change_pct"] == pytest.approx(-6.0, abs=0.2)
    # 价格变化必须落在同一窗口（-6% 量级），而不是最后一根的小跌
    assert state["leverage"]["price_change_pct"] < -5.0
    assert state["leverage"]["label"]


def test_build_state_reports_missing_not_zero(tmp_path):
    conn, _ = _mk_db(tmp_path, with_oi=False, with_taker=False, with_cvd=False,
                     with_top=False, with_funding=False)
    state = ts.build_state(conn, "BTC", "4h", 240)
    conn.close()
    miss = " ".join(state["missing"])
    assert "leverage" in miss and "taker" in miss and "positioning" in miss
    # 缺数据必须是 None，不能补 0 冒充"中性"
    assert state["leverage"]["oi_change_pct"] is None
    assert state["flow"]["taker"]["buy_ratio"] is None


def test_build_state_short_candles_errors(tmp_path):
    conn, _ = _mk_db(tmp_path)
    state = ts.build_state(conn, "BTC", "4h", 10)
    conn.close()
    assert "error" in state


# ---------------------------------------------------------------------------
# 合成偏置
# ---------------------------------------------------------------------------

def _state(**kw):
    base = {
        "trend": {"stage": "强趋势", "trend_dir": "down", "adx": 60.0,
                  "plus_di": 7.0, "minus_di": 30.0, "donchian_position": 0.2},
        "leverage": {"bias": "bear", "strength": 1.0},
        "flow": {"taker": {"bias": "bear", "strength": 0.4}},
        "positioning": {"bias": "bear"},
        "value_area": {"position": "below_value"},
        "missing": [],
    }
    base.update(kw)
    return base


def test_bias_full_coverage_and_direction():
    bd = ts.bias_of(_state())
    assert bd["coverage"] == 1.0
    assert bd["bias"] < -0.3
    assert bd["usable"] is True
    assert set(bd["components"]) == {"trend", "leverage", "flow", "positioning",
                                     "donchian", "value_area"}


def test_bias_drops_missing_weights_instead_of_zeroing():
    """缺维度应丢权重并抬高 coverage 的缺口，而不是把它当 0 分拉低偏置。"""
    full = ts.bias_of(_state())
    thin = ts.bias_of(_state(
        leverage={"bias": None}, flow={"taker": {"bias": None}},
        positioning={"bias": None}, value_area={"position": "in_value"}))
    assert thin["coverage"] < 0.5
    assert thin["usable"] is False          # 覆盖不足 → 不许当证据
    assert thin["bias"] is not None
    assert abs(thin["bias"]) > abs(full["bias"]) * 0.5


def test_bias_range_trend_uses_di_fallback():
    """结构 mixed(无 up/down 定论) 但 DI 有偏向时，不能让 trend 整条缺席。"""
    bd = ts.bias_of(_state(trend={"stage": "强趋势", "trend_dir": "range", "adx": 44.0,
                                  "plus_di": 14.0, "minus_di": 27.0,
                                  "donchian_position": 0.7}))
    assert "trend" in bd["components"]
    assert bd["components"]["trend"] < 0


def test_bias_returns_none_when_nothing_computable():
    bd = ts.bias_of({"trend": {}, "leverage": {}, "flow": {"taker": {}},
                     "positioning": {}, "value_area": {}, "missing": ["全部"]})
    assert bd["bias"] is None
    assert bd["usable"] is False


def test_summary_line_marks_missing():
    state = _state(missing=["leverage(OI)"], readout="强趋势·空头主导")
    state["bias_detail"] = ts.bias_of(_state())
    line = ts.summary_line(state, "BTC")
    assert "强趋势" in line and "偏置" in line and "缺: leverage(OI)" in line


def test_block_skips_unavailable_asset():
    block_text = ts.block(["ZZZ"], timeframe="4h")
    assert "不可用" in block_text


# ---------------------------------------------------------------------------
# 闸门接入
# ---------------------------------------------------------------------------

def _guard_row(technical_state, sentiment, action):
    ctx = {"assets": {"BTC": {
        "status": "ok", "change_24h_str": "-1.20%", "change_24h_pct": -1.2,
        "funding_rate_str": "+0.0100%", "funding_rate_pct": 0.01,
        "stats_7d": {"trend": "bear"},
        "technical_state": technical_state,
    }}}
    return {
        "target_asset": "BTC", "decision_context": json.dumps(ctx),
        "sentiment_score": sentiment, "suggested_action": action,
        "impact_score": 7, "heat_score": 5, "asset_impact": 6,
        "cluster_size": 2, "history_sample": {"total": 10, "wins": 6},
        "market_confirmation": "confirmed",
    }


def _tech(bias, coverage=1.0, usable=True):
    return {"available": True, "bias": bias, "coverage": coverage,
            "usable": usable, "readout": "强趋势·空头主导", "missing": []}


def test_guard_damps_when_direction_fights_readout():
    plain = guard.evaluate_decision(_guard_row({"available": False}, 0.6, "buy"))
    fought = guard.evaluate_decision(_guard_row(_tech(-0.63), 0.6, "buy"))
    assert fought["technical_state"]["conflict"] is True
    assert fought["technical_state"]["damp"] < 1.0
    assert abs(fought["final_score"]) < abs(plain["final_score"])
    assert "news_vs_tech_state" in [c["type"] for c in fought["contradictions"]]


def test_guard_no_conflict_when_aligned():
    r = guard.evaluate_decision(_guard_row(_tech(0.63), 0.6, "buy"))
    assert r["technical_state"]["conflict"] is False
    assert r["technical_state"]["damp"] == 1.0


def test_guard_ignores_low_coverage_readout():
    """单维度推出来的偏置不许当反方证据（黄金只有区间位置可算那种情形）。"""
    r = guard.evaluate_decision(_guard_row(_tech(-0.63, coverage=0.14, usable=False),
                                           0.6, "buy"))
    assert r["technical_state"]["conflict"] is False
    assert r["technical_state"]["usable"] is False


def test_guard_adds_no_directional_weight():
    """接入只走"反方证据"通道：方向权重表一个字都不能改。"""
    assert "technical_state" not in guard.DIRECTIONAL_WEIGHTS
    assert "technical_state" not in guard.evaluate_decision(
        _guard_row(_tech(-0.63), 0.6, "buy"))["weights"]
