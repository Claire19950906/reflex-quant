#!/usr/bin/env python3
"""把 raw_news 里"没有 filter_reason"的归档行补上理由，并捞回真正漏掉的单子。

背景（2026-11-17 复盘）
------------------------
`raw_news` 里 FILTERED 且有 15,986 行 `filter_reason` 为空 —— 归档了但没记理由，
漏斗统计（analysis_yield.py）就没法回答"这条到底为什么没进分析"。
可按旁证字段 `gate_verdict` / `gate_unavailable` / `is_noise` 还原理由：

    可交易    → 门卫判可交易却仍被归档（漏单嫌疑）
    排除      → 门卫判噪音
    无信号    → 门卫判无信号
    gate_unavailable=2 → 门卫当时不可用、未判定
    其余空     → 属于更早期版本的遗留行

处置规则（幂等，可反复跑）
--------------------------
1. 门卫判「可交易」且新闻还在时效内（默认 6h）→ **恢复成 PENDING 重跑**（这是真漏单）；
   已过时效 → `filter_reason='tradeable_but_expired'`（归档是对的，只是要留痕）。
2. 「排除」→ `gate_noise`；「无信号」→ `gate_no_signal`；
   门卫不可用 → `gate_undetermined`；其余 → `legacy_unknown`。
3. 历史遗留的 `degraded_stale` 统一改名 `gate_down`（与 v433 口径一致）。

用法
----
    python3 backend/scripts/backfill_filter_reason.py --check
    python3 backend/scripts/backfill_filter_reason.py --apply
    python3 backend/scripts/backfill_filter_reason.py --apply --no-restore   # 只补理由，不捞单
"""
from __future__ import annotations

import argparse
import os
import sqlite3
import sys
import time
from typing import Dict, List, Tuple

DB = os.environ.get(
    "TRIDENT_DB",
    os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                 "trident_event_bus.db"),
)
EXPIRE_H = float(os.getenv("NEWS_EXPIRE_GATE_DOWN_H", os.getenv("NEWS_EXPIRE_H", "6")))


def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(DB, timeout=30)
    conn.execute("PRAGMA busy_timeout=30000")
    return conn


def _now() -> int:
    return int(time.time())


def plan(conn: sqlite3.Connection, restore: bool) -> Dict[str, List[Tuple[int, str]]]:
    """给出每一类要改的行 id，先看再改。"""
    now = _now()
    cutoff = now - int(EXPIRE_H * 3600)
    out: Dict[str, List[Tuple[int, str]]] = {}

    # 1) 门卫判可交易、仍在时效内 → 捞回（真漏单）
    if restore:
        rows = conn.execute(
            "SELECT id FROM raw_news WHERE status='FILTERED' AND filter_reason=''"
            " AND gate_verdict='可交易' AND ts >= ?", (cutoff,)).fetchall()
        out["restore_tradeable"] = [(int(r[0]), "PENDING") for r in rows]

    # 2) 门卫判可交易但已过时效
    rows = conn.execute(
        "SELECT id FROM raw_news WHERE status='FILTERED' AND filter_reason=''"
        " AND gate_verdict='可交易' AND ts < ?", (cutoff,)).fetchall()
    out["tradeable_but_expired"] = [(int(r[0]), "tradeable_but_expired") for r in rows]

    # 3) 其余按旁证还原（每个分类互斥，避免 catch-all 把已分类的行也算进去）
    for label, cond in (
        ("gate_noise", "gate_verdict LIKE '%排除%' AND COALESCE(gate_unavailable,0) != 2"),
        ("gate_no_signal", "gate_verdict LIKE '%无信号%' AND COALESCE(gate_unavailable,0) != 2"),
        ("gate_undetermined", "COALESCE(gate_unavailable,0)=2"),
        ("legacy_unknown",
         "gate_verdict NOT LIKE '%排除%' AND gate_verdict NOT LIKE '%无信号%'"
         " AND COALESCE(gate_unavailable,0) != 2"),
    ):
        rows = conn.execute(
            f"SELECT id FROM raw_news WHERE status='FILTERED' AND filter_reason=''"
            f" AND gate_verdict != '可交易' AND {cond}").fetchall()
        if rows:
            out[label] = [(int(r[0]), label) for r in rows]

    # 4) 老名字归一
    rows = conn.execute(
        "SELECT id FROM raw_news WHERE status='FILTERED'"
        " AND filter_reason='degraded_stale'").fetchall()
    if rows:
        out["rename_degraded_stale"] = [(int(r[0]), "gate_down") for r in rows]
    return out


def apply_plan(conn: sqlite3.Connection, groups: Dict[str, List[Tuple[int, str]]]) -> Dict[str, int]:
    done: Dict[str, int] = {}
    for name, items in groups.items():
        if not items:
            done[name] = 0
            continue
        ids = [i for i, _ in items]
        touched = 0
        if name == "restore_tradeable":
            # 捞回：status FILTERED → PENDING，清掉理由让引擎重新走一遍
            for chunk_start in range(0, len(ids), 200):
                chunk = ids[chunk_start:chunk_start + 200]
                ph = ",".join("?" * len(chunk))
                cur = conn.execute(
                    f"UPDATE raw_news SET status='PENDING', filter_reason=''"
                    f" WHERE id IN ({ph}) AND status='FILTERED'", chunk)
                touched += cur.rowcount or 0
                conn.commit()
        else:
            reason = items[0][1]
            for chunk_start in range(0, len(ids), 200):
                chunk = ids[chunk_start:chunk_start + 200]
                ph = ",".join("?" * len(chunk))
                cur = conn.execute(
                    f"UPDATE raw_news SET filter_reason=? WHERE id IN ({ph})", [reason] + chunk)
                touched += cur.rowcount or 0
                conn.commit()
        done[name] = touched
    return done


def summary(conn: sqlite3.Connection) -> None:
    rows = conn.execute(
        "SELECT CASE WHEN filter_reason IS NULL OR filter_reason='' THEN '(空)'"
        " ELSE filter_reason END r, COUNT(*) FROM raw_news"
        " WHERE status='FILTERED' GROUP BY 1 ORDER BY 2 DESC LIMIT 12").fetchall()
    print("  当前 FILTERED 理由分布：")
    for r, n in rows:
        print(f"    {r:<24} {n}")


def main() -> int:
    ap = argparse.ArgumentParser(description="补 filter_reason / 捞回漏单")
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--check", action="store_true", help="只统计将改动多少行")
    g.add_argument("--apply", action="store_true", help="真正写库")
    ap.add_argument("--no-restore", action="store_true", help="只补理由，不把可交易单捞回")
    args = ap.parse_args()

    conn = _connect()
    try:
        groups = plan(conn, restore=not args.no_restore)
        print(f"库={DB}")
        for name, items in groups.items():
            print(f"  {name:<24} {len(items)} 行")
        if args.check:
            summary(conn)
            print("（--check 模式，未写库）")
            return 0
        done = apply_plan(conn, groups)
        print("已写库：")
        for name, n in done.items():
            print(f"  {name:<24} {n} 行")
        summary(conn)
        return 0
    finally:
        conn.close()


if __name__ == "__main__":
    sys.exit(main())
