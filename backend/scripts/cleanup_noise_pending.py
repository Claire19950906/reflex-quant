#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""清理「永远不会被分析」的僵尸待分析行。

两种僵尸（2026-11-05 实测 PENDING 4850 条里 100% 属于这两类）：

1. **超龄行**：超过 `NEWS_ANALYZE_MAX_AGE_HOURS`（默认 6 小时）。
   它们过了建仓时效（45 分钟）→ 信号必然被丢弃；也过了认领门槛 → 不会被分析。
   留在 PENDING 只会**占着「待分析」计数**，让人误以为还有 2500 条在排队。

2. **门卫误杀的噪音行**：`is_noise=1` 但其实是正常新闻。
   根因是门卫把「模型没回答字段」当成「不是噪音」（见 OPERATIONS_v322.md 10.18），
   这些行永远不会被认领（认领 SQL 要求 `is_noise=0`），却一直挂在计数里。
   实测 2340 条（占 61%），抽样全是波兰央行/IIF 数据/美联储讲话这类正常新闻。

本脚本把这两类行标为 `FILTERED`：
  · **不是丢数据** —— 原始行还在库里，可查、可回放；
  · 只是不再占「待分析」计数、不再被送去分析（省额度）。

用法：
    python3 scripts/cleanup_noise_pending.py --dry-run   # 只看，不改
    python3 scripts/cleanup_noise_pending.py             # 执行（自动备份）
    python3 scripts/cleanup_noise_pending.py --limit 500  # 只处理前 N 条
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SRC = os.path.join(BASE_DIR, "src_python")
if SRC not in sys.path:
    sys.path.insert(0, SRC)


def main() -> int:
    ap = argparse.ArgumentParser(
        description="清理永远不会被分析的僵尸待分析行（超龄 + 门卫误杀）")
    ap.add_argument("--dry-run", action="store_true", help="只看会改多少，不执行")
    ap.add_argument("--limit", type=int, default=0, help="最多处理多少条（0=不限）")
    args = ap.parse_args()

    import db

    conn = db.get_connection()
    try:
        conn.execute("PRAGMA busy_timeout=30000;")
        max_age = 0
        try:
            import config
            max_age = int(getattr(config, "NEWS_ANALYZE_MAX_AGE_HOURS", 0) or 0)
        except Exception:
            max_age = 0
        cutoff = int(time.time()) - max_age * 3600 if max_age > 0 else 0

        # 僵尸判定：status=PENDING 且（超龄 或 is_noise=1）且未被门卫冻结
        where = ("FROM raw_news WHERE status='PENDING'"
                 " AND COALESCE(gate_unavailable,0)=0"
                 " AND ((? > 0 AND ts IS NOT NULL AND ts < ?)"
                 "      OR COALESCE(is_noise,0)=1)")
        total = conn.execute(
            "SELECT COUNT(*) " + where, (max_age, cutoff)).fetchone()[0]
        print("僵尸待分析行（超龄 或 门卫误杀）: %d 条" % total)
        if not total:
            print("✅ 无需清理")
            return 0

        # 抽样看看这些行长什么样（确认确实是过期/误杀，而不是误判）
        print("\n抽样 5 条：")
        rows = conn.execute(
            "SELECT id, status, is_noise, gate_unavailable, "
            "substr(coalesce(content, ''), 1, 60) AS txt "
            "FROM raw_news WHERE status='PENDING' "
            "AND COALESCE(gate_unavailable,0)=0 "
            "AND ((? > 0 AND ts IS NOT NULL AND ts < ?) OR COALESCE(is_noise,0)=1) "
            "ORDER BY id DESC LIMIT 5", (max_age, cutoff)).fetchall()
        for r in rows:
            print("  id=%s noise=%s | %s" % (r[0], r[2], (r[4] or "")[:60]))

        if args.dry_run:
            print("\n(dry-run) 未做任何修改")
            return 0

        # 备份被改的 id（可回滚）
        ids = [r[0] for r in conn.execute(
            "SELECT id " + where + " ORDER BY id", (max_age, cutoff)).fetchall()]
        if args.limit > 0:
            ids = ids[:args.limit]
        backup = os.path.join(
            BASE_DIR, "runtime",
            "zombie_pending_backup_%d.json" % int(time.time()))
        os.makedirs(os.path.dirname(backup), exist_ok=True)
        with open(backup, "w", encoding="utf-8") as fh:
            json.dump({"ids": ids, "ts": time.time()}, fh)

        # 分批 UPDATE（每批 500 条，避免长写事务占锁）
        changed = 0
        for i in range(0, len(ids), 500):
            chunk = ids[i:i + 500]
            qs = ",".join("?" * len(chunk))
            cur = conn.execute(
                "UPDATE raw_news SET status='FILTERED', processing_at=NULL, "
                # v408：归档必须留痕。原来这里只改 status，于是这些行在库里
                # 看起来和"门卫判过的正常新闻"没区别，事后谁也说不清是谁、为什么归档的
                # （正是 11-16 那次 742 条黑洞查不出原因的原因之一）。
                "filter_reason=CASE WHEN COALESCE(filter_reason,'')='' "
                "THEN 'zombie_pending_manual' ELSE filter_reason END "
                "WHERE id IN (%s) AND status='PENDING' "
                "AND COALESCE(gate_unavailable,0)=0" % qs,
                chunk)
            changed += cur.rowcount or 0
            conn.commit()
            print("  已处理 %d/%d" % (i + len(chunk), len(ids)))

        print("\n✅ 已把 %d 条僵尸行改标为 FILTERED" % changed)
        print("   备份（含可回滚 id 列表）: %s" % backup)
        print("   「待分析」计数应立即下降。")
        return 0
    finally:
        try:
            conn.close()
        except Exception:
            pass


if __name__ == "__main__":
    sys.exit(main())
