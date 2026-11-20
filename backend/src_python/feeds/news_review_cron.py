#!/usr/bin/env python3
"""新闻交易归因定时生成 worker（v311）

背景
----
`news_trade_reviews` 表长期是空的 —— AI 归因(summary)只能靠人手点一次 POST
触发, 于是「新闻 → 决策 → 交易 → 归因」的闭环在自动化上是断的: 交易结算完了,
但没有归因结论, 前端复盘页看不到任何「为什么赚/为什么亏」的分析。

这里把它变成定时任务:
  1. 扫出「有已结算模拟交易、但还没有归因记录」的新闻 news_id;
  2. 逐条调用 history_review.run_news_review 生成 AI 归因并落库;
  3. 落库后天然幂等(表里有 news_id 就跳过), 失败不写缓存, 下轮重试。

限速: 每轮最多 --limit 条(默认 2), 不让 LLM 通道被占满, 也不让一次扫描跑太久。

用法:
    python3 news_review_cron.py --dry-run   # 只看有哪些待归因, 不调 LLM
    python3 news_review_cron.py --limit 2   # 实际生成
"""
from __future__ import annotations
from ._paths import SRC_DIR, BACKEND_DIR  # P2: 路径锚点

import argparse
import json
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(SRC_DIR))

import config


def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(config.DB_PATH, timeout=60.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout=30000;")
    return conn


def pending_news_ids(limit: int) -> list[int]:
    """有已结算交易、但 news_trade_reviews 里还没有归因的新闻 id。

    优先处理交易笔数多的(信息量最大)。news_id 必须是 raw_news 里真实存在的 id,
    否则 run_news_review 找不到原文上下文。

    ⚠ 必须限定 q.settled = 1: 归因是「事后复盘」, 对还没结算的交易做归因等于
    拿未发生的行情下结论(入场价/出场价/盈亏都还是空的)。当前库里带 news_id 的
    交易恰好都已结算, 但查询本身不能依赖这个巧合 —— 未结算交易随时会产生。
    """
    conn = _connect()
    try:
        rows = conn.execute(
            """
            SELECT q.news_id AS news_id, COUNT(*) AS n
            FROM quick_sim_trades q
            JOIN raw_news rn ON rn.id = q.news_id
            LEFT JOIN news_trade_reviews r ON r.news_id = q.news_id
            WHERE q.news_id IS NOT NULL AND q.news_id > 0
              AND q.settled = 1
              AND r.news_id IS NULL
            GROUP BY q.news_id
            ORDER BY n DESC, q.news_id DESC
            LIMIT ?
            """,
            (limit,),
        ).fetchall()
        return [int(r["news_id"]) for r in rows]
    finally:
        conn.close()


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=2, help="每轮最多归因几条")
    ap.add_argument("--dry-run", action="store_true", help="只列待归因, 不调用 LLM")
    args = ap.parse_args()

    ids = pending_news_ids(max(1, args.limit))
    print(json.dumps({"ok": True, "pending": ids, "dry_run": args.dry_run},
                     ensure_ascii=False), flush=True)
    if args.dry_run or not ids:
        return 0

    import history_review # 延迟导入: 避免模块级副作用影响 --dry-run

    done: list[int] = []
    failed: list[dict] = []
    for nid in ids:
        try:
            res = history_review.run_news_review(nid)
            if res.get("ok"):
                done.append(nid)
            else:
                failed.append({"news_id": nid, "error": res.get("error")})
        except Exception as exc:  # 单条失败不影响其它
            failed.append({"news_id": nid, "error": str(exc)})

    print(json.dumps({"ok": True, "done": done, "failed": failed},
                     ensure_ascii=False), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
