#!/usr/bin/env python3
"""回放 decision_space='LLM_UNAVAILABLE' 的新闻事件（默认试运行）。

为什么做：
    2026-11-15 MiniMax 额度到顶后，news_score_worker 按设计**不写假结论**，
    只把事件标成 decision_space='LLM_UNAVAILABLE' 留在 PENDING —— 这是对的。
    但全项目 grep 下来，除了写这个标记的地方，**没有任何代码读它**：
    引擎的队列是 raw_news，而这些事件的 raw_news 早已是 DONE，
    于是它们被永久搁置。实测积压 3,789 条。

做法：
    把候选事件的 raw_news 重新置回 PENDING，让引擎重新吃一遍。
    · 默认 --dry-run，只打印影响面
    · --limit N 限制本轮回放条数（默认 200）—— 额度是按条消耗的，
      3,789 条一次性灌进去会把新恢复的通道再打爆
    · 只动 raw_news.status / processing_at，不碰任何分值字段

用法：
    python3 replay_llm_unavailable.py                 # 试运行
    python3 replay_llm_unavailable.py --limit 200 --apply
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
    ap.add_argument("--limit", type=int, default=200, help="本轮回放条数上限")
    ap.add_argument("--apply", action="store_true", help="真的写库（默认只试运行）")
    ap.add_argument("--max-age-days", type=int, default=7,
                    help="只看这么多天内的事件（更老的新闻已无交易价值）")
    args = ap.parse_args()

    conn = sqlite3.connect(db.get_db_path(), timeout=30, isolation_level=None)
    import time
    cutoff_ts = int(time.time()) - args.max_age_days * 86400

    rows = conn.execute(
        """SELECT n.id, n.news_id, n.status, n.asset, r.status AS raw_status
             FROM news_events n
             LEFT JOIN raw_news r ON r.id = n.news_id
            WHERE n.decision_space='LLM_UNAVAILABLE'
              AND n.ts >= ?
            ORDER BY n.ts DESC
            LIMIT ?""",
        (cutoff_ts, args.limit)).fetchall()

    total = conn.execute(
        "SELECT COUNT(*) FROM news_events WHERE decision_space='LLM_UNAVAILABLE'").fetchone()[0]
    in_window = conn.execute(
        "SELECT COUNT(*) FROM news_events WHERE decision_space='LLM_UNAVAILABLE' "
        "AND ts >= ?", (cutoff_ts,)).fetchone()[0]

    print(f"LLM_UNAVAILABLE 总计 {total} 条 · {args.max_age_days} 天内 {in_window} 条")
    print(f"本轮候选 {len(rows)} 条（limit={args.limit}）")
    raw_pending = sum(1 for r in rows if r[4] == "PENDING")
    raw_done = sum(1 for r in rows if r[4] == "DONE")
    print(f"  其中 raw_news 已 PENDING {raw_pending} 条（引擎会自己吃，无需回放）")
    print(f"  其中 raw_news 已 DONE {raw_done} 条（需要回放，否则永不处理）")

    if not args.apply:
        print("\n(试运行，未写库 — 加 --apply 生效)")
        return 0

    n = 0
    for r in rows:
        if r[4] != "DONE":
            continue
        conn.execute(
            "UPDATE raw_news SET status='PENDING', processing_at=NULL WHERE id=?",
            (r[1],))
        conn.execute(
            "UPDATE news_events SET status='PENDING', decision_space='' WHERE id=?",
            (r[0],))
        n += 1
    print(f"\n✅ 已回放 {n} 条（raw_news → PENDING，news_events 清除 LLM_UNAVAILABLE 标记）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
