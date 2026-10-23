# -*- coding: utf-8 -*-
"""
source_stats_backfill.py — 新闻源权重闭环播种器 (2026-10-22 新增)
==================================================================

问题
----
source_stats 表 0 行 → signal_weight_evaluator 的 source_mult 恒为 1.0。
审计发现这不是代码 bug, 而是**上游数据源断了**:
  - update_source_stats_from_settlement() 只挂在 history_review.retry_pending()
  - 那条路走 quick_sim_trades, 而该表现仅剩 2 行
  - ai_decisions 里 settled=1 的只有 1 条 (527 条 settled=0)
所以闭环的输入端几乎无数据, 表自然是空的。

本模块的作用
------------
不"修复"断掉的上游 (那是另一件事), 而是给闭环补一个**独立的数据入口**:
直接用 decision_engine 已经结算好的 trade_decisions 作为样本, 按
(news_source, asset) 聚合命中率, 播种 source_stats。

安全阀 (与 _source_mult_from_cache 对齐)
----------------------------------------
- 只统计 status='settled' 且 final_action IN ('BUY','SELL') 的样本
- BUY 需 outcome>0, SELL 需 outcome<0 → 与 settle_one 的 hit 定义一致
- 解析不到 raw_news.source 的样本跳过并计数, 绝不臆造来源
- 样本 < 5 的键 source_mult 门槛会自动忽略, 与既有保护一致

用法
----
    python3 source_stats_backfill.py --dry-run
    python3 source_stats_backfill.py
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

BACKEND_DIR = Path(__file__).resolve().parents[1]
DB_PATH = BACKEND_DIR / "trident_event_bus.db"


def _db(db_path: Optional[Path] = None) -> sqlite3.Connection:
    conn = sqlite3.connect(str(db_path or DB_PATH), timeout=60)
    conn.row_factory = sqlite3.Row
    return conn


def collect_samples(conn: sqlite3.Connection) -> Dict[str, Any]:
    """从已结算 trade_decisions 聚合 (source, asset) → 样本统计。

    溯源用两级 fallback (实测 raw_news 在 [10522,10950] 区间已被清空,
    稀疏度仅 29%, 24/25 条已结算决策在 raw_news 里查不到):
      1. raw_news.source          —— 权威来源
      2. news_events.source       —— 归档来源 (需 EXISTS 去重, 同一条新闻
                                      在 news_events 里最多有 9 行, 直接
                                      JOIN 会把样本数放大近一倍)
    """
    rows = conn.execute(
        """
        SELECT src.news_source AS news_source,
               td.asset        AS asset,
               COUNT(*)        AS n,
               SUM(CASE
                     WHEN td.final_action='BUY'  AND td.actual_outcome_pct > 0 THEN 1
                     WHEN td.final_action='SELL' AND td.actual_outcome_pct < 0 THEN 1
                     ELSE 0
                   END) AS win,
               AVG(td.actual_outcome_pct) AS avg_outcome
        FROM trade_decisions td
        JOIN (
            SELECT id AS news_id, source AS news_source
            FROM raw_news
            WHERE source IS NOT NULL AND source <> ''
            UNION ALL
            SELECT ne.news_id, ne.source
            FROM news_events ne
            WHERE ne.news_id IS NOT NULL AND ne.news_id > 0
              AND ne.source IS NOT NULL AND ne.source <> ''
              AND NOT EXISTS (
                    SELECT 1 FROM raw_news rn2
                    WHERE rn2.id = ne.news_id
                      AND rn2.source IS NOT NULL AND rn2.source <> ''
              )
        ) src ON src.news_id = td.news_id
        WHERE td.status = 'settled'
          AND td.final_action IN ('BUY','SELL')
          AND td.actual_outcome_pct IS NOT NULL
          AND td.asset IS NOT NULL AND td.asset <> ''
        GROUP BY src.news_source, td.asset
        """
    ).fetchall()

    agg: Dict[str, Any] = {}
    for r in rows:
        if not r["news_source"]:
            continue
        key = "%s|%s" % (r["news_source"], r["asset"])
        n = int(r["n"] or 0)
        win = int(r["win"] or 0)
        agg[key] = {
            "news_source": r["news_source"],
            "asset": r["asset"],
            "n": n, "win": win, "loss": n - win,
            "hit_rate": (win / n) if n else 0.0,
            "avg_outcome": round(float(r["avg_outcome"] or 0.0), 4),
        }

    unresolved = conn.execute(
        "SELECT COUNT(*) FROM trade_decisions td "
        "WHERE td.status='settled' AND td.final_action IN ('BUY','SELL') "
        "AND NOT EXISTS (SELECT 1 FROM raw_news rn WHERE rn.id=td.news_id "
        "                AND rn.source IS NOT NULL AND rn.source<>'') "
        "AND NOT EXISTS (SELECT 1 FROM news_events ne WHERE ne.news_id=td.news_id "
        "                AND ne.source IS NOT NULL AND ne.source<>'')"
    ).fetchone()[0]

    return {"agg": agg, "unresolved": int(unresolved or 0)}


def apply_backfill(conn: sqlite3.Connection, agg: Dict[str, Any],
                   dry_run: bool = False) -> Dict[str, Any]:
    """把聚合结果写入 source_stats (UPSERT)。"""
    now = int(time.time())
    written = 0
    details: List[Dict[str, Any]] = []

    for key, v in sorted(agg.items(), key=lambda kv: -kv[1]["n"]):
        hit = v["hit_rate"]
        mult = max(0.3, min(1.5, 0.5 + hit))
        if not dry_run:
            conn.execute(
                """
                INSERT INTO source_stats
                    (news_source, asset, sample_n, win_n, loss_n,
                     avg_forward_pnl, hit_rate, source_mult, last_updated)
                VALUES (?,?,?,?,?,?,?,?,?)
                ON CONFLICT(news_source, asset) DO UPDATE SET
                    sample_n=excluded.sample_n,
                    win_n=excluded.win_n,
                    loss_n=excluded.loss_n,
                    avg_forward_pnl=excluded.avg_forward_pnl,
                    hit_rate=excluded.hit_rate,
                    source_mult=excluded.source_mult,
                    last_updated=excluded.last_updated
                """,
                (v["news_source"], v["asset"], v["n"], v["win"], v["loss"],
                 v["avg_outcome"], round(hit, 4), round(mult, 4), now),
            )
        written += 1
        details.append({**v, "source_mult": round(mult, 4),
                        "effective": "yes" if v["n"] >= 5 else "no(n<5 ignored)"})

    if not dry_run:
        conn.commit()
    return {"written": written, "details": details, "dry_run": dry_run}


def run(db_path: Optional[Path] = None, dry_run: bool = False) -> Dict[str, Any]:
    conn = _db(db_path)
    try:
        samples = collect_samples(conn)
        res = apply_backfill(conn, samples["agg"], dry_run=dry_run)
        total = conn.execute("SELECT COUNT(*) FROM source_stats").fetchone()[0]
        return {
            "ts": int(time.time()),
            "dry_run": dry_run,
            "unresolved_settled_decisions": samples["unresolved"],
            "keys_written": res["written"],
            "source_stats_rows_now": total,
            "effective_keys": sum(1 for d in res["details"] if d["n"] >= 5),
            "details": res["details"],
        }
    finally:
        conn.close()


def main() -> int:
    ap = argparse.ArgumentParser(description="新闻源权重闭环播种")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--db", default=None)
    args = ap.parse_args()
    out = run(Path(args.db) if args.db else None, dry_run=args.dry_run)
    print(json.dumps(out, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())