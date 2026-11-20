#!/usr/bin/env python3
"""把"没有 raw_news 源、且发生在纪元之前"的 news_events 标记为历史遗留（默认试运行）。

为什么做：
    实测 news_events 里有 10,399 条记录的 news_id 在 raw_news 里根本不存在
    （journal 类源的 13 位合成 id，从来没有落过 raw_news）。其中 246 条仍是
    PENDING，被验收 C4 算进"真积压"；另外 551 条被算进"孤儿"。
    引擎的队列是 raw_news，这些事件永远不会被处理 —— 它们只会让"还有多少没做完"
    这个数字长期虚高，掩盖真正需要补的东西。

口径：
    · 只动 ts < 数据纪元 的行（纪元之后的一条都没有，实测 pre=10399 / post=0）
    · 只改 status / decision_space，不删行、不碰分值、不碰时间
    · 标记值 decision_space='ORPHAN_PRE_EPOCH'，可随时按它筛回原样

用法：
    python3 mark_orphan_events.py             # 试运行
    python3 mark_orphan_events.py --apply
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
    epoch = int(conn.execute(
        "SELECT strftime('%s', started_at) FROM data_epoch ORDER BY rowid DESC LIMIT 1"
    ).fetchone()[0])

    rows = conn.execute(
        """SELECT n.status, COUNT(*)
             FROM news_events n
             LEFT JOIN raw_news r ON r.id = n.news_id
            WHERE r.id IS NULL AND n.ts < ?
            GROUP BY n.status""", (epoch,)).fetchall()
    total = sum(r[1] for r in rows)
    print(f"纪元 {epoch} 之前、且无 raw_news 源的事件：{total} 条")
    for st, n in rows:
        print(f"  {st:<10}{n}")
    print(f"其中 PENDING {sum(n for st, n in rows if st == 'PENDING')} 条会被 C4 算成真积压")

    if not args.apply:
        print("\n(试运行，未写库 — 加 --apply 生效)")
        return 0

    n = conn.execute(
        """UPDATE news_events SET status='FILTERED', decision_space='ORPHAN_PRE_EPOCH'
            WHERE ts < ?
              AND id IN (SELECT n2.id FROM news_events n2
                          LEFT JOIN raw_news r ON r.id = n2.news_id
                         WHERE r.id IS NULL)""", (epoch,)).rowcount
    print(f"\n✅ 已标记 {n} 条为 ORPHAN_PRE_EPOCH（行保留，可随时筛回）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
