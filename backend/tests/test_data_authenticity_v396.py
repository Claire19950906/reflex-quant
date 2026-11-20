#!/usr/bin/env python3
"""v396 P2 回归：报价健康闸门（R10）+ 数据真实性审计（S1/S3/S5/S7）。

守的是什么
----------
1. 冻结报价期间绝不允许建仓（否则入场价、止损距离、盈亏全部是假数）。
2. 结算价必须能对回"到期那一刻"的行情，对不上就是事故。
3. 账本与净值快照必须一致，不一致说明有一处在编数。
"""
from __future__ import annotations

import sqlite3
import sys
import time
from pathlib import Path

import pytest

SRC = Path(__file__).resolve().parents[1] / "src_python"
sys.path.insert(0, str(SRC))

import config
import data_authenticity as da
import db
import sample_quality_filter as sqf

NOW = 1794820321


def _conn() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript("""
        CREATE TABLE market_ticks (symbol TEXT, ts INTEGER, price REAL);
        CREATE TABLE ohlcv_intraday (asset TEXT, interval TEXT, ts INTEGER, close REAL);
        CREATE TABLE ohlcv_daily (asset TEXT, ts INTEGER, close REAL);
        CREATE TABLE quick_sim_trades (
            id INTEGER PRIMARY KEY, decision_id INTEGER, asset TEXT, action TEXT,
            entry_price REAL, entry_ts INTEGER, exit_price REAL, exit_ts INTEGER,
            horizon_minutes INTEGER, pnl_pct REAL, pnl_usdt REAL, verdict TEXT,
            settled INTEGER, cost_bps REAL, gross_pnl_pct REAL);
        CREATE TABLE sim_equity_snapshots (ts INTEGER, equity REAL, realized_pnl REAL);
    """)
    return conn


# ─────────────────────── R10 报价健康闸门 ───────────────────────


def test_quote_health_frozen():
    conn = _conn()
    # 60 分钟内 40 笔 tick 全是一个价 —— 事故现场的样子
    # 用真实时钟：固定 NOW 会随时间推移滑出报价健康窗口，R10 的拒绝理由就从
    # "冻结"漂成"过期"，断言就不稳定了（pytest 现在跑独立临时库，没有生产 tick 兜底）
    now = int(time.time())
    for i in range(40):
        conn.execute("INSERT INTO market_ticks VALUES ('WTI', ?, 88.985)", (now - i * 60,))
    qh = db.quote_health(conn, "WTI", now_ts=now, window_sec=3600, min_ticks=20)
    assert qh["frozen"] is True
    assert qh["n_ticks"] == 40
    assert qh["distinct_prices"] == 1


def test_quote_health_live():
    conn = _conn()
    for i in range(40):
        conn.execute("INSERT INTO market_ticks VALUES ('WTI', ?, ?)",
                     (NOW - i * 60, 88.9 + i * 0.01))
    qh = db.quote_health(conn, "WTI", now_ts=NOW)
    assert qh["frozen"] is False
    assert qh["distinct_prices"] > 1


def test_quote_health_no_ticks_returns_empty():
    assert db.quote_health(_conn(), "WTI", now_ts=NOW) == {}


def test_r10_blocks_frozen_quote(tmp_path, monkeypatch):
    conn = _conn()
    # 用真实时钟写入"冻结行情"（40 笔同价、覆盖最近 40 分钟）
    now = int(time.time())
    for i in range(40):
        conn.execute("INSERT INTO market_ticks VALUES ('WTI', ?, 88.985)", (now - i * 60,))
    monkeypatch.setattr(config, "QUOTE_HEALTH_GATE", True)
    trade = {"id": 1, "asset": "WTI", "action": "BUY", "pnl_pct": 0.5}
    decision = {"decision_id": 1, "trade_gate_reason": "", "evidence_action": "BUY",
                "evidence_confidence": 80.0, "status": "APPROVED"}
    res = sqf._check_one(trade, decision, ("WTI",), 10.0, max_batch_siblings=0, conn=conn)
    assert res["is_valid_for_alpha"] is False
    assert any(r.startswith("R10") for r in res["reasons"]), res["reasons"]


def test_r10_passes_live_quote(monkeypatch):
    conn = _conn()
    # 用真实时钟，否则 age 会随时间推移变成"过期"
    now = int(time.time())
    for i in range(40):
        conn.execute("INSERT INTO market_ticks VALUES ('WTI', ?, ?)",
                     (now - i * 60, 88.9 + i * 0.01))
    monkeypatch.setattr(config, "QUOTE_HEALTH_GATE", True)
    trade = {"id": 2, "asset": "WTI", "action": "BUY", "pnl_pct": 0.5}
    decision = {"decision_id": 2, "trade_gate_reason": "", "evidence_action": "BUY",
                "evidence_confidence": 80.0, "status": "APPROVED"}
    res = sqf._check_one(trade, decision, ("WTI",), 10.0, max_batch_siblings=0, conn=conn)
    assert res["is_valid_for_alpha"] is True


def test_r10_blocks_stale_quote(monkeypatch):
    """报价 40 分钟没更新 → 过期，不许建仓。"""
    conn = _conn()
    now = int(time.time())
    conn.execute("INSERT INTO market_ticks VALUES ('XAU', ?, 2400.0)", (now - 2400,))
    conn.execute("INSERT INTO market_ticks VALUES ('XAU', ?, 2400.1)", (now - 2399,))
    monkeypatch.setattr(config, "QUOTE_HEALTH_GATE", True)
    trade = {"id": 3, "asset": "XAU", "action": "SELL", "pnl_pct": 0.5}
    decision = {"decision_id": 3, "trade_gate_reason": "", "evidence_action": "SELL",
                "evidence_confidence": 80.0, "status": "APPROVED"}
    res = sqf._check_one(trade, decision, ("XAU",), 10.0, max_batch_siblings=0, conn=conn)
    assert res["is_valid_for_alpha"] is False
    assert any("R10" in r and "过期" in r for r in res["reasons"])


# ─────────────────────── 数据真实性审计 ───────────────────────


def test_s1_flags_settlement_price_mismatch():
    conn = _conn()
    entry_ts = NOW - 3600
    due = entry_ts + 20 * 60
    conn.execute("INSERT INTO ohlcv_intraday VALUES ('WTI','1m',?,88.30)", (due,))
    # 结算价用的是 11-10 的 86.43（差 2%），必须被抓出来
    conn.execute("""INSERT INTO quick_sim_trades
        (id, asset, action, entry_price, entry_ts, exit_price, exit_ts,
         horizon_minutes, pnl_pct, pnl_usdt, verdict, settled, cost_bps, gross_pnl_pct)
        VALUES (10,'WTI','BUY',88.98,?,86.43,?,20,-2.86,-2.86,'LOSS',1,30,0.0)""",
                 (entry_ts, due))
    violations = da.check_settle_price(conn)
    assert any(v["rule"] == "S1" and v["id"] == 10 for v in violations)


def test_s1_passes_when_price_matches():
    conn = _conn()
    entry_ts = NOW - 3600
    due = entry_ts + 20 * 60
    conn.execute("INSERT INTO ohlcv_intraday VALUES ('WTI','1m',?,88.30)", (due,))
    conn.execute("""INSERT INTO quick_sim_trades
        (id, asset, action, entry_price, entry_ts, exit_price, exit_ts,
         horizon_minutes, pnl_pct, pnl_usdt, verdict, settled, cost_bps, gross_pnl_pct)
        VALUES (11,'WTI','BUY',88.98,?,88.335,?,20,-0.13,-0.13,'LOSS',1,30,0.167)""",
                 (entry_ts, due))
    assert da.check_settle_price(conn) == []


def test_s3_flags_frozen_quote():
    conn = _conn()
    for i in range(40):
        conn.execute("INSERT INTO market_ticks VALUES ('WTI', ?, 88.985)", (NOW - i * 60,))
    violations = da.check_frozen_quotes(conn, now_ts=NOW)
    assert any(v["rule"] == "S3" and v["asset"] == "WTI" for v in violations)


def test_s5_flags_book_vs_equity_mismatch():
    conn = _conn()
    conn.execute("""INSERT INTO quick_sim_trades
        (id, asset, action, entry_price, entry_ts, exit_price, exit_ts,
         horizon_minutes, pnl_pct, pnl_usdt, verdict, settled, cost_bps, gross_pnl_pct)
        VALUES (12,'WTI','SELL',88.483,?,88.335,?,20,-0.13,-0.13,'LOSS',1,30,0.167)""",
                 (NOW - 1200, NOW))
    conn.execute("INSERT INTO sim_equity_snapshots VALUES (?, 9784.72, -215.28)", (NOW,))
    violations = da.check_account_reconciliation(conn)
    assert any(v["rule"] == "S5" for v in violations)


def test_s7_flags_overdue_open_trade():
    conn = _conn()
    conn.execute("""INSERT INTO quick_sim_trades
        (id, asset, action, entry_price, entry_ts, exit_price, exit_ts,
         horizon_minutes, pnl_pct, pnl_usdt, verdict, settled, cost_bps, gross_pnl_pct)
        VALUES (13,'WTI','BUY',88.98,?,NULL,NULL,20,0.0,0.0,'',0,30,NULL)""",
                 (NOW - 7200,))
    violations = da.check_dangling_open(conn, now_ts=NOW)
    assert any(v["rule"] == "S7" and v["id"] == 13 for v in violations)
