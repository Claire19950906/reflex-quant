#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""v409 · 回填 decision_kind（analysis / routing_miss）

问题
----
S16「降级路径占比」实测 87.2%（450/516），但其中 279 条是 `target_asset=NONE`
的决策 —— 它们根本没有可分析对象（不是模型答得差，也不是数据缺失）。
把这两类混在一个分母里，「路由没打上标的」就会被读成「模型没被问」。

本脚本只做一件事：给历史决策补 `decision_kind`
    · target_asset 为空或 NONE  → routing_miss
    · 其余                      → analysis
不改任何判定、不改 analysis_degraded、不碰反思与错误码。

用法
----
    python3 scripts/backfill_decision_kind.py            # dry-run，只打印分布
    python3 scripts/backfill_decision_kind.py --commit   # 落库
"""
from __future__ import annotations

import argparse
import os
import sqlite3
import sys

BACKEND = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SRC = os.path.join(BACKEND, "src_python")
for p in (BACKEND, SRC):
    if p not in sys.path:
        sys.path.insert(0, p)

import db


def _ensure_column(conn: sqlite3.Connection) -> None:
    cols = {r[1] for r in conn.execute("PRAGMA table_info(ai_decisions)")}
    if "decision_kind" not in cols:
        conn.execute(
            "ALTER TABLE ai_decisions ADD COLUMN decision_kind TEXT DEFAULT 'analysis'")


def plan(conn: sqlite3.Connection) -> dict:
    rows = conn.execute(
        """SELECT CASE
                 WHEN COALESCE(target_asset,'') = '' OR UPPER(target_asset) = 'NONE'
                 THEN 'routing_miss' ELSE 'analysis' END AS kind,
               COUNT(*) n
             FROM ai_decisions GROUP BY 1"""
    ).fetchall()
    return {r[0]: int(r[1]) for r in rows}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="回填 decision_kind（默认 dry-run）")
    ap.add_argument("--db", default="", help="DB 路径（默认生产库）")
    ap.add_argument("--commit", action="store_true", help="真的写库")
    args = ap.parse_args(argv)

    path = args.db or db.get_db_path()
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    try:
        _ensure_column(conn)
        before = plan(conn)
        current = {}
        for r in conn.execute("SELECT COALESCE(decision_kind,'') k, COUNT(*) n "
                              "FROM ai_decisions GROUP BY 1"):
            current[r[0]] = int(r[1])
        print(f"[db] {path}")
        print(f"[before] 目标分布 {before}")
        print(f"[current] 现有 decision_kind 分布 {current}")
        if not args.commit:
            print("[dry-run] 未写库；加 --commit 落库")
            return 0

        # 注意：ALTER 加列时 DEFAULT 'analysis' 会把历史行直接填成 analysis，
        # 所以不能只找"空值"——必须显式把无标的行翻成 routing_miss。
        # 两条 UPDATE 都是幂等的，可重复跑。
        n = 0
        cur = conn.execute(
            """UPDATE ai_decisions SET decision_kind='routing_miss'
                WHERE (COALESCE(target_asset,'') = '' OR UPPER(target_asset) = 'NONE')
                  AND COALESCE(decision_kind,'') <> 'routing_miss'""")
        n += cur.rowcount
        cur = conn.execute(
            """UPDATE ai_decisions SET decision_kind='analysis'
                WHERE COALESCE(target_asset,'') <> '' AND UPPER(target_asset) <> 'NONE'
                  AND COALESCE(decision_kind,'') = ''""")
        n += cur.rowcount
        conn.commit()
        print(f"[commit] 回填 {n} 行")
        after = {}
        for r in conn.execute("SELECT COALESCE(decision_kind,'') k, COUNT(*) n "
                              "FROM ai_decisions GROUP BY 1"):
            after[r[0]] = int(r[1])
        print(f"[after] {after}")
        return 0
    finally:
        conn.close()


if __name__ == "__main__":
    raise SystemExit(main())
