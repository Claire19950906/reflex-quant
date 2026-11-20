#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""v434：`processing_at IS NULL` 的 PROCESSING 僵尸必须能被回收。

事故（2026-11-17 18:00–19:14 生产实测）
--------------------------------------
6 条新闻（33918 / 33981 / 34008 / 34011 / 34017 / 34025）挂在 status='PROCESSING'，
`processing_at` 为空，从 18:00 一直到 19:14 都没人处理。看门狗每轮都跑，
但回收 SQL 有一条 `processing_at IS NOT NULL` —— 这 6 行被这条判据排除在外，
**永远回收不到**，只有重启时的启动复位（`_STARTUP_RESET_PENDING_SQL`）才清得掉。
后果：它们占着「待分析」计数、占着认领幂等（已经有决策的行不再进队列），
但对账口径看不出来 —— 因为状态既不是 PENDING 也不是 DONE。

为什么 NULL 一定是异常
---------------------
认领 SQL（`_BATCH_CLAIM_FRESH_SQL` / `_BATCH_CLAIM_SQL`）是**同一个 UPDATE**
同时写 `status='PROCESSING'` 与 `processing_at=datetime('now','localtime')`，
不可能只写一半。所以 NULL 只可能来自异常路径（批中途被杀 / 落库回写异常）。
"""
from __future__ import annotations

import os
import sqlite3
import sys
import time

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src_python"))

import engine.ai_worker as aw


_SCHEMA = """
CREATE TABLE raw_news (
  id INTEGER PRIMARY KEY,
  status TEXT,
  processing_at TEXT,
  ts INTEGER,
  retry_count INTEGER DEFAULT 0,
  is_noise INTEGER DEFAULT 0
);
"""


@pytest.fixture
def db():
    conn = sqlite3.connect(":memory:")
    conn.executescript(_SCHEMA)
    yield conn
    conn.close()


def _insert(conn, rid, status="PROCESSING", processing_at=None,
            ts=None, retry_count=0):
    conn.execute(
        "INSERT INTO raw_news (id, status, processing_at, ts, retry_count)"
        " VALUES (?,?,?,?,?)", (rid, status, processing_at, ts, retry_count))
    conn.commit()


def _reclaim(conn, active_ids=None):
    sql, params = aw._build_reclaim_sql(active_ids)
    return conn.execute(sql, params).rowcount


def _state(conn, rid):
    return conn.execute(
        "SELECT status, retry_count FROM raw_news WHERE id=?", (rid,)).fetchone()


def test_null_processing_at_stale_is_reclaimed(db):
    """processing_at 为空 + 新闻 2 小时前 → 必须回收（本轮主修复）。"""
    _insert(db, 1, processing_at=None, ts=int(time.time()) - 7200)
    assert _reclaim(db) == 1
    status, retry = _state(db, 1)
    assert status == "PENDING" and retry == 1


def test_null_processing_at_fresh_is_kept(db):
    """processing_at 为空但新闻是刚入库的 → 不回收（避免误伤在办批次）。"""
    _insert(db, 2, processing_at=None, ts=int(time.time()) - 60)
    assert _reclaim(db) == 0
    assert _state(db, 2) == ("PROCESSING", 0)


def test_null_processing_at_and_null_ts_is_reclaimed(db):
    """两个时间都缺失的异常行也要能回收（否则就是永远的死行）。"""
    _insert(db, 3, processing_at=None, ts=None)
    assert _reclaim(db) == 1
    assert _state(db, 3) == ("PENDING", 1)


def test_old_behaviour_still_works(db):
    """原来的「processing_at 超 15 分钟」路径不能被改坏。"""
    old = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(time.time() - 7200))
    _insert(db, 4, processing_at=old, ts=int(time.time()) - 7200)
    assert _reclaim(db) == 1


def test_fresh_claim_with_processing_at_is_kept(db):
    """刚认领（processing_at=now）的行绝不能被回收。"""
    now = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime())
    _insert(db, 5, processing_at=now, ts=int(time.time()) - 7200)
    assert _reclaim(db) == 0


def test_retry_threshold_marks_failed(db):
    """retry_count 达阈值 → 标 FAILED，不再无限重试。"""
    _insert(db, 6, processing_at=None,
            ts=int(time.time()) - 7200,
            retry_count=aw._MAX_PENDING_RETRIES - 1)
    assert _reclaim(db) == 1
    assert _state(db, 6) == ("FAILED", aw._MAX_PENDING_RETRIES)


def test_active_batch_is_excluded(db):
    """在办批次（租约未过期）必须跳过，否则批事务末尾匹配 0 行、整批白跑。"""
    _insert(db, 7, processing_at=None, ts=int(time.time()) - 7200)
    _insert(db, 8, processing_at=None, ts=int(time.time()) - 7200)
    assert _reclaim(db, active_ids=[7]) == 1
    assert _state(db, 8)[0] == "PENDING"
    assert _state(db, 7)[0] == "PROCESSING"


def test_non_processing_rows_untouched(db):
    """PENDING / DONE 的行不属于回收对象。"""
    _insert(db, 9, status="PENDING", processing_at=None, ts=int(time.time()) - 7200)
    _insert(db, 10, status="DONE", processing_at=None, ts=int(time.time()) - 7200)
    assert _reclaim(db) == 0
