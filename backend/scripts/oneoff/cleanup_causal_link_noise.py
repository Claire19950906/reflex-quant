#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""清理 link_causal_chain_decisions 产生的**噪声关联**。

背景（2026-11-07 实测）
----------------------
link_causal_chain_decisions.py 的 docstring 说它链接"**真实成交决策**"，
但它的 SQL 是：

    FROM trade_decisions WHERE decision_ts >= ? AND news_id IS NOT NULL

**没有过滤 status/final_action** → 而 trade_decisions 3241 条里
**REJECTED 占 3187 条（98.4%）**，真正成交只有 BUY 42 + SELL 7 + CLOSE 5 = 54 条。

实跑一次的后果：
    配对 143444，链接表 2078 → 143635 行
    其中 **141207 条（98.4%）指向被拒的决策** —— 纯噪声

噪声关联会污染下游：
    · causal_chain_outcomes 里多出 2225 条待对账 outcome（未来会带噪声反馈）
    · causal_weight_feedback 用这些结果改权重 → **拿噪声修权重**

本脚本做**可回滚**的清理：
    1. 删链接表里指向 REJECTED 决策的行
    2. 删/解绑 由噪声关联产生、且尚未对账（PENDING）的 outcome
    3. 保留所有已对账的历史（HIT/MISS/NEUTRAL/HIT_WEAK/EXPIRED）—— 它们是历史事实

用法：
    python3 scripts/cleanup_causal_link_noise.py --dry-run
    python3 scripts/cleanup_causal_link_noise.py
"""
from __future__ import annotations
from pathlib import Path

import argparse
import json
import os
import sqlite3
import sys
import time

BASE_DIR = str(Path(__file__).resolve().parents[2])
DB = os.environ.get("TRIDENT_DB_PATH") or os.path.join(
    BASE_DIR, "trident_event_bus.db")

#: 只有这些动作才算"真实成交决策"
REAL_ACTIONS = ("BUY", "SELL", "CLOSE")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    if not os.path.exists(DB):
        print("DB 不存在: %s" % DB)
        return 2
    conn = sqlite3.connect(DB, timeout=60)
    conn.row_factory = sqlite3.Row
    try:
        conn.execute("PRAGMA busy_timeout=60000;")

        total_link = conn.execute(
            "SELECT COUNT(*) FROM causal_chain_outcome_decisions").fetchone()[0]
        bad_link = conn.execute(
            """SELECT COUNT(*) FROM causal_chain_outcome_decisions l
               JOIN trade_decisions d ON d.id = l.decision_id
               WHERE d.final_action = 'REJECTED'""").fetchone()[0]
        # 噪声 outcome：指向 REJECTED 决策、且还没对账
        bad_pending = conn.execute(
            """SELECT COUNT(*) FROM causal_chain_outcomes o
               JOIN trade_decisions d ON d.id = o.decision_id
               WHERE d.final_action = 'REJECTED' AND o.status = 'PENDING'""").fetchone()[0]
        good_link = conn.execute(
            """SELECT COUNT(*) FROM causal_chain_outcome_decisions l
               JOIN trade_decisions d ON d.id = l.decision_id
               WHERE d.final_action IN ('BUY','SELL','CLOSE')""").fetchone()[0]

        print("链接表总行      : %d" % total_link)
        print("  指向 REJECTED : %d  ← 噪声，删" % bad_link)
        print("  指向真实成交  : %d  ← 保留" % good_link)
        print("待对账 PENDING 且来自 REJECTED : %d  ← 删" % bad_pending)
        if args.dry_run:
            print("\n(dry-run) 未做任何修改")
            return 0
        if bad_link == 0 and bad_pending == 0:
            print("\n✅ 无需清理")
            return 0

        # 备份被删的 id（可回滚）
        ids = [r[0] for r in conn.execute(
            """SELECT l.id FROM causal_chain_outcome_decisions l
               JOIN trade_decisions d ON d.id = l.decision_id
               WHERE d.final_action = 'REJECTED'""")]
        out_ids = [r[0] for r in conn.execute(
            """SELECT o.id FROM causal_chain_outcomes o
               JOIN trade_decisions d ON d.id = o.decision_id
               WHERE d.final_action = 'REJECTED' AND o.status = 'PENDING'""")]
        bak = os.path.join(BASE_DIR, "runtime",
                            f"causal_link_noise_backup_{int(time.time())}.json")
        os.makedirs(os.path.dirname(bak), exist_ok=True)
        with open(bak, "w", encoding="utf-8") as fh:
            json.dump({"link_ids": ids, "outcome_ids": out_ids,
                       "ts": time.time()}, fh)
        print("\n备份(含可回滚 id): %s" % bak)

        conn.execute("BEGIN IMMEDIATE")
        try:
            if ids:
                conn.executemany("DELETE FROM causal_chain_outcome_decisions WHERE id=?",
                                 [(i,) for i in ids])
            if out_ids:
                conn.executemany("DELETE FROM causal_chain_outcomes WHERE id=?",
                                 [(i,) for i in out_ids])
            # 残留的指向 REJECTED 的 outcome（已对账的历史不动，只解绑 decision_id）
            conn.execute(
                """UPDATE causal_chain_outcomes SET decision_id=NULL
                   WHERE decision_id IS NOT NULL AND status <> 'PENDING'
                     AND decision_id IN (SELECT id FROM trade_decisions
                                         WHERE final_action='REJECTED')""")
            conn.commit()
        except Exception:
            conn.rollback()
            raise

        after = conn.execute(
            "SELECT COUNT(*) FROM causal_chain_outcome_decisions").fetchone()[0]
        after_good = conn.execute(
            """SELECT COUNT(*) FROM causal_chain_outcome_decisions l
               JOIN trade_decisions d ON d.id = l.decision_id
               WHERE d.final_action IN ('BUY','SELL','CLOSE')""").fetchone()[0]
        print("\n清理后链接表: %d (其中指向真实成交 %d)" % (after, after_good))
        return 0
    finally:
        try:
            conn.close()
        except Exception:
            pass


if __name__ == "__main__":
    sys.exit(main())
