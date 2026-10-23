# -*- coding: utf-8 -*-
"""source_status.areport() 事件循环安全落库 (v200 P0-C1) 单元测试。

背景：api_server 的后台 watcher（infra_health_watcher 每 30s 6 次 + qinghong_news_watcher）
原先在 async 函数里直接调 source_status.report()。report() 结尾的 DB 落库在写锁竞争时
最坏阻塞 ~1.9s（busy_timeout=500ms × retry_on_busy 3 次退避），而整段跑在事件循环线程上
→ 整条 loop 冻结，连零 DB 访问的 /health 都只能排队，表现为周期性秒级延迟尖刺。

本文件锁住四件事：
1. report() 默认行为不变（仍然落库）—— engine 进程 / cron 同步调用方零影响；
2. report(persist=False) 不落库，且与 areport() 的内存状态完全等价（read-after-write 不变）；
3. areport() 落库期间事件循环仍能推进其它任务（真正的不阻塞，而非"只是变慢"）；
4. register() 在 DB 读取异常时也关闭连接（fd 泄漏回归，与 v189-8 ai_worker 同类）。
"""
from __future__ import annotations

import asyncio
import sqlite3
import threading
import time

import pytest

import source_status


@pytest.fixture(autouse=True)
def _clean_registry():
    """隔离模块级 _registry（面板状态是全局单例，别污染其它测试）。"""
    saved = dict(source_status._registry)
    source_status._registry.clear()
    yield
    source_status._registry.clear()
    source_status._registry.update(saved)


@pytest.fixture
def spy_persist(monkeypatch):
    """把落库换成记录器：不碰 DB，又能断言调用参数。"""
    calls = []

    def _spy(source_id, ok, latency_ms, error, detail):
        calls.append((source_id, ok, latency_ms, error, detail))

    monkeypatch.setattr(source_status, "_persist_with_retry", _spy)
    return calls


def _snap(src_id):
    return {s["source_id"]: s for s in source_status.snapshot()}[src_id]


# ── 1. 同步 report() 的向后兼容 ─────────────────────────────────────────────

def test_report_still_persists_by_default(spy_persist):
    """engine/cron 侧同步调用方行为不变：默认仍落库。"""
    source_status.report("sync_src", True, latency_ms=12.5, detail="d")
    assert spy_persist == [("sync_src", True, 12.5, "", "d")]


def test_report_persist_false_skips_db(spy_persist):
    """persist=False 只更新内存，不落库（areport 的第一步）。"""
    source_status.report("nodb_src", False, error="boom", persist=False)
    assert spy_persist == []
    assert _snap("nodb_src")["last_error"] == "boom"


# ── 2. areport() 的内存语义与 report() 完全等价 ───────────────────────────

def test_areport_persists_same_args(spy_persist):
    asyncio.run(
        source_status.areport("async_src", False, error="boom", latency_ms=3.0, detail="d")
    )
    assert spy_persist == [("async_src", False, 3.0, "boom", "d")]


def test_areport_matches_report_memory_state(spy_persist):
    """两条路径产出的内存状态必须逐字段一致（除 source_id 本身）。"""
    source_status.report("cmp_sync", False, error="boom", detail="d")
    asyncio.run(source_status.areport("cmp_async", False, error="boom", detail="d"))
    a, b = _snap("cmp_sync"), _snap("cmp_async")
    for k in ("status", "last_result", "last_error", "detail",
              "consecutive_failures", "total_fail", "last_latency_ms"):
        assert a[k] == b[k], f"areport 与 report 的 {k} 不一致: {a[k]!r} vs {b[k]!r}"


def test_areport_memory_visible_immediately(monkeypatch):
    """read-after-write 不变：面板 snapshot() 立刻能看到新状态。"""
    monkeypatch.setattr(source_status, "_persist_with_retry", lambda *a: None)
    asyncio.run(source_status.areport("mem_src", True, detail="ok"))
    assert _snap("mem_src")["last_result"] == "ok"


# ── 3. 核心回归：areport() 不阻塞事件循环 ────────────────────────────────

def test_areport_does_not_block_event_loop(monkeypatch):
    """落库卡住 2s（模拟写锁竞争）时，事件循环必须照常调度其它任务。

    判定用「时间戳先后」而不是 timeout：若落库仍跑在事件循环线程上，marker 任务
    只能在 persist 解除阻塞之后才有机会执行 → marker_ts >= persist_end，断言失败。
    """
    loop_thread = threading.get_ident()
    seen = {}
    release = threading.Event()

    def blocking_persist(*_args):
        seen["persist_thread"] = threading.get_ident()
        seen["persist_start"] = time.monotonic()
        release.wait(timeout=2.0)
        seen["persist_end"] = time.monotonic()

    monkeypatch.setattr(source_status, "_persist_with_retry", blocking_persist)

    async def _main():
        async def marker():
            await asyncio.sleep(0.05)
            seen["marker_ts"] = time.monotonic()

        m = asyncio.create_task(marker())
        task = asyncio.create_task(
            source_status.areport("loop_src", True, detail="probe"))
        # 等落库线程真正进入阻塞态
        deadline = time.monotonic() + 2.0
        while "persist_start" not in seen and time.monotonic() < deadline:
            await asyncio.sleep(0.005)
        # 关键：让落库持续阻塞足够久，marker(50ms) 必须在锁未解除时到期，
        # 否则"先解锁再断言"会让同步阻塞的实现也蒙混过关。
        await asyncio.sleep(0.15)
        release.set()
        await task
        await m
        return seen

    seen = asyncio.run(_main())

    assert "persist_start" in seen, "落库根本没启动"
    assert "marker_ts" in seen, "marker 任务未执行"
    assert seen["persist_thread"] != loop_thread, "落库必须离开事件循环线程"
    assert seen["marker_ts"] < seen["persist_end"], (
        "marker 任务在落库解除阻塞之后才执行 → 事件循环被同步落库卡住了"
    )
    assert seen["marker_ts"] - seen["persist_start"] < 0.5, (
        "0.05s 的 marker 被推迟到 0.5s 之后 → 事件循环明显被阻塞"
    )


def test_areport_does_not_block_heartbeat_loop(monkeypatch):
    """贴近生产形态：30s 周期的 infra watcher 里，areport 期间心跳不能掉拍。"""
    release = threading.Event()
    seen = {}

    def blocking_persist(*_args):
        seen["persist_entered"] = time.monotonic()
        release.wait(timeout=3.0)
        seen["persist_end"] = time.monotonic()

    monkeypatch.setattr(source_status, "_persist_with_retry", blocking_persist)

    async def _main():
        beats = []
        stop = False

        async def heartbeat():
            while not stop:
                beats.append(time.monotonic())
                await asyncio.sleep(0.02)

        hb = asyncio.create_task(heartbeat())
        rep = asyncio.create_task(source_status.areport("hb_src", True))
        # 等落库线程真正进入阻塞态再开始计时：默认线程池是懒创建的，第一次 submit
        # 会在事件循环线程上做 Thread.start()（阻塞到新线程起来）。重载机器上这步
        # 可能要上百毫秒，用固定 sleep 会让"落库确实阻塞住"的前提悄悄失效。
        deadline = time.monotonic() + 3.0
        while "persist_entered" not in seen and time.monotonic() < deadline:
            await asyncio.sleep(0.005)
        await asyncio.sleep(0.15)          # 落库保持阻塞 150ms，期间量心跳
        stop = True
        release.set()
        await hb
        await rep
        return beats

    beats = asyncio.run(_main())

    assert "persist_entered" in seen, "测试前提失效：落库线程没有进入阻塞态"
    assert "persist_end" in seen, "测试前提失效：落库没有被正常解除阻塞"
    inside = [t for t in beats
              if seen["persist_entered"] <= t <= seen["persist_end"]]
    assert len(inside) >= 2, "测试前提失效：阻塞窗口内心跳拍数不足"
    # 20ms 周期的心跳，单拍间隔不应被落库拖成百毫秒级
    max_gap = max(b - a for a, b in zip(inside, inside[1:])) * 1000
    assert max_gap < 120, f"心跳单拍间隔 {max_gap:.0f}ms，落库期间事件循环被阻塞"


# ── 4. register() 的 fd 泄漏回归 ──────────────────────────────────────────

class _BoomConn:
    """模拟表缺失 / DB 异常的连接：execute 必抛，统计 close 是否被调用。"""

    def __init__(self):
        self.row_factory = None
        self.closed = 0

    def execute(self, *args, **kwargs):
        raise sqlite3.OperationalError("no such table: source_status")

    def close(self):
        self.closed += 1


def test_register_closes_conn_when_db_read_fails(monkeypatch):
    """表缺失时 connect 到的连接也必须关闭，否则每次懒注册泄漏 1 个 fd。"""
    import db

    conn = _BoomConn()
    monkeypatch.setattr(db, "get_connection", lambda *a, **kw: conn)
    source_status.register("fd_leak_src", category="行情", label="x")
    assert conn.closed == 1, "DB 读取异常时连接没有被关闭 → fd 泄漏"


def test_register_closes_conn_on_success(monkeypatch):
    """正常路径也必须关闭一次（别把 close 搬丢）。"""
    import db

    class _OkConn(_BoomConn):
        def execute(self, *args, **kwargs):
            return self

        def fetchone(self):
            return None

    conn = _OkConn()
    monkeypatch.setattr(db, "get_connection", lambda *a, **kw: conn)
    source_status.register("ok_src", category="行情", label="x")
    assert conn.closed == 1