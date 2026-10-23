# -*- coding: utf-8 -*-
"""事件循环不再被报价链路 / DB watcher / FF 日历阻塞 (v2026.10.23) 单元测试。

背景（生产实测）：/health 端点自身零 DB 访问，但 p99 延迟高达 6.6s、max 9.2s。
用看门狗采样主线程栈归因，阻塞全部集中在事件循环线程上的三类同步操作：

  1. _broadcast_gold / _broadcast_wti 里的 db.retry_on_busy(record_market_snapshot)
     —— 5s busy_timeout × 4 次尝试最坏 ~20s，XAU+WTI 相邻执行实测单次连续阻塞 38s；
  2. _cached_change24h 里的同步 HTTPS（EastMoney 2s 超时 + Binance 兜底）；
  3. db_watcher 每轮 aiosqlite.connect() —— aiosqlite 每次 connect 新建线程，
     而 Thread.start() 会阻塞调用方直到新线程真正启动（threading._started.wait()）；
     另有 macro_calendar_ff_sync_loop 里同步 requests + 同步写库（单次最长 83s）。

本文件锁住"真正不阻塞"（而不是"只是变慢"）：在协程运行期间用 10ms 心跳任务量事件
循环最大卡顿，被测依赖被人为放慢 0.8~1.0s；若实现退回同步执行，卡顿会同步放大。

判定用**增量**而不是绝对阈值：同一次运行里先量一段空闲期作为基线，再量被测协程
期间的卡顿，只断言"增量"小。这样在重载机器上（load average 上百时实测基线本身就能
到 300ms+）依然稳定，而同步实现会因为增量 ≈ 阻塞时长而被稳定判失败。
"""
from __future__ import annotations

import asyncio
import time
from typing import Any, Dict, List

import pytest

import api_server
import db
import macro_calendar_ff


# 被测依赖被人为放慢的时长（秒）。必须显著大于 _MAX_LAG_DELTA。
_BLOCK_TICK = 1.0
_BLOCK_NET = 0.8
_BLOCK_DB = 0.8

# 允许的"卡顿增量"上限（秒）。同步实现时增量 ≈ _BLOCK_*（0.8~1.0s），必然被判失败。
_MAX_LAG_DELTA = 0.35


async def _lag_while(coro) -> float:
    """跑 coro 并返回事件循环最大卡顿秒数（10ms 心跳）。"""
    ticks: List[float] = []
    stop = False

    async def _heartbeat() -> None:
        last = time.perf_counter()
        while not stop:
            await asyncio.sleep(0.01)
            now = time.perf_counter()
            ticks.append(now - last)
            last = now

    hb = asyncio.create_task(_heartbeat())
    try:
        await coro
    finally:
        stop = True
        await hb
    return max(ticks) if ticks else 0.0


async def _measure_delta(coro_factory) -> float:
    """返回"被测期间卡顿 - 空闲基线"的增量（秒）。"""
    baseline = await _lag_while(asyncio.sleep(0.25))
    during = await _lag_while(coro_factory())
    return during - baseline


def _assert_not_blocking(delta: float, label: str) -> None:
    assert delta < _MAX_LAG_DELTA, (
        f"{label} 把事件循环拖慢了 {delta * 1000:.0f}ms"
        f"（上限 {int(_MAX_LAG_DELTA * 1000)}ms；同步实现会 ≈ 阻塞时长）"
    )


@pytest.fixture
def quiet_broadcast(monkeypatch):
    """屏蔽 SSE 广播与 24h 涨跌的网络调用，只留被测的落库路径。"""
    monkeypatch.setattr(api_server, "_broadcast_sse", _lambda_no_sse(), raising=False)
    monkeypatch.setattr(api_server, "_cached_change24h", lambda _asset: 1.25, raising=False)
    monkeypatch.setattr(api_server, "_last_gold_price", None, raising=False)
    monkeypatch.setattr(api_server, "_last_wti_price", None, raising=False)


def _lambda_no_sse():
    async def _no_sse(_payload) -> None:
        return None

    return _no_sse


@pytest.fixture
def slow_tick_write(monkeypatch):
    """把 tick 落库放慢到 0.6s（模拟 SQLite 写锁竞争下的 retry_on_busy）。"""
    calls: List[Dict[str, Any]] = []

    def _slow(fn, **_kw):
        calls.append({"fn": getattr(fn, "__name__", "lambda")})
        time.sleep(_BLOCK_TICK)
        return 1

    monkeypatch.setattr(api_server.db, "retry_on_busy", _slow)
    return calls


# ── 1. 报价广播：慢写库不得阻塞事件循环 ──────────────────────────────────────

def test_broadcast_gold_slow_db_write_does_not_block_loop(quiet_broadcast, slow_tick_write):
    delta = asyncio.run(_measure_delta(lambda: api_server._broadcast_gold(2410.5, "test")))
    assert len(slow_tick_write) == 1, "tick 仍应被写入，只是改到线程里"
    _assert_not_blocking(delta, "_broadcast_gold 写库")


def test_broadcast_wti_slow_db_write_does_not_block_loop(quiet_broadcast, slow_tick_write):
    delta = asyncio.run(_measure_delta(lambda: api_server._broadcast_wti(78.25, "test")))
    assert len(slow_tick_write) == 1, "tick 仍应被写入，只是改到线程里"
    _assert_not_blocking(delta, "_broadcast_wti 写库")


# ── 2. 24h 涨跌：同步 HTTPS 必须在线程里 ────────────────────────────────────

def test_change24h_slow_network_does_not_block_loop(monkeypatch):
    def _slow(_asset):
        time.sleep(_BLOCK_NET)
        return 2.5

    monkeypatch.setattr(api_server, "_cached_change24h", _slow, raising=False)
    delta = asyncio.run(_measure_delta(lambda: api_server._change24h_async("XAU")))
    _assert_not_blocking(delta, "_change24h_async 同步 HTTPS")


# ── 3. EastMoney opener 复用（build_opener 会重载系统 CA，实测可达数秒）──────

def test_eastmoney_change_reuses_module_opener(monkeypatch):
    import urllib.request

    def _boom(*_a, **_k):
        raise AssertionError("不应在每次请求时重建 opener（会重载 CA 证书）")

    monkeypatch.setattr(urllib.request, "build_opener", _boom)
    # 断网：opener.open 直接抛异常，函数按设计吞掉返回 None。不打真实网络。
    def _offline(*_a, **_k):
        raise OSError("offline")

    monkeypatch.setattr(api_server._EASTMONEY_OPENER, "open", _offline)
    assert api_server._fetch_eastmoney_change("113.USDXAU") is None


# ── 4. tick 背压：在途达到上限时丢弃而不是无限排队 ──────────────────────────

def test_persist_tick_backpressure_drops_when_saturated(monkeypatch):
    calls: List[Any] = []

    monkeypatch.setattr(api_server.timeseries, "record_market_snapshot", calls.append)
    monkeypatch.setattr(api_server.db, "retry_on_busy", lambda fn, **kw: fn())
    monkeypatch.setitem(api_server._tick_pending, "XAU", api_server._TICK_MAX_PENDING)

    asyncio.run(api_server._persist_tick("XAU", 2400.0, 1.0, "test"))

    assert calls == [], "在途已达上限时不应再提交写库任务"
    assert api_server._tick_pending["XAU"] == api_server._TICK_MAX_PENDING, "不应递增"


def test_persist_tick_writes_payload_and_releases_slot(monkeypatch):
    calls: List[Dict[str, Any]] = []

    monkeypatch.setattr(api_server.timeseries, "record_market_snapshot", calls.append)
    monkeypatch.setattr(api_server.db, "retry_on_busy", lambda fn, **kw: fn())
    monkeypatch.setitem(api_server._tick_pending, "WTI", 0)

    asyncio.run(api_server._persist_tick("WTI", 78.5, 1.5, "HTTP"))

    assert len(calls) == 1
    item = calls[0]["items"][0]
    assert item["asset"] == "WTI"
    assert item["price"] == pytest.approx(78.5)
    assert item["change24h"] == pytest.approx(1.5)
    assert item["source"] == "http", "来源应与原来一样小写归一"
    assert api_server._tick_pending["WTI"] == 0, "写完必须把在途计数还回去"


# ── 5. db_watcher 热路径：不得因 aiosqlite 建线程而卡住 loop ────────────────

def test_fetch_change_cursors_does_not_block_loop(monkeypatch):
    class _SlowConn:
        def execute(self, _sql, _params=()):
            time.sleep(_BLOCK_DB)
            return self

        def fetchall(self):
            return [(11, 22)]

        def close(self):
            return None

    monkeypatch.setattr(db, "get_connection", lambda: _SlowConn())
    delta = asyncio.run(_measure_delta(api_server._fetch_change_cursors))
    _assert_not_blocking(delta, "DB 游标查询")


def test_fetch_change_cursors_values(monkeypatch):
    class _FakeConn:
        def execute(self, _sql, _params=()):
            return self

        def fetchall(self):
            return [(7, 9)]

        def close(self):
            return None

    monkeypatch.setattr(db, "get_connection", lambda: _FakeConn())
    assert asyncio.run(api_server._fetch_change_cursors()) == (7, 9)


# ── 6. FF 日历同步请求不得阻塞 loop ─────────────────────────────────────────

def test_ff_calendar_sync_loop_does_not_block_loop(monkeypatch):
    fetched: List[int] = []

    def _slow_fetch():
        time.sleep(_BLOCK_DB)
        fetched.append(1)
        return [{"event": "x"}]

    monkeypatch.setattr(macro_calendar_ff, "fetch_ff_events", _slow_fetch)
    monkeypatch.setattr(macro_calendar_ff, "sync_to_db",
                        lambda evs: {"fetched": len(evs), "inserted": 0})
    monkeypatch.setattr(macro_calendar_ff, "_sync_started", False, raising=False)

    async def _drive():
        # ThreadPoolExecutor 冷启动的第一次 submit 会在调用线程上做 Thread.start()
        # （阻塞到新线程起来），那是一次性、与被测逻辑无关的开销；先预热再量卡顿。
        await asyncio.to_thread(lambda: None)
        task = asyncio.create_task(macro_calendar_ff.macro_calendar_ff_sync_loop(0.05))
        await asyncio.sleep(_BLOCK_DB + 0.3)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    delta = asyncio.run(_measure_delta(_drive))
    assert fetched, "循环体应至少跑过一次"
    _assert_not_blocking(delta, "FF 日历同步请求")