"""true_anomaly（真异常识别器）单测。

2026-11-20: 覆盖率表里这个模块是 0%。它是纯函数、纯因果、无 IO，恰恰最该被测——
这类"减噪"逻辑一改，信号密度就会从每 140 根 1 个变成每 13 根 1 个（历史事故原话），
而没有任何用例拦着。

测的都是设计承诺，不是实现细节：
  锚点必须是真 swing（盘整不算）、证据数与分数双门槛、同方向冷却、
  形态方向不能反着用、MACD 柱体归一化（跨价位通用）。
"""

import true_anomaly as ta


def _bar(ts, o, h, l, c, v=100.0):
    return {"ts": ts, "open": o, "high": h, "low": l, "close": c, "volume": v}


def _series(n=150, start=100.0, step=0.0, vol=100.0):
    rows = []
    price = start
    for i in range(n):
        price += step
        rows.append(_bar(1700000000 + i * 600, price, price + 0.5, price - 0.5, price, vol))
    return rows


# ── 分位数 ─────────────────────────────────────────────────────────────────

def test_pct_handles_edge_cases():
    assert ta._pct([], 0.5) == 0.0
    assert ta._pct([7.0], 0.9) == 7.0
    assert ta._pct([0.0, 10.0], 0.5) == 5.0
    assert ta._pct([0.0, 10.0], 0.0) == 0.0
    assert ta._pct([0.0, 10.0], 1.0) == 10.0


# ── 自适应阈值 ─────────────────────────────────────────────────────────────

def test_adaptive_thresholds_have_minimum_span():
    ind = {
        "rsi": [50.0] * 50,                              # 分布极窄：全 50
        "vol_ratio": [1.0] * 40 + [2.0] * 10,
    }
    os_band, ob_band, vol_gate = ta._adaptive_thresholds(ind)
    # RSI 全 50 时分位带会塌成一点，必须被撑开至少 rsi_band_min_span
    assert ob_band - os_band >= ta.CFG["rsi_band_min_span"]
    assert vol_gate >= 1.05                           # 门槛不会低于 1.05

    # 分布很宽时直接用分位，不再硬撑
    wide = {"rsi": [float(i) for i in range(100)], "vol_ratio": [1.0, 1.2, 1.5, 3.0]}
    os2, ob2, _ = ta._adaptive_thresholds(wide)
    assert os2 < 20 and ob2 > 80


def test_indicators_align_with_rows():
    rows = _series(120, step=0.3)
    ind = ta._indicators(rows)
    assert ind["n"] == 120
    assert len(ind["rsi"]) == len(ind["hist_rel"]) == len(ind["vol_ratio"]) == 120
    # 量比要 20 根均量才起步
    assert all(v is None for v in ind["vol_ratio"][:20])
    assert any(v is not None for v in ind["vol_ratio"][20:])
    assert all(0 <= v <= 100 for v in ind["rsi"] if v is not None)


# ── 锚点闸 ────────────────────────────────────────────────────────────────

def test_anchor_state_requires_strict_extreme():
    rows = [
        _bar(1, 10, 11, 9, 10),
        _bar(2, 10, 12, 9, 11),
        _bar(3, 11, 20, 10, 19),   # 高于窗口内所有 high → pivot high
    ]
    is_high, is_low = ta._anchor_state(rows, 2, 2)
    assert is_high is True and is_low is False

    # 与前高相等 → 不算锚点（防止横盘所有 bar 都成锚点）
    rows[2] = _bar(3, 11, 12, 10, 11)
    assert ta._anchor_state(rows, 2, 2)[0] is False


def test_anchor_state_first_bar_has_no_history():
    rows = [_bar(1, 10, 11, 9, 10)]
    assert ta._anchor_state(rows, 0, 12) == (False, False)


# ── 噪音门 ────────────────────────────────────────────────────────────────

def test_macd_shrinking_detects_fade():
    hist = [None] * 3 + [0.9, 0.6, 0.3, 0.1, 0.02]
    assert ta._is_macd_shrinking(hist, 7) is True      # 末值/峰值 ≈ 0.02 → 衰竭
    assert ta._is_macd_shrinking(hist, 2) is False     # 窗口不够
    rising = [None] * 3 + [0.1, 0.3, 0.6, 0.9, 1.2]
    assert ta._is_macd_shrinking(rising, 7) is False   # 还在放大，不是噪音


# ── 证据 ──────────────────────────────────────────────────────────────────

def test_ev_rsi_reversal_needs_touch_and_turn():
    rsi = [None, 20.0, 25.0, 35.0]
    assert ta._ev_rsi_reversal(rsi, 3, "up", os_band=30.0, ob_band=70.0) is not None
    # RSI 还在往下走 → 不算反转
    rsi_down = [None, 20.0, 30.0, 28.0]
    assert ta._ev_rsi_reversal(rsi_down, 3, "up", 30.0, 70.0) is None
    # 从没进过超卖带 → 不算
    calm = [None, 45.0, 50.0, 55.0]
    assert ta._ev_rsi_reversal(calm, 3, "up", 30.0, 70.0) is None


def test_ev_macd_flip_requires_real_cross():
    hist = [None, -0.5, 0.6]
    assert ta._ev_macd_flip(hist, 2, "up") is not None
    assert ta._ev_macd_flip(hist, 2, "down") is None
    # 穿越太浅（< macd_flip_rel）→ 判为噪声
    hist2 = [None, -0.005, 0.005]
    assert ta._ev_macd_flip(hist2, 2, "up") is None


def test_ev_vol_surge_uses_gate():
    assert ta._ev_vol_surge([None, 1.2], 1, 1.2) is not None
    assert ta._ev_vol_surge([None, 0.8], 1, 1.2) is None
    assert ta._ev_vol_surge([None, None], 1, 1.2) is None


# ── 主入口 ────────────────────────────────────────────────────────────────

def test_detect_flat_market_returns_nothing():
    assert ta.detect(_series(150, step=0.0, vol=100.0)) == []


def test_detect_too_short_returns_nothing():
    assert ta.detect(_series(30, step=0.5)) == []


def test_detect_signal_shape_and_no_future_use():
    rows = _series(150, step=0.0)
    # 人为造一个 V 底 + 放量：先跌 60 根，再涨
    for i in range(60):
        rows[i]["close"] = 100.0 - i * 0.5
        rows[i]["open"] = rows[i]["close"] + 0.2
        rows[i]["high"] = rows[i]["close"] + 0.6
        rows[i]["low"] = rows[i]["close"] - 0.6
    for i in range(60, 150):
        rows[i]["close"] = 70.0 + (i - 60) * 0.3
        rows[i]["open"] = rows[i]["close"] - 0.2
        rows[i]["high"] = rows[i]["close"] + 0.6
        rows[i]["low"] = rows[i]["close"] - 0.6
    rows[60]["volume"] = 900.0                      # 底部放量

    signals = ta.detect(rows)
    assert isinstance(signals, list)
    for s in signals:
        assert s["uses_future"] is False            # 声明就该被断言
        assert set(s) >= {"ts", "kind", "close", "pattern", "evidence", "score"}
        assert s["kind"] in ("bottom_reversal", "top_reversal")
        assert 0 <= s["score"] <= 1.0
        # 只出现在 warmup(60 根) 之后
        assert s["ts"] >= rows[60]["ts"]

    # 信号必须按时间升序（前端按顺序画箭头）
    assert [s["ts"] for s in signals] == sorted(s["ts"] for s in signals)


def test_detect_respects_cooldown_between_same_direction():
    rows = _series(300, step=0.0)
    for i in range(len(rows)):
        rows[i]["close"] = 100.0 + (5.0 if i % 40 < 20 else -5.0)
    for i in range(0, len(rows), 45):
        rows[i]["volume"] = 900.0
    signals = ta.detect(rows)
    same = [s["ts"] for s in signals if s["kind"] == "bottom_reversal"]
    if len(same) > 1:
        gaps = [(b - a) // 600 for a, b in zip(same, same[1:])]
        assert min(gaps) >= ta.CFG["cooldown"]       # 同方向冷却不能被绕过


def test_thresholds_public_view():
    out = ta.thresholds(_series(120, step=0.2))
    assert set(out) == {"rsi_os", "rsi_ob", "vol_gate", "adaptive"}
    assert out["rsi_os"] < out["rsi_ob"] or out["adaptive"] is False
