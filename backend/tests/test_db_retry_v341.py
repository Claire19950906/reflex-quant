#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""v341: db_retry —— 写锁竞争兜底。

对应真实现象：wscn_flash / sina_flash 日志里的
    [WARN] 写入失败: database is locked
根因是"读事务升级成写事务"时 SQLITE_BUSY 不等 busy_timeout。
"""
import os
import sqlite3
import sys
import threading
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src_python"))

import db_retry


def _mkdb(tmp_path):
    p = tmp_path / "t.db"
    conn = sqlite3.connect(p)
    conn.execute("CREATE TABLE t (id INTEGER PRIMARY KEY, v TEXT)")
    conn.commit()
    conn.close()
    return str(p)


def test_connect_is_autocommit(tmp_path):
    path = _mkdb(tmp_path)
    conn = db_retry.connect_autocommit(path)
    try:
        assert conn.isolation_level is None      # 自动提交：不会开隐式读事务
        assert conn.execute("PRAGMA busy_timeout").fetchone()[0] >= 20000
    finally:
        conn.close()


def test_insert_retries_until_lock_released(tmp_path):
    """另一个连接持写锁 → 本连接重试 → 锁释放后写成功（不丢数据）。"""
    path = _mkdb(tmp_path)
    # check_same_thread=False：释放锁要在另一个线程里做（sqlite3 对象默认线程绑定）
    holder = sqlite3.connect(path, timeout=1, isolation_level=None,
                             check_same_thread=False)
    holder.execute("BEGIN IMMEDIATE")            # 占住写锁

    def release():
        time.sleep(0.4)
        try:
            holder.execute("ROLLBACK")
            holder.close()
        except sqlite3.Error:
            pass

    threading.Thread(target=release, daemon=True).start()

    # busy_ms 调小：验证的是"重试逻辑"，不是"等 30 秒 busy_timeout"
    conn = db_retry.connect_autocommit(path, busy_ms=150)
    cur = conn.cursor()
    ok = db_retry.insert_with_retry(cur, "INSERT INTO t (v) VALUES (?)", ("x",),
                                    tries=5, base_sleep=0.2)
    n = conn.execute("SELECT COUNT(*) FROM t").fetchone()[0]
    conn.close()
    assert ok is True
    assert n == 1


def test_insert_gives_up_after_tries(tmp_path):
    path = _mkdb(tmp_path)
    holder = sqlite3.connect(path, timeout=1, isolation_level=None)
    holder.execute("BEGIN IMMEDIATE")
    conn = db_retry.connect_autocommit(path, busy_ms=150)
    cur = conn.cursor()
    ok = db_retry.insert_with_retry(cur, "INSERT INTO t (v) VALUES (?)", ("y",),
                                    tries=2, base_sleep=0.1)
    holder.execute("ROLLBACK")
    holder.close()
    conn.close()
    assert ok is False       # 放弃而不是抛异常，整轮抓取不中断


def test_run_write_reraises_non_lock_errors(tmp_path):
    path = _mkdb(tmp_path)
    conn = db_retry.connect_autocommit(path)

    def boom(c):
        c.execute("INSERT INTO t (v) VALUES (?)", None)   # 参数错误，不是锁

    try:
        db_retry.run_write(conn, boom, tries=3)
    except sqlite3.Error:
        pass
    else:
        raise AssertionError("非锁错误应原样抛出")
    finally:
        conn.close()
