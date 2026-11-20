# -*- coding: utf-8 -*-
"""经验库 / 反思触发路由（结构切片，2026-11-17）。

从 api_server.py 搬来 v186 那 8 条端点：经验列表与统计、交易反思列表、
门控反思周期（读 + 跑）、人格统计、经验提炼触发、模拟盘反思触发。
端点内原本都是惰性 import（gate_reflection_worker / experience_reflector /
paper_reflection_worker），原样保留。

注：路径里的 v186 是历史版本号，P3 命名治理时再考虑对外兼容（老路径转发新路径）。
"""
from __future__ import annotations

import sqlite3
from typing import Any, Dict, List, Optional

from fastapi import APIRouter

import db

router = APIRouter()


@router.get("/api/v186/experiences")
def list_experiences(
    kind: Optional[str] = None,
    status: Optional[str] = "active",
    asset: Optional[str] = None,
    actor: Optional[str] = None,
    limit: int = 100,
):
    """列出 v186 经验库 experience_entries;可按 kind/status/asset/actor 过滤。"""
    conn = db.get_connection()
    try:
        sql = "SELECT id, kind, title, content, applicable_assets, applicable_kinds, " \
              "applicable_actors, weight, priority, source, status, apply_count, " \
              "hit_count, last_applied_at, created_at " \
              "FROM experience_entries WHERE 1=1"
        params: list = []
        if status:
            sql += " AND status = ?"
            params.append(status)
        if kind:
            sql += " AND kind = ?"
            params.append(kind)
        sql += " ORDER BY priority DESC, id ASC LIMIT ?"
        params.append(max(1, min(limit, 500)))
        cur = conn.execute(sql, params)
        cols = [d[0] for d in cur.description]
        rows = [{cols[i]: r[i] for i in range(len(cols))} for r in cur.fetchall()]
        if actor:
            a_low = actor.lower()
            rows = [r for r in rows
                    if r.get("applicable_actors") and a_low in str(r["applicable_actors"]).lower()]
        if asset:
            a_up = asset.upper()
            rows = [r for r in rows
                    if r.get("applicable_assets") and a_up in str(r["applicable_assets"]).upper()]
    finally:
        conn.close()
    return {"items": rows, "count": len(rows)}


@router.get("/api/v186/experiences/stats")
def experiences_stats():
    """经验库统计:按 kind/status 分组 + 总数/平均权重。

    v337 fix: db.get_connection() 不设 row_factory（默认返回 tuple），
    原实现直接 `dict(r)` → TypeError: cannot convert dictionary update
    sequence element #0 to a sequence → 该端点一直 500。经验库空的时候
    没人发现，经验一入库前端就白屏。这里显式设 Row。
    """
    conn = db.get_connection()
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute("""
            SELECT kind, status, COUNT(*) AS n,
                   ROUND(AVG(weight), 3) AS avg_weight,
                   ROUND(AVG(priority), 1) AS avg_priority,
                   SUM(apply_count) AS total_apply,
                   SUM(hit_count) AS total_hit
              FROM experience_entries
             GROUP BY kind, status
             ORDER BY kind, status
        """).fetchall()
        items = [dict(r) for r in rows]
        for it in items:
            apply_n = it.get("total_apply") or 0
            it["hit_rate"] = round((it.get("total_hit") or 0) / apply_n * 100, 1) if apply_n else None
        total = conn.execute("SELECT COUNT(*) FROM experience_entries").fetchone()[0]
    finally:
        conn.close()
    return {"items": items, "total": total}


@router.get("/api/v186/trade-reflections")
def list_trade_reflections(
    asset: Optional[str] = None,
    layer: Optional[str] = None,
    min_depth: int = 0,
    limit: int = 50,
):
    """列出 v186 trade_reflections_v2 记录(模拟实盘反思三件套)。"""
    conn = db.get_connection()
    try:
        # v231+ (H5): LEFT JOIN quality_rejections 暴露 is_dirty 标记, 前端可标记
        # "基于脏数据的反思" 视觉警告. 用 EXISTS 而非 IN 避免大集合.
        sql = "SELECT r.id, r.decision_id, r.paper_run_id, r.asset, r.action, " \
              "r.pnl_pct, r.mfe_pct, r.mae_pct, r.settled_at, " \
              "r.error_layer, r.error_sub_layer, r.error_signal, " \
              "r.root_cause, r.lesson, r.depth_score, r.applied_experience_ids, " \
              "r.reasoning_chain, r.created_at, " \
              "CASE WHEN EXISTS (SELECT 1 FROM quality_rejections qr " \
              "                   WHERE qr.decision_id = r.decision_id) " \
              "     THEN 1 ELSE 0 END AS is_decision_dirty " \
              "FROM trade_reflections_v2 r WHERE 1=1"
        params: list = []
        if asset:
            sql += " AND asset = ?"
            params.append(asset.upper())
        if layer:
            sql += " AND error_layer = ?"
            params.append(layer)
        sql += " AND depth_score >= ?"
        params.append(max(0, min(min_depth, 10)))
        sql += " ORDER BY id DESC LIMIT ?"
        params.append(max(1, min(limit, 500)))
        cur = conn.execute(sql, params)
        cols = [d[0] for d in cur.description]
        rows = [{cols[i]: r[i] for i in range(len(cols))} for r in cur.fetchall()]
    finally:
        conn.close()
    return {"items": rows, "count": len(rows)}


@router.get("/api/v186/gate-reflection-cycle")
def list_gate_reflection_cycle(
    asset: Optional[str] = None,
    limit: int = 50,
):
    """列出 gate_reflections_cycle 周期汇总(每 30min 一行)。"""
    conn = db.get_connection()
    try:
        sql = "SELECT id, asset, cycle_ts, cycle_iso, days, " \
              "n_judged, n_decided, n_win, n_loss, precision, " \
              "n_abnormal, n_covered, n_miss, recall, created_at " \
              "FROM gate_reflections_cycle WHERE 1=1"
        params: list = []
        if asset:
            sql += " AND asset = ?"
            params.append(asset.upper())
        sql += " ORDER BY id DESC LIMIT ?"
        params.append(max(1, min(limit, 500)))
        cur = conn.execute(sql, params)
        cols = [d[0] for d in cur.description]
        rows = [{cols[i]: r[i] for i in range(len(cols))} for r in cur.fetchall()]
    finally:
        conn.close()
    return {"items": rows, "count": len(rows)}


@router.get("/api/v186/persona-stats")
def persona_stats(asset: Optional[str] = None, limit: int = 20):
    """按 actor (via persona_match_id → experience_entries) 聚合 ai_decisions 命中。"""
    conn = db.get_connection()
    try:
        sql = """
            SELECT e.id AS experience_id,
                   e.title,
                   e.applicable_actors AS actors_json,
                   COUNT(a.id) AS apply_n,
                   SUM(CASE WHEN a.is_correct='WIN' THEN 1 ELSE 0 END) AS win_n,
                   SUM(CASE WHEN a.is_correct='LOSS' THEN 1 ELSE 0 END) AS loss_n,
                   ROUND(AVG(a.persona_weight), 3) AS avg_weight,
                   ROUND(AVG(a.forward_pnl), 3) AS avg_pnl_pct
              FROM experience_entries e
         LEFT JOIN ai_decisions a ON a.persona_match_id = e.id
             WHERE e.kind IN ('persona','government') AND e.status='active'
        """
        params: list = []
        if asset:
            sql += " AND (a.target_asset = ? OR a.target_asset IS NULL)"
            params.append(asset.upper())
        sql += " GROUP BY e.id ORDER BY apply_n DESC LIMIT ?"
        params.append(max(1, min(limit, 100)))
        rows = [dict(r) for r in conn.execute(sql, params).fetchall()]
        for r in rows:
            apply_n = r.get("apply_n") or 0
            r["win_rate"] = round((r.get("win_n") or 0) / apply_n * 100, 1) if apply_n else None
    finally:
        conn.close()
    return {"items": rows, "count": len(rows)}


@router.post("/api/v186/experience-reflect/run")
async def trigger_experience_reflect(asset: Optional[str] = None):
    """手动触发经验自适应评审。"""
    try:
        import experience_reflector as _er
        if asset:
            exps = _er.load_experiences(asset=asset, top_k=20)
        else:
            exps = _er.load_experiences(top_k=50)
        return {"triggered": True, "asset": asset or "ALL", "candidates": len(exps)}
    except Exception as exc:
        return {"triggered": False, "error": f"{type(exc).__name__}: {exc}"}


@router.post("/api/v186/gate-reflection-cycle/run")
async def trigger_gate_reflection_cycle(asset: Optional[str] = None):
    """手动触发门卫反思周期(立即跑某品种或全量)。"""
    try:
        from review import gate_reflection_worker as _grw
        if asset:
            summary = _grw.run_once(asset.upper())
        else:
            summary = _grw.main()
        return {"triggered": True, "summary": summary or {}}
    except Exception as exc:
        return {"triggered": False, "error": f"{type(exc).__name__}: {exc}"}


@router.post("/api/v186/paper-reflection/run")
async def trigger_paper_reflection():
    """手动触发模拟实盘反思。"""
    try:
        import paper_reflection_worker as _prw
        n = _prw.main()
        return {"triggered": True, "inserted": n}
    except Exception as exc:
        return {"triggered": False, "error": f"{type(exc).__name__}: {exc}"}
