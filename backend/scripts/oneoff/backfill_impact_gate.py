"""backfill_impact_gate.py —— 用门卫分补历史决策的 impact_score（可回滚）

背景（2026-11-14）：
  ai_decisions 8865 条里 8379 条（94.5%）的 impact_score 恰好 = 5.0 ——
  是缺省兜底，不是判断。其中 7263 条对应的新闻在 raw_news.gate_impact_1to100
  已经有门卫给的 0-100 分（均值 16.9 / 最大 95），没被用上。
  结果 base_mult 在 88.7% 的决策上是常数 0.550，权重分辨率只有 14.6%。

做法：
  只改 impact_score 一个字段（值 = gate_impact/10，clamp 0-10），
  · 不改 suggested_action / signal_weight / reasoning
  · 写入前把原值存进 ai_decisions_impact_backfill（整表可回滚）
  · 默认 dry-run，只有 --commit 才写

用法：
  python3 backfill_impact_gate.py            # 只看会改多少
  python3 backfill_impact_gate.py --commit   # 真写（含回滚表）
  python3 backfill_impact_gate.py --rollback # 从回滚表还原
"""
from __future__ import annotations

import argparse
import sqlite3
import sys
from pathlib import Path
from typing import Any, Dict, List

BACKEND_DIR = Path(__file__).resolve().parents[2]
DB_PATH = BACKEND_DIR / "trident_event_bus.db"
RB_TABLE = "ai_decisions_impact_backfill"


def candidates(conn: sqlite3.Connection) -> List[Dict[str, Any]]:
    rows = conn.execute(
        "SELECT d.id, d.impact_score old_score, r.gate_impact_1to100 g "
        "FROM ai_decisions d JOIN raw_news r ON r.id = d.news_id "
        "WHERE ABS(COALESCE(d.impact_score, 5.0) - 5.0) < 1e-9 "
        "  AND COALESCE(r.gate_impact_1to100, 0) > 0"
    ).fetchall()
    out = []
    for r in rows:
        new = max(0.0, min(10.0, round(int(r["g"]) / 10.0, 1)))
        out.append({"id": int(r["id"]), "old": float(r["old_score"]),
                    "gate": int(r["g"]), "new": new})
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--commit", action="store_true")
    ap.add_argument("--rollback", action="store_true")
    args = ap.parse_args()

    conn = sqlite3.connect(str(DB_PATH), timeout=30)
    conn.row_factory = sqlite3.Row

    if args.rollback:
        if RB_TABLE not in {r[0] for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'")}:
            print(f"没有 {RB_TABLE}，无法回滚")
            return 1
        n = conn.execute(f"SELECT COUNT(*) FROM {RB_TABLE}").fetchone()[0]
        conn.execute(
            f"UPDATE ai_decisions SET impact_score = "
            f"(SELECT b.old_score FROM {RB_TABLE} b WHERE b.decision_id = ai_decisions.id) "
            f"WHERE id IN (SELECT decision_id FROM {RB_TABLE})")
        conn.commit()
        print(f"已回滚 {n} 条 impact_score")
        return 0

    rows = candidates(conn)
    n_all = conn.execute("SELECT COUNT(*) FROM ai_decisions").fetchone()[0]
    print(f"ai_decisions {n_all} 条 · 可补 {len(rows)} 条 "
          f"({len(rows)/max(1,n_all):.1%})")

    if not rows:
        print("没有需要补的行")
        return 0

    by_new: Dict[float, int] = {}
    for r in rows:
        by_new[r["new"]] = by_new.get(r["new"], 0) + 1
    top = sorted(by_new.items(), key=lambda kv: -kv[1])[:6]
    print("  补之后 impact_score 的主要档位: " +
          ", ".join(f"{k}({v}条)" for k, v in top))

    if not args.commit:
        print("\n[dry-run] 未写入。加 --commit 才执行（会先建回滚表）")
        for r in rows[:4]:
            print(f"  id={r['id']} {r['old']} → {r['new']} (gate={r['gate']})")
        return 0

    conn.execute(
        f"CREATE TABLE IF NOT EXISTS {RB_TABLE} ("
        f" decision_id INTEGER PRIMARY KEY, old_score REAL NOT NULL,"
        f" gate_impact_1to100 INTEGER NOT NULL, backfilled_at INTEGER NOT NULL)")
    import time as _t
    now = int(_t.time())
    conn.executemany(
        f"INSERT OR REPLACE INTO {RB_TABLE} VALUES (?,?,?,?)",
        [(r["id"], r["old"], r["gate"], now) for r in rows])
    conn.executemany(
        "UPDATE ai_decisions SET impact_score = ? WHERE id = ?",
        [(r["new"], r["id"]) for r in rows])
    conn.commit()

    n5 = conn.execute(
        "SELECT COUNT(*) FROM ai_decisions WHERE ABS(impact_score-5.0)<1e-9").fetchone()[0]
    print(f"\n[commit] 已写入 {len(rows)} 条 · 回滚表 {RB_TABLE}（{RB_TABLE} 可还原）")
    print(f"  现在 impact_score 仍为 5.0 的: {n5} 条 "
          f"({n5/max(1,n_all):.1%}, 原 94.5%)")
    print(f"  撤销: python3 backfill_impact_gate.py --rollback")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
