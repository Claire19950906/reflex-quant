#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""db_retry.py — 写入端的锁竞争兜底（v341）。

问题形态（实测 `wscn_flash` / `sina_flash` 日志）：
    [WARN] 写入失败: database is locked

不是"锁被占了几十秒"那么简单。这两个抓取脚本典型写法是：

    conn = sqlite3.connect(path)            # 默认 isolation_level=''
    cur.execute("SELECT 1 FROM raw_news WHERE content_fingerprint=? ...")
    cur.execute("INSERT INTO raw_news ...")  # ← 这里炸

Python 的默认隔离级别会在**第一条 DML 之前**隐式 BEGIN，但 SELECT 已经
开启了读事务。此时别的进程（引擎 / 其他 cron）正好提交了写，
当前连接要从"读事务"升级成"写事务" → SQLite 直接返回 SQLITE_BUSY，
**busy_timeout 根本不会等**（busy handler 只对"拿不到锁"重试，
不解决快照升级冲突）。于是 PRAGMA busy_timeout=20000 形同虚设。

两条修法，这里都用：
  · `connect_autocommit()`：isolation_level=None，每条语句自带事务，
    不再有"读事务升级"这条路；
  · `insert_with_retry()`：真撞上别的进程长时间持锁时退避重试，
    而不是直接丢一条新闻。
"""
from __future__ import annotations

import random
import sqlite3
import time

DEFAULT_TIMEOUT = 30.0
# v342 实测修正（2026-11-16）：探针 60 秒抢写锁 75 次，成功 51 次（68%），
# 拿锁平均 41 ms、最长 758 ms —— 说明写锁**不是被长事务占几十秒**，
# 而是几十个短写入者在亚秒级高频互撞。此时"先睡 2 秒再试"是错的：
# 睡得越久越抢不到，还会把抓取节奏拖垮。正确做法是**先密集快速重试**，
# 越到后面才拉长间隔。
DEFAULT_TRIES = 10          # 快速重试多给几次机会
DEFAULT_BASE_SLEEP = 0.15   # 首轮只等 0.15 秒（覆盖 41ms 的中位抢锁时间）
DEFAULT_MAX_SLEEP = 2.0     # 单次等待封顶 2 秒，不做"一睡 20 秒"
BACKOFF_JITTER = 0.2        # ±20% 抖动：多个 cron 同时撞锁时不要同步重试


def connect_autocommit(path: str, timeout: float = DEFAULT_TIMEOUT,
                       busy_ms: int = 30000) -> sqlite3.Connection:
    """自动提交模式的连接：每次 execute 独立事务，避免读事务升级成写事务。"""
    conn = sqlite3.connect(path, timeout=timeout, isolation_level=None)
    conn.execute(f"PRAGMA busy_timeout={int(busy_ms)}")
    return conn


def _is_lock_error(exc: Exception) -> bool:
    return isinstance(exc, sqlite3.OperationalError) and "locked" in str(exc).lower()


def _backoff(i: int, base_sleep: float, max_sleep: float) -> float:
    """第 i 次失败后的等待秒数：指数增长、封顶 max_sleep、带 ±20% 抖动。"""
    wait = min(base_sleep * (2 ** i), max_sleep)
    return max(0.05, wait * (1 + random.uniform(-BACKOFF_JITTER, BACKOFF_JITTER)))


def insert_with_retry(cur: sqlite3.Cursor, sql: str, params,
                      tries: int | None = None, base_sleep: float = DEFAULT_BASE_SLEEP,
                      max_sleep: float = DEFAULT_MAX_SLEEP) -> bool:
    """执行一条写语句；遇到 database is locked 退避重试。成功返回 True。

    默认节奏：0.15 → 0.3 → 0.6 → 1.2 → 2.0 → 2.0 …（10 次，含 ±20% 抖动）。
    这是按实测调出来的：写锁被占的是"零点几秒"，密集快试比长睡更容易抢到。
    放弃时只打日志不抛异常，保证整轮抓取不中断。
    """
    tries = DEFAULT_TRIES if tries is None else tries
    for i in range(tries):
        try:
            cur.execute(sql, params)
            return True
        except sqlite3.Error as exc:
            if not _is_lock_error(exc) or i == tries - 1:
                print(f"[WARN] 写入失败(重试 {i + 1} 次后放弃): {exc}", flush=True)
                return False
            time.sleep(_backoff(i, base_sleep, max_sleep))
    return False


def run_write(conn: sqlite3.Connection, fn, tries: int | None = None,
              base_sleep: float = DEFAULT_BASE_SLEEP, max_sleep: float = DEFAULT_MAX_SLEEP,
              busy_ms: int = 500):
    """把一段写操作整体包起来重试（用于事务型写入）。

    fn(conn) 里应该做完所有写并自己 commit；遇到锁冲突整段重跑。

    v489 事故：探针里 `daily_archive.archive_signal_chain` 出现 **66.5 秒** 的事务。
    原因不是重试本身慢（10 次×封顶 2s 也就 12s），而是**驱动层的 busy handler**
    每次都先阻塞 20 秒（连接默认 busy_timeout=20000ms）才抛 locked，重试 3 轮
    就叠成 60 秒 —— 整段时间还占着写事务计时。重试本来是"我们自己控制节奏"，
    就不该让驱动层再插一层长阻塞：进入前把 busy_timeout 压到 busy_ms（默认 500ms），
    退出时原样还原，让退避节奏由 _backoff 决定。
    """
    tries = DEFAULT_TRIES if tries is None else tries
    try:
        prev_busy = int(conn.execute("PRAGMA busy_timeout").fetchone()[0])
    except Exception:
        prev_busy = None
    if prev_busy is not None and prev_busy > busy_ms:
        try:
            conn.execute(f"PRAGMA busy_timeout={int(busy_ms)}")
        except Exception:
            prev_busy = None
    try:
        for i in range(tries):
            try:
                return fn(conn)
            except sqlite3.Error as exc:
                if not _is_lock_error(exc) or i == tries - 1:
                    raise
                wait = _backoff(i, base_sleep, max_sleep)
                print(f"[LOCK] 写库被占，{wait:.2f}s 后重试 ({i + 1}/{tries - 1})", flush=True)
                time.sleep(wait)
    finally:
        if prev_busy is not None:
            try:
                conn.execute(f"PRAGMA busy_timeout={prev_busy}")
            except Exception:
                pass
