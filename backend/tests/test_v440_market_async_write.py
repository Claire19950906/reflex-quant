#!/usr/bin/env python3
"""v440 回归：行情采样异步写入（timeseries.enqueue_market_snapshot）。

背景（2026-11-17 22:05 长事务哨兵）：
  api_server._write_tick_sync → record_market_snapshot 一次 commit 31.6s，
  里面几乎全是 retry_on_busy 的退避等待 —— 7 个品种每隔几秒各写一次，
  和引擎批次写抢同一把锁。tick/快照是采样数据（丢几条不影响决策），
  改为"入队即返回 + 单写线程顺序落库"。
"""
import os
import sqlite3
import sys

SRC = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "src_python"))
if SRC not in sys.path:
    sys.path.insert(0, SRC)

import timeseries as ts


def _payload(asset="BTC"):
    return {"items": [{"asset": asset, "price": 100.0, "change24h": 1.0,
                       "source": "test", "sourceCount": 1}]}


def test_enqueue_then_flush_writes_row(monkeypatch, isolated_db):
    monkeypatch.setattr(ts, "_MARKET_SYNC", False)
    monkeypatch.setenv("TRIDENT_MARKET_ASYNC_WRITE", "1")
    # 建表（record_market_snapshot 只写不建表，schema 归 db.migrate 管）
    # v459: 用独立临时库 —— 会话共享库上 migrate 会撞 database is locked（全量独有）
    conn = isolated_db

    assert ts.enqueue_market_snapshot(_payload()) is True
    st = ts.flush_market_writes()
    assert st["written"] >= 1, st

    n = conn.execute("SELECT COUNT(*) FROM market_ticks").fetchone()[0]
    assert n >= 1


def test_empty_payload_not_enqueued():
    assert ts.enqueue_market_snapshot({}) is False
    assert ts.enqueue_market_snapshot({"items": []}) is False


def test_sync_mode_returns_false(tmp_path, monkeypatch):
    monkeypatch.setattr(ts, "_MARKET_SYNC", True)
    assert ts.enqueue_market_snapshot(_payload()) is False
