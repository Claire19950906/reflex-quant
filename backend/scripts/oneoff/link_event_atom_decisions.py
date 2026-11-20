#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""link_event_atom_decisions.py — 事件原子 ↔ AI 决策 的双向索引后半段。

背景（2026-11-07 实测）
----------------------
能力验证发现：**串联是断的**。

    event_atom              3347 行   （第一性原理拆出来的事件原子）
    event_atom_decisions       0 行   ← 孤儿表！全代码库**零写入**
    ai_decisions.event_atom_ids 2253/4574 非空  ← 只有前半段（atom→news 方向）

`ai_worker` 在批处理里写 event_atom（ai_worker.py:10628-10704），随后把本次产生的
atom id 列表写进 `ai_decisions.event_atom_ids`（10706+）—— 但**从来没有**把
`(atom_id, decision_id)` 写进 `event_atom_decisions`。

后果：事件原子（"这件事是什么、什么方向、多确定"）与决策（"因此买/卖/观望"）
之间**没有可查询的连接** → 归因页/复盘无法回溯"这条决策是基于哪个事件原子做出的"，
因果图谱的自进化闭环也拿不到第一性原理层的证据。

做法
----
    对每条**主行**决策（is_sibling=0，副品种行不重复计）：
      找到 raw_news_id = 该决策 news_id 的事件原子
      写 (event_atom_id, ai_decision_id, weight)
      weight = 原子的 direction_confidence（方向越确定，对决策的贡献越大）

幂等：INSERT OR IGNORE（主键 event_atom_id + ai_decision_id）。

用法：
    python3 scripts/link_event_atom_decisions.py --dry-run
    python3 scripts/link_event_atom_decisions.py
"""
from __future__ import annotations
from pathlib import Path

import argparse
import os
import sqlite3
import sys
import time

BASE_DIR = str(Path(__file__).resolve().parents[2])
DB = os.environ.get("TRIDENT_DB_PATH") or os.path.join(
    BASE_DIR, "trident_event_bus.db")

DDL = """
CREATE TABLE IF NOT EXISTS event_atom_decisions (
    event_atom_id INTEGER NOT NULL,
    ai_decision_id INTEGER NOT NULL,
    weight REAL DEFAULT 1.0,
    created_at TEXT DEFAULT (datetime('now')),
    PRIMARY KEY (event_atom_id, ai_decision_id)
);
CREATE INDEX IF NOT EXISTS ix_event_atom_decisions_decision
    ON event_atom_decisions(ai_decision_id);
"""


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default=DB)
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--days", type=int, default=180,
                    help="只链接最近 N 天的决策（0 = 全部）")
    args = ap.parse_args()

    if not os.path.exists(args.db):
        print("DB 不存在: %s" % args.db)
        return 2
    conn = sqlite3.connect(args.db, timeout=60)
    conn.row_factory = sqlite3.Row
    try:
        conn.execute("PRAGMA busy_timeout=60000;")
        conn.executescript(DDL)

        where = "d.is_sibling = 0 AND a.raw_news_id = d.news_id"
        params: list = []
        if args.days > 0:
            # ai_decisions.created_at 是 ISO（T 分隔），直接与 datetime() 比会恒真 → 归一化后比
            where += " AND substr(replace(d.created_at,'T',' '),1,19) >= datetime('now','-%d days')" % args.days
            params = []

        pairs = conn.execute(
            """SELECT a.id AS atom_id, d.id AS decision_id,
                      a.direction_confidence AS conf
               FROM ai_decisions d
               JOIN event_atom a ON a.raw_news_id = d.news_id
               WHERE """ + where, params).fetchall()

        before = conn.execute(
            "SELECT COUNT(*) FROM event_atom_decisions").fetchone()[0]
        print("[INFO] 待链接的 (事件原子, 决策) 配对: %d" % len(pairs))
        print("[INFO] 现有 event_atom_decisions: %d 行" % before)
        if args.dry_run:
            print("\n(dry-run) 未做任何修改")
            return 0
        if not pairs:
            print("✅ 无需链接")
            return 0

        t0 = time.time()
        rows = []
        for p in pairs:
            # 方向置信度作为贡献权重；缺失/0（方向未知）时回落到 1.0
            try:
                w = float(p["conf"])
            except (TypeError, ValueError):
                w = 1.0
            if not (0.0 < w <= 1.0):
                w = 1.0
            rows.append((int(p["atom_id"]), int(p["decision_id"]), w))
        conn.executemany(
            "INSERT OR IGNORE INTO event_atom_decisions "
            "(event_atom_id, ai_decision_id, weight) VALUES (?,?,?)", rows)
        conn.commit()

        after = conn.execute(
            "SELECT COUNT(*) FROM event_atom_decisions").fetchone()[0]
        cov_dec = conn.execute(
            "SELECT COUNT(DISTINCT ai_decision_id) FROM event_atom_decisions").fetchone()[0]
        cov_atom = conn.execute(
            "SELECT COUNT(DISTINCT event_atom_id) FROM event_atom_decisions").fetchone()[0]
        tot_dec = conn.execute(
            "SELECT COUNT(*) FROM ai_decisions WHERE is_sibling=0").fetchone()[0]
        tot_atom = conn.execute(
            "SELECT COUNT(*) FROM event_atom WHERE raw_news_id IS NOT NULL").fetchone()[0]
        print("[OK] 新增 %d 行 (耗时 %.1fs)" % (after - before, time.time() - t0))
        print("[OK] event_atom_decisions 总 %d 行" % after)
        print("[OK] 覆盖决策 %d/%s (%.1f%%) | 覆盖事件原子 %d/%s (%.1f%%)"
              % (cov_dec, tot_dec, cov_dec / max(1, tot_dec) * 100,
                 cov_atom, tot_atom, cov_atom / max(1, tot_atom) * 100))
        return 0
    finally:
        try:
            conn.close()
        except Exception:
            pass


if __name__ == "__main__":
    sys.exit(main())
