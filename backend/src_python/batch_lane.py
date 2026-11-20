#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""batch_lane —— 给"重批量写"一条单独的车道（2026-11-16）。

为什么需要（事故链）：
    这个库里住着 100+ 个 launchd 作业 + 3 个常驻服务，而 SQLite 同一时刻
    只允许**一个写者**。实测两次全站停摆的根因都不是"锁坏了"，而是：
    一个批量任务（kline 聚合）合法地占着写者名额 8 分钟、48% CPU，
    另外 30+ 个写入方（含 api 启动）全部排队 → 8000 端口整段不可用。

设计取舍（重要，别改错）：
    · **不用** db_write_guard 的写闸门做这件事 —— 已验证过高频写走闸门会反噬；
    · 这里是"批处理车道"：只有**重批量任务**进来，同一时刻只放一个；
      拿不到就**这轮不跑**（批量任务晚一轮无所谓，接口挂掉是事故）；
    · 车道锁随进程退出自动释放（flock 语义），因此**不需要**清理僵死状态；
    · 不要在这里 kill 超时任务：强杀写事务中的进程会制造 SQLite stale 锁，
      那是另一个更大的坑（见 ops 报告第十九节）。超时只记录、只告警。

用法（最小改动：在脚本入口调一行，不需要缩进重构）：
    from batch_lane import acquire_or_exit
    acquire_or_exit("kline_aggregate")      # 拿不到车道 → 本轮跳过，exit 0
"""
from __future__ import annotations

import fcntl
import json
import os
import sys
import time
from pathlib import Path

BACKEND = Path(__file__).resolve().parent.parent
_LOCK = BACKEND / "runtime" / "batch_lane.lock"
_STATE = BACKEND / "runtime" / "batch_lane_holder.json"
_fd = None


def _write_state(name: str) -> None:
    try:
        _STATE.parent.mkdir(parents=True, exist_ok=True)
        _STATE.write_text(json.dumps({
            "pid": os.getpid(),
            "name": name,
            "since": time.time(),
            "since_str": time.strftime("%Y-%m-%d %H:%M:%S"),
        }, ensure_ascii=False), encoding="utf-8")
    except Exception:
        pass


def current_holder() -> dict:
    """谁在车道里（进程已死则返回空）。"""
    try:
        data = json.loads(_STATE.read_text(encoding="utf-8"))
        pid = int(data.get("pid") or 0)
        if pid and pid != os.getpid():
            os.kill(pid, 0)
        return data
    except Exception:
        return {}


def acquire(name: str, timeout_s: float = 5.0) -> bool:
    """尝试进入车道。True=拿到；False=别人在跑（本轮应跳过）。"""
    global _fd
    try:
        _LOCK.parent.mkdir(parents=True, exist_ok=True)
        _fd = os.open(_LOCK, os.O_CREAT | os.O_RDWR, 0o644)
    except Exception:
        return True          # 车道本身不可用 → 宁可照旧跑，也不要卡死任务
    deadline = time.monotonic() + max(0.0, timeout_s)
    while True:
        try:
            fcntl.flock(_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            _write_state(name)
            return True
        except OSError:
            if time.monotonic() >= deadline:
                return False
            time.sleep(0.2)


def release() -> None:
    global _fd
    try:
        if _fd is not None:
            fcntl.flock(_fd, fcntl.LOCK_UN)
            os.close(_fd)
    except Exception:
        pass
    finally:
        _fd = None
        try:
            if _STATE.exists():
                _STATE.unlink()
        except Exception:
            pass


def acquire_or_exit(name: str, timeout_s: float = 5.0) -> None:
    """拿不到车道就安静退出（exit 0）。

    为什么是 exit 0 而不是 1：这不是失败，是"让道"。
    报成失败会让 launchd / 体检把它们当事故，噪声反而掩盖真事故。
    """
    if acquire(name, timeout_s):
        return
    holder = current_holder()
    print(f"[LANE] {name}: 车道被 {holder.get('name') or '?'}"
          f"(pid={holder.get('pid')}, since={holder.get('since_str')}) 占用 → 本轮跳过",
          flush=True)
    sys.exit(0)
