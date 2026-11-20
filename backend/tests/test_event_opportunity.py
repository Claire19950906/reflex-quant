# -*- coding: utf-8 -*-
"""v319+ 事件机会卡 (event_opportunity) 的回归测试。

核心断言是**时点口径** (point-in-time):
  · 计算 t 时刻事件的机会时, 只能用 t **之前**的历史事件与日线;
  · t 之后的数据 (哪怕改得再离谱) 不能影响结果;
  · 证据不足 / 幅度淹没在噪声里 / 扣完成本没优势 → 必须 NO_TRADE (不许硬给方向)。

纯离线: 临时 sqlite, 不碰生产库、不联网。
"""
from __future__ import annotations

import os
import sqlite3
import sys
import tempfile
import time

import pytest

SRC = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "src_python"))
if SRC not in sys.path:
    sys.path.insert(0, SRC)

import event_opportunity as eo

DAY = 86400


@pytest.fixture()
def db():
    path = tempfile.NamedTemporaryFile(prefix="rq_eo_", suffix=".db", delete=False).name
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute(
        "CREATE TABLE event_kline_impact ("
        " id INTEGER PRIMARY KEY AUTOINCREMENT, event_source TEXT DEFAULT '',"
        " event_id TEXT DEFAULT '', event_title TEXT DEFAULT '', event_time INTEGER,"
        " importance TEXT DEFAULT '', asset TEXT DEFAULT '', pre_event_close REAL,"
        " max_high REAL, min_low REAL, impact_pct REAL, impact_window_start INTEGER,"
        " impact_window_end INTEGER, impact_severity TEXT DEFAULT 'low',"
        " direction TEXT DEFAULT 'neutral')"
    )
    conn.execute(
        "CREATE TABLE ohlcv_daily (asset TEXT, ts INTEGER, open REAL, high REAL,"
        " low REAL, close REAL, volume INTEGER, source TEXT)"
    )
    conn.commit()
    yield conn
    conn.close()
    os.unlink(path)


def _add_event(conn, *, ts, title="普通新闻", asset="XAU", impact=0.01,
               direction="up", severity="medium"):
    conn.execute(
        "INSERT INTO event_kline_impact (event_source, event_id, event_title, event_time,"
        " importance, asset, pre_event_close, max_high, min_low, impact_pct,"
        " impact_window_start, impact_window_end, impact_severity, direction)"
        " VALUES ('s', ?, ?, ?, 'high', ?, 100, 101, 99, ?, ?, ?, ?, ?)",
        (f"e{int(ts)}", title, int(ts), asset, impact, int(ts) - 60, int(ts) + 60,
         severity, direction),
    )
    conn.commit()


def _add_bars(conn, *, asset="XAU", end_ts, n=30, start=100.0, step=0.002):
    """给 end_ts 之前铺 n 根日线 (带点波动, 让 σ > 0 而不是一条直线)。"""
    import math
    for i in range(n):
        ts = end_ts - (n - i) * DAY
        close = start * (1 + step * i + 0.6 * step * math.sin(i * 1.7))
        conn.execute(
            "INSERT INTO ohlcv_daily (asset, ts, open, high, low, close, volume, source)"
            " VALUES (?,?,?,?,?,?,0,'test')",
            (asset, ts, close, close, close, close),
        )
    conn.commit()



# ─────────────────────────── 时点口径 (核心) ───────────────────────────

def test_uses_only_events_before_the_event(db):
    """机会卡只能用事件时点**之前**的历史事件。"""
    now = int(time.time())
    t0 = now - 10 * DAY
    for i in range(8):
        _add_event(db, ts=t0 - (i + 1) * DAY, impact=0.02, direction="up")
    # 事件时点**之后**的 5 条大额下跌 —— 绝不能进统计
    for i in range(5):
        _add_event(db, ts=t0 + (i + 1) * DAY, impact=-0.05, direction="down")

    op = eo.build_opportunity(db, asset="XAU", event_time=t0, title="测试事件")
    assert op["evidence"]["samples"] == 8, op["evidence"]
    assert op["direction"] == "LONG", "未来那批下跌不能翻转方向"
    assert op["edge_pct"] == pytest.approx(2.0)


def test_future_rows_cannot_change_result(db):
    """把未来数据改得再离谱, t 时刻的机会卡也不变。"""
    now = int(time.time())
    t0 = now - 10 * DAY
    for i in range(8):
        _add_event(db, ts=t0 - (i + 1) * DAY, impact=0.02, direction="up")
    _add_bars(db, end_ts=t0, n=30)
    before = eo.build_opportunity(db, asset="XAU", event_time=t0, title="测试事件")

    for i in range(5):
        _add_event(db, ts=t0 + (i + 1) * DAY, impact=-0.5, direction="down")
    after = eo.build_opportunity(db, asset="XAU", event_time=t0, title="测试事件")
    assert before["direction"] == after["direction"]
    assert before["edge_pct"] == after["edge_pct"]
    assert before["evidence"]["samples"] == after["evidence"]["samples"]


def test_sigma_uses_only_bars_before_event(db):
    """σ 只能用事件之前的日线。"""
    now = int(time.time())
    t0 = now - 10 * DAY
    _add_bars(db, end_ts=t0, n=30, step=0.002)
    op = eo.build_opportunity(db, asset="XAU", event_time=t0, title="测试事件")
    assert 0 < op["evidence"]["sigma_daily_pct"] < 1.0
    assert op["evidence"]["point_in_time"] is True


# ─────────────────────────── 方向 / 门槛 ───────────────────────────

def test_direction_from_empirical_stats(db):
    """样本足够 → 方向来自历史条件统计。"""
    now = int(time.time())
    t0 = now - 10 * DAY
    for i in range(8):
        _add_event(db, ts=t0 - (i + 1) * DAY, title="央行购金", impact=0.02, direction="up")
    _add_bars(db, end_ts=t0, n=30, step=0.002)
    op = eo.build_opportunity(db, asset="XAU", event_time=t0, title="央行购金")
    assert op["direction"] == "LONG"
    assert op["direction_source"] == "empirical_same_type"
    assert op["status"] == "READY"
    assert op["edge_pct"] == pytest.approx(2.0)
    assert op["edge_pct_net"] == pytest.approx(2.0 - 0.16)  # XAU 往返 16bps


def test_cost_gate_blocks_tiny_edge(db):
    """edge 扣完成本后没优势 → NO_TRADE。"""
    now = int(time.time())
    t0 = now - 10 * DAY
    # XAU 往返成本 0.16% → 历史均值 0.10% 不够
    for i in range(8):
        _add_event(db, ts=t0 - (i + 1) * DAY, impact=0.001, direction="up")
    _add_bars(db, end_ts=t0, n=30, step=0.0001)
    op = eo.build_opportunity(db, asset="XAU", event_time=t0, title="测试事件")
    assert op["status"] == "NO_TRADE"
    assert op["direction"] == "NO_TRADE"
    assert "成本" in op["reason"]


def test_noise_gate_blocks_small_edge(db):
    """edge 淹没在持有期噪声里 → NO_TRADE (不许硬给方向)。"""
    now = int(time.time())
    t0 = now - 10 * DAY
    # 历史均值 0.30% (能过成本门槛 0.16%), 但远小于持有期噪声
    for i in range(8):
        _add_event(db, ts=t0 - (i + 1) * DAY, impact=0.003, direction="up")
    # 日线大幅波动 → σ 大
    _add_bars(db, end_ts=t0, n=30, step=0.02)
    op = eo.build_opportunity(db, asset="XAU", event_time=t0, title="测试事件")
    assert op["status"] == "NO_TRADE"
    assert "信噪比" in op["reason"]


def test_rule_prior_fallback_when_no_samples(db):
    """样本不足但规则先验有方向 → 用规则先验 (并标明来源)。"""
    now = int(time.time())
    t0 = now - 10 * DAY
    _add_bars(db, end_ts=t0, n=30, step=0.02)
    op = eo.build_opportunity(db, asset="XAU", event_time=t0, title="美联储加息25个基点")
    assert op["event_type"] == "加息"
    assert op["direction_source"] == "rule_prior"
    assert op["direction"] == "SHORT", "加息 → XAU 利空"
    assert "XAU" in op["affected_assets"]


# ─────────────────────────── 风控 / 时点 / 列表 ───────────────────────────

def test_risk_controls_respect_budget(db):
    """单笔最大亏损不能超过风险预算, 仓位不能超过上限。"""
    now = int(time.time())
    t0 = now - 10 * DAY
    for i in range(10):
        _add_event(db, ts=t0 - (i + 1) * DAY, impact=0.05, direction="up")
    _add_bars(db, end_ts=t0, n=30, step=0.002)
    op = eo.build_opportunity(db, asset="XAU", event_time=t0, title="测试事件",
                              risk_budget_pct=0.5, max_position_pct=10.0)
    r = op["risk"]
    assert r["stop_loss_pct"] >= eo.MIN_STOP_PCT
    assert r["position_pct_cap"] <= 10.0
    assert r["max_loss_pct"] <= 0.5 + 1e-9
    assert r["target_pct"] >= r["stop_loss_pct"]


def test_t0_and_t1_settlement_timing(db):
    """XAU T+0 → 当日可平; 股票 T+1 → 最早次日。"""
    now = int(time.time())
    t0 = now - 10 * DAY
    op0 = eo.build_opportunity(db, asset="XAU", event_time=t0, title="测试事件")
    assert op0["timing"]["t_plus"] == "T+0"
    assert op0["timing"]["earliest_exit_ts"] == t0

    op1 = eo.build_opportunity(db, asset="STOCK", event_time=t0, title="测试事件")
    assert op1["timing"]["t_plus"] == "T+1"
    assert op1["timing"]["earliest_exit_ts"] > t0


def test_entry_plan_modes(db):
    """CPI 默认"确认后进"; 显式 immediate 时立刻进。"""
    now = int(time.time())
    t0 = now - 10 * DAY
    auto = eo.build_opportunity(db, asset="XAU", event_time=t0, title="CPI高于预期")
    assert auto["timing"]["entry_mode"] == "confirm"
    assert auto["timing"]["entry_window_minutes"] == eo.CONFIRM_WINDOW_MINUTES

    imm = eo.build_opportunity(db, asset="XAU", event_time=t0, title="CPI高于预期",
                               entry_mode="immediate")
    assert imm["timing"]["entry_mode"] == "immediate"
    assert imm["timing"]["entry_window_minutes"] == eo.IMMEDIATE_WINDOW_MINUTES


def test_list_opportunities_shape(db):
    """列表接口: n/ready/items + 时点声明。"""
    now = int(time.time())
    t0 = now - 2 * DAY
    for i in range(8):
        _add_event(db, ts=t0 - (i + 1) * DAY, impact=0.02, direction="up")
    _add_bars(db, end_ts=t0, n=30, step=0.002)
    _add_event(db, ts=t0, title="央行购金", impact=0.02, direction="up")

    out = eo.list_opportunities(db, asset="XAU", from_ts=t0 - DAY, to_ts=now, limit=10)
    assert out["ok"] is True
    assert out["n"] == len(out["items"])
    assert out["point_in_time"] is True
    assert out["source"] == "event_kline_impact"
    assert out["honest_note"]
    for it in out["items"]:
        assert it["status"] in ("READY", "NO_TRADE")
        assert it["direction"] in ("LONG", "SHORT", "NO_TRADE")
        assert it["rationale"]
        assert it["timing"]["entry_mode"] in ("confirm", "immediate")



def test_edge_measure_is_declared_as_upper_bound(db):
    """edge 来自"4h 最大偏移" → 必须显式声明它是可实现收益的**上界**。

    不能让前端/复盘把 edge_pct 误读成"这一单预期能赚多少"。
    """
    now = int(time.time())
    t0 = now - 10 * DAY
    for i in range(8):
        _add_event(db, ts=t0 - (i + 1) * DAY, impact=0.02, direction="up")
    _add_bars(db, end_ts=t0, n=30, step=0.002)

    op = eo.build_opportunity(db, asset="XAU", event_time=t0, title="测试事件")
    assert op["edge_measure"] == eo.EDGE_MEASURE
    assert op["edge_measure"] == "max_excursion_4h"
    assert op["edge_measure_note"], "必须带口径说明, 空的等于没说"
    assert "上界" in op["edge_measure_note"]

    # 列表接口的 honest_note 也要带上这条边界
    out = eo.list_opportunities(db, asset="XAU", from_ts=t0 - DAY, to_ts=now, limit=5)
    assert "上界" in out["honest_note"]
    for it in out["items"]:
        assert it["edge_measure"] == eo.EDGE_MEASURE


def test_no_direction_no_trade(db):
    """既没样本也没规则先验 → 诚实报 NO_TRADE。"""
    now = int(time.time())
    t0 = now - 10 * DAY
    op = eo.build_opportunity(db, asset="XAU", event_time=t0, title="某公司发公告")
    assert op["direction"] == "NO_TRADE"
    assert op["status"] == "NO_TRADE"
    assert op["reason"]
    assert op["confidence"] == 0.0
