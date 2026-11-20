#!/usr/bin/env python3
"""Crowd Pulse — 定时采集 worker（launchd 托管，单实例循环采集）。

用法：
    python crowd_worker.py --once            # 跑一次就退出（CLI / 冒烟）
    python crowd_worker.py --interval 60     # 循环采集（launchd 用）

设计约束
--------
- 采集失败**不静默**：异常与各源错误都打印到 stdout（launchd 收进日志），
  并写进 crowd_source_runs.errors_json，可事后追溯。
- 单次运行失败不终止循环：下一轮继续（数据源抖动是常态）。
- SIGTERM 会被响应：sleep 分片进行，保证 launchd 停止时能及时退出。
"""
from __future__ import annotations

import argparse
import os
import signal
import sqlite3
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import config
import db
import crowd_pulse


def run_once() -> dict:
    conn = db.get_connection()
    conn.row_factory = sqlite3.Row
    try:
        # 幂等迁移（铁律 1：schema 只在 db.py 定义）。生产库可能被 engine/api 占锁，
        # db.get_connection() 的 busy_timeout 会等待重试。
        db.migrate(conn)
        # v342: migrate 里只有 DDL、没有 commit，默认隔离级别下这会把一个**写事务**
        # 一直开着；紧接着 crowd_pulse 要发 HTTP 采集，网络卡住时这把写锁就被占死
        # （实测 19 分钟 0% CPU、全库其它任务全部 database is locked）。
        conn.commit()
        return crowd_pulse.run_once(conn, list(config.CROWD_PULSE_SOURCES))
    finally:
        try:
            conn.close()
        except Exception:
            pass


def main() -> int:
    ap = argparse.ArgumentParser(description="Crowd Pulse collector")
    ap.add_argument("--once", action="store_true", help="只跑一次就退出")
    ap.add_argument("--interval", type=int,
                    default=config.CROWD_PULSE_INTERVAL_MINUTES,
                    help="采集间隔（分钟）")
    args = ap.parse_args()

    running = {"on": True}

    def _stop(signum, _frame):
        running["on"] = False
        print("[CROWD] 收到信号 %s，准备退出" % signum, flush=True)

    signal.signal(signal.SIGTERM, _stop)
    signal.signal(signal.SIGINT, _stop)

    if not config.CROWD_PULSE_ENABLED:
        print("[CROWD] CROWD_PULSE_ENABLED=0，退出", flush=True)
        return 0

    while running["on"]:
        try:
            s = run_once()
            print("[CROWD] %s status=%s items=%d per_source=%s errors=%s"
                  % (time.strftime("%Y-%m-%d %H:%M:%S"), s["status"],
                     s["items_total"], s["per_source"], s["errors"]), flush=True)
        except Exception as exc:
            print("[CROWD] 运行失败: %s: %s" % (type(exc).__name__, exc), flush=True)
        if args.once:
            break
        # 分片 sleep，保证 SIGTERM 能及时生效
        for _ in range(max(1, args.interval) * 6):
            if not running["on"]:
                break
            time.sleep(10)
    return 0


if __name__ == "__main__":
    sys.exit(main())
