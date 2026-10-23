#!/usr/bin/env python3
"""backfill_three_state.py — Q3 P0: 把空 action 一次性填回 HOLD

news_score_worker 现在能正确扫描 action='' 的行, 但生产环境历史积压的 58 条
(techflow/eastmoney 源, score=0 → 无 bull/bear 关键词) 需要一次性 backfill.

用法:
  python3 backfill_three_state.py            # dry-run, 显示会改的 id
  python3 backfill_three_state.py --commit   # 实际执行 UPDATE
"""
import argparse
import sqlite3
import os
import sys
from pathlib import Path

DB_PATH = Path(os.getenv("TRIDENT_DB_PATH") or (
    Path(__file__).resolve().parent.parent / "trident_event_bus.db"
))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--commit", action="store_true",
                    help="实际写回 DB (默认 dry-run)")
    args = ap.parse_args()

    if not DB_PATH.exists():
        print(f"DB not found: {DB_PATH}", file=sys.stderr)
        sys.exit(1)

    conn = sqlite3.connect(str(DB_PATH))
    cur = conn.cursor()

    # 1) 找出所有待修复行
    pending = cur.execute("""
        SELECT id, source, score,
              UPPER(IFNULL(action, '')) AS raw_action,
              substr(headline, 1, 50) AS h
        FROM news_events
        WHERE action IS NULL OR action = ''
        ORDER BY ts DESC
    """).fetchall()

    if not pending:
        print("[OK] 没有待修复行 (Q3 已 clean)")
        return 0

    print(f"[INFO] {len(pending)} 行 action 为空, 准备填为 HOLD")
    for r in pending[:10]:
        print(f"  id={r[0]:<6} src={r[1]:<12} score={r[2]:<6} h={r[3]}")
    if len(pending) > 10:
        print(f"  ... 还有 {len(pending)-10} 行")

    if not args.commit:
        print("\n[DRY-RUN] 加 --commit 实际执行")
        return 0

    # 2) 三态派生 (与 worker 一致): |score|>=0.3 → BUY/SELL, 否则 HOLD
    # 注: 这 58 行 score 都 = 0, 全部映射到 HOLD
    for r in pending:
        new_action = "HOLD"  # 三态语义: 无信号 = HOLD
        new_score = r[2] or 0.0
        cur.execute(
            "UPDATE news_events SET action=?, score=? WHERE id=?",
            (new_action, new_score, r[0]),
        )

    conn.commit()

    # 3) 验证
    after = cur.execute("""
        SELECT
          SUM(CASE WHEN UPPER(IFNULL(action,''))='BUY'  THEN 1 ELSE 0 END) buy,
          SUM(CASE WHEN UPPER(IFNULL(action,''))='SELL' THEN 1 ELSE 0 END) sell,
          SUM(CASE WHEN UPPER(IFNULL(action,''))='HOLD' THEN 1 ELSE 0 END) hold,
          SUM(CASE WHEN IFNULL(action,'')='' THEN 1 ELSE 0 END) empty,
          SUM(CASE WHEN action NOT IN ('BUY','SELL','HOLD','') AND action IS NOT NULL THEN 1 ELSE 0 END) polluted
        FROM news_events
    """).fetchone()
    total = sum(c or 0 for c in after)
    pct_valid = round(100 * (after[0]+after[1]+after[2]) / max(total, 1), 1)
    print(f"\n[AFTER] BUY={after[0]} SELL={after[1]} HOLD={after[2]} empty={after[3]} polluted={after[4]}")
    print(f"[OK] 三态覆盖率: {pct_valid}% (期望 100%)")
    conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())