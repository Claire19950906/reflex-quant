# -*- coding: utf-8 -*-
"""信号权重路由（结构切片，2026-11-17）。

从 api_server.py 搬来 v20251014 的 4 条 /signal-weights 端点：
list / stats / recompute / {decision_id}。顺序照原样保留 —— /stats 与
/recompute 必须早于 /{decision_id}，否则会被当成 decision_id 解析失败。
逻辑逐字未改。
"""
from __future__ import annotations

import json
import sqlite3
import sys
import time
from typing import List, Optional

from fastapi import APIRouter
from pydantic import BaseModel

import db

router = APIRouter()


@router.get("/api/signal-weights", tags=["signal-weight"])
def list_signal_weights(
    asset: str = "",
    hours: int = 24,
    limit: int = 50,
    tier: str = "",  # strong / medium / weak / ignored (optional filter)
):
    """List recent decisions with their computed signal_weight."""
    conn = db.get_connection()
    conn.row_factory = sqlite3.Row
    try:
        try:
            cols = {r["name"] for r in conn.execute("PRAGMA table_info(ai_decisions)").fetchall()}
        except Exception:
            cols = set()
        if "signal_weight" not in cols:
            return {"error": "signal_weight columns not yet migrated; run migrate()", "rows": []}
        cutoff = int(time.time()) - max(1, min(720, int(hours))) * 3600
        sql = """SELECT id, news_id, target_asset, suggested_action, sentiment_score,
                        signal_weight, weight_tier, weight_explain, weight_updated_at,
                        cold_signal, ts, extra_models_consensus, regime_at_decision
                 FROM ai_decisions
                 WHERE ts >= ? AND is_sibling = 0
                       AND suggested_action IN ('BUY', 'SELL')"""
        params = [cutoff]
        if asset:
            sql += " AND target_asset = ?"
            params.append(asset.upper())
        if tier:
            sql += " AND weight_tier = ?"
            params.append(tier.lower())
        sql += " ORDER BY signal_weight DESC, ts DESC LIMIT ?"
        params.append(max(1, min(500, int(limit))))
        rows = conn.execute(sql, params).fetchall()
        return [
            {
                "decision_id": int(r["id"]),
                "news_id": int(r["news_id"]) if r["news_id"] else None,
                "asset": r["target_asset"] or "",
                "action": r["suggested_action"] or "",
                "score": r["sentiment_score"],
                "signal_weight": r["signal_weight"],
                "weight_tier": r["weight_tier"] or "",
                "weight_explain": r["weight_explain"] or "",
                "cold_signal": bool(r["cold_signal"]),
                "weight_updated_at": r["weight_updated_at"],
                "ts": r["ts"],
                "consensus": r["extra_models_consensus"] or "",
                "regime": r["regime_at_decision"] or "",
            }
            for r in rows
        ]
    finally:
        conn.close()


@router.get("/api/signal-weights/stats", tags=["signal-weight"])
def signal_weight_stats():
    """Aggregate stats: distribution by tier, source_stats leaderboard, cold counts.

    Note: declared BEFORE the {decision_id} route so /stats is not captured as
    decision_id="stats" (which would 422 on int parse).
    """
    conn = db.get_connection()
    conn.row_factory = sqlite3.Row
    try:
        try:
            cols = {r["name"] for r in conn.execute("PRAGMA table_info(ai_decisions)").fetchall()}
        except Exception:
            cols = set()
        if "signal_weight" not in cols:
            return {"error": "signal_weight columns not yet migrated"}
        cutoff = int(time.time()) - 24 * 3600
        tier_rows = conn.execute(
            """SELECT weight_tier, COUNT(*) as n,
                      AVG(signal_weight) as avg_weight
               FROM ai_decisions
               WHERE ts >= ? AND is_sibling = 0
                     AND signal_weight IS NOT NULL
               GROUP BY weight_tier""",
            (cutoff,),
        ).fetchall()
        tiers = {r["weight_tier"]: {"n": int(r["n"]), "avg_weight": r["avg_weight"]} for r in tier_rows}
        cold_n = conn.execute(
            """SELECT COUNT(*) as n FROM ai_decisions
               WHERE ts >= ? AND is_sibling = 0 AND cold_signal=1""",
            (cutoff,),
        ).fetchone()["n"]
        try:
            src_rows = conn.execute(
                """SELECT news_source, asset, sample_n, win_n, loss_n,
                          hit_rate, source_mult, avg_forward_pnl
                   FROM source_stats
                   WHERE sample_n >= 5
                   ORDER BY hit_rate DESC LIMIT 10""",
            ).fetchall()
            sources = [dict(r) for r in src_rows]
        except sqlite3.OperationalError:
            sources = []
        try:
            last_run = conn.execute(
                """SELECT started_at, finished_at, processed_n, updated_n, cold_n, error_msg
                   FROM signal_weight_cron_log ORDER BY id DESC LIMIT 1""",
            ).fetchone()
            last_run_dict = dict(last_run) if last_run else None
        except sqlite3.OperationalError:
            last_run_dict = None
        return {
            "window_hours": 24,
            "tier_distribution": tiers,
            "cold_signal_count_24h": int(cold_n),
            "top_sources": sources,
            "last_cron_run": last_run_dict,
        }
    finally:
        conn.close()


@router.post("/api/signal-weights/recompute", tags=["signal-weight"])
async def force_recompute_signal_weights(hours: int = 24):
    """Force a synchronous batch recompute (returns immediately with stats)."""
    from signals import signal_weight_cron as _swc
    stats = _swc.run_once(hours=max(1, min(720, int(hours))))
    return {"ok": True, "stats": stats}


# ── 人工覆盖 (v500) ────────────────────────────────────────────────────────
# 验收 #3「信号权重人工可调」的写入口。设计原则：只允许**乘法**，必须写理由和
# 操作人，默认不自动失效但可撤销；每次决策的 explain 会留 "manual_override ×1.2
# (#3 by claire: ...)"，事后能分清"哪条是人压的、哪条是模型算的"。
#
# 路由顺序：这三条必须排在 /api/signal-weights/{decision_id} 之前，否则
# "overrides" 会被当成 decision_id 解析成 422。
class WeightOverrideBody(BaseModel):
    asset: str = ""          # '' = 全品种
    action: str = ""         # '' = BUY/SELL 都生效
    multiplier: float
    reason: str
    actor: str
    ttl_hours: int | None = None


def _override_out(row) -> dict:
    d = dict(row)
    d["multiplier"] = float(d.get("multiplier") or 0)
    d["active"] = bool(d.get("active"))
    return d


@router.post("/api/signal-weights/override", tags=["signal-weight"])
def create_weight_override(body: WeightOverrideBody):
    """新建一个人工权重覆盖（乘法系数）。"""
    from signals import signal_weight_evaluator as swe

    asset = (body.asset or "").strip().upper()
    action = (body.action or "").strip().upper()
    reason = (body.reason or "").strip()
    actor = (body.actor or "").strip()
    if action not in ("", "BUY", "SELL"):
        return {"ok": False, "error": "action 只能是 '' / BUY / SELL"}
    if not (swe.OVERRIDE_MIN <= float(body.multiplier) <= swe.OVERRIDE_MAX):
        return {"ok": False,
                "error": f"multiplier 必须落在 {swe.OVERRIDE_MIN}~{swe.OVERRIDE_MAX}"}
    if len(reason) < 4:
        return {"ok": False, "error": "reason 太短：至少写清楚为什么调（≥4 字）"}
    if not actor:
        return {"ok": False, "error": "actor 必填：谁调的"}
    now = int(time.time())
    expires = now + int(body.ttl_hours) * 3600 if body.ttl_hours else None
    conn = db.get_connection()
    conn.row_factory = sqlite3.Row
    try:
        swe.ensure_override_table(conn)
        cur = conn.execute(
            """INSERT INTO signal_weight_overrides
               (asset, action, multiplier, reason, actor, created_at, expires_at, active)
               VALUES (?,?,?,?,?,?,?,1)""",
            (asset, action, round(float(body.multiplier), 4), reason, actor, now, expires),
        )
        conn.commit()
        new_id = int(cur.lastrowid)
        # 审计日志（表在则写，不在也不阻断）
        try:
            conn.execute(
                "INSERT INTO audit_log (actor, action, target, detail) VALUES (?,?,?,?)",
                (actor, "signal_weight_override.create", f"override#{new_id}",
                 json.dumps({"asset": asset, "action": action,
                             "multiplier": float(body.multiplier),
                             "ttl_hours": body.ttl_hours, "reason": reason},
                            ensure_ascii=False)),
            )
            conn.commit()
        except sqlite3.Error as exc:
            # 审计行失败不能吞：覆盖本身已落库，但"谁在什么时候调的"必须留痕
            sys.stderr.write(f"[signal_weight_override] audit_log 写入失败 override#{new_id}: {exc}\n")
        row = conn.execute("SELECT * FROM signal_weight_overrides WHERE id=?",
                           (new_id,)).fetchone()
        return {"ok": True, "override": _override_out(row)}
    finally:
        conn.close()


@router.get("/api/signal-weights/overrides", tags=["signal-weight"])
def list_weight_overrides(active_only: int = 1, limit: int = 50):
    conn = db.get_connection()
    conn.row_factory = sqlite3.Row
    try:
        from signals import signal_weight_evaluator as swe
        if not swe._has_override_table(conn):
            return {"ok": True, "rows": [], "note": "还没有任何人工覆盖记录"}
        now = int(time.time())
        sql = "SELECT * FROM signal_weight_overrides"
        params: list = []
        if active_only:
            sql += " WHERE active=1 AND revoked_at IS NULL AND (expires_at IS NULL OR expires_at > ?)"
            params.append(now)
        sql += " ORDER BY id DESC LIMIT ?"
        params.append(max(1, min(500, int(limit))))
        rows = [_override_out(r) for r in conn.execute(sql, params).fetchall()]
        return {"ok": True, "rows": rows, "now": now}
    finally:
        conn.close()


@router.post("/api/signal-weights/override/{override_id}/revoke", tags=["signal-weight"])
def revoke_weight_override(override_id: int, actor: str = ""):
    """撤销一条人工覆盖（保留行，只置 revoked，便于事后归因）。"""
    if not (actor or "").strip():
        return {"ok": False, "error": "actor 必填：谁撤销的"}
    conn = db.get_connection()
    conn.row_factory = sqlite3.Row
    try:
        from signals import signal_weight_evaluator as swe
        if not swe._has_override_table(conn):
            return {"ok": False, "error": "没有任何人工覆盖记录"}
        row = conn.execute("SELECT * FROM signal_weight_overrides WHERE id=?",
                           (int(override_id),)).fetchone()
        if not row:
            return {"ok": False, "error": "override not found"}
        if row["revoked_at"]:
            return {"ok": True, "override": _override_out(row), "note": "已撤销过"}
        now = int(time.time())
        conn.execute(
            "UPDATE signal_weight_overrides SET active=0, revoked_at=?, revoked_by=? WHERE id=?",
            (now, actor.strip(), int(override_id)),
        )
        conn.commit()
        try:
            conn.execute(
                "INSERT INTO audit_log (actor, action, target, detail) VALUES (?,?,?,?)",
                (actor.strip(), "signal_weight_override.revoke", f"override#{int(override_id)}",
                 json.dumps({"multiplier": row["multiplier"], "asset": row["asset"],
                             "action": row["action"]}, ensure_ascii=False)),
            )
            conn.commit()
        except sqlite3.Error as exc:
            sys.stderr.write(f"[signal_weight_override] audit_log 写入失败 revoke#{int(override_id)}: {exc}\n")
        row = conn.execute("SELECT * FROM signal_weight_overrides WHERE id=?",
                           (int(override_id),)).fetchone()
        return {"ok": True, "override": _override_out(row)}
    finally:
        conn.close()


@router.get("/api/signal-weights/{decision_id}", tags=["signal-weight"])
def get_signal_weight(decision_id: int):
    """Get full signal_weight breakdown (all 6 dims + history) for one decision.

    Note: declared AFTER /stats and /recompute so those static routes match
    first instead of being captured as decision_id.
    """
    conn = db.get_connection()
    conn.row_factory = sqlite3.Row
    try:
        try:
            cols = {r["name"] for r in conn.execute("PRAGMA table_info(ai_decisions)").fetchall()}
        except Exception:
            cols = set()
        if "signal_weight" not in cols:
            return {"error": "signal_weight columns not yet migrated"}
        row = conn.execute(
            """SELECT id, news_id, target_asset, suggested_action, sentiment_score,
                      signal_weight, weight_tier, weight_explain, weight_updated_at,
                      cold_signal, ts, extra_models_consensus, regime_at_decision,
                      impact_magnitude, heat_score, expected_horizon
               FROM ai_decisions WHERE id=?""",
            (int(decision_id),),
        ).fetchone()
        if not row:
            return {"error": "decision not found", "decision_id": int(decision_id)}
        try:
            hist = conn.execute(
                """SELECT computed_at, trigger_kind, signal_weight, weight_tier,
                          base_mult, consensus_mult, source_mult, reaction_mult,
                          regime_mult, time_mult, explain, abnormal_id
                   FROM signal_weight_history
                   WHERE decision_id=?
                   ORDER BY computed_at DESC LIMIT 20""",
                (int(decision_id),),
            ).fetchall()
            history = [dict(h) for h in hist]
        except sqlite3.OperationalError:
            history = []
        return {
            "decision_id": int(row["id"]),
            "news_id": int(row["news_id"]) if row["news_id"] else None,
            "asset": row["target_asset"] or "",
            "action": row["suggested_action"] or "",
            "score": row["sentiment_score"],
            "impact_magnitude": row["impact_magnitude"],
            "heat_score": row["heat_score"],
            "expected_horizon": row["expected_horizon"] or "",
            "regime": row["regime_at_decision"] or "",
            "consensus": row["extra_models_consensus"] or "",
            "signal_weight": row["signal_weight"],
            "weight_tier": row["weight_tier"] or "",
            "weight_explain": row["weight_explain"] or "",
            "weight_updated_at": row["weight_updated_at"],
            "cold_signal": bool(row["cold_signal"]),
            "ts": row["ts"],
            "history": history,
        }
    finally:
        conn.close()
