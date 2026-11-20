#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""跨进程单写闸门 —— 用 flock 代替"各写各的 BEGIN IMMEDIATE"。

为什么需要（2026-11-16 事故）：
    26 个 worker 直连 SQLite 抢一把写锁。某次 `kill -9` 打在"正处于写事务中"的
    进程上，SQLite 的 `-shm` 写锁没有被正常释放，于是**所有进程都在等一把没人
    持有的锁**，整库写死约数小时，api 启动卡死、决策链路停摆。

核心差别（这是选 flock 的全部理由）：
    - SQLite 的 shm 锁在异常终止路径上出现过"僵死未释放"；
    - `fcntl.flock` 由内核在进程退出（含 SIGKILL）时**无条件释放**，
      实测 kill -9 后 0.0s 即可重新获取，不会把系统锁死。
    行为差异在 tests/test_write_guard.py 里有可执行的对照实验。

用法：
    from db_write_guard import write_txn

    with write_txn(purpose="fomc-upgrade") as conn:
        conn.execute("UPDATE ...")
        # 退出 with 自动 COMMIT；异常自动 ROLLBACK；无论如何都释放 flock

设计约束：
    - 只做**串行化**，不做业务判断；SQL 仍由调用方给。
    - 锁文件与状态文件放 runtime/ 内，状态文件里记 pid + purpose，
      出问题时能一眼看出"谁握着锁、在干什么"，不用再靠 sample 猜。
    - 超时后抛 BlockedError（而不是无声等待），让调用方决定降级还是重试。
"""
from __future__ import annotations

import errno
import fcntl
import json
import os
import signal
import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator, Optional

_RUNTIME = Path(__file__).resolve().parent.parent / "runtime"
_LOCK_PATH = _RUNTIME / "db_write.lock"
_STATE_PATH = _RUNTIME / "db_write_lock.json"


class BlockedError(RuntimeError):
    """在 timeout 内没拿到写闸门。"""


def _db_path() -> str:
    env = os.getenv("TRIDENT_DB_PATH") or os.getenv("TRIDENT_DB")
    if env:
        return env
    try:
        import config
        return str(config.DB_PATH)
    except Exception:
        return str(Path(__file__).resolve().parent.parent / "trident_event_bus.db")


def _ensure_runtime() -> None:
    _RUNTIME.mkdir(parents=True, exist_ok=True)


def _write_state(purpose: str) -> None:
    try:
        _STATE_PATH.write_text(
            json.dumps(
                {"pid": os.getpid(), "purpose": purpose, "since": time.time(),
                 "host": os.uname().nodename},
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
    except Exception:
        pass          # 状态文件只是可观测性，写不进去不影响加锁


def _clear_state() -> None:
    try:
        _STATE_PATH.unlink()
    except Exception:
        pass


def current_holder() -> Optional[dict]:
    """谁正握着写闸门（读状态文件；进程已死则返回 None）。"""
    try:
        data = json.loads(_STATE_PATH.read_text(encoding="utf-8"))
    except Exception:
        return None
    pid = int(data.get("pid") or 0)
    if pid and pid != os.getpid():
        try:
            os.kill(pid, 0)          # 探活：进程还在吗
        except OSError as exc:
            if exc.errno == errno.ESRCH:
                return None          # 持锁进程已死 → 状态文件是过期残留
    return data


@contextmanager
def write_gate(purpose: str = "", timeout_s: float = 10.0) -> Iterator[None]:
    """只拿"写闸门"，不开事务 —— 需要自己控制事务边界时用这个。"""
    _ensure_runtime()
    fd = os.open(_LOCK_PATH, os.O_CREAT | os.O_RDWR, 0o644)
    deadline = time.monotonic() + max(0.1, timeout_s)
    try:
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except OSError:
                if time.monotonic() >= deadline:
                    holder = current_holder() or {}
                    raise BlockedError(
                        f"等写闸门 {timeout_s:.1f}s 超时；当前持有者="
                        f"{holder.get('pid')} ({holder.get('purpose')})"
                    )
                time.sleep(0.05)
        _write_state(purpose or "unspecified")
        try:
            yield
        finally:
            _clear_state()
            try:
                fcntl.flock(fd, fcntl.LOCK_UN)
            except Exception:
                pass
    finally:
        os.close(fd)          # 关 fd 时内核也会释放锁（双保险）


@contextmanager
def write_txn(purpose: str = "", timeout_s: float = 10.0,
              conn: Optional[sqlite3.Connection] = None,
              busy_timeout_ms: int = 5000) -> Iterator[sqlite3.Connection]:
    """闸门 + 事务一起管：进 with 即 BEGIN IMMEDIATE，出 with 即 COMMIT。

    - 抛出任何异常都 ROLLBACK，绝不让半个事务挂在那儿。
    - 外部传入 conn 时复用（不新建、也不关闭），方便嵌在已有流程里。
    """
    with write_gate(purpose=purpose, timeout_s=timeout_s):
        own = conn is None
        if own:
            conn = sqlite3.connect(_db_path(), timeout=busy_timeout_ms / 1000)
            conn.execute(f"PRAGMA busy_timeout={int(busy_timeout_ms)};")
        try:
            conn.execute("BEGIN IMMEDIATE")
            try:
                yield conn
            except Exception:
                conn.rollback()
                raise
            else:
                conn.commit()
        finally:
            if own:
                try:
                    conn.close()
                except Exception:
                    pass


class GatedConnection:
    """带写闸门的连接：拿到 flock 之后才 open sqlite，close() 时释放闸门。

    针对的是"高频小写入"场景（source_status.report 这类每秒多次的埋点）：
    它们原本各自去撞 SQLite 的写锁，撞不到就丢数据（日志里那句
    `[source_status] DB persist skipped` 就是这么来的）。
    改成先排队拿闸门，拿到后 SQLite 侧几乎一定立刻成功，写就真能落库。

    只对**写**路径用；读路径不要用这个，否则会把 WAL 的并发读能力浪费掉。
    """

    def __init__(self, conn, fd: int, purpose: str):
        self._conn = conn
        self._fd = fd
        self._purpose = purpose
        self._closed = False

    def __getattr__(self, item):
        return getattr(self._conn, item)

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        try:
            self._conn.close()
        except Exception:
            pass
        try:
            _clear_state()
            fcntl.flock(self._fd, fcntl.LOCK_UN)
        except Exception:
            pass
        try:
            os.close(self._fd)
        except Exception:
            pass


def gated_connection(purpose: str = "", timeout_s: float = 1.0,
                     busy_timeout_ms: int = 500, **connect_kwargs) -> GatedConnection:
    """排队拿闸门 → 打开 sqlite 写连接。拿不到闸门抛 BlockedError（调用方决定丢弃或重试）。"""
    _ensure_runtime()
    fd = os.open(_LOCK_PATH, os.O_CREAT | os.O_RDWR, 0o644)
    deadline = time.monotonic() + max(0.1, timeout_s)
    try:
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except OSError:
                if time.monotonic() >= deadline:
                    holder = current_holder() or {}
                    raise BlockedError(
                        f"等写闸门 {timeout_s:.1f}s 超时；持有者="
                        f"{holder.get('pid')} ({holder.get('purpose')})"
                    )
                time.sleep(0.02)
    except BaseException:
        os.close(fd)
        raise

    _write_state(purpose or "unspecified")
    conn = sqlite3.connect(_db_path(), timeout=busy_timeout_ms / 1000, **connect_kwargs)
    try:
        conn.execute(f"PRAGMA busy_timeout={int(busy_timeout_ms)};")
    except Exception:
        pass
    return GatedConnection(conn, fd, purpose)


def _selftest() -> int:
    """命令行自检：拿锁、打印持有者、释放。"""
    with write_gate(purpose="selftest", timeout_s=3):
        print("拿到写闸门; 状态文件内容 =", current_holder())
    print("已释放; 再读状态 =", current_holder())
    return 0


if __name__ == "__main__":
    raise SystemExit(_selftest())
