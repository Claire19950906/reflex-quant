# -*- coding: utf-8 -*-
"""
causal_weight_feedback.py — 因果图谱权重闭环回写 (2026-10-22 新增)
=================================================================

问题
----
causal_chain_outcomes 持续结算 (已 269 条), 但 causal_chains.weight
永远停在 1.0 —— outcome 算了从不回写, 图谱"只读不写"。

为什么不能直接回写
-----------------
实测: 159 条链有 outcome, 平均样本 1.4 (n=1 有 97 条, n=2 有 62 条,
n>=3 为 0)。n=1 时单条 HIT 就能算出 1.31 倍权重、单条 MISS 给 0.42,
那是纯噪声, 写进决策会污染信号。

因此本模块用**贝叶斯收缩**:
  - 样本不足 MIN_SAMPLES 时权重不动 (skipped_sample)
  - 用全局命中率作先验, PRIOR_STRENGTH 个"虚拟样本"做收缩
  - 收缩后偏离 <MIN_DELTA 仍不动, 避免噪声抖动
  - 只写 0.3 ~ 2.0 区间

随着样本积累, 权重会**自动、渐进、可解释**地长出来。

用法
----
    python3 causal_weight_feedback.py --dry-run     # 只看会改什么
    python3 causal_weight_feedback.py               # 实际回写
    python3 causal_weight_feedback.py --stats-only   # 只输出统计
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

BACKEND_DIR = Path(__file__).resolve().parents[1]
DB_PATH = BACKEND_DIR / "trident_event_bus.db"

MIN_SAMPLES = 8          # 低于此样本数不下结论
PRIOR_STRENGTH = 12.0   # 收缩强度(虚拟样本数)
MIN_DELTA = 0.05        # 偏离 1.0 超过此值才写
WEIGHT_LO, WEIGHT_HI = 0.3, 2.0


def _db(db_path: Optional[Path] = None) -> sqlite3.Connection:
    conn = sqlite3.connect(str(db_path or DB_PATH), timeout=60)
    conn.row_factory = sqlite3.Row
    return conn


def _chain_stats(conn: sqlite3.Connection) -> Dict[int, Dict[str, float]]:
    """按 chain_id 聚合已结算 outcome。weak 命中折半计分。"""
    rows = conn.execute(
        "SELECT CAST(chain_id AS INTEGER) AS cid, "
        "       COUNT(*) AS n, "
        "       SUM(CASE WHEN status IN ('HIT','HIT_WEAK') THEN 1 ELSE 0 END) AS hit, "
        "       SUM(CASE WHEN status='HIT' THEN 1 ELSE 0 END) AS strong_hit, "
        "       SUM(CASE WHEN status='HIT_WEAK' THEN 1 ELSE 0 END) AS weak_hit, "
        "       SUM(CASE WHEN status='MISS' THEN 1 ELSE 0 END) AS miss, "
        "       SUM(CASE WHEN status IN ('HIT','HIT_WEAK') THEN 1 ELSE 0 END) "
        "         + 0.5 * SUM(CASE WHEN status='HIT_WEAK' THEN 1 ELSE 0 END) AS credit "
        "FROM causal_chain_outcomes "
        "WHERE status IN ('HIT','HIT_WEAK','MISS') "
        "GROUP BY CAST(chain_id AS INTEGER)"
    ).fetchall()

    out: Dict[int, Dict[str, float]] = {}
    for r in rows:
        if r["cid"] is None:
            continue
        out[int(r["cid"])] = {
            "n": int(r["n"] or 0),
            "hit": int(r["hit"] or 0),
            "strong_hit": int(r["strong_hit"] or 0),
            "weak_hit": int(r["weak_hit"] or 0),
            "miss": int(r["miss"] or 0),
            "credit": float(r["credit"] or 0.0),
        }
    return out


def compute_global_prior(stats: Dict[int, Dict[str, float]]) -> float:
    """全局命中率作为先验 (带弱命中折算), 防除零。"""
    gn = sum(v["n"] for v in stats.values())
    gh = sum(v["credit"] for v in stats.values())
    if gn <= 0:
        return 0.5
    return max(gh / gn, 0.05)


def plan_updates(conn: sqlite3.Connection) -> Dict[str, Any]:
    """算出每条链应写入的权重, 但不写库。"""
    stats = _chain_stats(conn)
    prior = compute_global_prior(stats)

    plans: List[Dict[str, Any]] = []
    for cid, v in stats.items():
        n = v["n"]
        raw = v["credit"] / n if n else 0.0
        shrunk = (v["credit"] + PRIOR_STRENGTH * prior) / (n + PRIOR_STRENGTH)
        ratio = shrunk / max(prior, 1e-6)
        w = max(WEIGHT_LO, min(WEIGHT_HI, ratio))

        if n < MIN_SAMPLES:
            reason = "skipped_sample(n=%d<%d)" % (n, MIN_SAMPLES)
        elif abs(w - 1.0) < MIN_DELTA:
            reason = "skipped_noise(Δ=%.3f<%.2f)" % (abs(w - 1.0), MIN_DELTA)
        else:
            reason = "update"

        cur = conn.execute(
            "SELECT weight FROM causal_chains WHERE id=?", (cid,)
        ).fetchone()
        cur_w = float(cur["weight"]) if cur and cur["weight"] is not None else 1.0

        if reason == "update" and abs(w - cur_w) < 1e-6:
            reason = "unchanged"

        plans.append({
            "chain_id": cid, "reason": reason, "n": n,
            "hit": v["hit"], "credit": round(v["credit"], 3),
            "raw_rate": round(raw, 4), "shrunk_rate": round(shrunk, 4),
            "proposed_weight": round(w, 4), "current_weight": round(cur_w, 4),
        })

    return {
        "prior": round(prior, 4),
        "min_samples": MIN_SAMPLES,
        "prior_strength": PRIOR_STRENGTH,
        "chains_with_outcomes": len(stats),
        "plans": plans,
    }


def apply_updates(conn: sqlite3.Connection, plan: Dict[str, Any],
                  dry_run: bool = False) -> Dict[str, Any]:
    """执行回写。dry_run=True 时只报告不落库。"""
    now = int(time.time())
    applied: List[Dict[str, Any]] = []
    skipped_sample = 0
    skipped_noise = 0

    for p in plan["plans"]:
        r = p["reason"]
        if r.startswith("skipped_sample"):
            skipped_sample += 1
            continue
        if r.startswith("skipped_noise"):
            skipped_noise += 1
            continue
        if r == "unchanged":
            continue

        row = conn.execute(
            "SELECT name FROM causal_chains WHERE id=?", (p["chain_id"],)
        ).fetchone()
        applied.append({
            **p,
            "name": str(row["name"])[:70] if row else "",
        })
        if not dry_run:
            conn.execute(
                "UPDATE causal_chains SET weight=?, updated_at=? WHERE id=?",
                (p["proposed_weight"], now, p["chain_id"]),
            )

    if not dry_run:
        conn.commit()

    return {
        "applied": len(applied),
        "skipped_sample": skipped_sample,
        "skipped_noise": skipped_noise,
        "dry_run": dry_run,
        "details": applied,
    }


def report(conn: sqlite3.Connection, plan: Dict[str, Any],
           res: Dict[str, Any], dry_run: bool = False) -> Dict[str, Any]:
    """生成人可读摘要 (JSON)。"""
    dist = dict(conn.execute(
        "SELECT weight, COUNT(*) FROM causal_chains GROUP BY weight ORDER BY 2 DESC"
    ).fetchall())

    all_n = [p["n"] for p in plan["plans"]]
    return {
        "ts": int(time.time()),
        "dry_run": dry_run,
        "global_prior_hit_rate": plan["prior"],
        "min_samples_required": plan["min_samples"],
        "prior_strength": plan["prior_strength"],
        "chains_with_outcomes": plan["chains_with_outcomes"],
        "sample_per_chain": {
            "max": max(all_n) if all_n else 0,
            "avg": round(sum(all_n) / len(all_n), 2) if all_n else 0,
        },
        "applied": res["applied"],
        "skipped_insufficient_sample": res["skipped_sample"],
        "skipped_noise": res["skipped_noise"],
        "weight_distribution_after": dist,
        "updates": res["details"][:40],
    }


def run(db_path: Optional[Path] = None, dry_run: bool = False) -> Dict[str, Any]:
    conn = _db(db_path)
    try:
        plan = plan_updates(conn)
        res = apply_updates(conn, plan, dry_run=dry_run)
        return report(conn, plan, res, dry_run=dry_run)
    finally:
        conn.close()


def main() -> int:
    ap = argparse.ArgumentParser(description="因果图谱权重闭环回写")
    ap.add_argument("--dry-run", action="store_true", help="只报告不写库")
    ap.add_argument("--stats-only", action="store_true", help="只输出统计")
    ap.add_argument("--db", default=None)
    args = ap.parse_args()

    if args.stats_only:
        conn = _db(Path(args.db) if args.db else None)
        try:
            plan = plan_updates(conn)
            print(json.dumps({
                "global_prior_hit_rate": plan["prior"],
                "chains_with_outcomes": plan["chains_with_outcomes"],
                "would_update": sum(1 for p in plan["plans"]
                                    if p["reason"] == "update"),
                "skipped_sample": sum(1 for p in plan["plans"]
                                      if p["reason"].startswith("skipped_sample")),
            }, ensure_ascii=False, indent=2))
        finally:
            conn.close()
        return 0

    out = run(Path(args.db) if args.db else None, dry_run=args.dry_run)
    print(json.dumps(out, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())