"""db_write_guard 契约测试 —— 重点是"强杀不留死锁"这条性质。

事故背景：SQLite 的 -shm 写锁在进程被 kill -9 后出现过僵死未释放，
导致 26 个 worker 全部排队等一把没人持有的锁。这里用 flock 就是为了绕开它，
所以必须用可执行的方式证明：SIGKILL 掉持锁进程后，锁立刻可用。
"""
from __future__ import annotations

import os
import subprocess
import sys
import textwrap
import time
from pathlib import Path

import pytest

BACKEND = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BACKEND / "src_python"))

from db_write_guard import BlockedError, write_gate

_CHILD_HOLD = textwrap.dedent(
    """
    import sys
    import time
    sys.path.insert(0, {py!r})
    from db_write_guard import write_gate
    with write_gate(purpose="child-holder", timeout_s=5):
        print("HELD", flush=True)
        time.sleep(30)
    """
)


def _spawn_holder() -> subprocess.Popen:
    code = _CHILD_HOLD.format(py=str(BACKEND / "src_python"))
    proc = subprocess.Popen([sys.executable, "-c", code],
                            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True)
    # v452：必须带超时读 —— 引擎在跑的时候会握着全局写闸门，子进程
    # `write_gate(timeout_s=5)` 抢不到就抛 BlockedError 静默退出，
    # 此时阻塞的 readline() 会一直等下去（全量跑卡在 98% 就是这个）。
    import select
    ready, _, _ = select.select([proc.stdout], [], [], 15)
    line = proc.stdout.readline() if ready else ""
    assert "HELD" in line, (
        "子进程没能拿到闸门（多半是活体引擎握着写闸门）: %r" % line)
    return proc


def test_mutual_exclusion():
    """别人持锁时, 我方必须拿不到 —— 且超时后抛 BlockedError 而不是静默阻塞。"""
    proc = _spawn_holder()
    try:
        with pytest.raises(BlockedError):
            with write_gate(purpose="parent-should-fail", timeout_s=0.5):
                pass
    finally:
        proc.kill()
        proc.wait(timeout=5)


def test_sigkill_releases_immediately():
    """核心性质: kill -9 掉持锁进程后, 锁在 1 秒内可用（对照 SQLite shm 僵死）。"""
    proc = _spawn_holder()
    proc.kill()                      # SIGKILL
    proc.wait(timeout=5)
    t0 = time.monotonic()
    with write_gate(purpose="after-sigkill", timeout_s=1.0):
        pass
    assert time.monotonic() - t0 < 1.0


def test_state_file_cleared_after_release():
    """退出 with 后状态文件必须清空, 否则诊断时会看到"幽灵持有者"。"""
    with write_gate(purpose="cleanup-check", timeout_s=2):
        pass
    from db_write_guard import current_holder
    assert current_holder() is None
