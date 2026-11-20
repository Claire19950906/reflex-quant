#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""backfill_decision_linked_outcomes.py — 让因果链结果挂到真实决策 (G08)。

背景
----
causal_chain_outcomes 只记"哪条链被哪条新闻触发", 508 行里 **0 行**能追到
trade_decisions —— 也就是说: 系统判断一条链对不对, 只看行情涨没涨, 从来不看
"这条链当时到底影响了哪笔真实交易"。闭环改链(需求 #8)的前提就是这条链接。

做法
----
  1. 给已有 outcome 行补 decision_id (按触发新闻反查成交决策);
  2. 为"新闻命中了链 且 该新闻确实成交了"的组合**新建** outcome 行, 并写入
     decision_id —— 这才是链与决策的闭环样本。

    python3 backfill_decision_linked_outcomes.py --days 60
"""
from __future__ import annotations

import argparse
import os
import sqlite3
import sys
from pathlib import Path
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src_python"))

import causal_chain_evaluator as cce  # noqa: E402
from signals import decision_id_space as dis  # noqa: E402

DB_PATH = os.environ.get("TRIDENT_DB_PATH") or os.path.join(
    str(Path(__file__).resolve().parents[2]), "trident_event_bus.db"
)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default=DB_PATH)
    ap.add_argument("--days", type=int, default=60)
    args = ap.parse_args()

    conn = sqlite3.connect(args.db, timeout=60)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    dis.ensure_columns(conn)
    conn.commit()

    idx = dis.DecisionIndex(conn)

    # ── 1) 已有 outcome 行补 decision_id ───────────────────────────────────
    rows = conn.execute(
        """SELECT o.id, o.trigger_ref_id, n.trade_decision_id, n.ai_decision_id
           FROM causal_chain_outcomes o
           LEFT JOIN news_events n ON n.id = o.trigger_ref_id
           WHERE o.decision_id IS NULL"""
    ).fetchall()
    filled = 0
    for r in rows:
        td_id = r["trade_decision_id"]
        if not td_id:
            news_row = conn.execute(
                "SELECT news_id, ts FROM news_events WHERE id=?", (r["trigger_ref_id"],)
            ).fetchone()
            if news_row:
                td_id = idx.resolve_trade(news_row["news_id"], int(news_row["ts"] or 0))
        if td_id:
            conn.execute(
                "UPDATE causal_chain_outcomes SET decision_id=?, decision_space='trade' WHERE id=?",
                (int(td_id), int(r["id"])),
            )
            filled += 1
    conn.commit()
    print("[OK] 已有 outcome 补 decision_id: %d / %d" % (filled, len(rows)))

    # ── 2) 为"链 × 决策"组合新建 outcome 行 ────────────────────────────────
    chains = cce.load_active_chains(conn)
    print("[INFO] 可评估链: %d" % len(chains))
    deadline_days = 5
    created = 0
    seen = set()
    t0 = time.time()
    for chain in chains:
        try:
            triggers = list(cce.find_news_triggers(conn, chain, args.days))
        except Exception as exc:  # noqa: BLE001
            print("[WARN] chain %s 扫描失败: %s" % (chain.chain_id, exc))
            continue
        for ev in triggers:
            news_row = conn.execute(
                "SELECT id, news_id, ts, trade_decision_id FROM news_events WHERE id=?",
                (ev["id"],),
            ).fetchone()
            if not news_row:
                continue
            td_id = news_row["trade_decision_id"]
            if not td_id:
                td_id = idx.resolve_trade(news_row["news_id"], int(news_row["ts"] or 0))
            if not td_id:
                continue  # 这条新闻没有对应成交决策 → 不构成"链→决策"闭环
            key = (chain.chain_id, int(ev["id"]))
            if key in seen:
                continue
            seen.add(key)
            deadline = int(ev["ts"]) + deadline_days * 86400
            cur = conn.execute(
                """INSERT OR IGNORE INTO causal_chain_outcomes
                     (chain_id, trigger_ts, trigger_source, trigger_ref_id,
                      trigger_headline, trigger_keywords_matched,
                      predicted_direction, predicted_mag_lo, predicted_mag_hi,
                      eval_deadline_ts, status, decision_id, decision_space, created_at)
                   VALUES (?, ?, 'news_events', ?, ?, ?, ?, ?, ?, ?, 'PENDING', ?, 'trade', ?)""",
                (chain.chain_id, int(ev["ts"]), int(ev["id"]),
                 (ev["headline"] or "")[:200], ",".join(ev["matched_kws"]),
                 chain.expected_direction, chain.magnitude_lo, chain.magnitude_hi,
                 deadline, int(td_id), int(time.time())),
            )
            if cur.rowcount:
                created += 1
            else:
                conn.execute(
                    "UPDATE causal_chain_outcomes SET decision_id=?, decision_space='trade' "
                    "WHERE chain_id=? AND trigger_ts=? AND trigger_ref_id=?",
                    (int(td_id), chain.chain_id, int(ev["ts"]), int(ev["id"])),
                )
        if created and created % 50 == 0:
            conn.commit()
    conn.commit()

    total = conn.execute("SELECT COUNT(*) FROM causal_chain_outcomes").fetchone()[0]
    linked = conn.execute(
        "SELECT COUNT(*) FROM causal_chain_outcomes WHERE decision_id IS NOT NULL"
    ).fetchone()[0]
    resolvable = conn.execute(
        """SELECT COUNT(*) FROM causal_chain_outcomes o
           JOIN trade_decisions d ON d.id = o.decision_id"""
    ).fetchone()[0]
    print("[OK] 新建 outcome: %d (耗时 %.0fs)" % (created, time.time() - t0))
    print("[OK] outcome 总数 %d, 带决策 %d, 可解析到 trade_decisions %d"
          % (total, linked, resolvable))
    conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
