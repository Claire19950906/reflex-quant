#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""safe_kill —— 杀进程之前先问三个问题，别再制造一次 stale 锁。

事故来源（2026-11-16）：
    我用 `kill -9` 清孤儿进程，正好打在一个"正握着 SQLite 写事务"的进程上。
    SQLite 的 -shm 写锁没被释放，26 个进程随后全部卡在 busy 等待，
    整库写死数小时，api 起不来、决策停摆。

所以这个脚本强制走三步：
    1. 先判断**该不该由你来杀**：api / bridge / engine / frontend 这些
       由 launchd 管的服务，一律拒绝，改用 `launchctl bootout`（它会先发 TERM，
       并连带整个进程树）。
    2. 判断它是不是**正握着写事务**：看 runtime/db_write_lock.json，
       以及它有没有打开 db 文件。
    3. 一律 **TERM 先发**，等够时间（默认 8s）让事务回滚，才允许 KILL。
       杀完立刻复测写锁是否可用，不可用就提示走 db_lock_doctor --recover。

用法：
    python3 backend/scripts/safe_kill.py --pid 12345            # 预演（默认）
    python3 backend/scripts/safe_kill.py --pid 12345 --apply    # 真杀
    python3 backend/scripts/safe_kill.py --list-orphans         # 列出端口孤儿
"""
from __future__ import annotations

import argparse
import errno
import json
import os
import signal
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

BACKEND = Path(__file__).resolve().parent.parent
DB = BACKEND / "trident_event_bus.db"
GATE_STATE = BACKEND / "runtime" / "db_write_lock.json"

# 这些进程归 launchd 管，不允许直接 kill（要先 bootout，否则会留下孤儿子进程）
# v454: api_server.py(8000) 已停用，不再纳入托管映射
MANAGED_SERVICES = {
    "bridge_server.py": "com.trident.bridge",
    "engine.py": "com.trident.engine",
    "next-server": "com.trident.frontend",
}


def _ssh(cmd: list[str]) -> str:
    try:
        return subprocess.run(cmd, capture_output=True, text=True, timeout=10).stdout
    except Exception:
        return ""


def cmdline(pid: int) -> str:
    return _ssh(["ps", "-o", "command=", "-p", str(pid)]).strip()


def is_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except OSError as exc:
        return exc.errno != errno.ESRCH


def has_db_open(pid: int) -> bool:
    out = _ssh(["lsof", "-p", str(pid)])
    return "trident_event_bus.db" in out


def gate_holder() -> dict | None:
    try:
        return json.loads(GATE_STATE.read_text(encoding="utf-8"))
    except Exception:
        return None


def managed_job(pid: int) -> str | None:
    cmd = cmdline(pid)
    for needle, job in MANAGED_SERVICES.items():
        if needle in cmd:
            return job
    return None


def probe_write(timeout_s: float = 1.5) -> bool:
    try:
        conn = sqlite3.connect(f"file:{DB}", uri=True, timeout=timeout_s,
                               isolation_level=None)
        conn.execute("BEGIN IMMEDIATE")
        conn.execute("ROLLBACK")
        conn.close()
        return True
    except Exception:
        return False


def kill_pid(pid: int, wait_s: float, apply: bool) -> int:
    cmd = cmdline(pid)
    if not cmd:
        print(f"[SKIP] pid={pid} 不存在")
        return 0

    job = managed_job(pid)
    if job:
        print(f"[REFUSE] pid={pid} 是 launchd 管的服务（{job}）。\n"
              f"         正确做法：launchctl bootout gui/$UID/{job}\n"
              f"         它才会先发 TERM 并带走整个进程树，不会留下孤儿。")
        return 2

    holder = gate_holder()
    holding_gate = bool(holder and int(holder.get("pid") or 0) == pid)
    db_open = has_db_open(pid)

    print(f"[INFO] pid={pid}  cmd={cmd[:80]}")
    print(f"[INFO] 持有写闸门={holding_gate}  打开数据库={db_open}")
    if holding_gate:
        print(f"[WARN] 它握着写闸门（purpose={holder.get('purpose')}）—— 先 TERM，等它回滚事务。")

    if not apply:
        print(f"[DRY-RUN] 将发 SIGTERM，等 {wait_s}s；仍存活才 SIGKILL。加 --apply 执行。")
        return 0

    print("[STEP1] SIGTERM")
    try:
        os.kill(pid, signal.SIGTERM)
    except OSError as exc:
        print(f"[SKIP] TERM 失败: {exc}")
        return 0

    deadline = time.monotonic() + wait_s
    while time.monotonic() < deadline:
        if not is_alive(pid):
            print(f"[OK] 已在 {wait_s}s 内优雅退出，未使用 SIGKILL")
            return _after(pid)
        time.sleep(0.2)

    print("[STEP2] 仍在，SIGKILL")
    try:
        os.kill(pid, signal.SIGKILL)
    except OSError:
        pass
    time.sleep(1.0)
    return _after(pid)


def _after(pid: int) -> int:
    writable = probe_write()
    if writable:
        print("[VERIFY] 写锁可用 —— 没有留下 stale 锁")
        return 0
    print("[VERIFY] ⚠️ 写锁不可用！立刻执行：\n"
          "         python3 backend/scripts/db_lock_doctor.py --recover")
    return 1


def list_orphans() -> int:
    """列出"祖链挂在 1"的持库进程 —— 这类最可能是强杀留下的残骸。"""
    out = _ssh(["lsof", str(DB)])
    pids = sorted({int(l.split()[1]) for l in out.splitlines()[1:]
                   if l.split() and l.split()[0] == "Python"})
    for pid in pids:
        ppid = _ssh(["ps", "-o", "ppid=", "-p", str(pid)]).strip()
        if ppid != "1":
            continue
        # launchd 直接拉起的服务，ppid 也是 1 —— 那不是孤儿，别误报成"残骸"
        if managed_job(pid):
            continue
        print(f"pid={pid} ppid=1  {cmdline(pid)[:90]}")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--pid", type=int)
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--wait", type=float, default=8.0, help="TERM 后等待秒数")
    ap.add_argument("--list-orphans", action="store_true")
    args = ap.parse_args()

    if args.list_orphans:
        return list_orphans()
    if not args.pid:
        ap.error("需要 --pid 或 --list-orphans")
    return kill_pid(args.pid, args.wait, args.apply)


if __name__ == "__main__":
    sys.exit(main())
