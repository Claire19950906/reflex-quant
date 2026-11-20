#!/usr/bin/env python3
"""normalize_raw_news_ts.py — raw_news 时间戳归一（v433 / P0-4）

## 为什么要有这个脚本

2026-11-17 实测生产库：

| 格式 | 条数 | 样例 |
|---|---|---|
| ISO-8601 | 16,415 | `2026-11-17T14:06:40+08:00` |
| epoch 字符串 | 5,922 | `1794555679` |
| `YYYY-MM-DD HH:MM:SS` | 5 | `2026-11-16 04:51:01` |

同一列三种格式，SQLite 做 `now - timestamp` 时会把 ISO 文本当 0 算 →
超龄归档、去重、窗口统计**全部静默算错**（实测跑出「PENDING 积压 49 万小时」的假数）。

## 归一规则（不猜时区）

`ts` 列是采集时刻的唯一真源；抽样 400 条 ISO 行验证 **100% 与 ts 秒级一致**
（中位偏差 0 秒）。所以：

· `ts > 0` → `timestamp = str(ts)`，不做任何时区解析；
· `ts` 为空/0 → 用 `db.coerce_epoch_ts(timestamp)` 解析，解析失败则跳过并报出来。

## 用法

    cd backend
    python3 scripts/normalize_raw_news_ts.py --check   # 只报，退出码 1 = 有脏数据
    python3 scripts/normalize_raw_news_ts.py --apply   # 修，事务提交
"""
from __future__ import annotations

import argparse
import os
import sqlite3
import sys
import time

_HERE = os.path.dirname(os.path.abspath(__file__))
_SRC = os.path.join(os.path.dirname(_HERE), "src_python")
if _SRC not in sys.path:
    sys.path.insert(0, _SRC)

from db import coerce_epoch_ts


def _db_path() -> str:
    return os.getenv("TRIDENT_DB_PATH") or os.path.join(os.path.dirname(_HERE), "trident_event_bus.db")


def _is_clean(timestamp, ts) -> bool:
    s = str(timestamp or "").strip()
    if not (s.isdigit() and len(s) == 10):
        return False
    if ts is None or int(ts) <= 0:
        return True
    return int(s) == int(ts)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--check", action="store_true", help="只检查（默认行为）")
    ap.add_argument("--apply", action="store_true", help="写库修复（默认只检查）")
    ap.add_argument("--db", default=None, help="库路径（默认 TRIDENT_DB_PATH 或 backend/trident_event_bus.db）")
    args = ap.parse_args()

    path = args.db or _db_path()
    if not os.path.exists(path):
        print(f"[ERR] 库不存在：{path}")
        return 2

    conn = sqlite3.connect(path, timeout=30.0)
    conn.execute("PRAGMA busy_timeout=10000")
    dirty = []
    skipped = []
    try:
        rows = conn.execute("SELECT id, ts, timestamp FROM raw_news").fetchall()
        for rid, ts, timestamp in rows:
            if _is_clean(timestamp, ts):
                continue
            target = int(ts) if ts not in (None, "", 0) and int(ts) > 0 else coerce_epoch_ts(timestamp)
            if target is None or int(target) <= 0:
                skipped.append((rid, timestamp, ts))
                continue
            dirty.append((rid, str(int(target)), ts))

        print(f"库：{path}")
        print(f"总行：{len(rows)}  需修：{len(dirty)}  无法解析：{len(skipped)}")
        if skipped:
            for rid, t, ts in skipped[:5]:
                print(f"  [SKIP] id={rid} timestamp={t!r} ts={ts!r}")

        if not dirty:
            print("[OK] timestamp 已全部是 epoch 秒")
            return 0
        if not args.apply:
            print("[CHECK] 有脏数据，退出码 1；加 --apply 修复")
            return 1

        # 生产库在跑引擎，一次 1.6 万行的大事务会撞写锁（实测 database is locked）。
        # 改成分块提交 + 重试：每块 200 行，锁冲突退避 0.3s 重试最多 20 次。
        applied = 0
        failed = 0
        fixed_ts = [(int(v), rid) for rid, v, ts in dirty if ts in (None, "", 0)]
        for i in range(0, len(dirty), 200):
            chunk = dirty[i:i + 200]
            sqls = [("UPDATE raw_news SET timestamp=? WHERE id=?",
                     [(v, rid) for rid, v, _ in chunk])]
            ts_chunk = [(v, rid) for v, rid in fixed_ts if any(r == rid for r, _, _ in chunk)]
            if ts_chunk:
                sqls.append(("UPDATE raw_news SET ts=? WHERE id=?", ts_chunk))
            for _ in range(20):
                try:
                    for sql, params in sqls:
                        conn.executemany(sql, params)
                    conn.commit()
                    applied += len(chunk)
                    break
                except sqlite3.OperationalError as exc:
                    conn.rollback()
                    if "locked" not in str(exc).lower():
                        raise
                    time.sleep(0.3)
            else:
                failed += len(chunk)
            if (i // 200) % 10 == 0:
                print(f"  ... {min(i + 200, len(dirty))}/{len(dirty)}", flush=True)

        left = [r for r in conn.execute("SELECT id, ts, timestamp FROM raw_news")
                if not _is_clean(r[2], r[1])]
        print(f"[APPLY] 已修 {applied} 条，失败 {failed} 条；复检剩余 {len(left)} 条")
        return 0 if not left else 1
    finally:
        conn.close()


if __name__ == "__main__":
    raise SystemExit(main())
