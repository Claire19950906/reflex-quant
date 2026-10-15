#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""v233 P0-A: 双轨置信度（量纲修复）— Shadow 计算模块。

背景: 690 个 ai_decisions 中 99% evidence_action=HOLD, evidence_confidence
实际分布 p50=4, p90=10, p99=18 — 闸门 20 仅 0.43% 通过。根因是 evidence.py
的 unified_confidence 公式叠加 chi-square 权重 + 矛盾惩罚后接近 0。

本模块: 保留老 evidence_confidence 不动, 加 evidence_strength (0-1) /
evidence_confidence_raw (0-100) / shadow_passed_new (0/1) / shadow_gate_new
四个新字段, 默认 SHADOW_CONFIDENCE_MODE=0 不动生产, 1=实时写新字段。
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import sqlite3
from typing import Any, Dict, List, Optional, Tuple

import db

logger = logging.getLogger("trident.confidence_shadow")

SHADOW_CONFIDENCE_MODE = os.getenv("SHADOW_CONFIDENCE_MODE", "0") == "1"
SHADOW_CONFIDENCE_GATE = max(0.0, min(100.0, float(os.getenv("SHADOW_CONFIDENCE_GATE", "10"))))

try:
    import config as _config
    LEGACY_CONFIDENCE_GATE = _config.EVIDENCE_CONFIDENCE_GATE
except (ImportError, AttributeError):
    # 与 config.EVIDENCE_CONFIDENCE_GATE 默认值(10)对齐
    LEGACY_CONFIDENCE_GATE = 10.0


def compute_evidence_strength(final_score: Any) -> float:
    """v233 P0-A: 从 final_score (-1~1) 算 evidence_strength (0-1).

    物理意义: 系统对该决策方向性的"裸强度", 不含历史 chi-square + 矛盾惩罚。
    final_score = sum(factor_score * weight), 已在 decision_guard 算过。
    """
    try:
        v = abs(float(final_score or 0.0))
    except (TypeError, ValueError):
        return 0.0
    return round(max(0.0, min(1.0, v)), 4)


def compute_confidence_raw(
    final_score: Any,
    test: Optional[Dict[str, Any]] = None,
    contradictions: Optional[List[Dict[str, str]]] = None,
    *,
    include_significance_weight: bool = False,
) -> float:
    """v233 P0-A: 算 evidence_confidence_raw (0-100). 三档可选.

    - 最纯: abs(final_score) * 100           (无权重, 无惩罚) ← 默认
    - 中:   base * significance_weight        (加权重, 无惩罚)
    - 全:   base * weight - penalty           (= evidence.py 当前公式, 几乎必 0)
    """
    try:
        base = min(abs(float(final_score or 0.0)), 1.0) * 100.0
    except (TypeError, ValueError):
        return 0.0

    if include_significance_weight and test:
        if not test.get("sufficient_sample"):
            weight = 0.80
        else:
            p = float(test.get("p_value", 1.0))
            if p <= 0.05:
                weight = 1.0
            elif p <= 0.20:
                weight = 0.85
            else:
                weight = 0.70
        base *= weight

    if contradictions:
        penalty = min(len(contradictions) * 12.0, 36.0)
        base = max(0.0, base - penalty)

    return round(max(0.0, min(100.0, base)), 2)


def compute_shadow_gate(
    confidence_raw: Any,
    suggested_action: Any,
    *,
    gate: Optional[float] = None,
) -> int:
    """v233 P0-A: 新 gate 判定 — 仅看 raw confidence + 原始 LLM action."""
    g = gate if gate is not None else SHADOW_CONFIDENCE_GATE
    action = (str(suggested_action or "")).upper().strip()
    if action not in ("BUY", "SELL"):
        return 0
    if confidence_raw is None:
        return 0
    try:
        return 1 if float(confidence_raw) >= float(g) else 0
    except (TypeError, ValueError):
        return 0


def shadow_backfill(decision_ids=None, *, dry_run=False, verbose=True):
    """v233 P0-A: 对已有 ai_decisions 跑新公式，写入 shadow 字段.

    保守回填: 用 sentiment_score abs 当 final_score 代理 + 影响加权
    (ai_decisions 没存 final_score 字段; 真实时 shadow 在 ai_worker 算)。
    """
    conn = db.get_connection()
    conn.row_factory = sqlite3.Row
    try:
        if decision_ids is not None:
            ph = ",".join("?" * len(decision_ids))
            sql = (f"SELECT id, sentiment_score, suggested_action, evidence_action, "
                   f"       evidence_confidence, heat_score, impact_score, asset_impact, "
                   f"       target_asset "
                   f"FROM ai_decisions WHERE id IN ({ph}) ORDER BY id")
            rows = conn.execute(sql, decision_ids).fetchall()
        else:
            sql = ("SELECT id, sentiment_score, suggested_action, evidence_action, "
                   "       evidence_confidence, heat_score, impact_score, asset_impact, "
                   "       target_asset FROM ai_decisions ORDER BY id")
            rows = conn.execute(sql).fetchall()
    except sqlite3.OperationalError as e:
        logger.warning("shadow_backfill: schema missing — run migration first. err=%s", e)
        conn.close()
        return {"scanned": 0, "updated": 0, "skipped": 0,
                "pass_rate_old": 0.0, "pass_rate_new": 0.0,
                "pass_rate_by_asset": {}, "dry_run": dry_run,
                "error": f"schema missing: {e}"}

    n_scanned = n_updated = n_skipped = n_old_pass = n_new_pass = 0
    by_asset: Dict[str, List[int]] = {}
    updates: List[Tuple[float, float, int, int]] = []

    for r in rows:
        n_scanned += 1
        sentiment_abs = abs(float(r["sentiment_score"] or 0.0))
        heat = float(r["heat_score"] or 5.0)
        impact = float(r["impact_score"] or 5.0)
        asset_impact = float(r["asset_impact"] or 5.0)
        impact_boost = (impact - 5.0) * 0.03 + (heat - 5.0) * 0.01 + (asset_impact - 5.0) * 0.02
        est_final = max(-1.0, min(1.0, sentiment_abs + impact_boost))

        strength = compute_evidence_strength(est_final)
        conf_raw = compute_confidence_raw(est_final, include_significance_weight=False)
        passed = compute_shadow_gate(conf_raw, r["suggested_action"], gate=SHADOW_CONFIDENCE_GATE)

        old_conf = float(r["evidence_confidence"] or 0.0)
        old_action = (r["evidence_action"] or "").upper()
        old_passed = old_conf >= LEGACY_CONFIDENCE_GATE and old_action in ("BUY", "SELL")

        if old_passed:
            n_old_pass += 1
        if passed:
            n_new_pass += 1

        asset = (r["target_asset"] or "NONE").upper()
        bucket = by_asset.setdefault(asset, [0, 0])
        if old_passed:
            bucket[0] += 1
        if passed:
            bucket[1] += 1
        updates.append((strength, conf_raw, passed, int(r["id"])))

    if dry_run:
        conn.close()
        return {
            "scanned": n_scanned, "updated": 0, "skipped": 0,
            "pass_rate_old": n_old_pass / max(1, n_scanned),
            "pass_rate_new": n_new_pass / max(1, n_scanned),
            "pass_rate_by_asset": {a: tuple(v) for a, v in by_asset.items()},
            "dry_run": True,
        }

    for strength, conf_raw, passed, did in updates:
        try:
            conn.execute(
                "UPDATE ai_decisions SET "
                "  evidence_strength = ?, "
                "  evidence_confidence_raw = ?, "
                "  shadow_passed_new = ?, "
                "  shadow_gate_new = ? "
                "WHERE id = ?",
                (strength, conf_raw, passed, float(conf_raw), did),
            )
            n_updated += 1
        except sqlite3.OperationalError as e:
            n_skipped += 1
            if verbose:
                logger.warning("UPDATE failed id=%d: %s", did, e)

    conn.commit()
    conn.close()

    if verbose:
        old_pct = n_old_pass / max(1, n_scanned) * 100
        new_pct = n_new_pass / max(1, n_scanned) * 100
        lift = (n_new_pass / n_old_pass) if n_old_pass > 0 else float('inf')
        logger.info("shadow_backfill: scanned=%d updated=%d skipped=%d old_pass=%.2f%% new_pass=%.2f%% lift=%s",
                    n_scanned, n_updated, n_skipped, old_pct, new_pct,
                    f"{lift:.1f}x" if lift != float('inf') else "inf")

    return {
        "scanned": n_scanned, "updated": n_updated, "skipped": n_skipped,
        "pass_rate_old": n_old_pass / max(1, n_scanned),
        "pass_rate_new": n_new_pass / max(1, n_scanned),
        "pass_rate_by_asset": {a: tuple(v) for a, v in by_asset.items()},
        "dry_run": False,
    }


def shadow_compare_summary(conn=None):
    """v233 P0-A: dashboard 端点用 — 旧 vs 新 gate 通过率对比."""
    own = conn is None
    if own:
        conn = db.get_connection()
        conn.row_factory = sqlite3.Row
    try:
        total = conn.execute("SELECT COUNT(*) FROM ai_decisions").fetchone()[0]
        old_passed = conn.execute(
            "SELECT COUNT(*) FROM ai_decisions "
            "WHERE evidence_confidence IS NOT NULL "
            "  AND evidence_confidence >= ? "
            "  AND UPPER(evidence_action) IN ('BUY', 'SELL')",
            (LEGACY_CONFIDENCE_GATE,),
        ).fetchone()[0]
        try:
            new_passed = conn.execute(
                "SELECT COUNT(*) FROM ai_decisions WHERE shadow_passed_new = 1"
            ).fetchone()[0]
        except sqlite3.OperationalError:
            new_passed = 0

        by_action: Dict[str, Tuple[int, int]] = {}
        for action in ("BUY", "SELL", "HOLD"):
            n_total = conn.execute(
                "SELECT COUNT(*) FROM ai_decisions WHERE UPPER(suggested_action) = ?",
                (action,),
            ).fetchone()[0]
            try:
                n_new = conn.execute(
                    "SELECT COUNT(*) FROM ai_decisions "
                    "WHERE UPPER(suggested_action) = ? AND shadow_passed_new = 1",
                    (action,),
                ).fetchone()[0]
            except sqlite3.OperationalError:
                n_new = 0
            by_action[action] = (n_total, n_new)

        by_asset: Dict[str, Tuple[int, int]] = {}
        try:
            for r in conn.execute(
                "SELECT target_asset, COUNT(*) total, "
                "       SUM(CASE WHEN shadow_passed_new=1 THEN 1 ELSE 0 END) new_pass "
                "FROM ai_decisions GROUP BY target_asset ORDER BY total DESC"
            ).fetchall():
                by_asset[r["target_asset"] or "NONE"] = (r["total"], r["new_pass"])
        except sqlite3.OperationalError:
            pass

        lift = (new_passed / old_passed) if old_passed > 0 else float('inf')
        return {
            "total": total,
            "old_passed": old_passed,
            "old_pass_rate": old_passed / max(1, total),
            "new_passed": new_passed,
            "new_pass_rate": new_passed / max(1, total),
            "old_gate": LEGACY_CONFIDENCE_GATE,
            "new_gate": SHADOW_CONFIDENCE_GATE,
            "lift_ratio": lift,
            "by_action": by_action,
            "by_asset": by_asset,
        }
    finally:
        if own:
            conn.close()


def shadow_pnl_simulation(conn=None):
    """v233 P0-A: 用 forward_pnl 模拟新 gate 选中的决策的预期 pnl.

    Returns: {old: {n, avg, win_rate, sum}, new: {...}, delta_avg_pnl, verdict}
    verdict: insufficient_data | switch_recommended | marginal | keep_legacy
    """
    own = conn is None
    if own:
        conn = db.get_connection()
        conn.row_factory = sqlite3.Row
    try:
        def _stats(sql, params):
            try:
                rows = conn.execute(sql, params).fetchall()
            except sqlite3.OperationalError:
                return {"n": 0, "avg_pnl": 0.0, "win_rate": 0.0, "sum_pnl": 0.0}
            n = len(rows)
            if n == 0:
                return {"n": 0, "avg_pnl": 0.0, "win_rate": 0.0, "sum_pnl": 0.0}
            pnls = [float(r["forward_pnl"] or 0) for r in rows]
            wins = sum(1 for p in pnls if p > 0)
            return {"n": n, "avg_pnl": sum(pnls) / n, "win_rate": wins / n, "sum_pnl": sum(pnls)}

        old = _stats(
            "SELECT forward_pnl FROM ai_decisions "
            "WHERE forward_pnl IS NOT NULL "
            "  AND evidence_confidence IS NOT NULL "
            "  AND evidence_confidence >= ? "
            "  AND UPPER(evidence_action) IN ('BUY', 'SELL')",
            (LEGACY_CONFIDENCE_GATE,),
        )
        new = _stats(
            "SELECT forward_pnl FROM ai_decisions "
            "WHERE forward_pnl IS NOT NULL AND shadow_passed_new = 1",
            (),
        )
        delta = new["avg_pnl"] - old["avg_pnl"]
        if new["n"] < 5 or old["n"] < 5:
            verdict = "insufficient_data"
        elif new["avg_pnl"] >= old["avg_pnl"] and new["win_rate"] >= old["win_rate"]:
            verdict = "switch_recommended"
        elif new["avg_pnl"] >= old["avg_pnl"] - 0.001:
            verdict = "marginal"
        else:
            verdict = "keep_legacy"
        return {"old": old, "new": new, "delta_avg_pnl": delta, "verdict": verdict}
    finally:
        if own:
            conn.close()


def _main():
    parser = argparse.ArgumentParser(description="v233 P0-A shadow backfill")
    parser.add_argument("--dry-run", action="store_true", help="只算不算")
    parser.add_argument("--decision-ids", help="逗号分隔 decision id (默认全部)")
    parser.add_argument("--quiet", action="store_true", help="静默模式")
    args = parser.parse_args()
    if args.decision_ids:
        ids = [int(x) for x in args.decision_ids.split(",") if x.strip()]
    else:
        ids = None
    result = shadow_backfill(decision_ids=ids, dry_run=args.dry_run, verbose=not args.quiet)
    print(json.dumps(result, ensure_ascii=False, indent=2, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
