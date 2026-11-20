"""quarantine_duplicate_decisions.py —— 把重复分析产出的复制品决策标为隔离

背景（2026-11-14）：
  812 个 (news_id, target_asset) 组合被重复分析，2096 条决策是同一条新闻
  同一品种的复制品（权重逐位相同）。同一份 LLM 输出被反复计分。
  认领幂等已修（`_CLAIM_DEDUP`），这里处理历史存量。

规则：每个 (news_id, target_asset) 只保留**最早**一条（id 最小），
其余写 quarantined_at + quarantine_reason='dup_reanalysis'。
不改任何数值字段 —— 记录还在，只是不再参与指标统计。

用法：
  python3 quarantine_duplicate_decisions.py            # dry-run
  python3 quarantine_duplicate_decisions.py --commit
  python3 quarantine_duplicate_decisions.py --rollback
"""
from __future__ import annotations

import argparse
import sqlite3
import time
from pathlib import Path

DB = Path(__file__).resolve().parents[1] / "trident_event_bus.db"
REASON = "dup_reanalysis"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--commit", action="store_true")
    ap.add_argument("--rollback", action="store_true")
    args = ap.parse_args()

    conn = sqlite3.connect(str(DB), timeout=30)

    if args.rollback:
        n = conn.execute(
            "SELECT COUNT(*) FROM ai_decisions WHERE quarantine_reason = ?",
            (REASON,)).fetchone()[0]
        conn.execute(
            "UPDATE ai_decisions SET quarantined_at = NULL, quarantine_reason = NULL"
            " WHERE quarantine_reason = ?", (REASON,))
        conn.commit()
        print(f"已解隔离 {n} 条")
        return 0

    dup_ids = [r[0] for r in conn.execute(
        "SELECT id FROM ai_decisions WHERE news_id IS NOT NULL AND id NOT IN ("
        "  SELECT MIN(id) FROM ai_decisions WHERE news_id IS NOT NULL"
        "  GROUP BY news_id, target_asset)")]
    total = conn.execute("SELECT COUNT(*) FROM ai_decisions").fetchone()[0]
    print(f"总决策 {total} · 待隔离 {len(dup_ids)} 条 ({len(dup_ids)/max(1,total):.1%})")

    if not args.commit:
        print("[dry-run] 未写入。样例 id:", dup_ids[:8])
        print("加 --commit 执行")
        return 0

    now = int(time.time())
    conn.executemany(
        "UPDATE ai_decisions SET quarantined_at = ?, quarantine_reason = ? WHERE id = ?",
        [(now, REASON, i) for i in dup_ids])
    conn.commit()
    live = conn.execute(
        "SELECT COUNT(*) FROM ai_decisions WHERE COALESCE(quarantined_at,0)=0"
    ).fetchone()[0]
    print(f"[commit] 已隔离 {len(dup_ids)} 条 · 参与统计的决策 {live} 条")
    print("撤销: python3 quarantine_duplicate_decisions.py --rollback")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
