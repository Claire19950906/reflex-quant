# -*- coding: utf-8 -*-
"""v321.3 P2 —— 共享超时工具 `timeout_util.call_with_timeout` 的行为测试。

背景：项目里曾有 7 处各自手写 `ThreadPoolExecutor` + `shutdown(wait=False)`，
且**同一个坑犯过两遍**（`with` 写法退出时 wait=True → 超时形同虚设 →
pytest 整轮卡在 71%）。本文件把「超时保护真的生效」钉死成测试。
"""
from __future__ import annotations

import os
import sys
import time

_SRC_PY = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                       "src_python")
if _SRC_PY not in sys.path:
    sys.path.insert(0, _SRC_PY)

from timeout_util import call_with_timeout


def test_returns_value_on_success():
    assert call_with_timeout(lambda: 42, 2.0, label="ok") == 42


def test_timeout_returns_none_and_does_not_block():
    """超时 → 返回 None，且**调用方不能被卡住**。

    这是本轮修的核心：`with ThreadPoolExecutor` 会等线程跑完，
    导致超时形同虚设。这里断言「超时后立刻返回」。
    """
    def _slow():
        time.sleep(5)
        return "never"

    t0 = time.monotonic()
    out = call_with_timeout(_slow, 0.3, label="slow")
    elapsed = time.monotonic() - t0
    assert out is None
    assert elapsed < 3.0, f"超时后仍等了 {elapsed:.1f}s（wait=True 陷阱）"


def test_background_thread_is_daemon_so_exit_is_not_blocked():
    """⚠️ 超时后留下的后台线程必须是 **daemon**。

    ThreadPoolExecutor 的 worker 是**非 daemon** → 解释器退出时 atexit 会
    join 它们 → 即便 shutdown(wait=False)，**进程退出仍被拖住**
    (实测: 一个 sleep(30) 的任务让整个 pytest 进程收尾多等 30s)。
    """
    import threading
    before = {t.name for t in threading.enumerate()}
    call_with_timeout(lambda: time.sleep(3), 0.2, label="daemoncheck")
    time.sleep(0.1)
    new_threads = [t for t in threading.enumerate()
                   if t.name not in before and t.name.startswith("timeout-")]
    assert new_threads, "应留下一个后台线程"
    assert all(t.daemon for t in new_threads), "后台线程必须是 daemon, 否则拖住退出"


def test_exception_returns_none_not_raises():
    def _boom():
        raise ValueError("boom")

    assert call_with_timeout(_boom, 2.0, label="boom") is None


def test_on_timeout_and_on_error_callbacks():
    """失败必须**可见** —— 原先各处 `except: pass` 静默吞掉，线上无从发现。"""
    seen = []
    call_with_timeout(lambda: time.sleep(3), 0.2, label="t",
                      on_timeout=lambda lbl, t: seen.append(("to", lbl, t)))
    call_with_timeout(lambda: 1 / 0, 2.0, label="e",
                      on_error=lambda lbl, exc: seen.append(("err", lbl, type(exc).__name__)))
    kinds = [s[0] for s in seen]
    assert "to" in kinds and "err" in kinds
    err = [s for s in seen if s[0] == "err"][0]
    assert err[2] == "ZeroDivisionError"


def test_timeout_error_callback_gets_label():
    seen = []
    call_with_timeout(lambda: time.sleep(3), 0.2, label="mylabel",
                      on_timeout=lambda lbl, t: seen.append((lbl, t)))
    assert seen and seen[0][0] == "mylabel"
