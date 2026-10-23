"""v230+ safety_guards 模块单测。"""
# -*- coding: utf-8 -*-
from __future__ import annotations

import os
import sys

import pytest

SRC = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "src_python"))
if SRC not in sys.path:
    sys.path.insert(0, SRC)

import db
import safety_guards


@pytest.fixture
def conn():
    """用临时 :memory: 数据库 + 必要的 quick_sim_trades schema。

    关闭 FK 强制：测试插入的 decision_id/raw_news_id 不一定有父行；
    其他测试用 migrate() 跑过真实 schema 会留下 FK 约束，这里直接关掉避免污染。
    """
    c = db.get_connection()
    c.execute("PRAGMA foreign_keys=OFF")
    c.execute("""
        CREATE TABLE IF NOT EXISTS quick_sim_trades (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            decision_id INTEGER, news_id INTEGER,
            asset TEXT NOT NULL, action TEXT NOT NULL,
            score REAL, evidence_confidence REAL,
            gate_passed INTEGER, gate_reason TEXT,
            strategy_id INTEGER, strategy_version_id INTEGER,
            paper_trading_run_id INTEGER, notional_usdt REAL,
            entry_price REAL, exit_price REAL,
            entry_ts INTEGER, exit_ts INTEGER,
            horizon_minutes INTEGER, settled INTEGER NOT NULL DEFAULT 0,
            pnl_pct REAL, pnl_usdt REAL, verdict TEXT,
            max_price REAL, min_price REAL,
            mfe_pct REAL, mae_pct REAL, event_strength TEXT,
            created_at TEXT NOT NULL DEFAULT (datetime('now'))
        )
    """)
    c.commit()
    # 每个测试前清表：conftest 把 DB 隔离到 temp 文件而非 :memory:，
    # 跨测试会残留数据，导致"昨日排除"这类断言读到上一次的记录。
    c.execute("DELETE FROM quick_sim_trades")
    c.commit()
    yield c
    c.close()


_DECISION_ID_COUNTER = [10000]  # 避 UNIQUE 冲突：从大数起


def _insert_settled(c, exit_ts, pnl_usdt):
    _DECISION_ID_COUNTER[0] += 1
    did = _DECISION_ID_COUNTER[0]
    c.execute(
        """INSERT INTO quick_sim_trades
           (decision_id, asset, action, entry_price, exit_price,
            entry_ts, exit_ts, horizon_minutes, settled, pnl_pct, pnl_usdt, verdict)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
        (did, "BTC", "BUY", 100.0, 101.0, exit_ts - 1200, exit_ts, 20, 1,
         (pnl_usdt / 100.0) * 100, pnl_usdt,
         "WIN" if pnl_usdt > 0 else "LOSS"),
    )
    c.commit()


# ── P0.3 日亏熔断 ─────────────────────────────────────────────────────────
def test_daily_drawdown_no_data_allows(conn):
    """无任何已结算交易 → 放行。"""
    import time
    decision = safety_guards.check_daily_drawdown(conn, daily_loss_limit_usdt=10.0,
                                                  now_ts=int(time.time()))
    assert decision.allow is True
    assert decision.guard == "daily_loss"
    assert decision.metadata["daily_loss_usdt"] == 0.0


def test_daily_drawdown_profit_allows(conn):
    """当日累计盈利 → 放行。"""
    import time
    now = int(time.time())
    _insert_settled(conn, now, pnl_usdt=5.0)
    _insert_settled(conn, now - 10, pnl_usdt=3.0)
    decision = safety_guards.check_daily_drawdown(conn, daily_loss_limit_usdt=50.0,
                                                  now_ts=now)
    assert decision.allow is True
    assert decision.metadata["daily_pnl_usdt"] == pytest.approx(8.0, abs=1e-6)
    assert decision.metadata["daily_loss_usdt"] == 0.0


def test_daily_drawdown_loss_blocks(conn):
    """当日亏损 >= 限额 → 拒绝。"""
    import time
    now = int(time.time())
    _insert_settled(conn, now, pnl_usdt=-15.0)
    _insert_settled(conn, now - 10, pnl_usdt=-10.0)
    decision = safety_guards.check_daily_drawdown(conn, daily_loss_limit_usdt=20.0,
                                                  now_ts=now)
    assert decision.allow is False
    assert decision.guard == "daily_loss"
    assert "daily_loss" in decision.reason
    assert decision.metadata["daily_loss_usdt"] == pytest.approx(25.0, abs=1e-6)


def test_daily_drawdown_yesterday_excluded(conn):
    """昨日亏损不在今日统计内 → 放行。"""
    import time
    now = int(time.time())
    yesterday = now - 86400 - 100
    _insert_settled(conn, yesterday, pnl_usdt=-100.0)
    decision = safety_guards.check_daily_drawdown(conn, daily_loss_limit_usdt=20.0,
                                                  now_ts=now)
    assert decision.allow is True
    assert decision.metadata["daily_pnl_usdt"] == 0.0


def test_daily_drawdown_boundary(conn):
    """临界值：亏损 == 限额 → 拒绝（>=）。"""
    import time
    now = int(time.time())
    _insert_settled(conn, now, pnl_usdt=-20.0)
    decision = safety_guards.check_daily_drawdown(conn, daily_loss_limit_usdt=20.0,
                                                  now_ts=now)
    assert decision.allow is False


# ── P0.4 事件窗口 ─────────────────────────────────────────────────────────
def test_event_window_no_events_allows():
    """无事件列表 → 放行。"""
    decision = safety_guards.check_event_window(now_ts=1_700_000_000, events=[])
    assert decision.allow is True
    assert decision.guard == "event_window"
    assert decision.metadata["events_loaded"] == 0


def test_event_window_hit_blocks():
    """当前时间在事件窗口内 → 拒绝。"""
    now = 1_700_000_000
    events = [{"name": "FOMC", "ts": now - 100, "window_seconds": 300}]
    decision = safety_guards.check_event_window(now_ts=now, events=events)
    assert decision.allow is False
    assert "FOMC" in decision.reason
    assert decision.metadata["distance_sec"] == 100


def test_event_window_outside_allows():
    """超出事件窗口 → 放行。"""
    now = 1_700_000_000
    events = [{"name": "FOMC", "ts": now - 1000, "window_seconds": 300}]
    decision = safety_guards.check_event_window(now_ts=now, events=events)
    assert decision.allow is True


def test_event_window_per_event_override():
    """单条事件可用 window_seconds 字段覆盖默认窗口。"""
    now = 1_700_000_000
    events = [{"name": "CPI", "ts": now - 100, "window_seconds": 60}]
    decision_default = safety_guards.check_event_window(
        now_ts=now, window_seconds=300, events=events)
    assert decision_default.allow is True
    events2 = [{"name": "CPI", "ts": now - 50, "window_seconds": 100}]
    decision_hit = safety_guards.check_event_window(
        now_ts=now, window_seconds=300, events=events2)
    assert decision_hit.allow is False


# ── 一站式 ────────────────────────────────────────────────────────────────
def test_evaluate_all_disabled_guards(conn):
    """两个护栏都 disabled → 直接放行。"""
    import time
    now = int(time.time())
    _insert_settled(conn, now, pnl_usdt=-100.0)
    events = [{"name": "FOMC", "ts": now, "window_seconds": 300}]
    decision = safety_guards.evaluate_all(
        conn, daily_loss_limit_usdt=20.0,
        daily_loss_enabled=False, event_window_enabled=False,
        events=events, now_ts=now,
    )
    assert decision.allow is True
    assert decision.reason == "all_guards_passed"


def test_evaluate_all_daily_blocks_first(conn):
    """日亏先拒绝 → 整体拒绝。"""
    import time
    now = int(time.time())
    _insert_settled(conn, now, pnl_usdt=-50.0)
    decision = safety_guards.evaluate_all(
        conn, daily_loss_limit_usdt=20.0, now_ts=now,
    )
    assert decision.allow is False
    assert decision.guard == "daily_loss"


def test_evaluate_all_event_blocks(conn):
    """日亏通过但事件命中 → 整体拒绝。"""
    import time
    now = int(time.time())
    _insert_settled(conn, now, pnl_usdt=-5.0)
    events = [{"name": "NFP", "ts": now, "window_seconds": 300}]
    decision = safety_guards.evaluate_all(
        conn, daily_loss_limit_usdt=20.0, events=events, now_ts=now,
    )
    assert decision.allow is False
    assert decision.guard == "event_window"


def test_safety_decision_to_dict():
    """SafetyDecision.to_dict() 应可序列化。"""
    d = safety_guards.SafetyDecision(
        allow=True, guard="ok", reason="test",
        metadata={"x": 1, "y": "z"},
    )
    out = d.to_dict()
    assert out["allow"] is True
    assert out["guard"] == "ok"
    assert out["metadata"] == {"x": 1, "y": "z"}
