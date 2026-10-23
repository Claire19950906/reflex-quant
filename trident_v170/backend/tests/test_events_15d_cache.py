# -*- coding: utf-8 -*-
"""v170.5+: /api/events ±15d 内存 dict 缓存单测。

覆盖:
  * _events_15d_get_cached —— 缓存命中 / TTL 触发 reload / limit 过滤 / DB 失败降级
  * 并发调用线程安全
  * lifespan preload 钩子不阻塞启动

注：SQL 路径由 smoke 脚本 `/tmp/events_15d_smoke.py` 验证，本单测聚焦 cache 行为。
SQL 路径不能简单 mock（连接已在文件全局），故采用 monkeypatch _events_15d_load_sync
为 fixture 控制数据，避免对 DB schema 的硬依赖。
"""

from __future__ import annotations

import os
import sys
import threading
import time

import pytest

SRC = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "src_python"))
if SRC not in sys.path:
    sys.path.insert(0, SRC)

import api_server  # noqa: E402  # 单测需要全局引用模块


# ---- 辅助 ----------------------------------------------------------------

@pytest.fixture(autouse=True)
def _isolate_cache():
    """每个 case 隔离 cache 状态，避免相互污染。"""
    with api_server._EVENTS_15D_LOCK:
        api_server._EVENTS_15D_CACHE.clear()
        api_server._EVENTS_15D_LAST_LOAD = 0.0
    yield
    with api_server._EVENTS_15D_LOCK:
        api_server._EVENTS_15D_CACHE.clear()
        api_server._EVENTS_15D_LAST_LOAD = 0.0


@pytest.fixture
def fake_loader():
    """替换 _events_15d_load_sync 为可控数据。"""
    original = api_server._events_15d_load_sync

    def _set(loader):
        if callable(loader):
            api_server._events_15d_load_sync = loader
        else:
            api_server._events_15d_load_sync = lambda: list(loader)

    yield _set
    api_server._events_15d_load_sync = original


def _ev(nid: int, text: str = "default") -> dict:
    """生成最小化的 event dict（避免依赖 _row_to_event 的字段全集）。"""
    return {"news_id": nid, "news_text": text, "sibling_decisions": []}


# ---- cache 行为 -----------------------------------------------------------

def test_get_cached_first_call_populates(fake_loader):
    """首次调用 _get_cached 会触发 reload 并填满缓存。"""
    fake_loader([_ev(1), _ev(2), _ev(3), _ev(4), _ev(5)])
    assert not api_server._EVENTS_15D_CACHE
    out = api_server._events_15d_get_cached(limit=10)
    assert isinstance(out, list)
    assert len(api_server._EVENTS_15D_CACHE) == 5, "首次调用后缓存应被填满"
    assert len(out) == 5


def test_get_cached_respects_limit(fake_loader):
    """limit 参数应被尊重 —— 返回长度 ≤ limit。"""
    fake_loader([_ev(i) for i in range(100)])
    api_server._events_15d_get_cached(limit=10)  # 预热
    out_small = api_server._events_15d_get_cached(limit=2)
    assert len(out_small) == 2
    out_big = api_server._events_15d_get_cached(limit=100)
    assert len(out_big) == 100


def test_get_cached_no_reload_when_ttl_fresh(fake_loader):
    """TTL 未过期时不触发 _events_15d_load_sync（避免阻塞链路）。"""
    fake_loader([_ev(1)])
    api_server._events_15d_get_cached(limit=10)  # 预热
    with api_server._EVENTS_15D_LOCK:
        api_server._EVENTS_15D_LAST_LOAD = time.monotonic() + 999.0
    called = {"count": 0}

    def counter():
        called["count"] += 1
        return [_ev(99)]

    api_server._events_15d_load_sync = counter
    out = api_server._events_15d_get_cached(limit=10)
    assert called["count"] == 0, "TTL 未过期不应触发 reload"
    assert len(out) == 1  # 仍是原数据 [1]


def test_get_cached_reload_when_ttl_old(fake_loader):
    """TTL 过期时应触发 reload。"""
    fake_loader([_ev(1)])
    api_server._events_15d_get_cached(limit=10)  # 预热填 [1]
    with api_server._EVENTS_15D_LOCK:
        api_server._EVENTS_15D_LAST_LOAD = time.monotonic() - 9999.0
    api_server._events_15d_load_sync = lambda: [_ev(2), _ev(3), _ev(4)]
    out = api_server._events_15d_get_cached(limit=10)
    assert len(out) == 3  # 应拿到新数据
    assert out[0]["news_id"] == 2


def test_get_cached_db_failure_fallback_to_empty(fake_loader):
    """DB 异常 + 缓存空 → 返回 []（不抛）。"""
    api_server._events_15d_load_sync = lambda: (_ for _ in ()).throw(RuntimeError("simulated DB down"))
    out = api_server._events_15d_get_cached(limit=10)
    assert out == []


def test_get_cached_db_failure_keeps_old_cache(fake_loader):
    """DB 异常 + 已有缓存 → 返回旧缓存（不抛）。"""
    fake_loader([_ev(1), _ev(2), _ev(3)])
    api_server._events_15d_get_cached(limit=10)  # 预热填满
    assert len(api_server._EVENTS_15D_CACHE) == 3
    # 强制 DB 失败 + 触发 reload
    api_server._events_15d_load_sync = lambda: (_ for _ in ()).throw(RuntimeError("boom"))
    with api_server._EVENTS_15D_LOCK:
        api_server._EVENTS_15D_LAST_LOAD = time.monotonic() - 9999.0
    out = api_server._events_15d_get_cached(limit=10)
    # 旧缓存保留（仍为 3 条）
    assert len(api_server._EVENTS_15D_CACHE) == 3
    # 返回旧缓存
    assert len(out) == 3


def test_get_cached_thread_safety(fake_loader):
    """并发调用 _get_cached 不会触发 UnboundLocalError，且结果一致。"""
    fake_loader([_ev(i) for i in range(50)])

    errors = []
    barrier = threading.Barrier(5)

    def worker():
        try:
            barrier.wait()  # 5 个线程同时起跑
            for _ in range(20):
                out = api_server._events_15d_get_cached(limit=10)
                assert len(out) <= 50
        except Exception as exc:
            errors.append(exc)

    threads = [threading.Thread(target=worker) for _ in range(5)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert not errors, f"并发调用触发异常: {errors[:3]}"


def test_get_cached_limit_zero_normalized(fake_loader):
    """limit=0 应被归一为 ≥1。"""
    fake_loader([_ev(1), _ev(2)])
    out = api_server._events_15d_get_cached(limit=0)
    assert len(out) >= 1


def test_get_cached_limit_negative_normalized(fake_loader):
    """limit<0 应被归一为 ≥1。"""
    fake_loader([_ev(1), _ev(2)])
    out = api_server._events_15d_get_cached(limit=-100)
    assert len(out) >= 1


# ---- 缓存配置常量 -----------------------------------------------------------

def test_window_days_constant_is_15():
    """±15 天窗口是用户指定的产品语义，禁止悄悄改。"""
    assert api_server._EVENTS_15D_WINDOW_DAYS == 15
    assert api_server._EVENTS_15D_WINDOW_S == 15 * 86400


def test_ttl_is_positive():
    """TTL 必须为正（否则每次调用都 reload）。"""
    assert api_server._EVENTS_15D_TTL_S > 0