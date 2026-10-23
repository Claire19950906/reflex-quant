#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
migrate_remove_other.py
=======================

**一次性**迁移脚本：清理 ai_decisions 表里所有 market_category='OTHER' 的存量脏数据。

执行逻辑：
  1. 遍历 ai_decisions WHERE market_category='OTHER'
  2. 用 market_classifier.TARGET_TO_MARKET 反推：
     - target_asset 有合法映射 → UPDATE market_category 为映射值
     - target_asset 无映射 / NONE → 写到 unclassified_events 表（人工/规则补全队列）
  3. 对每个处理结果都打印一行（id + 旧 cat → 新 cat 或 queued）

幂等：可重复执行（单行 UPDATE，不会重复加表）。

安全：
  - 不会删任何行
  - 不会触发 LLM（纯 DB 工作）
  - 每 100 行一个事务；失败自动回滚

运行：
  cd backend/src_python
  python3 migrate_remove_other.py [--dry-run] [--limit N]
"""
from __future__ import annotations

import argparse
import sqlite3
import sys
from datetime import datetime

import db  # noqa: E402
from market_classifier import TARGET_TO_MARKET  # noqa: E402


def _table_exists(conn: sqlite3.Connection, name: str) -> bool:
    cur = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
        (name,),
    ).fetchone()
    return cur is not None


def _process_row(
    conn: sqlite3.Connection,
    row_id: int,
    target_asset: str,
    news_id: int,
    reasoning_snippet: str,
    dry_run: bool,
) -> str:
    """处理单行：返回 (FIXED|QUEUED|SKIP) + 详情。"""
    asset = (target_asset or "").strip().upper()
    if asset and asset in TARGET_TO_MARKET:
        new_cat = TARGET_TO_MARKET[asset]
        if not dry_run:
            conn.execute(
                "UPDATE ai_decisions SET market_category=? WHERE id=?",
                (new_cat, row_id),
            )
        return f"FIXED {row_id}: {asset} -> {new_cat}"
    if not _table_exists(conn, "unclassified_events"):
        print(f"  [WARN] unclassified_events 表不存在，跳过 row {row_id}", flush=True)
        return f"SKIP {row_id}: no unclassified_events table"
    if not dry_run:
        try:
            conn.execute(
                """
                INSERT INTO unclassified_events (
                    created_at, news_id, title, source, published_at,
                    target_asset, llm_market_category, llm_target_asset,
                    reason, layer, status
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending')
                """,
                (
                    datetime.utcnow().isoformat(timespec="seconds") + "Z",
                    int(news_id or 0),
                    reasoning_snippet[:500],
                    "",
                    "",
                    asset,
                    "OTHER",
                    asset,
                    f"NO_MAPPING_FOR_ASSET:{asset}" if asset else "MISSING_TARGET_ASSET",
                    "migration",
                ),
            )
        except sqlite3.IntegrityError as e:
            return f"SKIP {row_id}: duplicate ({e})"
    if not dry_run:
        conn.execute(
            "UPDATE ai_decisions SET market_category=? WHERE id=?",
            ("", row_id),
        )
    return f"QUEUED {row_id}: asset={asset!r}"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true", help="只打印不修改")
    parser.add_argument("--limit", type=int, default=0, help="最多处理 N 行（默认全部）")
    args = parser.parse_args()
    dry_run = args.dry_run
    limit = args.limit

    conn = db.get_connection()
    try:
        # 让 fetchall() 返回 Row 对象（支持 r["key"] 索引）
        conn.row_factory = sqlite3.Row
        if not _table_exists(conn, "unclassified_events"):
            print("  [INFO] unclassified_events 表不存在，先 db.migrate(conn) 创建...", flush=True)
            db.migrate(conn)
            conn.commit()

        rows = conn.execute(
            "SELECT id, news_id, target_asset, reasoning, created_at "
            "FROM ai_decisions WHERE market_category='OTHER'"
            + (" LIMIT " + str(int(limit)) if limit > 0 else ""),
        ).fetchall()
        total = len(rows)
        print(f"Total 'OTHER' rows: {total}  ({'DRY-RUN' if dry_run else 'WRITE'})", flush=True)

        fixed = 0
        queued = 0
        skipped = 0
        BATCH = 100
        try:
            for i, row in enumerate(rows):
                msg = _process_row(
                    conn,
                    int(row["id"]),
                    row["target_asset"] or "",
                    int(row["news_id"]) if row["news_id"] else 0,
                    (row["reasoning"] or "")[:200],
                    dry_run,
                )
                if msg.startswith("FIXED"):
                    fixed += 1
                elif msg.startswith("QUEUED"):
                    queued += 1
                else:
                    skipped += 1
                if (i + 1) % BATCH == 0:
                    if not dry_run:
                        conn.commit()
                    print(
                        f"  [{i+1}/{total}] fixed={fixed} queued={queued} skipped={skipped}",
                        flush=True,
                    )
            if not dry_run:
                conn.commit()
        except Exception as e:
            conn.rollback()
            print(f"\n[FATAL] 中途异常已回滚: {type(e).__name__}: {e}", flush=True)
            return 2

        print("", flush=True)
        print("=== DONE ===", flush=True)
        print(f"Fixed by TARGET_TO_MARKET: {fixed}", flush=True)
        print(f"Queued to unclassified_events: {queued}", flush=True)
        print(f"Skipped (idempotent / no table): {skipped}", flush=True)
        return 0
    finally:
        try:
            conn.close()
        except Exception:
            pass


if __name__ == "__main__":
    sys.exit(main())