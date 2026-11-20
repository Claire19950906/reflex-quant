#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""v510：/api/events 的 15 天事件缓存行为测试（api_server 事件链路）。

这条链路出过两次真实事故，所以测试盯的是"读不到/读不停/写不回"：
  ① 缓存永不退化为空 —— 首加载要等，等不到就返回空，绝不能挂死；
  ② TTL 过期只允许一个后台刷新（单飞），否则并发请求各自跑全表 SQL 把线程池打满，
     实测就是 /api/events 90 秒超时；
  ③ 刷新失败沿用旧缓存，且锁必须释放 —— 锁不释放，后续永远不再刷新；
  ④ `_EVENTS_15D_LAST_LOAD` 必须真的写回模块级变量（v481 漏 global，
     导致 LAST_LOAD 恒为 0 → 每个请求都再起一次全表 reload）。
"""
import sqlite3
import sys
import threading
import time
from pathlib import Path

import pytest

BACKEND = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND / "src_python"))

import api_server as api


@pytest.fixture(autouse=True)
def reset_cache():
    """每个用例都从"干净缓存 + 空闲锁"开始，避免用例间互相污染。"""
    with api._EVENTS_15D_LOCK:
        api._EVENTS_15D_CACHE.clear()
        api._EVENTS_15D_LAST_LOAD = 0.0
    if api._EVENTS_15D_RELOAD_LOCK.locked():
        api._EVENTS_15D_RELOAD_LOCK.release()
    yield
    if api._EVENTS_15D_RELOAD_LOCK.locked():
        api._EVENTS_15D_RELOAD_LOCK.release()


@pytest.fixture()
def inline_threads(monkeypatch):
    """把后台线程改成"start 就地执行"，让时序可断言。"""
    started = []

    class _InlineThread:
        def __init__(self, target=None, daemon=None, name=None):
            self._target = target
            self.name = name

        def start(self):
            started.append(self.name)
            self._target()

    monkeypatch.setattr(api.threading, "Thread", _InlineThread)
    return started


def _ev(i, nid=None):
    return {"id": nid if nid is not None else i, "news_id": nid if nid is not None else i,
            "title": f"e{i}"}


# ── 首加载：等得到就等，等不到就空返回 ────────────────────
def test_first_load_waits_until_cache_filled(monkeypatch, inline_threads):
    monkeypatch.setattr(api, "_events_15d_load_sync", lambda: [_ev(1), _ev(2), _ev(3)])
    monkeypatch.setattr(api, "_EVENTS_15D_WAIT_S", 0.5)
    got = api._events_15d_get_cached(2)
    assert [e["id"] for e in got] == [1, 2], "首加载成功后按 limit 切片"
    assert inline_threads, "首加载必须发起一次刷新"


def test_first_load_times_out_and_returns_empty(monkeypatch, inline_threads):
    """刷新一直写不进缓存时，请求方最多等 WAIT_S，然后返回空 —— 不许挂死。"""
    monkeypatch.setattr(api, "_events_15d_load_sync", lambda: [])
    monkeypatch.setattr(api, "_EVENTS_15D_WAIT_S", 0.1)
    t0 = time.monotonic()
    got = api._events_15d_get_cached(5)
    assert got == []
    assert time.monotonic() - t0 < 1.0, "等待必须有上限"


# ── 新鲜缓存：直接秒回，不触发刷新 ──────────────────────────
def test_fresh_cache_does_not_reload(monkeypatch, inline_threads):
    api._EVENTS_15D_CACHE.extend([_ev(1), _ev(2)])
    api._EVENTS_15D_LAST_LOAD = time.monotonic()
    called = []
    monkeypatch.setattr(api, "_events_15d_load_sync",
                        lambda: called.append(1) or [])
    got = api._events_15d_get_cached(1)
    assert [e["id"] for e in got] == [1]
    assert inline_threads == [], "缓存新鲜时不许发起刷新"
    assert called == []


def test_limit_is_at_least_one(monkeypatch, inline_threads):
    api._EVENTS_15D_CACHE.extend([_ev(1), _ev(2)])
    api._EVENTS_15D_LAST_LOAD = time.monotonic()
    assert [e["id"] for e in api._events_15d_get_cached(0)] == [1]


def test_limit_beyond_cache_size_is_clipped(monkeypatch, inline_threads):
    api._EVENTS_15D_CACHE.extend([_ev(1)])
    api._EVENTS_15D_LAST_LOAD = time.monotonic()
    assert len(api._events_15d_get_cached(99)) == 1


# ── 过期缓存：先给旧数据，只允许一个刷新 ──────────────────
def test_stale_returns_old_cache_and_single_flight(monkeypatch):
    """两个并发请求同时命中过期缓存，只能起一个 reload（否则线程池被打满）。

    这里用"只记录不执行"的线程，让刷新权锁保持在占用状态，模拟 reload 正在进行。
    """
    api._EVENTS_15D_CACHE.extend([_ev(7), _ev(8)])
    api._EVENTS_15D_LAST_LOAD = time.monotonic() - 3600      # 远超 TTL
    starts = []

    class _Rec:
        def __init__(self, target=None, daemon=None, name=None):
            self._t = target
        def start(self):
            starts.append(1)
    monkeypatch.setattr(api.threading, "Thread", _Rec)

    out1 = api._events_15d_get_cached(2)
    assert [e["id"] for e in out1] == [7, 8], "过期时先给旧缓存，不阻塞请求"
    assert api._EVENTS_15D_RELOAD_LOCK.locked(), "刷新权已被占用"
    out2 = api._events_15d_get_cached(2)
    assert [e["id"] for e in out2] == [7, 8], "抢不到刷新权时继续给旧缓存"
    assert len(starts) == 1, "单飞：一次过期只允许一个刷新线程"


def test_stale_without_reload_returns_stale_not_empty(monkeypatch, inline_threads):
    """已经抢不到刷新权时（锁被占），必须返回旧缓存，不能返回空。"""
    api._EVENTS_15D_CACHE.extend([_ev(4)])
    api._EVENTS_15D_LAST_LOAD = time.monotonic() - 3600
    api._EVENTS_15D_RELOAD_LOCK.acquire(blocking=False)
    try:
        got = api._events_15d_get_cached(1)
        assert [e["id"] for e in got] == [4]
        assert inline_threads == []
    finally:
        api._EVENTS_15D_RELOAD_LOCK.release()


# ── 刷新 worker：写回缓存 + 写回时间 + 一定释放锁 ────────────
def test_worker_writes_cache_and_last_load(monkeypatch):
    api._EVENTS_15D_RELOAD_LOCK.acquire(blocking=False)
    monkeypatch.setattr(api, "_events_15d_load_sync", lambda: [_ev(11), _ev(12)])
    api._events_15d_reload_worker()
    assert [e["id"] for e in api._EVENTS_15D_CACHE] == [11, 12]
    assert api._EVENTS_15D_LAST_LOAD > 0, "v481 事故点：LAST_LOAD 必须写回模块级变量"
    assert not api._EVENTS_15D_RELOAD_LOCK.locked(), "刷新结束必须释放单飞锁"


def test_worker_failure_keeps_old_cache_and_releases_lock(monkeypatch):
    api._EVENTS_15D_CACHE.extend([_ev(1)])
    api._EVENTS_15D_LAST_LOAD = 123.0
    api._EVENTS_15D_RELOAD_LOCK.acquire(blocking=False)

    def _boom():
        raise RuntimeError("db locked")
    monkeypatch.setattr(api, "_events_15d_load_sync", _boom)
    api._events_15d_reload_worker()

    assert [e["id"] for e in api._EVENTS_15D_CACHE] == [1], "刷新失败沿用旧缓存"
    assert api._EVENTS_15D_LAST_LOAD == 123.0, "失败不许伪造成已刷新"
    assert not api._EVENTS_15D_RELOAD_LOCK.locked(), "失败也必须释放锁，否则永不刷新"


def test_repeated_stale_after_worker_does_not_spin(monkeypatch):
    """worker 写过 LAST_LOAD 之后，TTL 内不再触发刷新（防止全表 SQL 反复跑）。"""
    api._EVENTS_15D_RELOAD_LOCK.acquire(blocking=False)
    monkeypatch.setattr(api, "_events_15d_load_sync", lambda: [_ev(1)])
    api._events_15d_reload_worker()

    starts = []
    class _Rec:
        def __init__(self, target=None, daemon=None, name=None):
            self._t = target
        def start(self):
            starts.append(1)
    monkeypatch.setattr(api.threading, "Thread", _Rec)
    api._events_15d_get_cached(1)
    assert starts == [], "刚刷新过就不该再起线程"


# ── 取数窗口：±15 天，走北京时间归一化 ───────────────────
class _FakeCursor:
    def fetchall(self):
        return []


class _FakeConn:
    row_factory = None

    def execute(self, sql, params=None):
        self.last_sql = sql
        self.last_params = params
        return _FakeCursor()

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def test_load_sync_uses_plus_minus_15_days(monkeypatch):
    calls = []
    monkeypatch.setattr(api.time_utils, "epoch_to_bj_str",
                        lambda ep: calls.append(int(ep)) or f"BJ{int(ep)}", raising=False)
    monkeypatch.setattr(api.time, "time", lambda: 1_700_000_000)
    fake = _FakeConn()
    monkeypatch.setattr(api.db, "get_connection", lambda: fake, raising=False)

    assert api._events_15d_load_sync() == []
    assert calls == [1_700_000_000 - api._EVENTS_15D_WINDOW_S,
                     1_700_000_000 + api._EVENTS_15D_WINDOW_S]
    assert api._EVENTS_15D_WINDOW_S == 15 * 86400
    assert "_EVENT_SELECT" not in fake.last_sql, "SELECT 应已展开成 SQL，不是变量名占位"
    assert "ORDER BY rn.id DESC" in fake.last_sql


def test_load_sync_survives_db_error(monkeypatch):
    """DB 报错就抛给 worker，由 worker 记日志 + 沿用旧缓存（不静默返回假数据）。"""
    def _boom():
        raise sqlite3.OperationalError("no db")
    monkeypatch.setattr(api.db, "get_connection", _boom, raising=False)
    with pytest.raises(sqlite3.OperationalError):
        api._events_15d_load_sync()
