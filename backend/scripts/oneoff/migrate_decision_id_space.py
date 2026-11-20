#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""migrate_decision_id_space.py — 统一决策编号空间 (幂等, 可重复执行)。

    python3 migrate_decision_id_space.py --dry-run   # 只看会改什么
    python3 migrate_decision_id_space.py --apply     # 真正写入

做三件事:
  1. news_events: 标出 decision_id 属于哪个空间, 补 ai_decision_id / trade_decision_id,
     悬空 id 置 NULL (那条决策已不存在于任何表, 留着就是"断链")。
  2. signal_weight_history: 标出空间, 补 trade_decision_id (权重能跟真实盈亏对账的前提)。
  3. trade_reflections_v2: 补 ai_decision_id (反思 ← 权重 的桥)。
"""
from __future__ import annotations
from pathlib import Path

import argparse
import json
import os
import sqlite3
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src_python"))

from signals import decision_id_space as dis  # noqa: E402

DB_PATH = os.environ.get("TRIDENT_DB_PATH") or os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))), "trident_event_bus.db"
)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default=DB_PATH)
    ap.add_argument("--apply", action="store_true", help="真正写入 (默认 dry-run)")
    args = ap.parse_args()

    conn = sqlite3.connect(args.db, timeout=60)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    print("[INFO] DB =", args.db)

    before = dis.audit(conn)
    print("[BEFORE]", json.dumps(before, ensure_ascii=False))

    if not args.apply:
        idx = dis.DecisionIndex(conn)
        sample = conn.execute(
            "SELECT id, news_id, decision_id FROM news_events WHERE decision_id IS NOT NULL LIMIT 5"
        ).fetchall()
        for r in sample:
            space = idx.resolve_space(r["decision_id"])
            ai_id = idx.resolve_ai(r["news_id"])
            td_id = idx.resolve_trade(r["news_id"], 0,
                                      r["decision_id"] if space == dis.SPACE_AI else None)
            print("  news#%s news_id=%s decision_id=%s space=%s ai=%s trade=%s" % (
                r["id"], r["news_id"], r["decision_id"], space, ai_id, td_id))
        print("[DRY-RUN] 加 --apply 执行迁移")
        conn.close()
        return 0

    added = dis.ensure_columns(conn)
    print("[OK] 新增列:", added or "(无)")
    idx = dis.DecisionIndex(conn)
    print("[OK] swh 定向修复:", json.dumps(dis.repair_missing_weight_space(conn, idx), ensure_ascii=False))
    print("[OK] news_events   :", json.dumps(dis.migrate_news_decisions(conn, idx), ensure_ascii=False))
    print("[OK] swh           :", json.dumps(dis.migrate_signal_weight_space(conn, idx), ensure_ascii=False))
    print("[OK] reflections   :", json.dumps(dis.migrate_reflection_ai_link(conn, idx), ensure_ascii=False))
    # v321.3 P1: 清理 trade_decisions 里指向已删上游的孤儿引用
    # (「重置交易历史」删了 ai_decisions/raw_news 却没动 trade_decisions 的指针 →
    #  上次留下 1998 条死指针, 对账永远有悬空)。跑在这里 → 每次重置后自动收敛。
    print("[OK] td 孤儿引用   :", json.dumps(dis.repair_orphan_trade_decision_refs(conn), ensure_ascii=False))
    # v321.3 P2: 归档引用重建 —— 上游 (news_events / ai_decisions) 重建后,
    # 把归档里保存的决策引用自动接回去 (重置前归档, 重置不删归档)。
    try:
        from signals import decision_ref_archive as _dra
        print("[OK] 归档引用重建 :", json.dumps(_dra.restore_refs(conn), ensure_ascii=False))
    except Exception as _dra_exc:
        print(f"[WARN] 归档引用重建跳过: {type(_dra_exc).__name__}: {str(_dra_exc)[:120]}",
              flush=True)
    after = dis.audit(conn)
    print("[AFTER]", json.dumps(after, ensure_ascii=False))
    conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
