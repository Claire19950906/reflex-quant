# -*- coding: utf-8 -*-
"""v342: 给历史决策补 analysis_degraded 标记。

为什么补：289/291 条 HOLD 里绝大多数其实是"降级兜底"，不是"判断为观望"。
不补标记的话，新加的统计口径只对新数据生效，历史仍是混的。
只写标记（analysis_degraded / analysis_degraded_reason），
**不动 reasoning_path、不动 action、不动 score** —— 不修改任何证据。

用法：python backfill_analysis_degraded.py [--dry-run]
"""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src_python"))

import db # noqa: E402
from engine.ai_worker import classify_analysis_degraded  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    conn = db.get_connection()
    try:
        rows = conn.execute(
            "SELECT id, reasoning_path, no_trade_reason, "
            "COALESCE(analysis_degraded,0) FROM ai_decisions"
        ).fetchall()
        changed = 0
        by_reason = {}
        for r in rows:
            flag, why = classify_analysis_degraded(r[1], r[2])
            if flag and int(r[3] or 0) != flag:
                changed += 1
                by_reason[why] = by_reason.get(why, 0) + 1
                if not args.dry_run:
                    conn.execute(
                        "UPDATE ai_decisions SET analysis_degraded=?, analysis_degraded_reason=? WHERE id=?",
                        (flag, why, r[0]),
                    )
        if not args.dry_run:
            conn.commit()
        total = len(rows)
        print(f"总决策 {total} 条 | 需标记 {changed} 条 ({changed/total*100:.1f}%)")
        for k, v in sorted(by_reason.items(), key=lambda x: -x[1]):
            print(f"  {k:<16} {v}")
        print("（dry-run，未写库）" if args.dry_run else "已写库")
        return 0
    finally:
        conn.close()


if __name__ == "__main__":
    raise SystemExit(main())
