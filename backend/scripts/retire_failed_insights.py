#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""把「验证已否证」的洞察停用（出池），默认只打印不改库。

为什么要有（2026-11-18 实测）
----------------------------
`review_insights` 93 条里 26 条 `verification_verdict='failed'`（real_signal_pct 0.00~0.02，
按 `decide_verdict` 的语义是「有证据的否定」：交易样本够 / K 线侧 score≤0.3 已否证）。
它们仍然 `active=1`：
  · 每轮 verify_all_insights 都要重新算一遍（浪费）；
  · 还会作为「实战经验规则」注入 AI prompt（否证的规则继续指导交易）；
  · 把 S19 观察率的分母撑大，让「可执行率」这个数字更难看且失真。

本脚本只做一件事：`active=1 AND verification_verdict='failed'` → `active=0`，
并保留 `verification_verdict` / `verification_score` 原值（证据不删，可审计）。

用法：
    python3 scripts/retire_failed_insights.py            # 干跑，只列清单
    python3 scripts/retire_failed_insights.py --commit   # 落库
    python3 scripts/retire_failed_insights.py --min-score 0.05   # 只停用分数低于阈值的
"""
from __future__ import annotations

import argparse
import os
import sqlite3
import sys

_BACKEND = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_DB = os.path.join(_BACKEND, "trident_event_bus.db")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--commit", action="store_true", help="真正写库（默认干跑）")
    ap.add_argument("--limit", type=int, default=0, help="最多处理多少条（0=全部）")
    ap.add_argument("--min-score", type=float, default=None,
                    help="只停用 verification_score 小于该值的（默认不看分数）")
    args = ap.parse_args()

    if not os.path.exists(_DB):
        print(f"库不存在：{_DB}")
        return 2
    conn = sqlite3.connect(_DB)
    conn.row_factory = sqlite3.Row
    try:
        sql = ("SELECT id, asset, verification_score, verification_sample_n, rule"
               " FROM review_insights"
               " WHERE active=1 AND verification_verdict='failed'")
        params: list = []
        if args.min_score is not None:
            sql += " AND coalesce(verification_score, 0) < ?"
            params.append(float(args.min_score))
        sql += " ORDER BY id"
        rows = conn.execute(sql, params).fetchall()
        if args.limit:
            rows = rows[: args.limit]

        print(f"[SCAN] 命中 {len(rows)} 条 active=1 且 failed 的洞察")
        for r in rows:
            print(f"  #{r['id']} score={r['verification_score']} "
                  f"n={r['verification_sample_n']} asset={r['asset'] or '-'} "
                  f"| {str(r['rule'])[:52]}")
        if not rows:
            print("没有需要停用的（幂等：复跑应为 0）")
            return 0
        if not args.commit:
            print(f"\n[DRY-RUN] 未改动。确认后加 --commit 停用这 {len(rows)} 条。")
            return 0

        n = 0
        for r in rows:
            cur = conn.execute(
                "UPDATE review_insights SET active=0 WHERE id=? AND active=1",
                (int(r["id"]),),
            )
            n += 1 if cur.rowcount else 0
        conn.commit()
        print(f"\n[COMMIT] 已停用 {n} 条（active=0；verification_verdict 原值保留可审计）")
        left = conn.execute(
            "SELECT COUNT(*) FROM review_insights WHERE active=1"
        ).fetchone()[0]
        print(f"[POOL] 剩余 active={left}")
        return 0
    finally:
        conn.close()


if __name__ == "__main__":
    sys.exit(main())
