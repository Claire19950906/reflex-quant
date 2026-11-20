#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""migrate_v489_schema.py — 两项表结构变更（幂等，可重复跑）

1) `ai_decisions` 加 `exec_state TEXT`
   目的：方向单（BUY/SELL）如果没拿到建仓价，必须留下"为什么没落地"的状态，
   否则 89% 的方向单 entry_price 为空、no_trade_reason 也为空，等于黑洞。

2) `market_ticks.change24h` 由 `NOT NULL DEFAULT 0.0` 改为允许 NULL
   目的：sina 源拿不到昨收时 `change_pct=None`，下游 `float(x or 0.0)` 把"不知道"
   写成了 0.00%。实测 WTI 15.5 万条 100% 是 0、XAU 81.3% 是 0 —— 前端把"没数据"
   画成"今天没动"。这是语义错误，不是脏数据。改 NULL 后，只有真·没波动才是 0。
   SQLite 改不了列约束，只能重建表（46 万行，几秒级）。

用法：
    python3 backend/scripts/migrate_v489_schema.py --dry-run
    python3 backend/scripts/migrate_v489_schema.py
"""
from __future__ import annotations

import argparse
import os
import sqlite3
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
BACKEND = os.path.dirname(HERE)
DEFAULT_DB = os.path.join(BACKEND, "trident_event_bus.db")

NEW_TICKS_DDL = """CREATE TABLE market_ticks_new (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  symbol TEXT NOT NULL,
  ts INTEGER NOT NULL,
  price REAL NOT NULL,
  change24h REAL DEFAULT NULL,
  source TEXT NOT NULL DEFAULT 'median',
  source_count INTEGER NOT NULL DEFAULT 0,
  UNIQUE (symbol, ts, source)
)"""


def _cols(conn, table: str) -> dict:
    return {r[1]: r for r in conn.execute(f"PRAGMA table_info({table})")}


def _has_table(conn, table: str) -> bool:
    return conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)
    ).fetchone() is not None


def step_exec_state(conn, dry: bool) -> str:
    if not _has_table(conn, "ai_decisions"):
        return "跳过：没有 ai_decisions 表"
    if "exec_state" in _cols(conn, "ai_decisions"):
        return "已存在 exec_state，跳过"
    if dry:
        return "将 ALTER TABLE ai_decisions ADD COLUMN exec_state TEXT"
    conn.execute("ALTER TABLE ai_decisions ADD COLUMN exec_state TEXT")
    return "已加列 exec_state TEXT"


def step_change24h_nullable(conn, dry: bool) -> str:
    if not _has_table(conn, "market_ticks"):
        return "跳过：没有 market_ticks 表"
    ddl = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='market_ticks'"
    ).fetchone()
    sql = (ddl[0] or "") if ddl else ""
    if "change24h REAL DEFAULT NULL" in sql or (
        "NOT NULL" not in sql.split("change24h")[1].split(",")[0]
    ):
        return "change24h 已可空，跳过"
    n = conn.execute("SELECT COUNT(*) FROM market_ticks").fetchone()[0]
    if dry:
        return f"将重建 market_ticks（{n} 行）：change24h NOT NULL DEFAULT 0.0 → 允许 NULL"
    t0 = time.time()
    conn.execute("BEGIN IMMEDIATE")
    try:
        conn.execute("DROP TABLE IF EXISTS market_ticks_new")
        conn.execute(NEW_TICKS_DDL)
        conn.execute(
            """INSERT INTO market_ticks_new(id, symbol, ts, price, change24h, source, source_count)
               SELECT id, symbol, ts, price,
                      CASE WHEN change24h = 0.0 THEN NULL ELSE change24h END,
                      source, source_count
                 FROM market_ticks"""
        )
        moved = conn.execute("SELECT COUNT(*) FROM market_ticks_new").fetchone()[0]
        conn.execute("DROP TABLE market_ticks")
        conn.execute("ALTER TABLE market_ticks_new RENAME TO market_ticks")
        # 自增序列：重建后 sqlite_sequence 会丢，手工接上，避免 id 从 1 重排撞主键
        conn.execute("DELETE FROM sqlite_sequence WHERE name='market_ticks'")
        conn.execute(
            "INSERT INTO sqlite_sequence(name, seq) "
            "SELECT 'market_ticks', COALESCE(MAX(id), 0) FROM market_ticks"
        )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    return (f"已重建 market_ticks：{moved}/{n} 行，change24h 允许 NULL "
            f"（历史 0.0 一并转成 NULL，{time.time() - t0:.1f}s）")


def step_backfill_exec_state(conn, dry: bool) -> str:
    """历史方向单补 exec_state。

    老代码在"取不到价"时什么都没记 —— 这批行既没有 entry_price 也没有原因。
    现在能把**可观测事实**写下来：没有建仓价 = missing_entry_price。
    不臆造"为什么没取到价"（那需要当时的日志，已经没了）。
    """
    if not _has_table(conn, "ai_decisions"):
        return "跳过：没有 ai_decisions 表"
    if "exec_state" not in _cols(conn, "ai_decisions"):
        return "跳过：还没加 exec_state 列"
    n = conn.execute(
        "SELECT COUNT(*) FROM ai_decisions "
        "WHERE exec_state IS NULL AND UPPER(suggested_action) IN ('BUY','SELL') "
        "AND (entry_price IS NULL OR entry_price = 0)"
    ).fetchone()[0]
    if not n:
        return "历史方向单无需补齐"
    if dry:
        return f"将为 {n} 条历史方向单补 exec_state=missing_entry_price"
    conn.execute(
        "UPDATE ai_decisions SET exec_state='missing_entry_price' "
        "WHERE exec_state IS NULL AND UPPER(suggested_action) IN ('BUY','SELL') "
        "AND (entry_price IS NULL OR entry_price = 0)"
    )
    conn.commit()
    return f"已为 {n} 条历史方向单补 exec_state=missing_entry_price"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default=os.getenv("TRIDENT_DB_PATH") or DEFAULT_DB)
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args()
    if not os.path.exists(a.db):
        raise SystemExit(f"库不存在：{a.db}")
    print(f"[migrate v489] {a.db}{'（dry-run）' if a.dry_run else ''}")
    conn = sqlite3.connect(a.db, timeout=30)
    conn.execute("PRAGMA busy_timeout=30000")
    try:
        print(" 1.", step_exec_state(conn, a.dry_run))
        print(" 2.", step_change24h_nullable(conn, a.dry_run))
        print(" 3.", step_backfill_exec_state(conn, a.dry_run))
    finally:
        conn.close()
    print("[migrate v489] 完成")
    return 0


if __name__ == "__main__":
    sys.exit(main())
