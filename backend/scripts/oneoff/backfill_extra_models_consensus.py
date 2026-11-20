#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""backfill_extra_models_consensus.py — 用真实 peer_reviews 回填 extra_models_consensus。

背景 (2026-11-02)
------------------
`ai_decisions.peer_reviews` 一直有真实同行评审数据，但 `extra_models_consensus`
这一列在 v314 之前**全库为空**（写入路径直到 2026-11-02 才补上）。于是:

  * signal_weight_evaluator._consensus_mult 读不到共识投影 → fail-closed 回 1.0
    → `signal_weight_history.consensus_mult` 恒为 1.0（"共识空转"）；
  * capability_acceptance 验收 #4 的"多模型共识填充率 ≥ 10%"按**全表**
    ai_decisions 计算 → 历史决策全部算作"没采共识" → 长期 FAIL。

本脚本做的事
------------
把**已存在**的 peer_reviews（真实模型输出，无任何编造）按线上**同一个**
投影函数 `engine.ai_worker._build_extra_models_consensus` 回填到
`ai_decisions.extra_models_consensus`。

安全边界
--------
1. 只回填 `peer_reviews` 非空且 `extra_models_consensus` 为空的行 → 幂等，
   重复跑不会重复写。
2. **不改 signal_weight_history**：回填候选全部只有 1 个模型族（实测 91/91），
   `_consensus_mult` 对单票 fail-closed 回 1.0，与历史行里已存的
   consensus_mult=1.0 完全一致 → 不需要、也不应该改写权重历史。
   脚本会自检这一点：若某行回填后推导出与历史行不同的 consensus_mult，
   则该行**跳过并在报告里列出**，不做静默改写。
3. `--dry-run`（默认）只报告不落库；`--apply` 才写。

用法:
    python3 backfill_extra_models_consensus.py            # dry-run
    python3 backfill_extra_models_consensus.py --apply
"""
from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
from pathlib import Path
from typing import Any, Dict, List, Tuple

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src_python"))

from engine.ai_worker import _build_extra_models_consensus  # noqa: E402
from signals.signal_weight_evaluator import _consensus_mult  # noqa: E402

DB_PATH = os.environ.get("TRIDENT_DB_PATH") or os.path.join(
    str(Path(__file__).resolve().parents[2]), "trident_event_bus.db"
)

SELECT = """
SELECT id, created_at, suggested_action, peer_reviews, extra_models_consensus
FROM ai_decisions
WHERE COALESCE(TRIM(peer_reviews), '') != ''
  AND COALESCE(TRIM(extra_models_consensus), '') = ''
ORDER BY id
"""


def _load_hist_consensus(conn: sqlite3.Connection, decision_id: int) -> List[float]:
    rows = conn.execute(
        "SELECT consensus_mult FROM signal_weight_history WHERE decision_id = ?",
        (decision_id,),
    ).fetchall()
    return [float(r[0]) for r in rows if r[0] is not None]


def main() -> int:
    ap = argparse.ArgumentParser(description="回填 extra_models_consensus (真实 peer_reviews 投影)")
    ap.add_argument("--apply", action="store_true", help="真正落库 (默认 dry-run)")
    ap.add_argument("--db", default=DB_PATH, help="DB 路径")
    args = ap.parse_args()

    conn = sqlite3.connect(args.db, timeout=30)
    conn.row_factory = sqlite3.Row
    rows = conn.execute(SELECT).fetchall()
    print("[backfill] 候选行 = %d (peer_reviews 非空 且 extra_models_consensus 为空)" % len(rows))
    if not rows:
        print("[backfill] 无需回填")
        return 0

    planned: List[Tuple[int, str]] = []
    skipped: List[Tuple[int, str]] = []
    stats = {"1 票": 0, "≥2 票": 0}

    for r in rows:
        did = int(r["id"])
        action = r["suggested_action"] or ""
        # peer_reviews 列是 JSON **字符串** → 先解析成 list 再投影
        # （线上写入路径传的是已解析的 list，这里必须对齐，否则投影恒为空）
        try:
            pr = json.loads(r["peer_reviews"] or "[]")
        except Exception:
            pr = []
        if isinstance(pr, dict):
            pr = [pr]
        if not isinstance(pr, list):
            pr = []
        emc = _build_extra_models_consensus(pr, main_action=action)
        if not emc:
            skipped.append((did, "投影为空 (无 action / 主模型 action 未知)"))
            continue
        new_cm = _consensus_mult(emc, main_action=action)
        hist = _load_hist_consensus(conn, did)
        if hist and not any(abs(new_cm - h) < 1e-9 for h in hist):
            # 回填后会与历史权重行的 consensus_mult 不一致 → 不动，交人工判断
            skipped.append((did, "回填后 consensus_mult=%.3f 与历史行 %s 不一致" % (new_cm, hist)))
            continue
        try:
            keys = len(json.loads(emc))
        except Exception:
            keys = 0
        stats["1 票" if keys < 2 else "≥2 票"] = stats.get("1 票" if keys < 2 else "≥2 票", 0) + 1
        planned.append((did, emc))

    n_ai = conn.execute("SELECT COUNT(*) FROM ai_decisions").fetchone()[0]
    n_emc = conn.execute(
        "SELECT COUNT(*) FROM ai_decisions WHERE COALESCE(TRIM(extra_models_consensus),'') != ''"
    ).fetchone()[0]
    print("[backfill] 计划回填 = %d (其中 %s)，跳过 = %d" % (
        len(planned), " / ".join("%s %d" % (k, v) for k, v in stats.items()), len(skipped)))
    for did, why in skipped[:20]:
        print("   跳过 id=%s: %s" % (did, why))
    print("[backfill] 填充率 %d/%d (%.2f%%) → 回填后 %.2f%%" % (
        n_emc, n_ai, n_emc / max(n_ai, 1) * 100, (n_emc + len(planned)) / max(n_ai, 1) * 100))

    if not args.apply:
        print("[backfill] dry-run 完成 (加 --apply 落库)")
        return 0

    conn.execute("BEGIN IMMEDIATE")
    for did, emc in planned:
        conn.execute(
            "UPDATE ai_decisions SET extra_models_consensus = ? WHERE id = ?", (emc, did))
    conn.commit()
    n_emc2 = conn.execute(
        "SELECT COUNT(*) FROM ai_decisions WHERE COALESCE(TRIM(extra_models_consensus),'') != ''"
    ).fetchone()[0]
    print("[backfill] 已落库 %d 行，填充率 %d/%d (%.2f%%)" % (
        len(planned), n_emc2, n_ai, n_emc2 / max(n_ai, 1) * 100))
    conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
