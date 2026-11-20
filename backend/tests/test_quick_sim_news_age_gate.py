# -*- coding: utf-8 -*-
"""quick_sim.pending_signals 新闻时效闸回归。

背景：快速模拟建仓链路原先只按「决策创建时间 120 分钟内」过滤，
没按新闻年龄判断 —— 积压时老新闻迟到出的方向单（新闻已过 45 分钟建仓窗口）
仍会进模拟台账，污染评测胜率。本用例锁定：超过建仓窗口的老信号不入选，
拿不到可靠时间的行按新鲜放行（与主分析 auto_trade 同口径）。
"""
from __future__ import annotations

import os
import sqlite3
import sys
import time
from pathlib import Path

SRC = Path(__file__).resolve().parent.parent / "src_python"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

import db
import config
from quick_sim import pending_signals

_DID = [10000]


def _next_did() -> int:
    _DID[0] += 1
    return _DID[0]


def _setup_db() -> sqlite3.Connection:
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA foreign_keys = OFF")
    db.migrate(c)
    return c


def _insert_signal(c: sqlite3.Connection, *, action: str = "BUY",
                   asset: str = "XAU", age_sec: int | None = None,
                   set_ts: bool = True) -> int:
    """造一条满足质量门的方向性决策；age_sec 控制新闻年龄。"""
    did = _next_did()
    if set_ts:
        ts = int(time.time()) if age_sec is None else int(time.time()) - age_sec
        c.execute(
            "INSERT INTO raw_news (source, content, published_at, ts) "
            "VALUES ('t', 'c', '', ?)", (ts,))
    else:
        # 时间完全缺失 → 辅助函数返回 None，按新鲜放行
        c.execute(
            "INSERT INTO raw_news (source, content, published_at) VALUES ('t', 'c', '')")
    news_id = c.execute("SELECT last_insert_rowid()").fetchone()[0]
    c.execute(
        """INSERT INTO ai_decisions
           (id, news_id, sentiment_score, suggested_action, reasoning,
            target_asset, evidence_confidence, trade_gate_reason,
            event_strength, event_phase, evidence_action, status, created_at)
           VALUES (?, ?, 0.0, ?, '', ?, 30.0, ?, 'medium', 'mid', ?,
                   'AUTO_APPROVED', datetime('now', 'localtime'))""",
        (did, news_id, action, asset,
         config.TRADE_GATE_PASSED_REASON, action))
    c.commit()
    return did


def _ids(c: sqlite3.Connection) -> set:
    return {r["id"] for r in pending_signals(c, limit=50)}


def test_fresh_signal_included():
    c = _setup_db()
    did = _insert_signal(c, age_sec=0)
    assert did in _ids(c)


def test_stale_signal_excluded():
    # 新闻已 2 小时，远超 45 分钟建仓窗口 → 不进模拟台账
    c = _setup_db()
    stale = _insert_signal(c, action="SELL", asset="WTI", age_sec=7200)
    fresh = _insert_signal(c, age_sec=0)
    ids = _ids(c)
    assert fresh in ids
    assert stale not in ids


def test_unknown_time_allowed():
    # 时间缺失不臆断为老新闻，按新鲜放行（避免误杀）
    c = _setup_db()
    did = _insert_signal(c, set_ts=False)
    assert did in _ids(c)
