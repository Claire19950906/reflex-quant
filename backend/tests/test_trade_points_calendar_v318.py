"""v318 — 「下次交易点日历」回归测试 (含 T+0/T+1 与滑点)。

口径核心：
  · 每个交易点只用 as_of_ts(=现在) **之前**的数据评估 —— 未来的事件也必须
    用"今天能看到的历史"，不许把事件之后的行情算进来（未来函数）。
  · 每个点都带交收口径 (T+0/T+1)、每边滑点/手续费 bps、往返成本。
  · 入场窗口已过的点标记为 expired（不再算可执行点），但仍留档。
"""
from __future__ import annotations

import os
import sqlite3
import sys

SRC = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "src_python"))
if SRC not in sys.path:
    sys.path.insert(0, SRC)

import db
import event_opportunity as eo

NOW = 1_800_000_000
HOUR = 3600


def _fresh_db() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    db.migrate(conn)
    return conn


def _seed_impact(conn, *, ts, title, asset, impact_pct, direction, eid="e1"):
    conn.execute(
        """INSERT INTO event_kline_impact
           (event_source, event_id, event_title, event_time, importance, asset,
            impact_pct, impact_severity, direction)
           VALUES ('test', ?, ?, ?, 'high', ?, ?, 'high', ?)""",
        (eid, title, int(ts), asset, float(impact_pct), direction),
    )


def _seed_macro(conn, *, ts, title, impact="high", eid="m1"):
    conn.execute(
        """INSERT INTO macro_calendar
           (source, event_id, country, title, impact, event_time, star)
           VALUES ('test', ?, 'US', ?, ?, ?, 3)""",
        (eid, title, impact, int(ts)),
    )


def _seed_daily(conn, asset, *, n=30, start_ts=NOW - 30 * 86400, step=86400):
    try:
        for i in range(n):
            conn.execute(
                "INSERT INTO ohlcv_daily (asset, ts, open, high, low, close, volume) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (asset, start_ts + i * step, 100.0, 101.0, 99.0, 100.0 + i * 0.1, 10.0),
            )
    except sqlite3.Error:
        pass


# ─────────────────────── 1. 前瞻点位生成 ───────────────────────

def test_upcoming_macro_event_becomes_a_trade_point():
    conn = _fresh_db()
    # 未来 2 小时的一个 CPI 事件（规则能映射到 XAU/BTC/ETH）
    _seed_macro(conn, ts=NOW + 2 * HOUR, title="Italian Prelim CPI m/m")
    _seed_daily(conn, "XAU")
    for i in range(6):
        _seed_impact(conn, ts=NOW - (i + 1) * 86400,
                     title="Italian Prelim CPI m/m", asset="XAU",
                     impact_pct=0.01, direction="up", eid=f"h{i}")
    conn.commit()

    res = eo.list_trade_points(conn, as_of_ts=NOW, horizon_hours=72)
    assert res["ok"] is True
    assert res["sources"]["macro_calendar"] == 1
    upcoming = [p for p in res["points"] if p["kind"] == "upcoming"]
    assert upcoming, "未来的已排期事件应生成 upcoming 交易点"
    point = upcoming[0]
    assert point["asset"] in ("XAU", "BTC", "ETH")
    # 入场窗口锚在事件时点
    assert point["entry_not_before_ts"] == NOW + 2 * HOUR
    assert point["entry_deadline_ts"] > point["entry_not_before_ts"]
    conn.close()


def test_trade_point_carries_t_plus_and_slippage():
    conn = _fresh_db()
    _seed_macro(conn, ts=NOW + HOUR, title="Italian Prelim CPI m/m")
    _seed_daily(conn, "XAU")
    conn.commit()
    res = eo.list_trade_points(conn, as_of_ts=NOW, horizon_hours=72)
    point = [p for p in res["points"] if p["kind"] == "upcoming"][0]
    cost = point["cost"]
    assert point["t_plus"] in ("T+0", "T+1")
    assert cost["t_plus"] == point["t_plus"]
    assert float(cost["slippage_bps_per_side"]) > 0
    assert float(cost["fee_bps_per_side"]) > 0
    assert float(cost["round_trip_pct"]) > 0
    # 最早可平仓时点必须不早于入场时点（T+1 时尤其如此）
    assert point["earliest_exit_ts"] >= point["entry_not_before_ts"]
    conn.close()


def test_t_plus_one_asset_has_later_exit():
    """T+1 品种（股票 / 未知品种保守按 T+1）的最早平仓日必须晚于 T+0 品种。"""
    import cost_model
    assert cost_model.settlement_rule("BTC")["t_plus"] == "T+0"
    assert cost_model.settlement_rule("XAU")["t_plus"] == "T+0"
    assert cost_model.settlement_rule("STOCK")["t_plus"] == "T+1"
    assert cost_model.earliest_exit_ts("BTC", NOW) == NOW
    assert cost_model.earliest_exit_ts("STOCK", NOW) > NOW
    assert cost_model.earliest_exit_ts("UNKNOWN_ASSET", NOW) > NOW


# ─────────────────────── 2. 时点口径（不许未来函数） ───────────────────────

def test_future_stats_are_not_used():
    """as_of_ts 之后的历史事件不得进入统计（否则就是未来函数）。"""
    conn = _fresh_db()
    for i in range(6):
        _seed_impact(conn, ts=NOW - (i + 1) * 86400, title="CPI data",
                     asset="XAU", impact_pct=0.01, direction="up", eid=f"old{i}")
    # 未来（as_of 之后）再放 6 条超大冲击
    for i in range(6):
        _seed_impact(conn, ts=NOW + (i + 1) * 86400, title="CPI data",
                     asset="XAU", impact_pct=0.99, direction="up", eid=f"new{i}")
    conn.commit()
    card = eo.build_opportunity(conn, asset="XAU", event_time=NOW + HOUR,
                                title="CPI data", as_of_ts=NOW)
    assert card["evidence"]["samples"] == 6, "统计只应看到 as_of 之前的 6 条"
    # 旧的 6 条 impact_pct=0.01 → 平均 1.0%；若把未来的 0.99（99%）也算进来
    # 平均会变成 ~50% —— 这里断言期望幅度没被未来数据污染。
    assert abs(card["edge_pct"]) < 5.0, "未来的超大冲击不得进入期望幅度"
    conn.close()


def test_stats_include_past_events():
    conn = _fresh_db()
    for i in range(6):
        _seed_impact(conn, ts=NOW - (i + 1) * 86400, title="CPI data",
                     asset="XAU", impact_pct=0.02, direction="up", eid=f"old{i}")
    conn.commit()
    card = eo.build_opportunity(conn, asset="XAU", event_time=NOW + HOUR,
                                title="CPI data", as_of_ts=NOW)
    assert card["evidence"]["samples"] == 6
    conn.close()


# ─────────────────────── 3. 窗口与过期 ───────────────────────

def test_expired_entry_window_is_marked_expired():
    conn = _fresh_db()
    # 45 分钟前的事件 → CPI 属"定时公布类"→ 确认窗口 30 分钟已过 → 只留档
    _seed_impact(conn, ts=NOW - 2700, title="CPI data", asset="XAU",
                 impact_pct=0.01, direction="up", eid="recent")
    conn.commit()
    res = eo.list_trade_points(conn, as_of_ts=NOW, action_window_minutes=60)
    kinds = [p["kind"] for p in res["points"]]
    assert "expired" in kinds
    assert "actionable" not in kinds
    # 过期的点仍然留档（可回查），但不算 live
    assert res["live"] == 0
    conn.close()


def test_open_entry_window_is_actionable():
    conn = _fresh_db()
    # 1 分钟前的事件 → 进场窗口还开着 → 现在就能进
    _seed_impact(conn, ts=NOW - 60, title="CPI data", asset="XAU",
                 impact_pct=0.01, direction="up", eid="fresh")
    conn.commit()
    res = eo.list_trade_points(conn, as_of_ts=NOW, action_window_minutes=60)
    kinds = [p["kind"] for p in res["points"]]
    assert "actionable" in kinds
    assert res["live"] >= 1
    conn.close()


def test_points_sorted_by_entry_time():
    conn = _fresh_db()
    _seed_macro(conn, ts=NOW + 3 * HOUR, title="Italian Prelim CPI m/m", eid="m2")
    _seed_macro(conn, ts=NOW + 1 * HOUR, title="Italian Prelim CPI m/m", eid="m1")
    conn.commit()
    res = eo.list_trade_points(conn, as_of_ts=NOW, horizon_hours=72)
    times = [p["entry_not_before_ts"] for p in res["points"]]
    assert times == sorted(times)
    conn.close()


def test_asset_filter_applies():
    conn = _fresh_db()
    _seed_macro(conn, ts=NOW + HOUR, title="Italian Prelim CPI m/m")
    conn.commit()
    res = eo.list_trade_points(conn, as_of_ts=NOW, horizon_hours=72, asset="BTC")
    for p in res["points"]:
        assert p["asset"] == "BTC"
    conn.close()


def test_unmapped_event_is_skipped_honestly():
    """规则抽不出受影响标的 → 跳过并计数，不瞎猜品种。"""
    conn = _fresh_db()
    _seed_macro(conn, ts=NOW + HOUR, title="KOF Economic Barometer")
    conn.commit()
    res = eo.list_trade_points(conn, as_of_ts=NOW, horizon_hours=72)
    assert res["sources"]["skipped_no_asset"] == 1
    assert not [p for p in res["points"] if p["kind"] == "upcoming"]
    conn.close()

