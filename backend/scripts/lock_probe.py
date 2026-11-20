#!/usr/bin/env python3
"""写锁探针：每 3 秒尝试 BEGIN IMMEDIATE（1s 超时），记录 成功/被挡 与等待时长。

为什么要它：2026-11-17 实测外部写被引擎挡了 23 分钟（`database is locked`），
但代码里所有写点看起来都是「短连接 + 立即 commit」，光看代码找不到持锁者。
这个探针把「锁被占用的时间窗口」精确打出来，再和引擎日志（claim / gate / 落库）
对时间轴，就能定位到具体阶段。

用法：python3 scripts/lock_probe.py --seconds 180
只读：BEGIN IMMEDIATE 后立刻 ROLLBACK，不写任何数据。
"""
from __future__ import annotations

import argparse
import os
import sqlite3
import sys
import time
from pathlib import Path

DB = Path(__file__).resolve().parents[1] / "trident_event_bus.db"


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--seconds", type=int, default=180)
    ap.add_argument("--interval", type=float, default=3.0)
    ap.add_argument("--timeout-ms", type=int, default=1000)
    a = ap.parse_args(argv)

    t_end = time.time() + a.seconds
    miss_total = 0
    miss_streak_start = None
    print(f"[probe] db={DB} interval={a.interval}s timeout={a.timeout_ms}ms", flush=True)
    while time.time() < t_end:
        t0 = time.time()
        ok = False
        err = ""
        try:
            conn = sqlite3.connect(f"{DB}", timeout=a.timeout_ms / 1000.0)
            conn.execute(f"PRAGMA busy_timeout={a.timeout_ms};")
            conn.execute("BEGIN IMMEDIATE;")
            conn.execute("ROLLBACK;")
            ok = True
        except sqlite3.OperationalError as exc:
            err = str(exc)[:60]
        except Exception as exc:
            err = f"{type(exc).__name__}: {str(exc)[:60]}"
        finally:
            try:
                conn.close()
            except Exception:
                pass
        dt = time.time() - t0
        stamp = time.strftime("%H:%M:%S")
        if ok:
            if miss_streak_start is not None:
                print(f"{stamp} [probe] 锁释放（被挡 {int(time.time() - miss_streak_start)}s，"
                      f"期间 {miss_total} 次失败）", flush=True)
                miss_streak_start = None
                miss_total = 0
        else:
            miss_total += 1
            if miss_streak_start is None:
                miss_streak_start = t0
                print(f"{stamp} [probe] 写锁被占（{err}）", flush=True)
        time.sleep(max(0.0, a.interval - dt))
    if miss_streak_start is not None:
        print(f"[probe] 结束时仍在被挡（已 {int(time.time() - miss_streak_start)}s）", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
