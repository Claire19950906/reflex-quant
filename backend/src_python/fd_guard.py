#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
fd_guard.py — 文件描述符水位监控 + 自动回收 (P0 · 「AI分析挂了」的第二道防线)
================================================================================

为什么需要它
------------
`yf_fd_guard.py` 治的是**水源** (yfinance 的 peewee 连接泄漏)。但一个跑了几周的
服务还可能因为别的原因缓慢吃 fd (第三方库、日志句柄、socket 没关)。

没有监控时的失败模式 (就是过去 20 多次复发的方式):
    进程慢慢涨 → 涨到 256 → socket.accept() 全部失败 → **静默**地不再响应任何请求
    → 没人知道为什么 → 重启 → 又活 → 又慢慢涨 → 再死

有监控时的失败模式:
    涨到 70%  → 自动回收 + 打日志 (可观测)
    涨到 90%  → 记 DLQ + 心跳标 error (有人会看到)
    涨到 100% → 至少你知道**为什么**, 而且回收已经在跑了

设计
----
· 纯只读探测 fd 数量 (macOS 用 /dev/fd, Linux 用 /proc/self/fd), 不写业务库
· 回收动作复用 yf_fd_guard.close_yf_caches() (关掉本进程所有线程的 peewee 连接)
· 回收是**尽力而为**, 失败不影响主流程
· 监控线程是 daemon, 挂了不影响主进程
"""
from __future__ import annotations

import os
import resource
import threading
import time
from typing import Any, Dict, Optional

# 水位阈值 (占 fd 软上限的比例)
WARN_PCT = 70.0      # 超这个比例 → 回收 + 告警
CRITICAL_PCT = 90.0  # 超这个比例 → 记 DLQ + 心跳标 error

DEFAULT_INTERVAL_S = 30.0

_monitor_started = False
_monitor_lock = threading.Lock()
_last: Dict[str, Any] = {}


def _fd_dir() -> str:
    """返回本进程 fd 目录 (macOS /dev/fd, Linux /proc/self/fd)。"""
    if os.path.isdir("/dev/fd"):
        return "/dev/fd"
    return "/proc/self/fd"


def fd_used() -> int:
    """当前进程打开的 fd 数。取不到就返回 -1 (不猜、不编)。"""
    try:
        return len(os.listdir(_fd_dir()))
    except OSError:
        return -1


def fd_limits() -> tuple[int, int]:
    """(soft, hard) fd 上限。取不到返回 (-1, -1)。"""
    try:
        soft, hard = resource.getrlimit(resource.RLIMIT_NOFILE)
        return int(soft), int(hard)
    except Exception:
        return -1, -1


def fd_snapshot() -> Dict[str, Any]:
    """当前 fd 水位快照 —— 给 /api/health 用。"""
    used = fd_used()
    soft, hard = fd_limits()
    pct = (used / soft * 100.0) if (used >= 0 and soft > 0) else None
    return {
        "used": used,
        "soft_limit": soft,
        "hard_limit": hard,
        "pct": round(pct, 2) if pct is not None else None,
        "warn_pct": WARN_PCT,
        "critical_pct": CRITICAL_PCT,
        "level": ("unknown" if pct is None else
                  "critical" if pct >= CRITICAL_PCT else
                  "warn" if pct >= WARN_PCT else "ok"),
        "checked_at": time.strftime("%Y-%m-%d %H:%M:%S"),
    }


def reclaim() -> int:
    """尽力回收: 关掉 yfinance 的 peewee 缓存连接 (当前线程持有的)。

    返回回收的连接数。回收不彻底是常态 (别的线程还持有), 但每跑一次
    都能把**当前线程**的那几条还回去。
    """
    try:
        from yf_fd_guard import close_yf_caches
        return close_yf_caches()
    except Exception:
        return 0


def _record_alarm(level: str, snap: Dict[str, Any], reclaimed: int) -> None:
    """把水位告警写进 DLQ + worker 心跳 (有人会看到, 不再静默)。"""
    try:
        import sqlite3
        import db as _db
        import observability as _obs
    except Exception:
        return
    try:
        conn = _db.get_connection()
    except Exception:
        return
    try:
        msg = (f"fd 水位 {level}: {snap.get('used')}/{snap.get('soft_limit')} "
               f"({snap.get('pct')}%) 已回收 {reclaimed} 个连接")
        _obs.dlq_record(
            conn,
            kind="fd_leak_alarm",
            source="fd_guard",
            payload=snap,
            error=msg,
            retry_count=0,
        )
        _obs.heartbeat_tick(
            conn,
            worker_name="fd_guard",
            processed=1,
            detail=msg,
            status="error" if level == "critical" else "busy",
            error=msg if level == "critical" else "",
        )
    except Exception:
        pass
    finally:
        try:
            conn.close()
        except Exception:
            pass


def start_fd_monitor(interval_s: float = DEFAULT_INTERVAL_S) -> bool:
    """启动后台监控线程 (幂等)。返回是否启动成功。"""
    global _monitor_started
    if _monitor_started:
        return True
    with _monitor_lock:
        if _monitor_started:
            return True

        def _loop() -> None:
            while True:
                try:
                    time.sleep(interval_s)
                    snap = fd_snapshot()
                    _last.clear()
                    _last.update(snap)
                    pct = snap.get("pct")
                    if pct is None:
                        continue
                    if pct >= WARN_PCT:
                        reclaimed = reclaim()
                        snap2 = fd_snapshot()
                        _last.clear()
                        _last.update(snap2)
                        level = "critical" if pct >= CRITICAL_PCT else "warn"
                        print(f"[FD-GUARD] {level} fd={snap2.get('used')}/"
                              f"{snap2.get('soft_limit')} ({snap2.get('pct')}%) "
                              f"已回收 {reclaimed} 个连接", flush=True)
                        _record_alarm(level, snap2, reclaimed)
                    else:
                        _last.setdefault("reclaims", 0)
                except Exception as exc:  # pragma: no cover - 守护线程绝不退出
                    print(f"[FD-GUARD] 监控异常: {type(exc).__name__}: {exc}", flush=True)

        t = threading.Thread(target=_loop, name="fd-guard", daemon=True)
        t.start()
        _monitor_started = True
        print(f"[FD-GUARD] fd 监控已启动 (每 {int(interval_s)}s, 告警线 {WARN_PCT}% / "
              f"critical {CRITICAL_PCT}%)", flush=True)
        return True


def last_snapshot() -> Dict[str, Any]:
    """监控线程最近一次采样的快照 (没有就现场取一次)。"""
    if _last:
        return dict(_last)
    return fd_snapshot()


def is_started() -> bool:
    return _monitor_started


__all__ = [
    "WARN_PCT", "CRITICAL_PCT", "fd_used", "fd_limits", "fd_snapshot",
    "reclaim", "start_fd_monitor", "last_snapshot", "is_started",
]

if __name__ == "__main__":
    import json
    print(json.dumps(fd_snapshot(), ensure_ascii=False, indent=2))
