#!/usr/bin/env python3
"""把"源新闻已被闸门判为噪音、事件行却还挂着 PENDING"的行收尾（默认试运行）。

为什么做：
    实测有 471 条事件满足「raw_news.status='FILTERED'（闸门判为噪音，不需要 LLM）
    + news_events.status='PENDING'」。引擎的队列是 raw_news，源既然已经 FILTERED，
    这条事件永远不会再被处理 —— 但它一直挂在 PENDING 里，被验收 C4 算成"真积压"。
    这个数字既不反映队列堵塞，也不反映 LLM 故障，只是状态没同步。

口径：
    · 只把 news_events.status 从 PENDING 改成 FILTERED，decision_space 标 NOISE_FILTERED
    · 不动分值、不动 asset、不删行
    · 只处理 raw_news 明确是 FILTERED 的行；PENDING / PROCESSING / DONE 一律不碰

用法：
    python3 sync_filtered_events.py             # 试运行
    python3 sync_filtered_events.py --apply
"""
from __future__ import annotations

import argparse
import sqlite3
import sys

import sys as _sys
from pathlib import Path as _Path
_sys.path.insert(0, str(_Path(__file__).resolve().parents[2] / "src_python"))
import db


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true", help="真的写库（默认只试运行）")
    args = ap.parse_args()

    conn = sqlite3.connect(db.get_db_path(), timeout=30, isolation_level=None)
    rows = conn.execute(
        """SELECT n.asset, COUNT(*)
             FROM news_events n JOIN raw_news r ON r.id = n.news_id
            WHERE n.status='PENDING' AND r.status='FILTERED'
            GROUP BY n.asset ORDER BY COUNT(*) DESC LIMIT 8""").fetchall()
    total = conn.execute(
        """SELECT COUNT(*) FROM news_events n JOIN raw_news r ON r.id = n.news_id
            WHERE n.status='PENDING' AND r.status='FILTERED'""").fetchone()[0]
    print(f"源已 FILTERED、事件仍 PENDING：{total} 条")
    for a, n in rows:
        print(f"  {str(a):<12}{n}")
    if not args.apply:
        print("\n(试运行，未写库 — 加 --apply 生效)")
        return 0

    n = conn.execute(
        """UPDATE news_events SET status='FILTERED', decision_space='NOISE_FILTERED'
            WHERE status='PENDING'
              AND id IN (SELECT n2.id FROM news_events n2
                          JOIN raw_news r ON r.id = n2.news_id
                         WHERE r.status='FILTERED')""").rowcount
    print(f"\n✅ 已收尾 {n} 条（PENDING → FILTERED / NOISE_FILTERED，行保留）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
