# -*- coding: utf-8 -*-
"""attribution_ai_queue.py — 归因 AI 复核的**全量异步队列** (v340)。

为什么需要它
------------
v321.3 的 cron 是「每 30 分钟跑一次 `--limit 30`，只挑 `ai_called=0`，
按 `move_ts DESC` 排序」。问题不在 limit，在**没有逐行重试状态**：

  · LLM 通道 429 时，最新 30 条全部失败，`ai_called` 仍是 0；
  · 下一轮又按同样的排序挑出**同样的 30 条**重试；
  · 结果是同一批新行被反复烧额度，更老的行永远轮不到 ——
    实测 282 条归因里只有 24 条拿到 AI 判定（8.5%），并且卡在这个比例不动。

队列要解决的正是这件事：每行有独立的 attempts / next_retry_at，
失败就退避、退避期间不占用本轮的调用预算，游标因此能一直往前走。

设计
----
· 只存「谁需要复核、试过几次、下次什么时候试」，**不复制业务字段** ——
  复核时仍按 (src_table, src_id) 回主表/pending 表取原行，避免两处真相。
· status: pending → inflight → done / failed(可重试) / dead(放弃) / gone(源行已消失)
· 幂等：`UNIQUE(src_table, src_id)`，重复入队只是把已 done 的忽略掉。
· 僵尸回收：inflight 超过 30 分钟未回收 → 视为进程崩溃，退回 pending。

CLI
---
    python3 attribution_ai_queue.py --stats
    python3 attribution_ai_queue.py --enqueue          # 只入队，不跑 LLM
    python3 attribution_ai_queue.py --reap             # 回收僵尸 inflight
"""
from __future__ import annotations

import argparse
import os
import random
import sqlite3
import sys
import time
from typing import Any, Dict, List, Optional

SCHEMA = """
CREATE TABLE IF NOT EXISTS attribution_ai_queue (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    src_table     TEXT NOT NULL,              -- main | pending
    src_id        INTEGER NOT NULL,
    move_ts       INTEGER DEFAULT 0,          -- 仅用于排序/统计, 不是真相
    asset         TEXT,
    priority      INTEGER DEFAULT 0,          -- 大异动/近期优先
    status        TEXT DEFAULT 'pending',     -- pending|inflight|done|failed|dead|gone
    attempts      INTEGER DEFAULT 0,
    last_error    TEXT,
    next_retry_at INTEGER DEFAULT 0,
    claimed_at    INTEGER DEFAULT 0,
    created_at    INTEGER DEFAULT 0,
    updated_at    INTEGER DEFAULT 0
);
CREATE UNIQUE INDEX IF NOT EXISTS ux_attr_ai_queue_src
    ON attribution_ai_queue(src_table, src_id);
CREATE INDEX IF NOT EXISTS ix_attr_ai_queue_pick
    ON attribution_ai_queue(status, next_retry_at, priority, id);
"""

MAX_ATTEMPTS = 12          # 12 次退避仍失败 → dead（约跨 1 天，能扛住一次配额枯竭）
ZOMBIE_SECONDS = 1800      # inflight 超过 30 分钟视为进程崩溃
BASE_BACKOFF = 60          # 60s → 120 → 240 … 上限 6h
MAX_BACKOFF = 6 * 3600


def ensure_schema(conn: sqlite3.Connection) -> None:
    for stmt in [s.strip() for s in SCHEMA.split(";") if s.strip()]:
        conn.execute(stmt)
    conn.commit()


def _backoff(attempts: int) -> int:
    delay = min(BASE_BACKOFF * (2 ** max(0, attempts - 1)), MAX_BACKOFF)
    return int(delay * (0.8 + 0.4 * random.random()))


def enqueue_all(conn: sqlite3.Connection) -> Dict[str, int]:
    """把「主表 + 待复核区」里所有 ai_called=0 的行入队。幂等。

    done 的行不会再入队：AI 判定已落库，重复复核只是烧额度。
    """
    ensure_schema(conn)
    now = int(time.time())
    added = 0
    skipped = 0
    tasks: List[tuple] = []

    main_rows = conn.execute(
        "SELECT id, move_ts, asset, move_pct FROM event_move_attribution "
        "WHERE COALESCE(ai_called,0)=0").fetchall()
    for r in main_rows:
        tasks.append(("main", int(r[0]), int(r[1] or 0), r[2], 0))

    try:
        pend_rows = conn.execute(
            "SELECT id, move_ts, asset, move_pct FROM event_move_attribution_pending "
            "WHERE COALESCE(ai_called,0)=0").fetchall()
        for r in pend_rows:
            # 待复核区存的是 |move_pct|>=5% 的大异动 → 优先级更高
            tasks.append(("pending", int(r[0]), int(r[1] or 0), r[2], 1))
    except sqlite3.OperationalError:
        pass

    for src, sid, mts, asset, prio in tasks:
        # 已经 done 的行直接跳过；其余状态只刷新元信息，不动 attempts
        row = conn.execute(
            "SELECT id, status FROM attribution_ai_queue WHERE src_table=? AND src_id=?",
            (src, sid)).fetchone()
        if row:
            if row[1] == "done":
                skipped += 1
                continue
            conn.execute(
                "UPDATE attribution_ai_queue SET move_ts=?, asset=?, priority=MAX(priority,?), "
                "updated_at=? WHERE id=?",
                (mts, asset, prio, now, row[0]))
            continue
        conn.execute(
            "INSERT INTO attribution_ai_queue "
            "(src_table,src_id,move_ts,asset,priority,status,attempts,next_retry_at,"
            "claimed_at,created_at,updated_at) "
            "VALUES (?,?,?,?,?,'pending',0,0,0,?,?)",
            (src, sid, mts, asset, prio, now, now))
        added += 1
    conn.commit()
    return {"added": added, "skipped_done": skipped, "total_candidates": len(tasks)}


def reap_zombies(conn: sqlite3.Connection) -> int:
    """回收超时 inflight（进程被 kill / 机器休眠导致没回写）。"""
    now = int(time.time())
    n = conn.execute(
        "UPDATE attribution_ai_queue SET status='pending', next_retry_at=?, updated_at=? "
        "WHERE status='inflight' AND claimed_at < ?",
        (now, now, now - ZOMBIE_SECONDS)).rowcount
    conn.commit()
    return n


def claim(conn: sqlite3.Connection, limit: int) -> List[Dict[str, Any]]:
    """认领一批 pending；同一条不会被并发进程重复认领（BEGIN IMMEDIATE 内完成）。"""
    if limit <= 0:
        return []
    now = int(time.time())
    out: List[Dict[str, Any]] = []
    conn.execute("BEGIN IMMEDIATE")
    try:
        rows = conn.execute(
            "SELECT id, src_table, src_id, attempts, asset, move_ts, priority "
            "FROM attribution_ai_queue "
            "WHERE status IN ('pending','failed') AND next_retry_at<=? "
            "ORDER BY priority DESC, move_ts DESC, id ASC LIMIT ?",
            (now, limit)).fetchall()
        for r in rows:
            out.append(dict(r))
        if out:
            conn.executemany(
                "UPDATE attribution_ai_queue SET status='inflight', claimed_at=?, updated_at=? "
                "WHERE id=?", [(now, now, r["id"]) for r in out])
        conn.commit()
    except sqlite3.Error:
        conn.rollback()
        raise
    return out


def complete(conn: sqlite3.Connection, qid: int) -> None:
    now = int(time.time())
    conn.execute(
        "UPDATE attribution_ai_queue SET status='done', last_error=NULL, "
        "next_retry_at=0, updated_at=? WHERE id=?", (now, qid))


def fail(conn: sqlite3.Connection, qid: int, err: str, attempts: int,
         gone: bool = False) -> str:
    """记录失败并排下一次重试；返回新状态。"""
    now = int(time.time())
    if gone:
        conn.execute("UPDATE attribution_ai_queue SET status='gone', last_error=?, "
                     "updated_at=? WHERE id=?", ((err or "")[:200], now, qid))
        return "gone"
    new_attempts = attempts + 1
    if new_attempts >= MAX_ATTEMPTS:
        conn.execute("UPDATE attribution_ai_queue SET status='dead', attempts=?, "
                     "last_error=?, updated_at=? WHERE id=?",
                     (new_attempts, (err or "")[:200], now, qid))
        return "dead"
    conn.execute("UPDATE attribution_ai_queue SET status='failed', attempts=?, "
                 "last_error=?, next_retry_at=?, updated_at=? WHERE id=?",
                 (new_attempts, (err or "")[:200], now + _backoff(new_attempts), now, qid))
    return "failed"


def defer(conn: sqlite3.Connection, qid: int, err: str, seconds: int = 900) -> None:
    """通道整体不可用时的**暂停**：退回 pending，但**不增加 attempts**。

    和 fail() 的区别：fail 是"这一条有问题"（内容/解析），defer 是"整个通道没容量"
    （MiniMax 429、本地模型超时熔断）。后者不该消耗重试次数，否则一次持续一天的
    配额枯竭会把整个积压全部打成 dead。
    """
    now = int(time.time())
    conn.execute(
        "UPDATE attribution_ai_queue SET status='pending', last_error=?, "
        "next_retry_at=?, updated_at=? WHERE id=?",
        ((err or "")[:200], now + seconds, now, qid))


def coverage(conn: sqlite3.Connection) -> Dict[str, Any]:
    """覆盖率口径：ai_called=1 / 总行数（含待复核区，与 bridge 的 note 一致）。"""
    try:
        total = conn.execute("SELECT COUNT(*) FROM event_move_attribution").fetchone()[0]
        called = conn.execute(
            "SELECT COUNT(*) FROM event_move_attribution WHERE ai_called=1").fetchone()[0]
    except sqlite3.OperationalError:
        return {"total": 0, "called": 0, "pct": 0.0}
    pct = (called / total * 100.0) if total else 0.0
    return {"total": total, "called": called, "pct": round(pct, 1)}


def stats(conn: sqlite3.Connection) -> Dict[str, Any]:
    try:
        q = {r[0]: r[1] for r in conn.execute(
            "SELECT status, COUNT(*) FROM attribution_ai_queue GROUP BY 1").fetchall()}
    except sqlite3.OperationalError:
        q = {}
    now = int(time.time())
    due = 0
    if q:
        due = conn.execute(
            "SELECT COUNT(*) FROM attribution_ai_queue "
            "WHERE status IN ('pending','failed') AND next_retry_at<=?", (now,)).fetchone()[0]
    return {"queue": q, "due_now": due, "coverage": coverage(conn)}


def main() -> int:
    ap = argparse.ArgumentParser(description="归因 AI 复核队列")
    ap.add_argument("--db", default=os.environ.get("TRIDENT_DB_PATH") or os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "trident_event_bus.db"))
    ap.add_argument("--enqueue", action="store_true", help="只入队")
    ap.add_argument("--reap", action="store_true", help="只回收僵尸")
    ap.add_argument("--stats", action="store_true", help="只看统计")
    args = ap.parse_args()

    conn = sqlite3.connect(args.db, timeout=60)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout=60000")
    ensure_schema(conn)

    if args.enqueue:
        print("[OK] 入队", enqueue_all(conn))
    if args.reap:
        print("[OK] 回收僵尸", reap_zombies(conn))
    if args.stats or not (args.enqueue or args.reap):
        import json as _json
        print(_json.dumps(stats(conn), ensure_ascii=False, indent=2))
    conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
