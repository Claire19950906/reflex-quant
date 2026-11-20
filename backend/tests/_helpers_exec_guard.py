# -*- coding: utf-8 -*-
"""测试助手：给临时 DB 补齐 `execution_guards.portfolio_guard` 需要的表。

背景（2026-11-19）：`execution_guards.check_new_entry()` 上线后，TradeExecutor 每次
开仓前都会读 `t2_positions`（现有持仓）和 `t2_account`（权益）。很多老测试的临时
库只建了 `ai_decisions` / `trade_audit_log`，查不到表 → guard 返回
`portfolio_guard_db_error:no such table: t2_positions` → 下单被拒成 RISK_REJECTED，
表现为"这些用例以前全过、现在全红"。

这里统一补最小表结构 + 一条正权益账户，让 guard 走"放行"分支。
"""
from __future__ import annotations

import sqlite3

_DDL = """
CREATE TABLE IF NOT EXISTS t2_positions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    asset TEXT,
    side TEXT,
    qty REAL,
    entry_price REAL,
    stop_price REAL,
    status TEXT DEFAULT 'open',
    opened_ts TEXT DEFAULT '',
    closed_ts TEXT DEFAULT '',
    exit_price REAL,
    realized_pnl REAL,
    note TEXT DEFAULT ''
);
CREATE TABLE IF NOT EXISTS t2_account (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    cash REAL,
    equity REAL,
    ts TEXT DEFAULT (datetime('now'))
);
CREATE TABLE IF NOT EXISTS t2_orders (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    position_id INTEGER,
    asset TEXT,
    side TEXT,
    qty REAL,
    price REAL,
    status TEXT DEFAULT '',
    created_at TEXT DEFAULT (datetime('now'))
);
CREATE TABLE IF NOT EXISTS raw_news (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    title TEXT DEFAULT '',
    content TEXT DEFAULT '',
    source TEXT DEFAULT '',
    created_at TEXT DEFAULT (datetime('now'))
);
"""


def add_execution_guard_tables(conn: sqlite3.Connection, equity: float = 100000.0) -> None:
    """补齐 guard 依赖的表；默认账户权益 10 万 USDT（足够让 5,000 的单不被截）。"""
    conn.executescript(_DDL)
    conn.execute(
        "INSERT INTO t2_account (cash, equity) VALUES (?, ?)", (equity, equity)
    )
    conn.commit()
