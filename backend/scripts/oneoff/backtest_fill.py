#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""v176 P3-16-C backtest_fill.py — 历史已结算决策回填到 backtest_event_outcomes。

逻辑：
  从 ai_decisions WHERE settled=1 AND event_type_code IS NOT NULL AND target_asset NOT NULL
  聚合 (event_type_code, target_asset) 维度，写入 backtest_event_outcomes（sample_n / win_n /
  loss_n / win_rate_1h / win_rate_4h / win_rate_24h / avg_pnl_*h）。

用途：
  - baseline.by_type 之前只有 6 条样本 → LLM 看不到真实历史胜率，盲猜方向。
  - 回填后历史样本将涨到几百条，让 baseline block 真正可参考。

执行：
  cd backend && python3 backtest_fill.py [--dry-run]
"""
import argparse
import os
import sqlite3
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src_python"))

import db


def _upsert_event_outcomes(
    conn: sqlite3.Connection,
    type_code: str,
    asset: str,
    *,
    sample_n: int,
    win_n: int,
    loss_n: int,
    avg_pnl_1h: float | None,
    avg_pnl_4h: float | None,
    avg_pnl_24h: float | None,
    sample_1h: int,
    sample_24h: int,
    win_rate_1h: float | None,
    win_rate_4h: float | None,
    win_rate_24h: float | None,
    dry_run: bool,
) -> None:
    """upsert into backtest_event_outcomes。"""
    if dry_run:
        return
    now = int(time.time())
    cur = conn.execute(
        "SELECT id FROM backtest_event_outcomes WHERE type_code=? AND asset=?",
        (type_code, asset),
    ).fetchone()
    if cur:
        conn.execute(
            """UPDATE backtest_event_outcomes
               SET sample_n=?, win_n=?, loss_n=?, avg_pnl_1h=?, avg_pnl_4h=?, avg_pnl_24h=?,
                   sample_1h=?, sample_24h=?, win_rate_1h=?, win_rate_4h=?, win_rate_24h=?,
                   last_updated=?
               WHERE id=?""",
            (
                sample_n, win_n, loss_n, avg_pnl_1h, avg_pnl_4h, avg_pnl_24h,
                sample_1h, sample_24h, win_rate_1h, win_rate_4h, win_rate_24h,
                now, cur[0],
            ),
        )
    else:
        conn.execute(
            """INSERT INTO backtest_event_outcomes
               (type_code, asset, sample_n, win_n, loss_n,
                avg_pnl_1h, avg_pnl_4h, avg_pnl_24h,
                sample_1h, sample_24h,
                win_rate_1h, win_rate_4h, win_rate_24h, last_updated)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                type_code, asset, sample_n, win_n, loss_n,
                avg_pnl_1h, avg_pnl_4h, avg_pnl_24h,
                sample_1h, sample_24h,
                win_rate_1h, win_rate_4h, win_rate_24h, now,
            ),
        )


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true", help="只统计不写库")
    args = parser.parse_args()

    conn = db.get_connection()
    # backtest_fill 只读 — engine/api 持锁时短暂重试即可，避免无限等
    try:
        db.migrate(conn)
    except sqlite3.OperationalError as e:
        if "locked" in str(e):
            print("[migrate] db 持锁，跳过 migrate（schema 应已存在）；仅做读+upsert")
        else:
            raise
    cur = conn.cursor()

    rows = cur.execute(
        """SELECT event_type_code,
                   target_asset,
                   UPPER(COALESCE(is_correct, '')) AS outcome,
                   forward_pnl
            FROM ai_decisions
            WHERE event_type_code IS NOT NULL
              AND event_type_code != ''
              AND target_asset IS NOT NULL
              AND target_asset != ''
              AND target_asset != 'NONE'
        """
    ).fetchall()

    print(f"找到 {len(rows)} 条已结算历史决策")

    bucket = {}
    for r in rows:
        type_code = str(r[0] or "").strip()
        asset = str(r[1] or "").strip().upper()
        outcome = str(r[2] or "").upper()
        pnl = r[3]
        key = (type_code, asset)
        if key not in bucket:
            bucket[key] = {
                "sample_n": 0, "win_n": 0, "loss_n": 0,
                "pnl_list": [],
            }
        b = bucket[key]
        b["sample_n"] += 1
        if outcome == "WIN":
            b["win_n"] += 1
        elif outcome == "LOSS":
            b["loss_n"] += 1
        if pnl is not None:
            try:
                b["pnl_list"].append(float(pnl))
            except Exception:
                pass

    written = 0
    for (type_code, asset), b in bucket.items():
        pnl_list = b["pnl_list"]
        avg_pnl = sum(pnl_list) / len(pnl_list) if pnl_list else 0.0
        # 写库：avg_pnl_4h 字段用同一 pnl 平均值（向后兼容 schema）。None → 0.0 防 NOT NULL
        _upsert_event_outcomes(
            conn, type_code, asset,
            sample_n=b["sample_n"], win_n=b["win_n"], loss_n=b["loss_n"],
            avg_pnl_1h=0.0,
            avg_pnl_4h=float(avg_pnl) if avg_pnl is not None else 0.0,
            avg_pnl_24h=0.0,
            sample_1h=0, sample_24h=0,
            win_rate_1h=0.0,
            win_rate_4h=(b["win_n"] / b["sample_n"]) if b["sample_n"] else 0.0,
            win_rate_24h=0.0,
            dry_run=args.dry_run,
        )
        written += 1

    if not args.dry_run:
        conn.commit()
        print(f"已写入 {written} 条 (event_type_code, target_asset) 维度聚合")
    else:
        print(f"[DRY-RUN] 将写入 {written} 条")
        for (tc, a), b in sorted(bucket.items())[:10]:
            print(f"  {tc:30s} | {a:6s} | n={b['sample_n']:3d} win={b['win_n']:2d} loss={b['loss_n']:2d}")


if __name__ == "__main__":
    main()