# -*- coding: utf-8 -*-
"""v457：写锁重试。

背景：`cron_tech_signals` 和 `cron_tech_shadow` 每小时各跑一次，偶尔撞上
别的进程正在写同一个 sqlite 文件，直接抛 `database is locked` 整轮就废掉。
加退避重试时最容易出两个毛病，这里各锁一条：
  1. 把**所有**异常都当锁冲突重试 —— 真正的代码 bug 被吞掉，永远报不出来；
  2. 重试前不 `rollback()` —— 上一笔事务还挂着，重试多少次都拿不到锁。
"""
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src_python"))

import cron_tech_signals as cts


class _Conn:
    """只记 rollback 次数，够用。"""

    def __init__(self):
        self.rollbacks = 0

    def rollback(self):
        self.rollbacks += 1


def _patch_sleep(monkeypatch):
    sleeps = []
    monkeypatch.setattr(cts.time, "sleep", lambda s: sleeps.append(s))
    return sleeps


def test_locked_twice_then_success(monkeypatch):
    """锁冲突两次后成功：重试 2 次、回滚 2 次、退避 2/8 秒。

    重点不是次数本身，而是「重试确实发生过且最终拿到结果」——
    如果退避表被改成 [0, 0, ...]，这条也会过；所以同时断言退避秒数。
    """
    sleeps = _patch_sleep(monkeypatch)
    conn = _Conn()
    calls = []

    def fn(c, **kwargs):
        calls.append(1)
        if len(calls) <= 2:
            raise sqlite3.OperationalError("database is locked")
        return {"written": 7}

    res = cts._backfill_with_retry(conn, fn, dry_run=False)

    assert res == {"written": 7}
    assert len(calls) == 3
    assert conn.rollbacks == 2
    assert sleeps == [2, 8]


def test_non_lock_error_propagates_immediately(monkeypatch):
    """非锁异常不重试、不回滚 —— 代码 bug 必须当场炸出来。"""
    sleeps = _patch_sleep(monkeypatch)
    conn = _Conn()
    calls = []

    def fn(c, **kwargs):
        calls.append(1)
        raise sqlite3.OperationalError("no such column: foo")

    try:
        cts._backfill_with_retry(conn, fn)
    except sqlite3.OperationalError as exc:
        assert "no such column" in str(exc)
    else:
        raise AssertionError("非锁异常被吞了")
    assert len(calls) == 1
    assert conn.rollbacks == 0
    assert sleeps == []


def test_gives_up_after_max_retries(monkeypatch):
    """一直锁着：1 次首发 + 4 次重试 = 5 次，最后抛原异常，不静默返回空。"""
    sleeps = _patch_sleep(monkeypatch)
    conn = _Conn()
    calls = []

    def fn(c, **kwargs):
        calls.append(1)
        raise sqlite3.DatabaseError("database is locked")

    try:
        cts._backfill_with_retry(conn, fn)
    except sqlite3.DatabaseError:
        pass
    else:
        raise AssertionError("锁死时没有抛异常")
    assert len(calls) == cts.LOCK_RETRIES + 1
    assert conn.rollbacks == cts.LOCK_RETRIES   # 最后一次不留假重试
    assert sleeps == list(cts.LOCK_BACKOFF_S)
