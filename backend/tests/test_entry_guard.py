# -*- coding: utf-8 -*-
"""v446：开仓闸（位置分位 / 同向确认）的口径测试。

锁的是三条硬规矩：
  - 位置只在"决策前"的窗口里算，0=最低、1=最高；
  - tick 不够就不判（available=False），绝不把"没数据"当"安全"；
  - 默认影子模式不改动作，只有 enforce 才把 BUY/SELL 降成 HOLD。
"""
import os
import sqlite3
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src_python"))

import entry_guard as eg


def _mem_conn():
    conn = sqlite3.connect(":memory:")
    conn.execute("CREATE TABLE market_ticks(symbol TEXT, ts INTEGER, price REAL)")
    return conn


def _seed(conn, symbol, prices, start=1000, step=10):
    for i, p in enumerate(prices):
        conn.execute("INSERT INTO market_ticks VALUES(?,?,?)",
                     (symbol, start + i * step, float(p)))
    conn.commit()


# ── 位置 ────────────────────────────────────────────────
def test_range_metrics_edges():
    ticks = [(i, p) for i, p in enumerate([100, 101, 102, 103, 104])]
    m = eg.range_metrics(ticks, entry_price=104)
    assert m["available"] is True
    assert m["position"] == 1.0
    m2 = eg.range_metrics(ticks, entry_price=100)
    assert m2["position"] == 0.0
    m3 = eg.range_metrics(ticks, entry_price=102)
    assert abs(m3["position"] - 0.5) < 1e-9


def test_range_metrics_insufficient_ticks():
    m = eg.range_metrics([(1, 100), (2, 101)], entry_price=101)
    assert m["available"] is False
    assert "tick" in m["reason"]


def test_range_metrics_zero_span():
    m = eg.range_metrics([(i, 100.0) for i in range(10)], entry_price=100)
    assert m["available"] is False
    assert "跨度为 0" in m["reason"]


def test_range_metrics_uses_last_tick_when_no_entry():
    ticks = [(0, 100), (1, 101), (2, 102), (3, 103), (4, 104), (5, 103)]
    m = eg.range_metrics(ticks)
    assert m["entry"] == 103
    assert abs(m["position"] - 0.75) < 1e-9


# ── 同向 ────────────────────────────────────────────────
def test_pre_confirm_buy_up():
    ticks = [(1000, 100), (1100, 100.2), (1200, 100.3), (1800, 100.5), (1900, 100.6)]
    r = eg.pre_confirm(ticks, "BUY", window_min=30, min_move_pct=0.05)
    assert r["available"] is True
    assert r["pre_confirm"] == 1
    assert r["move_pct"] > 0


def test_pre_confirm_sell_up_is_not_confirmed():
    ticks = [(1000, 100), (1100, 100.1), (1200, 100.2), (1800, 100.5), (1900, 100.6)]
    r = eg.pre_confirm(ticks, "SELL", window_min=30, min_move_pct=0.05)
    assert r["pre_confirm"] == 0


def test_pre_confirm_non_directional():
    r = eg.pre_confirm([(1, 100)], "HOLD")
    assert r["available"] is False


# ── 组合判定 ────────────────────────────────────────────
def test_v447_defaults_match_calibration():
    """v447 校准：同向闸 20 分钟 / 0.10%，位置闸默认不拦单。"""
    assert eg.CONFIRM_WINDOW_MIN == 20
    assert abs(eg.CONFIRM_MIN_MOVE_PCT - 0.10) < 1e-9
    assert eg.POSITION_GATE_ENABLED is False
    assert eg.RECOMMENDED_HOLD_MIN == 60


def test_evaluate_buy_at_top_records_but_does_not_block():
    """v447：追在顶部只标注（position_gate），不再拦单。

    2030 笔实测位置分位与盈亏相关只有 ±0.05，1 小时档"追极值"组反而更好，
    所以这里锁的是"记录但不拦"，避免有人把闸门重新打开。
    """
    conn = _mem_conn()
    # 20 分钟窗口：100→104，当前 104（顶部）
    _seed(conn, "BTC", [100, 101, 102, 103, 104], start=1800, step=10)
    info = eg.evaluate(conn, "BTC", "BUY", ts=1840)
    assert info["position"] == 1.0
    assert info["position_gate"]["would_block"] is True
    assert "区间顶部" in info["position_gate"]["reason"]
    assert info["block"] is False                       # 位置不拦单
    assert not any("区间顶部" in x for x in info["block_reasons"])
    assert info["suggested_entry"] == 102.0             # 50% 分位仍给建议价


def test_evaluate_sell_at_bottom_records_but_does_not_block():
    conn = _mem_conn()
    _seed(conn, "XAU", [104, 103, 102, 101, 100], start=1800, step=10)
    info = eg.evaluate(conn, "XAU", "SELL", ts=1840)
    assert info["position"] == 0.0
    assert info["position_gate"]["would_block"] is True
    assert info["block"] is False


def test_evaluate_non_tradable_asset():
    conn = _mem_conn()
    info = eg.evaluate(conn, "NVDA", "BUY", ts=1000)
    assert info["block"] is False
    assert "白名单" in " ".join(info["notes"])


# ── 影子 / 生效 ──────────────────────────────────────────
def test_annotate_shadow_keeps_action():
    """影子模式：同向已确认（涨 4%）的单不拦，动作不变。"""
    conn = _mem_conn()
    _seed(conn, "BTC", [100, 101, 102, 103, 104], start=1800, step=10)
    result = {"action": "BUY", "verdict": "ok"}
    old = eg.MODE
    eg.MODE = "shadow"
    try:
        info = eg.annotate({"target_asset": "BTC"}, result, conn=conn, asof_ts=1840)
    finally:
        eg.MODE = old
    assert result["action"] == "BUY"
    assert info["block"] is False            # 位置不拦，同向已确认
    assert info["enforced"] is False


def test_annotate_enforce_downgrades_when_not_confirmed():
    """enforce 模式：盘面未同向（一路跌却要 BUY）→ 降为 HOLD。"""
    conn = _mem_conn()
    _seed(conn, "BTC", [100, 99.8, 99.6, 99.5, 99.4], start=1800, step=10)
    result = {"action": "BUY", "verdict": "ok"}
    old = eg.MODE
    eg.MODE = "enforce"
    try:
        info = eg.annotate({"target_asset": "BTC"}, result, conn=conn, asof_ts=1840)
    finally:
        eg.MODE = old
    assert result["action"] == "HOLD"
    assert result["entry_guard_blocked_action"] == "BUY"
    assert info["enforced"] is True
    assert any("未同向" in x for x in info["block_reasons"])


def test_enforce_does_not_block_on_position_alone():
    """v447：追高在 enforce 模式下也不许单靠位置拦单。"""
    conn = _mem_conn()
    # 一路涨到顶，但同向已确认 → 不该拦
    _seed(conn, "BTC", [100, 101, 102, 103, 104], start=1800, step=10)
    result = {"action": "BUY", "verdict": "ok"}
    old = eg.MODE
    eg.MODE = "enforce"
    try:
        info = eg.annotate({"target_asset": "BTC"}, result, conn=conn, asof_ts=1840)
    finally:
        eg.MODE = old
    assert result["action"] == "BUY"
    assert info["position_gate"]["would_block"] is True
    assert info["enforced"] is False


def test_enforce_skips_unvalidated_asset():
    """v447：enforce 只对 BTC 生效；ETH 未验证过，只标注不拦。"""
    conn = _mem_conn()
    _seed(conn, "ETH", [100, 99.8, 99.6, 99.5, 99.4], start=1800, step=10)
    result = {"action": "BUY", "verdict": "ok"}
    old = eg.MODE
    eg.MODE = "enforce"
    try:
        info = eg.annotate({"target_asset": "ETH"}, result, conn=conn, asof_ts=1840)
    finally:
        eg.MODE = old
    assert info["block"] is True            # 规则上该拦
    assert result["action"] == "BUY"         # 但品种不在 enforce 白名单
    assert info["enforced"] is False
    assert info["enforce_assets"] == ["BTC"]


# ── 方向收益 / 结论 ─────────────────────────────────────
def test_dir_return_sign():
    assert eg._dir_return_pct("BUY", 1.0) == 1.0
    assert eg._dir_return_pct("SELL", 1.0) == -1.0
    assert eg._dir_return_pct("HOLD", 1.0) is None
    assert eg._dir_return_pct("BUY", None) is None


def test_gate_verdict_refuses_bad_rule():
    assert "反向" in eg._gate_verdict(100, -0.2, -2.0, {"avg": -0.3})
    assert "样本不足" in eg._gate_verdict(10, 0.3, 3.0, {"avg": 0.1})
    assert "不显著" in eg._gate_verdict(100, 0.1, 1.0, {"avg": -0.2})
