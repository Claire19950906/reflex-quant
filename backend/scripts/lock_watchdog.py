#!/usr/bin/env python3
"""写锁看门狗：连续抢不到写锁就把"谁占着库"打出来。

背景（2026-11-17 21:42–21:50 事故）
---------------------------------
写入全面停摆 8 分钟：DB 文件 mtime 定格、主分析 0 次调用、日志满屏
`database is locked`。锁探针 `BEGIN IMMEDIATE`（timeout=3s）连续全超时，
`PRAGMA wal_checkpoint(TRUNCATE)` 返回 `(1, 281, 281)` = busy。
当时是**人工**逐个 SIGTERM 几十个进程、再全停 + 截断 WAL 才恢复，排查半小时。

结论：问题不是"锁慢"，而是**有写入方开着写事务不提交**（事务里夹着网络/取价等待），
所以 busy_timeout 调到 20s 也没用 —— 它永不结束。

这个脚本把那次人工排查固化：
  每 --interval 秒抢一次写锁（抢到立刻 ROLLBACK，不写任何数据）；
  连续 --threshold 次抢不到（默认 3 次 ≈ 90s）→ 判定"写锁停摆"，
  打出一份可直接照着处理的诊断：持库进程 pid + 命令行、长事务哨兵尾部、
  引擎日志里的锁错误尾部，并写进 ~/.trident/log/lock_watchdog.log。

用法
----
  python3 backend/scripts/lock_watchdog.py --once        # 单次探测（可接 cron）
  python3 backend/scripts/lock_watchdog.py               # 常驻，每 30s 一次
  python3 backend/scripts/lock_watchdog.py --threshold 2 --interval 15

退出码：0 = 锁可用（或已恢复）；1 = 本次判定停摆（--once 模式下可直接进告警系统）。
"""
from __future__ import annotations

import argparse
import json
import os
import sqlite3
import subprocess
import sys
import time

DB = os.environ.get(
    "TRIDENT_DB",
    os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                 "trident_event_bus.db"),
)
LOG_DIR = os.path.expanduser("~/.trident/log")
ALERT_LOG = os.path.join(LOG_DIR, "lock_watchdog.log")
TXN_LOG = os.environ.get("TRIDENT_TXN_PROFILE_LOG", "/tmp/trident_txn_profile.log")
STATE = "/tmp/trident_lock_watchdog_state.json"


def _now() -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S")


def _write(line: str) -> None:
    print(line, flush=True)
    try:
        os.makedirs(LOG_DIR, exist_ok=True)
        with open(ALERT_LOG, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:
        pass


def probe_wait(timeout: float) -> tuple[bool, float]:
    """抢一次写锁；抢到立刻回滚（零写入）。返回 (是否抢到, 耗时秒)。"""
    t0 = time.perf_counter()
    try:
        conn = sqlite3.connect(DB, timeout=timeout)
    except Exception:
        return False, time.perf_counter() - t0
    try:
        conn.execute("BEGIN IMMEDIATE")
        conn.execute("ROLLBACK")
        return True, time.perf_counter() - t0
    except Exception:
        return False, time.perf_counter() - t0
    finally:
        try:
            conn.close()
        except Exception:
            pass


def _holders() -> list[str]:
    """当前持有 DB 句柄的进程（pid + 命令行）。"""
    out: list[str] = []
    try:
        raw = subprocess.run(["lsof", DB], capture_output=True, text=True, timeout=10).stdout
    except Exception as exc:
        return [f"lsof 失败: {type(exc).__name__}: {exc}"]
    pids = sorted({ln.split()[1] for ln in raw.splitlines()[1:] if len(ln.split()) > 1})
    for pid in pids:
        try:
            ps = subprocess.run(["ps", "-Ao", "pid=,etime=,args="],
                                capture_output=True, text=True, timeout=10).stdout
            for ln in ps.splitlines():
                parts = ln.split(None, 2)
                if parts and parts[0] == pid:
                    etime = parts[1] if len(parts) > 1 else "?"
                    cmd = parts[2] if len(parts) > 2 else ""
                    cmd = cmd.replace("/Users/claire/Desktop/金融1.0/backend/src_python/", "")
                    out.append(f"  pid={pid} 运行={etime} {cmd[:150]}")
                    break
            else:
                out.append(f"  pid={pid} (ps 查不到，可能已退出)")
        except Exception:
            out.append(f"  pid={pid}")
    return out or ["  （没有任何进程持有库句柄 —— 可能是 -shm 状态卡死，先试 checkpoint）"]


def _tail(path: str, n: int, grep: str = "") -> list[str]:
    if not os.path.exists(path):
        return [f"  （{path} 不存在）"]
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            lines = f.readlines()[-2000:]
        picked = [ln.rstrip() for ln in lines if (not grep or grep in ln)]
        return ["  " + ln[:220] for ln in picked[-n:]] or [f"  （{path} 最近没有匹配行）"]
    except Exception as exc:
        return [f"  读取失败: {type(exc).__name__}: {exc}"]


def _load_state() -> dict:
    try:
        with open(STATE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {"fails": 0, "last_alert": 0.0, "alerting": False}


def _save_state(st: dict) -> None:
    try:
        with open(STATE, "w", encoding="utf-8") as f:
            json.dump(st, f)
    except Exception:
        pass


def diagnose(fails: int, waits: list[float]) -> None:
    _write(f"[{_now()}] ⛔ 写锁停摆：连续 {fails} 次抢不到（本次最久等 {max(waits):.1f}s，库={DB}）")
    _write("  持库进程：")
    for ln in _holders():
        _write(ln)
    _write(f"  长事务哨兵（{TXN_LOG}）尾部：")
    for ln in _tail(TXN_LOG, 6):
        _write(ln)
    _write("  引擎日志锁错误尾部：")
    for ln in _tail(os.path.join(LOG_DIR, "engine.out.log"), 5, "database is locked"):
        _write(ln)
    _write("  处置顺序：① 看上面 pid 里有没有刚起的 cron/一次性脚本 → 等它退出或 TERM；"
           "② 全停 engine/api/bridge + 所有持库进程，确认 lsof 为空后 "
           "`PRAGMA wal_checkpoint(TRUNCATE)` 截断 WAL；③ 重启三个服务。")


def run_once(interval: float, threshold: int, timeout: float) -> int:
    st = _load_state()
    ok, cost = probe_wait(timeout)
    if ok:
        if st.get("alerting"):
            _write(f"[{_now()}] ✅ 写锁已恢复（本次等待 {cost:.2f}s，此前连续失败 {st.get('fails', 0)} 次）")
        st.update({"fails": 0, "alerting": False})
        _save_state(st)
        return 0

    st["fails"] = int(st.get("fails", 0)) + 1
    st.setdefault("waits", []).append(round(cost, 1))
    st["waits"] = st["waits"][-5:]
    fails = st["fails"]

    # 逐次升级提示：第 1 次只是观察，到阈值才判定停摆
    if fails == 1:
        _write(f"[{_now()}] ⚠️ 抢不到写锁（等 {cost:.1f}s），开始计数（阈值 {threshold}）")
    if fails >= threshold:
        # 冷却：同一波故障最多 10 分钟警告一次，避免刷屏
        if time.time() - float(st.get("last_alert", 0.0)) > 600:
            diagnose(fails, st.get("waits") or [cost])
            st["last_alert"] = time.time()
        st["alerting"] = True
    _save_state(st)
    return 1


def main() -> int:
    ap = argparse.ArgumentParser(description="写锁看门狗")
    ap.add_argument("--interval", type=float, default=30.0, help="探测间隔秒（默认 30）")
    ap.add_argument("--threshold", type=int, default=3, help="连续几次抢不到判定停摆（默认 3）")
    ap.add_argument("--timeout", type=float, default=2.0, help="单次抢锁超时秒（默认 2）")
    ap.add_argument("--once", action="store_true", help="只探测一次就退出")
    args = ap.parse_args()

    if args.once:
        return run_once(args.interval, args.threshold, args.timeout)
    _write(f"[{_now()}] 看门狗启动 | 库={DB} | 间隔={args.interval}s | 阈值={args.threshold} 次")
    while True:
        run_once(args.interval, args.threshold, args.timeout)
        time.sleep(max(1.0, args.interval))


if __name__ == "__main__":
    sys.exit(main())
