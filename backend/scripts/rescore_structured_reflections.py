#!/usr/bin/env python3
"""v497：重算 structured_reflections 的历史判定。

背景：`structured_check._COVERAGE_KEYWORDS` 原来只认族名（geopolitical/earnings），
而库里真实 event_type_code 是细分码（geopolitical_conflict/earnings_release/ipo_listing…），
查不到就退回"匹配英文字面码"→ 中文 reasoning 永远匹配不上 → score_coverage 恒 0 →
verdict 恒 'partial'。飞轮因此收到的全是假阳性。

语法修好后，这里用 `ai_decisions.reasoning` + `ai_decisions.structured_input`
把历史行按新逻辑重算一遍，把"其实没有可校验主张"的行从 pending 里摘出来（置 skipped），
避免它们继续占着飞轮。

用法：
  python3 backend/scripts/rescore_structured_reflections.py            # dry-run
  python3 backend/scripts/rescore_structured_reflections.py --apply    # 落库
"""
from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys

_SRC = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "src_python")
sys.path.insert(0, _SRC)

DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "trident_event_bus.db")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true", help="真正写库（默认只打印）")
    ap.add_argument("--db", default=DB_PATH)
    args = ap.parse_args()

    import structured_check as sc

    conn = sqlite3.connect(args.db)
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        """SELECT r.id, r.decision_id, r.status, r.verdict AS old_verdict,
                  r.score_coverage AS old_cov, r.score_fabrication AS old_fab,
                  d.reasoning, d.structured_input
           FROM structured_reflections r
           LEFT JOIN ai_decisions d ON d.id = r.decision_id
           WHERE r.status IN ('pending', 'skipped')
           ORDER BY r.id"""
    ).fetchall()

    changed = 0
    skipped_to_ok = 0
    for r in rows:
        payload = {}
        if r["structured_input"]:
            try:
                payload = json.loads(r["structured_input"])
            except Exception:
                payload = {}
        check = sc.check_reasoning_vs_structured_input(r["reasoning"] or "", payload)
        new_v, new_cov = check["verdict"], check["score_coverage"]
        reason = check.get("verdict_reason", "")
        flips = (new_v != r["old_verdict"])
        if flips:
            changed += 1
        if new_v == "ok" and r["status"] == "pending":
            skipped_to_ok += 1

        print(f"r{r['id']:>5} d{r['decision_id']:>6} {r['status']:<8} "
              f"{r['old_verdict']:<10}→{new_v:<10} cov {r['old_cov']:.2f}→{new_cov:.2f} "
              f"({reason})")

        if args.apply and flips:
            if new_v == "ok":
                # 没有可校验主张 → 离开飞轮（不是"已核实"，是"无可核实"）
                conn.execute(
                    "UPDATE structured_reflections SET status='skipped', "
                    "verdict=?, score_coverage=?, score_fabrication=?, "
                    "last_error=?, updated_at=datetime('now') WHERE id=?",
                    (new_v, check["score_coverage"], check["score_fabrication"],
                     f"rescore:{reason}", r["id"]),
                )
            else:
                conn.execute(
                    "UPDATE structured_reflections SET verdict=?, score_coverage=?, "
                    "score_fabrication=?, last_error=?, updated_at=datetime('now') WHERE id=?",
                    (new_v, check["score_coverage"], check["score_fabrication"],
                     f"rescore:{reason}", r["id"]),
                )

    if args.apply:
        conn.commit()
    print(f"\n共 {len(rows)} 行：判定翻转 {changed}，pending→skipped {skipped_to_ok}"
          f"{'（已写库）' if args.apply else '（dry-run，加 --apply 落库）'}")
    conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
