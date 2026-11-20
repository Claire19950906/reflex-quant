#!/usr/bin/env python3
"""每小时把 WAL 截断回 0，防止长事务/长连接把 -wal 撑到上百 MB。

背景：SQLite 的 wal_autocheckpoint 只在「提交且没有读锁」时触发。桥接服务
(bridge_server) 常挂若干长连接做前端轮询，写侧又有大事务，实测 -wal 一天能
涨到 124MB，占盘被放大，checkpoint 时还会卡写操作。这里在窗口外强制
checkpoint(TRUNCATE)；被读者挡住时返回非 0，无害。
"""
import os
import sqlite3
import sys
import time

DB = os.environ.get(
    "TRIDENT_DB",
    os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                 "trident_event_bus.db"),
)
WAL = DB + "-wal"


def main() -> int:
    before = os.path.getsize(WAL) / 1048576 if os.path.exists(WAL) else 0.0
    try:
        conn = sqlite3.connect(DB, timeout=30)
        res = conn.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
        conn.close()
    except Exception as exc:
        print(f"[{time.strftime('%H:%M:%S')}] checkpoint 失败: {type(exc).__name__}: {exc}")
        return 1
    after = os.path.getsize(WAL) / 1048576 if os.path.exists(WAL) else 0.0
    print(f"[{time.strftime('%H:%M:%S')}] WAL {before:.1f}MB -> {after:.1f}MB | 结果={res}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
