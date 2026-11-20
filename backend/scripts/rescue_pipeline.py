#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""rescue_pipeline — 一键救援「待分析」堆积 / FAILED 积压 / 僵尸 PROCESSING。

背景（2026-11-04 生产实测）
--------------------------
引擎连着几小时不产信号，前端「待分析」只涨不跌。拆开看是三个独立的积压：

  1. **门卫冻结**（gate_unavailable=1）—— 324 条
     门卫通道 HTTP 401（key 失效）→ 每条新新闻都被冻结；
     主分析认领 SQL 明确排除该标记 → 一条都认领不到。

  2. **FAILED 积压** —— 581 条
     retry_count 打到 `_MAX_PENDING_RETRIES`(3) 被标 FAILED，之后再也不会被认领。
     这些多是通道故障期间被"连坐"的**好数据**。

  3. **僵尸 PROCESSING** —— 引擎崩在分析中途，认领了但没写回结果。

这个脚本把三类积压一次性清干净，并给出**每一步改了多少行**的账。
默认**只做只读预演**（--dry-run），加 `--apply` 才真正写库。

用法：
    python3 scripts/rescue_pipeline.py --dry-run     # 先看会改什么（默认）
    python3 scripts/rescue_pipeline.py --apply        # 真正执行
    python3 scripts/rescue_pipeline.py --apply --reset-failed
    python3 scripts/rescue_pipeline.py --status       # 只看当前积压快照
"""
from __future__ import annotations

import argparse
import sqlite3
import sys
from pathlib import Path

SRC = Path(__file__).resolve().parent.parent / "src_python"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

import config


def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(config.DB_PATH, timeout=60.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout=30000;")
    return conn


def snapshot(conn: sqlite3.Connection) -> dict:
    """当前流水线积压快照（只读）。"""
    q = lambda s: conn.execute(s).fetchone()[0]
    return {
        "pending_total": q("SELECT COUNT(*) FROM raw_news WHERE status='PENDING'"),
        "pending_signal": q(
            "SELECT COUNT(*) FROM raw_news WHERE status='PENDING' AND is_noise=0"),
        "pending_frozen": q(
            "SELECT COUNT(*) FROM raw_news WHERE status='PENDING' AND is_noise=0 "
            "AND COALESCE(gate_unavailable,0)=1"),
        "pending_claimable": q(
            "SELECT COUNT(*) FROM raw_news WHERE status='PENDING' AND is_noise=0 "
            "AND COALESCE(gate_unavailable,0)=0"),
        "processing": q("SELECT COUNT(*) FROM raw_news WHERE status='PROCESSING'"),
        "failed": q("SELECT COUNT(*) FROM raw_news WHERE status='FAILED'"),
        "done": q("SELECT COUNT(*) FROM raw_news WHERE status='DONE'"),
        "ai_decisions": q("SELECT COUNT(*) FROM ai_decisions"),
        "last_ai_decision": q("SELECT MAX(created_at) FROM ai_decisions"),
    }


def _print_snapshot(snap: dict) -> None:
    print("── 当前流水线状态 " + "─" * 46)
    print(f"  待分析(PENDING, 非噪音)   : {snap['pending_signal']}")
    print(f"    ├─ 门卫冻结(gate_unavail): {snap['pending_frozen']}")
    print(f"    └─ 可认领                : {snap['pending_claimable']}")
    print(f"  待分析合计(含噪音)         : {snap['pending_total']}")
    print(f"  处理中(PROCESSING)        : {snap['processing']}")
    print(f"  失败(FAILED)              : {snap['failed']}")
    print(f"  已完成(DONE)              : {snap['done']}")
    print(f"  信号数(ai_decisions)      : {snap['ai_decisions']}")
    print(f"  最近一条信号              : {snap['last_ai_decision']}")


def rescue(conn: sqlite3.Connection, *, apply: bool = False,
           reset_failed: bool = False) -> dict:
    """执行救援。apply=False 时只预演不写库。返回各项影响行数。"""
    stats = {"unfrozen": 0, "failed_reset": 0, "zombie_processing": 0}

    # ① 门卫冻结 → 解冻（全部解冻）
    #    TTL 兜底已在 ai_worker 主循环里跑（>30 分钟自动放行）；
    #    这里是**手动一键清仓**，把历史积压一次清干净。
    #    解冻后 gate_replay_guard 仍可对未判定的行补判（幂等，不冲突）。
    if apply:
        cur = conn.execute(
            "UPDATE raw_news SET gate_unavailable = 0, retry_count = 0"
            " WHERE COALESCE(gate_unavailable,0) = 1"
            "   AND status = 'PENDING' AND COALESCE(is_noise,0) = 0")
        stats["unfrozen"] = cur.rowcount or 0
        conn.commit()
    else:
        stats["unfrozen"] = conn.execute(
            "SELECT COUNT(*) FROM raw_news WHERE COALESCE(gate_unavailable,0)=1 "
            "AND status='PENDING' AND COALESCE(is_noise,0)=0").fetchone()[0]

    # ② FAILED → PENDING（重置 retry_count）
    #    这些多是通道故障期间被连坐的好数据，通道恢复后值得重跑。
    if reset_failed:
        if apply:
            cur = conn.execute(
                "UPDATE raw_news SET status='PENDING', retry_count=0, processing_at=NULL"
                " WHERE status='FAILED'")
            stats["failed_reset"] = cur.rowcount or 0
            conn.commit()
        else:
            stats["failed_reset"] = conn.execute(
                "SELECT COUNT(*) FROM raw_news WHERE status='FAILED'").fetchone()[0]

    # ③ 僵尸 PROCESSING → PENDING
    #    引擎崩在分析中途，认领了没写回。processing_at 超 15 分钟即视为僵尸。
    if apply:
        cur = conn.execute(
            "UPDATE raw_news SET status='PENDING', processing_at=NULL"
            " WHERE status='PROCESSING'"
            "   AND (processing_at IS NULL"
            "        OR processing_at < datetime('now','localtime','-15 minutes'))")
        stats["zombie_processing"] = cur.rowcount or 0
        conn.commit()
    else:
        stats["zombie_processing"] = conn.execute(
            "SELECT COUNT(*) FROM raw_news WHERE status='PROCESSING'").fetchone()[0]

    return stats


def main() -> int:
    ap = argparse.ArgumentParser(
        description="一键救援「待分析」堆积 / FAILED 积压 / 僵尸 PROCESSING")
    ap.add_argument("--apply", action="store_true", help="真正写库（默认只预演）")
    ap.add_argument("--reset-failed", action="store_true",
                    help="同时把 FAILED 重置回 PENDING（会重新消耗 LLM 额度）")
    ap.add_argument("--status", action="store_true", help="只打印状态快照，不做改动")
    args = ap.parse_args()

    conn = _connect()
    try:
        before = snapshot(conn)
        _print_snapshot(before)

        if args.status:
            return 0

        print()
        print("── 救援%s " % ("（执行）" if args.apply else "（预演，加 --apply 才写库）")
              + "─" * 30)
        stats = rescue(conn, apply=args.apply, reset_failed=args.reset_failed)
        print(f"  解冻门卫冻结新闻     : {stats['unfrozen']} 条")
        if args.reset_failed:
            print(f"  重置 FAILED → PENDING: {stats['failed_reset']} 条")
        else:
            print("  重置 FAILED → PENDING: 0 条（加 --reset-failed 才会做）")
        print(f"  回收僵尸 PROCESSING   : {stats['zombie_processing']} 条")

        if args.apply:
            after = snapshot(conn)
            print()
            print("── 执行后 " + "─" * 53)
            _print_snapshot(after)
            print()
            print("下一步：确认引擎在跑（launchctl list | grep com.trident.engine），")
            print("       引擎每轮会自动认领 PENDING 新闻并产出 ai_decisions 信号。")
        else:
            print()
            print("以上为预演结果。确认无误后执行：")
            print("  python3 scripts/rescue_pipeline.py --apply"
                  + (" --reset-failed" if args.reset_failed else ""))
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
