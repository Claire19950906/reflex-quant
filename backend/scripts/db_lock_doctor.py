#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""写锁诊断器 —— 区分「有人长时间持锁」和「锁僵死（没人持有）」这两种完全不同的病。

为什么必须区分（2026-11-16 事故）：
    两种症状在日志里长得一模一样，都是一片 `database is locked`。
    但处置完全相反：
      - 有人持锁（慢事务）：等它，或优化那条 SQL 的事务边界，别乱杀进程；
      - 锁僵死（没人持有）：所有人都在 busy 等，必须整组重启让 SQLite 做 WAL 恢复。
    当时靠逐进程 sample 才分辨出来，花了很久。这个脚本把那次判断固化成一条命令。

用法：
    python3 backend/scripts/db_lock_doctor.py             # 只诊断
    python3 backend/scripts/db_lock_doctor.py --json
    python3 backend/scripts/db_lock_doctor.py --recover   # 按安全顺序恢复（有确认提示）
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

BACKEND = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BACKEND / "src_python"))

DB = BACKEND / "trident_event_bus.db"


def _run(cmd: list[str]) -> str:
    try:
        return subprocess.run(cmd, capture_output=True, text=True, timeout=15).stdout
    except Exception:
        return ""


def probe_write(timeout_s: float = 1.5) -> dict:
    """真刀真枪试一次 BEGIN IMMEDIATE —— 这是唯一可靠的判据。"""
    t0 = time.monotonic()
    try:
        conn = sqlite3.connect(f"file:{DB}", uri=True,
                               timeout=timeout_s, isolation_level=None)
        conn.execute("BEGIN IMMEDIATE")
        conn.execute("ROLLBACK")
        conn.close()
        return {"writable": True, "waited_s": round(time.monotonic() - t0, 3)}
    except Exception as exc:
        return {"writable": False, "waited_s": round(time.monotonic() - t0, 3),
                "error": f"{type(exc).__name__}: {exc}"}


def db_holders() -> list[dict]:
    out = _run(["lsof", str(DB)])
    pids, rows = set(), []
    for line in out.splitlines()[1:]:
        parts = line.split()
        if len(parts) < 2 or parts[0] != "Python":
            continue
        pid = int(parts[1])
        if pid in pids:
            continue
        pids.add(pid)
        cmd = _run(["ps", "-o", "command=", "-p", str(pid)]).strip()
        et = _run(["ps", "-o", "etime=", "-p", str(pid)]).strip()
        rows.append({"pid": pid, "cmd": os.path.basename(cmd.split()[1]) if len(cmd.split()) > 1 else cmd[:40],
                     "etime": et})
    return rows


def busy_waiters(pids: list[int]) -> list[int]:
    """哪些进程正卡在 sqlite 的 busy 回调里（macOS 用 sample）。"""
    out = []
    for pid in pids:
        text = _run(["sample", str(pid), "1", "1"])
        if "sqliteDefaultBusyCallback" in text:
            out.append(pid)
    return out


def gate_holder() -> dict | None:
    try:
        from db_write_guard import current_holder
        return current_holder()
    except Exception:
        return None


def classify() -> dict:
    probe = probe_write()
    state = "ok" if probe["writable"] else "locked"
    holders = db_holders() if state == "locked" else []
    waiters = busy_waiters([h["pid"] for h in holders]) if holders else []
    holder_meta = gate_holder()

    kind, advice = "none", ""
    if state == "locked":
        if holders and len(waiters) >= max(1, len(holders) - 1):
            kind = "stale"        # 几乎所有人都在等 → 多半没人真持有
            advice = ("判为「锁僵死」：几乎所有持库进程都在 busy 等待。"
                      "按 --recover 走整组重启（先 bootout 再 TERM，最后才 -9）。")
        elif holder_meta:
            kind = "held"
            advice = (f"判为「有人持锁」：闸门状态显示 pid={holder_meta.get('pid')} "
                      f"({holder_meta.get('purpose')}) 持锁 "
                      f"{int(time.time() - float(holder_meta.get('since', time.time())))}s。先等它，别杀。")
        else:
            kind = "unknown"
            advice = "无法判定：持库进程不全在等待，也没有闸门状态。逐个 sample 定位慢事务。"

    return {
        "db": str(DB),
        "probe": probe,
        "state": state,
        "kind": kind,
        "advice": advice,
        "gate_holder": holder_meta,
        "holders": holders,
        "busy_waiters": waiters,
    }


RECOVER_STEPS = [
    "launchctl bootout gui/$UID/com.trident.engine",
    "launchctl bootout gui/$UID/com.trident.bridge",
    "# v454: api(8000) 已停用，全套路由由 bridge(8100) 托管，不再 bootout/bootstrap api",
    "# 对仍持有 db 的 python 进程先 TERM，5s 不退再 -9（切忌直接 -9 写事务中的进程）",
    "# 等 8s 后 python3 backend/scripts/db_lock_doctor.py 复查，可写则按序 bootstrap",
    "launchctl bootstrap gui/$UID ~/Library/LaunchAgents/com.trident.engine.plist",
    "launchctl bootstrap gui/$UID ~/Library/LaunchAgents/com.trident.bridge.plist",
]


def recover() -> int:
    print("即将按以下顺序恢复（engine → bridge 会被短暂中断；api 已停用不拉起）：")
    for s in RECOVER_STEPS:
        print("  " + s)
    if os.getenv("TRIDENT_DOCTOR_YES") != "1":
        print("\n未执行。确认后加环境变量 TRIDENT_DOCTOR_YES=1 重跑。")
        return 2

    uid = os.getuid()
    for job in ("engine", "bridge"):
        subprocess.run(["launchctl", "bootout", f"gui/{uid}/com.trident.{job}"],
                       capture_output=True, timeout=20)
    time.sleep(3)

    for h in db_holders():
        subprocess.run(["kill", "-TERM", str(h["pid"])], capture_output=True, timeout=10)
    time.sleep(5)
    for h in db_holders():
        subprocess.run(["kill", "-9", str(h["pid"])], capture_output=True, timeout=10)
    time.sleep(8)

    r = probe_write()
    print("恢复后探测:", r)
    if not r["writable"]:
        print("仍不可写 —— 需要人工介入（先备份 trident_event_bus.db 再考虑 WAL 重建）。")
        return 1

    for job in ("engine", "bridge"):
        subprocess.run(["launchctl", "bootstrap", f"gui/{uid}",
                        os.path.expanduser(f"~/Library/LaunchAgents/com.trident.{job}.plist")],
                       capture_output=True, timeout=20)
    print("已按序 bootstrap；稍后可用 service_guard.py 复核端口。")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--recover", action="store_true")
    args = ap.parse_args()

    if args.recover:
        return recover()

    result = classify()
    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        ts = time.strftime("%Y-%m-%d %H:%M:%S")
        print(f"[{ts}] 写探测: {'可写' if result['probe']['writable'] else '不可写'} "
              f"({result['probe']['waited_s']}s)")
        if result["state"] == "locked":
            print(f"持库进程 {len(result['holders'])} 个，其中 busy 等待 {len(result['busy_waiters'])} 个")
            print(f"分类: {result['kind']}")
        print(f"建议: {result['advice']}")
    return 0 if result["state"] == "ok" else 1


if __name__ == "__main__":
    sys.exit(main())
