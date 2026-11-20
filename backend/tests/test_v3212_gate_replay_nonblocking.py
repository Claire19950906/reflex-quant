# -*- coding: utf-8 -*-
"""v321.2: 门卫回放不得阻塞主分析的回归单测。

事故背景（2026-11-04 生产实测）
--------------------------------
v321 把「门卫回放自愈」内联进引擎主循环，写法是::

    _gr_stat = await asyncio.to_thread(_gr_once, limit=_GATE_REPLAY_BATCH)

阻塞调用确实被挪到了工作线程，但**主循环协程本身仍然 await 在这一句上**，
于是后面的 Phase 1 认领（`_batch_claim`）必须等**整轮回放跑完**才会执行。

实测：回放一轮 50 条，每条 6-13s → 一轮 11-12 分钟。引擎日志证据：
10:16:21 重启 → 10:28:03 才出现 `[BATCH] claimed 15 rows`（中间一条都没认领），
而 `[gate-replay] 自动回放 50 条` 就在 10:28:03 打印 —— 完全对应。
症状：可认领量堆积 22 条、DONE 停在 06:45、主分析整体空转。

v321.2 的修法：回放丢后台任务（`asyncio.create_task`）+ 单飞标志，
主循环立刻继续认领；同一时刻只允许一轮回放在跑。

本文件锁死的不变量：
  A. `schedule_gate_replay()` 立即返回 Task，调用方不被阻塞
  B. 单飞：上一轮还在跑时，再调度返回 None（不重复开轮）
  C. 回放线程抛异常不得影响主循环
  D. 结构回归：主循环里不再出现 `await asyncio.to_thread(... _gr_once ...)`
"""

from __future__ import annotations

import asyncio
import os
import sys
import time

SRC = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "src_python"))
if SRC not in sys.path:
    sys.path.insert(0, SRC)

import pytest

from engine import ai_worker


def _slow_once(limit=50, delay=0.25):
    """模拟一轮耗时回放（每条 6-13s 的极端情况压缩到 0.25s）。"""
    def _once(limit=50):
        time.sleep(delay)
        return {"ok": limit, "remaining": 100, "reason": "test"}
    return _once


def _failing_once(limit=50):
    def _once(limit=50):
        raise RuntimeError("回放通道炸了")
    return _once


# ───────────────── 不变量 A：调度不阻塞调用方 ─────────────────

def test_schedule_gate_replay_returns_immediately():
    """调度必须立刻返回 Task —— 调用方（主循环）继续往下走。"""
    async def _main():
        holder = {}
        task = ai_worker.schedule_gate_replay(_slow_once(), holder)
        assert task is not None, "首次调度必须建出后台任务"
        assert isinstance(task, asyncio.Task)
        await task
        return True

    assert asyncio.run(_main())


def test_caller_resumes_before_replay_finishes():
    """核心回归：回放还没跑完，主循环就已经继续往下走了。"""
    order = []

    async def _main():
        holder = {}
        ai_worker.schedule_gate_replay(_slow_once(delay=0.3), holder)
        order.append("main-continued")      # 主循环紧接着继续认领
        await asyncio.sleep(0.6)             # 等回放跑完
        return order

    order = asyncio.run(_main())

    assert order[0] == "main-continued", (
        "调度后主循环必须立即继续 —— v321 的 await 会把 Phase 1 认领"
        "推迟到整轮回放结束（实测 11-12 分钟）")
    assert len(order) == 1


# ───────────────── 不变量 B：单飞 ─────────────────

def test_single_flight_skips_overlapping_round():
    """上一轮还在跑时，再调度返回 None（不重复开轮）。"""
    started = []

    async def _main():
        holder = {}

        def _once(limit=50):
            started.append(1)
            time.sleep(0.2)
            return {"ok": 1, "remaining": 0, "reason": "test"}

        t1 = ai_worker.schedule_gate_replay(_once, holder)
        assert t1 is not None
        t2 = ai_worker.schedule_gate_replay(_once, holder)
        assert t2 is None, "上一轮未结束 → 必须跳过本轮（单飞）"
        await t1
        # 上一轮结束后可以再开新轮
        t3 = ai_worker.schedule_gate_replay(_once, holder)
        assert t3 is not None, "上一轮结束后必须允许再调度"
        await t3
        return started

    started = asyncio.run(_main())
    assert len(started) == 2, "只应真正跑了两轮（跳过的那轮不得触发调用）"


# ───────────────── 不变量 C：回放异常不影响主循环 ─────────────────

def test_replay_exception_is_swallowed():
    """回放线程炸了也不能把异常抛回主循环。"""
    async def _main():
        holder = {}
        task = ai_worker.schedule_gate_replay(_failing_once(), holder)
        await task          # 不抛异常 = 通过
        return True

    assert asyncio.run(_main())


# ───────────────── 不变量 D：结构回归 ─────────────────

def test_source_has_no_inline_await_of_replay():
    """源码级守卫：主循环不得再 await 回放调用。"""
    path = os.path.join(SRC, "engine", "ai_worker.py")
    with open(path, "r", encoding="utf-8") as f:
        src = f.read()

    # 只看真实代码行（注释里为了说明历史事故会引用旧写法）
    code_lines = [
        ln for ln in src.splitlines()
        if not ln.strip().startswith("#")
    ]
    code = "\n".join(code_lines)

    # 真实的 `await asyncio.to_thread(` 只能有一处 —— 后台执行器内部
    assert code.count("await asyncio.to_thread(") == 1, (
        "只允许在 run_gate_replay_background() 内部 await 回放；"
        "主循环内联 await 会再次饿死 Phase 1 认领")

    # 主循环里那一段（回放调度块 → Phase 1 认领）不得出现 to_thread
    anchor = code.index("schedule_gate_replay(_gr_once, _gr_holder)")
    loop_start = code.rindex("while True:", 0, anchor)
    phase1 = code.index("def _batch_claim(", anchor)
    main_loop_block = code[loop_start:phase1]
    assert "asyncio.to_thread(" not in main_loop_block, (
        "主循环的回放调度块不得直接 await to_thread（v321 的阻塞写法）")

    # 主循环调度点必须走后台任务
    assert "schedule_gate_replay(_gr_once, _gr_holder)" in src, (
        "主循环必须用 schedule_gate_replay() 调度回放")

    # 调度器内部必须是 create_task，且绝不能 await（用 AST 判断，避开 docstring）
    import ast

    tree = ast.parse(src)
    fn = next(
        (n for n in ast.walk(tree)
         if isinstance(n, ast.FunctionDef) and n.name == "schedule_gate_replay"),
        None,
    )
    assert fn is not None, "必须存在 schedule_gate_replay()"
    assert not any(isinstance(n, ast.Await) for n in ast.walk(fn)), (
        "调度器绝不能 await 任务（那等于同步阻塞，退化成 v321 的行为）")
    assert any(
        isinstance(n, ast.Call)
        and isinstance(n.func, ast.Attribute)
        and n.func.attr == "create_task"
        for n in ast.walk(fn)
    ), "调度器必须用 create_task 起后台任务"
