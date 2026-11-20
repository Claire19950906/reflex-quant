#!/usr/bin/env python3
"""v399：把「历史占位置信度 0」标成 NULL（一次性、幂等、可预演）。

问题
----
v399 起，降级路径的 `evidence_confidence` 不再写占位的 `0.0`，改写 `NULL`
（0 是"算过且结论为 0"，NULL 是"没有证据可算"，见 `confidence_availability.py`）。
但**历史行**里仍然是 0.0，漏斗日报会把它们统计成"真 0 分决策"，
于是「92% 的决策置信度为 0」这个错误结论会一直留在报表里。

口径（只动这一类，宁少不多）
--------------------------
只改同时满足以下三点的行：

  1. `evidence_confidence = 0`
  2. `analysis_degraded = 1`
  3. `analysis_degraded_reason IN ('target_none','llm_empty','summary_only')`

这三条意味着：决策走了明确的降级路径，根本没有产出可评分的方向/证据，
0.0 是占位而不是结论。**`server_fallback` 不在其列** —— 那条路径会拼出
真实方向（实测 3 笔成交里有它），可能真的算出 0，不能替它改口径。

不改任何 `> 0` 的行；不改 `analysis_degraded = 0` 的行（那是真分析）。
幂等：重复运行第二次命中 0 条。

用法
----
    python3 backfill_confidence_placeholder.py            # 预演（默认只读）
    python3 backfill_confidence_placeholder.py --commit   # 落库

落库前请先备份（项目惯例 `cp xxx.db xxx.db.bak_<ts>`）；本脚本**不会**替你备份。
"""
from __future__ import annotations

import argparse
import sqlite3
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import db as _db

DEGRADED_REASONS = ("target_none", "llm_empty", "summary_only")

WHERE = (
    "evidence_confidence = 0 "
    "AND COALESCE(analysis_degraded,0) = 1 "
    "AND COALESCE(analysis_degraded_reason,'') IN ('target_none','llm_empty','summary_only')"
)


def _count(conn) -> int:
    row = conn.execute(f"SELECT COUNT(*) FROM ai_decisions WHERE {WHERE}").fetchone()
    return int(row[0] or 0)


def _breakdown(conn):
    return conn.execute(
        "SELECT analysis_degraded_reason, COUNT(*) FROM ai_decisions "
        "WHERE evidence_confidence = 0 AND COALESCE(analysis_degraded,0)=1 "
        "GROUP BY 1 ORDER BY 2 DESC"
    ).fetchall()


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--commit", action="store_true", help="落库（默认只预演）")
    ap.add_argument("--db", default=None, help="覆盖 DB 路径（默认读 config）")
    args = ap.parse_args(argv)

    path = args.db or _db.get_db_path()
    if not Path(path).exists():
        print(f"[FATAL] DB 不存在: {path}")
        return 2
    mode = "ro" if not args.commit else "rw"
    conn = sqlite3.connect(f"file:{path}?mode={mode}", uri=True, timeout=30)
    try:
        targets = _count(conn)
        print(f"库: {path}")
        print(f"待改（0.0 → NULL）: {targets} 条")
        print("\n按降级原因分布（全部 degraded 且 =0 的行）：")
        for reason, n in _breakdown(conn):
            mark = "会改" if (reason or "") in DEGRADED_REASONS else "不动（server_fallback 可能真算出 0）"
            print(f"  {reason or '(空)':<16} {n:>6}  {mark}")

        if not args.commit:
            print("\n(预演，未落库 — 加 --commit 生效)")
            return 0

        before = int(conn.execute(
            "SELECT COUNT(*) FROM ai_decisions WHERE evidence_confidence IS NULL"
        ).fetchone()[0] or 0)
        conn.execute(f"UPDATE ai_decisions SET evidence_confidence = NULL WHERE {WHERE}")
        conn.commit()
        after = int(conn.execute(
            "SELECT COUNT(*) FROM ai_decisions WHERE evidence_confidence IS NULL"
        ).fetchone()[0] or 0)
        left = _count(conn)
        print(f"\n✅ 已更新 {targets} 条；NULL 行 {before} → {after};"
              f" 残留可改行 {left}（应为 0）")
        return 0 if left == 0 else 1
    finally:
        conn.close()


if __name__ == "__main__":
    sys.exit(main())
