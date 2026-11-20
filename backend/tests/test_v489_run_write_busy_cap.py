#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""v489: run_write 不再让驱动层 busy handler 叠 20 秒。

事故：探针里 `daily_archive.archive_signal_chain` 出现 66.5 秒写事务。
重试节奏本身只占 ~12 秒，多出来的是**驱动层**每次先阻塞 20 秒（连接默认
busy_timeout=20000ms）才抛 locked，重试 3 轮即叠到 60 秒。

契约：
  · run_write 执行期间把 busy_timeout 压到 busy_ms（默认 500ms）
  · 退出后**原样还原**（不能留下副作用影响同一连接的后续写入）
  · 原本就比 busy_ms 小的连接不做改动
"""
import os
import sqlite3
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src_python"))

import db_retry


def _mkconn(tmp_path, busy_ms: int) -> sqlite3.Connection:
    conn = sqlite3.connect(tmp_path / "t.db")
    conn.execute("CREATE TABLE IF NOT EXISTS t (id INTEGER PRIMARY KEY, v TEXT)")
    conn.commit()
    conn.execute(f"PRAGMA busy_timeout={busy_ms}")
    return conn


def test_run_write_lowers_busy_timeout_during_call(tmp_path):
    conn = _mkconn(tmp_path, 20000)
    seen = {}

    def _fn(c):
        seen["busy"] = int(c.execute("PRAGMA busy_timeout").fetchone()[0])
        return "ok"

    assert db_retry.run_write(conn, _fn) == "ok"
    assert seen["busy"] == 500, f"执行期间 busy_timeout 应压到 500ms，实际 {seen['busy']}"
    conn.close()


def test_run_write_restores_busy_timeout_after_call(tmp_path):
    conn = _mkconn(tmp_path, 20000)
    db_retry.run_write(conn, lambda c: None)
    assert int(conn.execute("PRAGMA busy_timeout").fetchone()[0]) == 20000, \
        "退出后必须还原原 busy_timeout，否则会污染同一连接的后续写入"
    conn.close()


def test_run_write_keeps_smaller_busy_timeout(tmp_path):
    conn = _mkconn(tmp_path, 200)
    seen = {}

    def _fn(c):
        seen["busy"] = int(c.execute("PRAGMA busy_timeout").fetchone()[0])

    db_retry.run_write(conn, _fn)
    assert seen["busy"] == 200, "原本更小的 busy_timeout 不该被抬回去"
    conn.close()


def test_run_write_restores_even_when_fn_raises(tmp_path):
    conn = _mkconn(tmp_path, 20000)

    def _boom(c):
        raise ValueError("写坏了")

    try:
        db_retry.run_write(conn, _boom)
    except ValueError:
        pass
    assert int(conn.execute("PRAGMA busy_timeout").fetchone()[0]) == 20000, \
        "业务异常路径也必须还原 busy_timeout"
    conn.close()
