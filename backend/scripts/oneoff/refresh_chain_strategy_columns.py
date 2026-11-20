#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""刷新因果链的策略列 + 重算已对账 outcome 的结论（用链的**当前**方向）。

背景（2026-11-07）
------------------
`causal_chain_evaluator` 里：
  · `derive_strategy_columns(conn)` —— 把知识列（name/cause/effect/trigger_words）
    派生为策略列（asset/trigger_keywords/expected_direction/magnitude_*），
    **expected_direction 写回 causal_chains 表**。
  · `resnapshot_predictions(conn)` —— 用链表当前的 expected_direction/magnitude
    覆盖 outcome 的预测快照（快照冻结的是结论，不是推断逻辑版本）。
  · `evaluate_one(conn, row, force=True)` —— 重算结论（direction_hit/status）。

2026-11-07 给 `_infer_direction` 增加了**机制语义**判据后（见 10.27），
链的方向变了 → 上面三个环节都要跟着重跑，否则：
  · 链表的方向是新的，但 outcome 快照还是旧的 → 评估口径不一致
  · 已对账的结论还是按旧方向算的 → 反馈闭环用的是旧口径

本脚本串起这三步，并打印**前后对比**（hit rate 会下降 —— 因为挤掉了
BOTH 链"幅度够大就算中"的虚高部分）。

用法：
    python3 scripts/refresh_chain_strategy_columns.py --dry-run
    python3 scripts/refresh_chain_strategy_columns.py
"""
from __future__ import annotations
from pathlib import Path

import argparse
import os
import sqlite3
import sys
import time

BASE_DIR = str(Path(__file__).resolve().parents[2])
SRC = os.path.join(BASE_DIR, "src_python")
DB = os.environ.get("TRIDENT_DB_PATH") or os.path.join(
    BASE_DIR, "trident_event_bus.db")

if SRC not in sys.path:
    sys.path.insert(0, SRC)


def _stats(conn):
    row = conn.execute(
        """SELECT COUNT(*) n,
                  SUM(CASE WHEN status='HIT' THEN 1 ELSE 0 END) hit,
                  SUM(CASE WHEN status='HIT_WEAK' THEN 1 ELSE 0 END) weak,
                  SUM(CASE WHEN status='MISS' THEN 1 ELSE 0 END) miss,
                  SUM(CASE WHEN status='NEUTRAL' THEN 1 ELSE 0 END) neutral,
                  SUM(CASE WHEN predicted_direction IN ('UP','DOWN') THEN 1 ELSE 0 END) dir_n,
                  SUM(CASE WHEN predicted_direction IN ('UP','DOWN')
                            AND direction_hit=1 THEN 1 ELSE 0 END) dir_hit
           FROM causal_chain_outcomes""").fetchone()
    return dict(zip(("n", "hit", "weak", "miss", "neutral", "dir_n", "dir_hit"), row))


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default=DB)
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    import causal_chain_evaluator as cce

    if not os.path.exists(args.db):
        print("DB 不存在: %s" % args.db)
        return 2
    conn = sqlite3.connect(args.db, timeout=60)
    conn.row_factory = sqlite3.Row
    try:
        conn.execute("PRAGMA busy_timeout=60000;")
        before = _stats(conn)
        print("[前] %s" % before)
        if args.dry_run:
            print("\n(dry-run) 未做任何修改")
            return 0

        t0 = time.time()
        # ① 策略列派生（含 expected_direction 写回）
        d = cce.derive_strategy_columns(conn)
        print("[1] 策略列刷新: %s" % d)

        # ② 预测快照刷新（用链当前方向/幅度覆盖 outcome 快照）
        rs = cce.resnapshot_predictions(conn)
        print("[2] 预测快照刷新: %s" % rs)

        # ③ 已对账结论重算
        # evaluate_all = 先 resnapshot_predictions 再用 evaluate_one(force=True)
        # 重算（见 causal_chain_evaluator.py:1024 的 docstring）
        res = cce.evaluate_all(conn)
        print("[3] 结论重算: %s" % (res if isinstance(res, (dict, list))
                                     else str(res)[:300]))

        after = _stats(conn)
        print("\n[后] %s" % after)
        n = max(1, before["n"])
        print("  hit rate(含BOTH幅度): %.1f%% → %.1f%%"
              % ((before["hit"] + before["weak"]) / n * 100,
                 (after["hit"] + after["weak"]) / max(1, after["n"]) * 100))
        if after["dir_n"]:
            print("  ★有方向链的方向命中率: %.1f%% (%d条)"
                  % (after["dir_hit"] / after["dir_n"] * 100, after["dir_n"]))
        print("  耗时 %.1fs" % (time.time() - t0))
        return 0
    finally:
        try:
            conn.close()
        except Exception:
            pass


if __name__ == "__main__":
    sys.exit(main())
