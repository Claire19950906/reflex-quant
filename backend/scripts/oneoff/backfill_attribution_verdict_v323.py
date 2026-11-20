#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""v323 P0-2 一次性回填: 从 ai_explanation 的中文标签反推 ai_verdict。

背景
----
2026-11-11 的《金融1.0 修改意见清单》P0-2 要求:
  · 主表只允许 ai_verdict ∈ {confirm, partial}（reject 的行要出表 + 留痕）
  · |move_pct|>=5 的行必须 100% 有 ai_verdict

但 ai_verdict 是**这次新增的列**，而库里已有 99 行**早就复核过**的行
(ai_called=1, ai_explanation="[确认 0.95] ..."), 它们只是没有这一列。
好在 verify_one() 写入的说明文本里**判定标签就在开头**:
    "[确认 0.95] ..."     -> confirm
    "[部分成立 0.80] ..." -> partial
    "[不成立 0.94] ..."   -> reject
所以可以**零 LLM 成本**地把历史判定补回来。

本脚本做两件事（幂等，可重复运行）:
  1) 从 ai_explanation 反推 ai_verdict
  2) 把判定为 reject 的行**移出主表**并写入 event_move_attribution_rejected 留痕

用法:
    python3 backend/tools/backfill_attribution_verdict_v323.py [--db PATH] [--dry-run]
"""
from __future__ import annotations

import argparse
import os
import sqlite3
import sys
from pathlib import Path
import time

DEFAULT_DB = os.path.join(
    str(Path(__file__).resolve().parents[2]),
    "trident_event_bus.db",
)

_VERDICT_BY_LABEL = (
    ("[确认", "confirm"),
    ("[部分成立", "partial"),
    ("[不成立", "reject"),
)


def _backfill_verdict(conn: sqlite3.Connection) -> int:
    """从 ai_explanation 反推 ai_verdict。返回更新行数。"""
    total = 0
    for label, verdict in _VERDICT_BY_LABEL:
        cur = conn.execute(
            "UPDATE event_move_attribution SET ai_verdict=? "
            "WHERE ai_verdict IS NULL AND ai_explanation IS NOT NULL "
            "AND ai_explanation LIKE ?",
            (verdict, label + "%"),
        )
        total += cur.rowcount or 0
    return total


def _evict_rejected(conn: sqlite3.Connection) -> int:
    """把 reject 的行搬进 rejected 表并从主表删除。返回搬走行数。"""
    rows = conn.execute(
        "SELECT id, move_id, news_event_id, asset, time_lag_sec, algo_score, algo_reasons, "
        "ai_called, ai_explanation, ai_confidence, ai_verdict, "
        "move_ts, move_pct, move_kind, duration_min, "
        "ev_news_id, ev_source, ev_action, ev_ts "
        "FROM event_move_attribution WHERE ai_verdict='reject'"
    ).fetchall()
    if not rows:
        return 0
    now = int(time.time())
    for r in rows:
        conn.execute(
            "INSERT INTO event_move_attribution_rejected "
            "(orig_row_id,move_id,news_event_id,asset,time_lag_sec,algo_score,algo_reasons,"
            "ai_called,ai_explanation,ai_confidence,ai_verdict,"
            "move_ts,move_pct,move_kind,duration_min,"
            "ev_news_id,ev_source,ev_action,ev_ts,reject_reason,created_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (r[0], r[1], r[2], r[3], r[4], r[5], r[6], r[7], r[8], r[9], r[10],
             r[11], r[12], r[13], r[14], r[15], r[16], r[17], r[18], r[8], now),
        )
        conn.execute("DELETE FROM event_move_attribution WHERE id=?", (r[0],))
    return len(rows)


def main() -> int:
    ap = argparse.ArgumentParser(description="回填 event_move_attribution.ai_verdict")
    ap.add_argument("--db", default=os.environ.get("TRIDENT_DB_PATH") or DEFAULT_DB)
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    if not os.path.exists(args.db):
        print("[FATAL] DB not found: %s" % args.db, file=sys.stderr)
        return 1

    conn = sqlite3.connect(args.db, timeout=60.0)
    conn.execute("PRAGMA busy_timeout=60000;")
    try:
        before = conn.execute(
            "SELECT COUNT(*) FROM event_move_attribution").fetchone()[0]
        print("[INFO] 主表行数(前) = %d" % before)
        print("[INFO] 已有 ai_verdict = %d" % conn.execute(
            "SELECT COUNT(*) FROM event_move_attribution "
            "WHERE ai_verdict IS NOT NULL AND ai_verdict != ''").fetchone()[0])
        print("[INFO] 待反推(有 ai_explanation 无 verdict) = %d" % conn.execute(
            "SELECT COUNT(*) FROM event_move_attribution "
            "WHERE ai_verdict IS NULL AND ai_explanation IS NOT NULL "
            "AND ai_explanation != ''").fetchone()[0])
        if args.dry_run:
            print("[DRY-RUN] 不写库")
            return 0
        n = _backfill_verdict(conn)
        print("[OK] 反推 ai_verdict = %d 行" % n)
        moved = _evict_rejected(conn)
        print("[OK] reject 行出主表 + 留痕 = %d 行" % moved)
        conn.commit()
        print("[OK] 判定分布 = %s" % conn.execute(
            "SELECT coalesce(ai_verdict,'(NULL)'), COUNT(*) FROM event_move_attribution "
            "GROUP BY 1").fetchall())
        print("[OK] rejected 留痕 = %d 行" % conn.execute(
            "SELECT COUNT(*) FROM event_move_attribution_rejected").fetchone()[0])
        return 0
    except sqlite3.Error as exc:
        print("[ERROR] %s: %s" % (type(exc).__name__, exc), file=sys.stderr)
        try:
            conn.rollback()
        except sqlite3.Error:
            pass
        return 2
    finally:
        conn.close()


if __name__ == "__main__":
    sys.exit(main())