#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""belief_maintenance.py — 心理预期/共识层的**维护作业** (需求 #3 / G12)。

背景
----
beliefs 表 (世界观 + 传导链条) 只有 7 行, 最后更新停在 2026-10-05 —— 心理预期层
**从来没有在维护**: 没有任何作业用新证据去复核这些世界观/链条, 于是页面上的
置信度永远是三周前的快照, 既不会上调也不会下调。

本作业做三件事 (都是真活, 不是刷 updated_at):
  1. 没有子链条的 worldview → 按最近 N 天的真实决策分解出子链条
     (belief_store.decompose_worldview);
  2. 每个 worldview → 用因果图谱里抽出的事实逐条复核子链条
     (belief_store._synthesize_from_children → revise_belief 写回 confidence/status);
  3. 没有任何证据支撑的世界观 → 标成 UNVERIFIED 并降置信度 (而不是假装还成立)。

用法:
    python3 belief_maintenance.py            # 跑一次维护
    python3 belief_maintenance.py --dry-run  # 只报告
"""
from __future__ import annotations

import argparse
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import belief_store as bs


def run(lookback_days: int = 30, dry_run: bool = False) -> dict:
    report = {"decomposed": [], "revised": [], "errors": []}
    try:
        import db
        conn = db.get_connection()
        conn.row_factory = __import__("sqlite3").Row
        worlds = conn.execute(
            "SELECT id, thesis, asset FROM beliefs WHERE level='worldview' "
            "AND status IN ('active','conflicted') ORDER BY id"
        ).fetchall()
        conn.close()
    except Exception as exc:
        report["errors"].append("load beliefs: %s" % exc)
        worlds = []

    for w in worlds:
        wid = int(w["id"])
        try:
            import db
            conn = db.get_connection()
            conn.row_factory = __import__("sqlite3").Row
            n_kids = conn.execute(
                "SELECT COUNT(*) n FROM beliefs WHERE parent_id=?", (wid,)
            ).fetchone()["n"]
            conn.close()
        except Exception as exc:
            report["errors"].append("belief %s count: %s" % (wid, exc))
            continue
        if not n_kids:
            if dry_run:
                report["decomposed"].append({"id": wid, "action": "would_decompose"})
                continue
            try:
                res = bs.decompose_worldview(wid, max_sub=7,
                                             lookback_days=lookback_days,
                                             auto_verify=True)
                report["decomposed"].append({"id": wid, "result": res})
            except Exception as exc:
                # 写锁被别的进程长时间占用 (engine 的 forward_tracker 会持锁数十秒)
                # → 重试, 而不是直接放弃 (放弃 = 子链条永远建不出来)
                import time as _t
                last = exc
                for attempt in range(3):
                    _t.sleep(10)
                    try:
                        res = bs.decompose_worldview(wid, max_sub=7,
                                                     lookback_days=lookback_days,
                                                     auto_verify=True)
                        report["decomposed"].append({"id": wid, "result": res,
                                                      "retried": attempt + 1})
                        last = None
                        break
                    except Exception as exc2:
                        last = exc2
                if last is not None:
                    report["errors"].append("decompose %s: %s" % (wid, last))
        if dry_run:
            continue
    # 复核全部 worldview (放在循环外: revise_all 本身就扫全表, 放循环里会
    # 在第一个 worldview 就 break, 后面的世界观永远轮不到)
    if not dry_run:
        try:
            report["revised"].append(bs.revise_all(lookback_days=lookback_days))
        except Exception as exc:
            report["errors"].append("revise_all: %s" % exc)
    return report


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=30)
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()
    rep = run(lookback_days=args.days, dry_run=args.dry_run)
    print(json.dumps(rep, ensure_ascii=False, indent=2)[:4000])
    try:
        import db
        conn = db.get_connection()
        rows = conn.execute(
            "SELECT id, level, status, confidence, updated_at FROM beliefs ORDER BY id"
        ).fetchall()
        print("\n%-5s %-10s %-12s %-6s %s" % ("id", "level", "status", "conf", "updated_at"))
        for r in rows:
            print("%-5s %-10s %-12s %-6s %s" % (r[0], r[1], r[2], r[3], r[4]))
        conn.close()
    except Exception as exc:
        print("[WARN] 读取 beliefs 失败: %s" % exc)
    return 0


if __name__ == "__main__":
    sys.exit(main())
