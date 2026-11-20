# -*- coding: utf-8 -*-
"""reflection 反思深度增强路由（结构切片，2026-11-17）。

从 api_server.py 搬来 v230 那一整段 8 条 /api/reflection/deep|meta-loop|
reasoning-logic-blindspot 端点。逻辑逐字未改，只把 `@app.` 换成 `@router.`。
点内保持原来的惰性 import（reflection_deep 等较重），避免拖慢启动。
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
import sqlite3
from datetime import datetime, timedelta, timezone
import time
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

import db

router = APIRouter()


# v230 反思系统深度增强 — 6 个端点(K线/跨资产/抢跑/运气 vs 信号/自循环)
# 不改 ai_worker / reflection_ontology / db.py;只读启发式 + 写 deep_root_cause
# ─────────────────────────────────────────────────────────────────────────────

@router.get("/api/reflection/deep/enrich/{reflection_id}", tags=["reflection-deep"])
async def reflection_deep_enrich(reflection_id: int):
    """对单条反思做 v230 深度增强(K线/跨资产/抢跑/运气 vs 信号/深度根因)."""
    try:
        from review import reflection_deep as _rd
        out = _rd.enrich_reflection(int(reflection_id), write=True)
        return out
    except Exception as exc:
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}


@router.post("/api/reflection/deep/enrich-pending", tags=["reflection-deep"])
async def reflection_deep_enrich_pending(payload: Optional[dict] = None):
    """批量对 pending 反思做深度增强."""
    try:
        from review import reflection_deep as _rd
        p = payload or {}
        out = _rd.enrich_pending_loop(
            batch_size=int(p.get("batch_size", 100)),
            max_per_run=int(p.get("max_per_run", 200)),
        )
        return out
    except Exception as exc:
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}


@router.get("/api/reflection/deep/stats", tags=["reflection-deep"])
async def reflection_deep_stats(window_days: int = 30):
    """v230 深度反思统计(按 pattern/luck/front_run/cross_sync 分桶的 win_rate)."""
    try:
        from review import reflection_deep as _rd
        return _rd.get_reflection_deep_stats(window_days=int(window_days))
    except Exception as exc:
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}


@router.post("/api/reflection/meta-loop/run", tags=["reflection-deep"])
async def reflection_meta_loop_run(payload: Optional[dict] = None):
    """自循环反思:对近 N 天每个 signal 重新评估(confirmed/refined/invalidated/unstable_luck)."""
    try:
        import meta_reflection_loop as _mrl
        p = payload or {}
        out = _mrl.run_meta_loop(
            window_days=int(p.get("window_days", 7)),
            compare_days=int(p.get("compare_days", 14)),
            max_level=int(p.get("max_level", 2)),
        )
        return out
    except Exception as exc:
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}


@router.get("/api/reflection/meta-loop/stats", tags=["reflection-deep"])
async def reflection_meta_loop_stats(window_days: int = 30):
    """自循环反思统计(by finding_kind / by level / 失效信号 Top)."""
    try:
        import meta_reflection_loop as _mrl
        return _mrl.get_meta_loop_stats(window_days=int(window_days))
    except Exception as exc:
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}


@router.post("/api/reflection/reasoning-logic-blindspot/detect", tags=["reflection-deep"])
async def reflection_reasoning_logic_blindspot_detect(payload: Optional[dict] = None):
    """v225+ reasoning_logic 盲区检测 — 7 checker 全未命中但 trade 错的案例.

    Returns: {ok, blindspot_count, sample_decisions, meta_md_appended, cycle_written}
    """
    try:
        import meta_reflection_loop as _mrl
        p = payload or {}
        return _mrl.detect_reasoning_logic_blindspots(
            window_days=int(p.get("window_days", 7)),
            min_cases=int(p.get("min_cases", 3)),
            write_cycle=bool(p.get("write_cycle", True)),
        )
    except Exception as exc:
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}


@router.get("/api/reflection/reasoning-logic-blindspot/scan", tags=["reflection-deep"])
async def reflection_reasoning_logic_blindspot_scan(window_days: int = 7, min_cases: int = 3):
    """v225+ reasoning_logic 盲区 dry-run — 只扫不写, 用于 UI 预览."""
    try:
        import meta_reflection_loop as _mrl
        return _mrl.detect_reasoning_logic_blindspots(
            window_days=int(window_days), min_cases=int(min_cases), write_cycle=False)
    except Exception as exc:
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}


@router.get("/api/reflection/deep/{reflection_id}", tags=["reflection-deep"])
def reflection_deep_get(reflection_id: int):
    """取一条 trade_reflections_v2 的 v230 13 个新字段详情."""
    try:
        import db as _db
        conn = _db.get_connection()
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            """
            SELECT id, decision_id, asset, action, pnl_pct, mfe_pct, mae_pct,
                   kline_at_decision, kline_pattern, cross_asset_sync_score,
                   front_run_score, luck_vs_signal_score, market_regime_detail,
                   deep_root_cause, deep_lesson,
                   secondary_reflection_id, secondary_reflection_at,
                   enriched_at, enrich_status, enrich_error
              FROM trade_reflections_v2 WHERE id = ?
            """,
            (int(reflection_id),),
        ).fetchone()
        if not row:
            conn.close()
            return {"ok": False, "error": "not_found"}
        d = dict(row)
        # Parse JSON fields
        for k in ("kline_at_decision", "market_regime_detail"):
            v = d.get(k)
            if v:
                try:
                    d[k] = json.loads(v)
                except (json.JSONDecodeError, TypeError):
                    pass
        conn.close()
        d["ok"] = True
        return d
    except Exception as exc:
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}


# ── v188：反思系统 CRUD + 迟到信号 + 反思统计 + 单决策诊断 ──────────────────

# ══════════════════════════════════════════════════════════════════
# v188: 反思系统 CRUD + missed-signal + reflection stats
# ──────────────────────────────────────────────────────────────────


class V188AddExperienceReq(BaseModel):
    kind: str = Field(..., description="knowledge/persona/government/essence/process/correction")
    title: str
    content: str
    applicable_assets: list[str] = []
    applicable_kinds: list[str] = []
    applicable_actors: list[str] = []
    weight: float = 1.0
    priority: int = 50
    source: str = "manual_v188"


class V188UpdateExperienceReq(BaseModel):
    kind: Optional[str] = None
    title: Optional[str] = None
    content: Optional[str] = None
    applicable_assets: Optional[list[str]] = None
    applicable_kinds: Optional[list[str]] = None
    applicable_actors: Optional[list[str]] = None
    weight: Optional[float] = None
    priority: Optional[int] = None
    status: Optional[str] = None


@router.post("/api/v188/experiences/add")
def v188_add_experience(req: V188AddExperienceReq):
    """v188: 新增经验。返回新经验 id。"""
    import json as _json
    conn = db.get_connection()
    try:
        cur = conn.execute("""
            INSERT INTO experience_entries
                (kind, title, content,
                 applicable_assets, applicable_kinds, applicable_actors,
                 weight, priority, source, status, apply_count, hit_count,
                 last_applied_at, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'active', 0, 0, '', datetime('now'), datetime('now'))
        """, (
            req.kind, req.title, req.content,
            _json.dumps(req.applicable_assets, ensure_ascii=False),
            _json.dumps(req.applicable_kinds, ensure_ascii=False),
            _json.dumps(req.applicable_actors, ensure_ascii=False),
            max(0.1, min(1.0, req.weight)),
            max(1, min(100, req.priority)),
            req.source,
        ))
        conn.commit()
        return {"ok": True, "id": int(cur.lastrowid)}
    except sqlite3.IntegrityError as exc:
        return {"ok": False, "error": f"UNIQUE 冲突 (source+title): {exc}"}
    except Exception as exc:
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
    finally:
        conn.close()


@router.post("/api/v188/experiences/{exp_id}/update")
def v188_update_experience(exp_id: int, req: V188UpdateExperienceReq):
    """v188: 部分字段更新经验。None 字段不动。"""
    import json as _json
    updates = []
    params: list = []
    if req.kind is not None:
        updates.append("kind=?"); params.append(req.kind)
    if req.title is not None:
        updates.append("title=?"); params.append(req.title)
    if req.content is not None:
        updates.append("content=?"); params.append(req.content)
    if req.applicable_assets is not None:
        updates.append("applicable_assets=?")
        params.append(_json.dumps(req.applicable_assets, ensure_ascii=False))
    if req.applicable_kinds is not None:
        updates.append("applicable_kinds=?")
        params.append(_json.dumps(req.applicable_kinds, ensure_ascii=False))
    if req.applicable_actors is not None:
        updates.append("applicable_actors=?")
        params.append(_json.dumps(req.applicable_actors, ensure_ascii=False))
    if req.weight is not None:
        updates.append("weight=?"); params.append(max(0.1, min(1.0, req.weight)))
    if req.priority is not None:
        updates.append("priority=?"); params.append(max(1, min(100, req.priority)))
    if req.status is not None:
        if req.status not in ("active", "archived", "draft"):
            return {"ok": False, "error": "status 必须是 active/archived/draft"}
        updates.append("status=?"); params.append(req.status)
    if not updates:
        return {"ok": False, "error": "至少传一个字段"}
    updates.append("updated_at=datetime('now')")
    params.append(int(exp_id))
    conn = db.get_connection()
    try:
        cur = conn.execute(
            f"UPDATE experience_entries SET {', '.join(updates)} WHERE id=?",
            params,
        )
        conn.commit()
        return {"ok": True, "id": int(exp_id), "rows_affected": cur.rowcount}
    except Exception as exc:
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
    finally:
        conn.close()


@router.post("/api/v188/experiences/{exp_id}/archive")
def v188_archive_experience(exp_id: int):
    """v188: 归档经验(status='archived')。"""
    conn = db.get_connection()
    try:
        cur = conn.execute(
            "UPDATE experience_entries SET status='archived', updated_at=datetime('now') WHERE id=?",
            (int(exp_id),),
        )
        conn.commit()
        return {"ok": True, "id": int(exp_id), "rows_affected": cur.rowcount}
    except Exception as exc:
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
    finally:
        conn.close()


@router.get("/api/v188/experiences/{exp_id}/stats")
def v188_experience_stats(exp_id: int, days: int = 30):
    """v188: 单条经验统计(应用次数 / 命中 / win_rate / lift / control 对照)。"""
    import json as _json
    days = max(1, min(180, days))
    cutoff_ts = int(time.time()) - days * 86400
    conn = db.get_connection()
    conn.row_factory = sqlite3.Row
    try:
        exp_row = conn.execute("""
            SELECT id, kind, title, content, applicable_assets, applicable_actors,
                   weight, priority, source, status, apply_count, hit_count,
                   last_applied_at, applied_win_rate, control_apply_count,
                   control_win_rate, lift, verified_count, created_at, updated_at
              FROM experience_entries WHERE id=?
        """, (int(exp_id),)).fetchone()
        if not exp_row:
            return {"ok": False, "error": f"experience {exp_id} not found"}
        exp = dict(exp_row)
        app_rows = conn.execute("""
            SELECT COUNT(*) AS n,
                   SUM(CASE WHEN ea.llm_invoked=1 THEN 1 ELSE 0 END) AS llm_invoked_n,
                   AVG(CASE WHEN qs.pnl_pct > 0 THEN 1.0 ELSE 0.0 END) AS win_rate,
                   AVG(qs.pnl_pct) AS avg_pnl,
                   MIN(ad.created_at) AS first_used,
                   MAX(ad.created_at) AS last_used
              FROM experience_applications ea
              JOIN ai_decisions ad ON ad.id = ea.decision_id
              JOIN quick_sim_trades qs ON qs.decision_id = ad.id
             WHERE ea.experience_id=?
               AND ea.applied_at >= datetime('now', ?)
               AND (ea.run_id = '' OR ea.run_id NOT LIKE 'BACKTEST:%')
        """, (int(exp_id), f"-{days} days")).fetchone()
        app_stats = dict(app_rows) if app_rows else {}
        ctrl_row = conn.execute("""
            SELECT COUNT(*) AS n,
                   AVG(CASE WHEN qs.pnl_pct > 0 THEN 1.0 ELSE 0.0 END) AS win_rate,
                   AVG(qs.pnl_pct) AS avg_pnl
              FROM ai_decisions ad
              JOIN quick_sim_trades qs ON qs.decision_id = ad.id
             WHERE qs.settled=1
               AND ad.ts >= ?
               AND (ad.run_id = '' OR ad.run_id NOT LIKE 'BACKTEST:%')
               AND NOT EXISTS (
                   SELECT 1 FROM experience_applications ea
                    WHERE ea.decision_id = ad.id AND ea.experience_id = ?
               )
        """, (cutoff_ts, int(exp_id))).fetchone()
        ctrl_stats = dict(ctrl_row) if ctrl_row else {}
        try:
            exp["applicable_assets"] = _json.loads(exp.get("applicable_assets") or "[]")
            exp["applicable_actors"] = _json.loads(exp.get("applicable_actors") or "[]")
        except Exception:
            pass
        return {
            "ok": True, "experience": exp,
            "applied": app_stats, "control": ctrl_stats,
            "lift": exp.get("lift"),
            "lift_pct": round((exp.get("lift") or 0) * 100, 1),
            "days": days,
        }
    except Exception as exc:
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
    finally:
        conn.close()


@router.post("/api/v188/missed-signal/detect")
async def v188_missed_signal_detect(
    asset: Optional[str] = None, window_hours: int = 24, limit: int = 200,
):
    """v188: 手动触发 missed_signal 自动检测(扫 HOLD 决策 + 异动点对照)。"""
    try:
        import missed_signal_detector as _msd
        stats = _msd.detect_for_recent(asset=asset, window_hours=window_hours, limit=limit)
        return {"ok": True, "stats": stats}
    except Exception as exc:
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}


@router.get("/api/v188/missed-signal/attention-candidates")
def v188_missed_signal_attention_candidates(
    lookback_hours: int = 0, min_platforms: int = 0, move_pct: float = 0.0,
    horizon_minutes: int = 0,
):
    """v317: 漏检信号的**先行**候选 —— 注意力已起、价格未动、系统未出手。

    与既有 missed_signal 的关系（别混淆）：
      · 既有 missed_signal = 事后判定：价格异动了，但当时系统给的是 HOLD；
      · 这里是事前候选：价格还没动，但跨平台注意力已经出现，且系统还没出手。

    两者是互补的，不互相覆盖，也不改写既有 missed_signal 字段的语义。

    注意：本路由必须注册在下面的 `/api/v188/missed-signal/{asset}` **之前** ——
    FastAPI 按注册顺序匹配，路径参数路由会先吞掉 "attention-candidates"。
    """
    try:
        import crowd_attention_lead as _cal
        conn = db.get_connection()
        conn.row_factory = sqlite3.Row
        try:
            res = _cal.pending_attention(
                conn,
                lookback_hours=lookback_hours or None,
                min_platforms=min_platforms or None,
                move_pct=move_pct or None,
                horizon_minutes=horizon_minutes or None,
            )
        finally:
            conn.close()
        return {
            "ok": True,
            "count": len(res.get("pending") or []),
            "candidates": res.get("pending") or [],
            "note": res.get("pending_note") or "",
            "summary": res.get("summary") or {},
        }
    except Exception as exc:
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}


@router.get("/api/v188/missed-signal/{asset}")
async def v188_missed_signal_stats(asset: str, days: int = 7):
    """v188: 某品种最近 N 天 missed_signal 统计 + 漏检案例列表。"""
    try:
        import missed_signal_detector as _msd
        return _msd.stats_for_asset(asset=asset, days=days)
    except Exception as exc:
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}


@router.post("/api/v188/trade-reflections/{reflection_id}/reproduce")
async def v188_reproduce_trade_reflection(reflection_id: int):
    """v188: 重新跑 LLM 深度反思(对单条 trade_reflections_v2)。"""
    try:
        import trade_reflection_llm as _trllm
        return _trllm.reproduce(int(reflection_id))
    except Exception as exc:
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}


@router.post("/api/v188/trade-reflections/enrich-pending")
async def v188_enrich_pending(batch_size: int = 10):
    """v188: 批量补全缺失的 LLM 深度反思。"""
    try:
        import trade_reflection_llm as _trllm
        n = _trllm.enrich_pending_loop(batch_size=batch_size)
        return {"ok": True, "processed": n}
    except Exception as exc:
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}


@router.get("/api/v188/reflection/stats")
def v188_reflection_stats(days: int = 7):
    """v188: 综合反思统计(decision_layer_signals 分布 + 经验 lift 分布)。"""
    days = max(1, min(90, days))
    cutoff_ts = int(time.time()) - days * 86400
    conn = db.get_connection()
    conn.row_factory = sqlite3.Row
    out: Dict[str, Any] = {"days": days, "by_layer": [], "by_signal": [],
                           "lift_top": [], "lift_bottom": []}
    try:
        # 1. 按 layer 聚合
        by_layer = conn.execute("""
            SELECT layer, SUM(n_total) AS n_total,
                   SUM(n_loss) AS n_loss, SUM(n_win) AS n_win
              FROM decision_layer_signals
             WHERE last_seen_ts >= ?
               AND (run_id = '' OR run_id NOT LIKE 'BACKTEST:%')
             GROUP BY layer ORDER BY n_total DESC
        """, (cutoff_ts,)).fetchall()
        for r in by_layer:
            nt = int(r["n_total"] or 0); nl = int(r["n_loss"] or 0)
            d = dict(r)
            d["loss_rate"] = round(nl / nt * 100, 1) if nt else None
            out["by_layer"].append(d)

        # 2. top 10 高频错误信号
        by_signal = conn.execute("""
            SELECT layer, sub_layer, signal_name, asset, action,
                   SUM(n_total) AS n_total, SUM(n_loss) AS n_loss
              FROM decision_layer_signals
             WHERE last_seen_ts >= ?
               AND (run_id = '' OR run_id NOT LIKE 'BACKTEST:%')
             GROUP BY layer, sub_layer, signal_name
             ORDER BY n_loss DESC LIMIT 10
        """, (cutoff_ts,)).fetchall()
        for r in by_signal:
            nt = int(r["n_total"] or 0); nl = int(r["n_loss"] or 0)
            d = dict(r)
            d["loss_rate"] = round(nl / nt * 100, 1) if nt else None
            out["by_signal"].append(d)

        # 3. lift 排行(top/bottom 5)
        lift_top = conn.execute("""
            SELECT id, kind, title, weight, priority,
                   applied_win_rate, control_win_rate, lift, control_apply_count
              FROM experience_entries
             WHERE status='active' AND lift IS NOT NULL
             ORDER BY lift DESC LIMIT 5
        """).fetchall()
        out["lift_top"] = [dict(r) for r in lift_top]
        lift_bot = conn.execute("""
            SELECT id, kind, title, weight, priority,
                   applied_win_rate, control_win_rate, lift, control_apply_count
              FROM experience_entries
             WHERE status='active' AND lift IS NOT NULL
             ORDER BY lift ASC LIMIT 5
        """).fetchall()
        out["lift_bottom"] = [dict(r) for r in lift_bot]

        # 4. missed_signal 概览
        missed = conn.execute("""
            SELECT target_asset, COUNT(*) AS n_missed
              FROM ai_decisions
             WHERE missed_signal=1 AND ts >= ?
             GROUP BY target_asset ORDER BY n_missed DESC
        """, (cutoff_ts,)).fetchall()
        out["missed_signal_by_asset"] = [dict(r) for r in missed]
        out["missed_signal_total"] = sum(int(r["n_missed"]) for r in missed)
    finally:
        conn.close()
    return out


# v188 P1-6: 归因页 ↔ decision_layer_signals 联动
@router.get("/api/v188/decision/{decision_id}/diagnosis")
def v188_decision_diagnosis(decision_id: int):
    """v188 P1-6: 单条 ai_decision 的 82-signal 三层诊断 + 聚合统计。

    返回:
      - decision: ai_decisions 行(摘要)
      - reflection: trade_reflections_v2 行(若有)
      - signals_hit: decision_layer_signals 中匹配 (asset, action) 的聚合行
      - signals_layer_total: 同 (layer, sub_layer, signal_name) 跨 asset 的全样本统计
      - applied_experiences: experience_applications 引用了哪些经验 + 是否 llm_invoked=1
    """
    conn = db.get_connection()
    conn.row_factory = sqlite3.Row
    out: Dict[str, Any] = {"decision_id": int(decision_id), "decision": None,
                            "reflection": None, "signals_hit": [],
                            "signals_layer_total": [], "applied_experiences": []}
    try:
        # 1. ai_decisions 摘要
        drow = conn.execute("""
            SELECT id, target_asset, suggested_action, is_correct, forward_pnl,
                   reasoning, sentiment_score, market_confirmation, event_strength,
                   event_type_code, no_trade_reason, paper_trading_run_id,
                   settled, missed_signal, created_at, ts
              FROM ai_decisions WHERE id = ?
        """, (int(decision_id),)).fetchone()
        if not drow:
            return {"ok": False, "error": f"decision {decision_id} not found"}
        out["decision"] = dict(drow)

        asset = out["decision"]["target_asset"] or "*"
        action = out["decision"]["suggested_action"] or "*"

        # 2. trade_reflections_v2 反思
        rrow = conn.execute("""
            SELECT id, layer, sub_layer, signal_name, root_cause, lesson,
                   verification, depth_score, llm_depth_reflection,
                   verification_window, verified_at,
                   -- v231+ (H5): 同一 connection 内顺带查 quality_rejections 标记
                   CASE WHEN EXISTS (SELECT 1 FROM quality_rejections qr
                                     WHERE qr.decision_id = ?) THEN 1 ELSE 0 END
                        AS is_decision_dirty
              FROM trade_reflections_v2 WHERE decision_id = ?
        """, (int(decision_id), int(decision_id))).fetchone()
        if rrow:
            out["reflection"] = dict(rrow)

        # 3. decision_layer_signals 命中行 (asset+action 维度)
        hit_rows = conn.execute("""
            SELECT layer, sub_layer, signal_name, n_total, n_loss, n_win,
                   last_seen_ts, last_decision_id
              FROM decision_layer_signals
             WHERE asset = ? AND action = ?
               AND (run_id = '' OR run_id NOT LIKE 'BACKTEST:%')
             ORDER BY n_total DESC LIMIT 20
        """, (asset, action)).fetchall()
        out["signals_hit"] = [dict(r) for r in hit_rows]

        # 4. 如果 reflection 有 (layer, sub_layer, signal_name) 跨 asset 统计
        if rrow and rrow["signal_name"]:
            agg_rows = conn.execute("""
                SELECT asset, action, n_total, n_loss, n_win
                  FROM decision_layer_signals
                 WHERE layer=? AND sub_layer=? AND signal_name=?
                   AND (run_id = '' OR run_id NOT LIKE 'BACKTEST:%')
                 ORDER BY n_total DESC LIMIT 20
            """, (rrow["layer"], rrow["sub_layer"], rrow["signal_name"])).fetchall()
            for r in agg_rows:
                nt = int(r["n_total"] or 0); nl = int(r["n_loss"] or 0); nw = int(r["n_win"] or 0)
                d = dict(r)
                d["loss_rate"] = round(nl / nt * 100, 1) if nt else None
                out["signals_layer_total"].append(d)

        # 5. experience_applications 引用 (含 llm_invoked)
        ea_rows = conn.execute("""
            SELECT ea.experience_id, ea.applied_action, ea.applied_at, ea.llm_invoked,
                   ee.title, ee.kind
              FROM experience_applications ea
              LEFT JOIN experience_entries ee ON ee.id = ea.experience_id
             WHERE ea.decision_id = ?
             ORDER BY ea.applied_at DESC
        """, (int(decision_id),)).fetchall()
        out["applied_experiences"] = [dict(r) for r in ea_rows]

        # 6. missed_signal 详情 (如果有)
        out["missed_signal"] = bool(int(drow["missed_signal"] or 0))

        out["ok"] = True
    except Exception as exc:
        out["ok"] = False
        out["error"] = f"{type(exc).__name__}: {exc}"
    finally:
        conn.close()
    return out


# v188+ 全球市场开闭市状态(主交易品种: WTI/XAU/BTC/美股)


# ── 反思错误库 / 聚类 / runner / 统计 / 股票反思通道（2026-11-17 切片）──────
# 从 api_server.py 搬来 7 条端点 + 7 个同步助手 + 3 个 _STOCK_AUDIT_* 常量。
# 逻辑逐字未改。

_STOCK_AUDIT_DEFAULT_LIMIT = 50
_STOCK_AUDIT_DEFAULT_DAYS = 30
_STOCK_AUDIT_ORIGIN = "stock_reflection_panel"


@router.post("/api/reflection/suggest-followups")
async def post_reflection_suggest_followups(body: Dict[str, Any]):
    """v275+: AI 建议追问方向 — 用户可自由挑选，不强制层数。"""
    body = body or {}
    decision_id = int(body.get("decision_id") or 0)
    error_code = str(body.get("error_code") or "")
    current = str(body.get("current_reflection") or "").strip()
    n_suggest = max(2, min(int(body.get("n_suggest") or 3), 4))
    return await asyncio.to_thread(_reflection_suggest_followups_sync,
                                    decision_id, error_code, current, n_suggest)


@router.get("/api/reflection/recent")
async def get_reflection_recent(limit: int = 50, code: Optional[str] = None,
                                origin: Optional[str] = None):
    """反思错误码最近条目 — 来自 review pipeline / reflection runner / llm blindspot 三个通道。

    limit: 取最近多少条（最大 1500；v225 放开上限让用户看全量根因，不限制反思分析数量）
    code: 按 error_code 过滤 (E001~E018)，None=全部
    origin: checker | llm | None=全部

    返回: {total, errors: [{decision_id, error_code, origin, severity, detail,
                            source, evidence_refs, created_at}],
           code_subkinds: {code/sub_kind: count},   # v225: 子原因精细化
           cross_asset_snapshot: {...},            # v225: 跨资产 K 线快照
           prompt_preview}
    """
    limit = min(max(limit, 1), 1500)
    return await asyncio.to_thread(_reflection_recent_sync, limit, code, origin)


@router.get("/api/reflection/clustering")
async def get_reflection_clustering(days: int = 30, min_count: int = 2,
                                    limit_per_group: int = 3):
    """失败模式聚类(PR-17)。

    days: 1~90
    min_count: 单模式最少出现次数(过滤稀疏噪音,默认 2)
    limit_per_group: 每个模式最多给几个 example decision(默认 3,上限 10)
    """
    return await asyncio.to_thread(_reflection_clustering_sync,
                                   days, min_count, limit_per_group)


@router.post("/api/reflection/run")
async def post_reflection_run(body: Dict[str, Any]):
    """触发反思 runner — 对一组 ai_decisions 一次性跑程序化校验器 + LLM 盲区。

    body: {limit?, scope?, decision_id?, skip_llm?, auto_promote?}
    默认 limit=50, scope=recent。
    """
    return await asyncio.to_thread(_reflection_run_sync, body or {})


@router.get("/api/reflection/stats")
async def get_reflection_stats(days: int = 30):
    """反思错误码统计 — 各错误码 / origin / severity 在最近 N 天的分布。

    days: 1-365, 默认 30
    """
    days = min(max(days, 1), 365)
    return await asyncio.to_thread(_reflection_stats_sync, days)


@router.get("/api/reflection/stock-decisions")
async def get_stock_decisions(days: int = _STOCK_AUDIT_DEFAULT_DAYS,
                              limit: int = _STOCK_AUDIT_DEFAULT_LIMIT,
                              include_errors: bool = True):
    """列出最近 N 条 STOCK 类 ai_decisions + 已触发错误码。"""
    days = min(max(days, 1), 365)
    limit = min(max(limit, 1), 200)
    return await asyncio.to_thread(
        _stock_decisions_sync, days, limit, include_errors
    )


@router.post("/api/reflection/stock-audit")
async def post_stock_audit(body: Dict[str, Any]):
    """对 STOCK 类 ai_decisions 批量跑反思 checker（不要求 paper-trading 结算）。

    body: {limit?, days?, decision_ids?: [int]?}
      - decision_ids 提供时，只对指定 ID 跑（增量模式）
      - 不提供则对最近 N 条（默认 50 / 30 天）跑

    返回：{mode, scanned, checker_triggered, inserted, skipped_completeness,
           by_code, decisions:[{id,asset,error_codes,error_count}]}
    """
    return await asyncio.to_thread(_stock_audit_sync, body or {})


def _reflection_suggest_followups_sync(decision_id: int, error_code: str,
                                          current: str, n_suggest: int):
    """同步函数 — 让 LLM 给 2-4 个不同方向的追问建议。

    不让 LLM 直接写反思 — 只生成「追问方向」（问句），由用户决定要不要答。
    体现「不写死公式 + 一事一议 + 反思深度由用户决定」。
    """
    import reasoning_quality_report as _rqr
    conn = db.get_connection()
    try:
        asset = action = ""
        reasoning_excerpt = ""
        if decision_id > 0:
            conn.row_factory = sqlite3.Row
            drow = conn.execute(
                "SELECT target_asset, suggested_action, reasoning_path "
                "FROM ai_decisions WHERE id = ?", (int(decision_id),),
            ).fetchone()
            if drow:
                asset = (drow["target_asset"] or "").upper()
                action = (drow["suggested_action"] or "").upper()
                reasoning_excerpt = (drow["reasoning_path"] or "")[:2000]
        # ─── 构造 LLM prompt — 关键约束:只生成「问句」，不生成「反思」───
        prompt = (
            "你是反思教练。用户正在对一笔已下行的决策做自由反思（可能仅写了 1-2 句，"
            "也可能已写了多段）。请你帮他想出 {} 个不同方向的追问（只输出问句，不要替用户作答）。\n"
            "\n"
            "原则：\n"
            "• 一事一议：不同错误码需要不同深度 —— 简单的(E001 数据缺失)给 1 个就够，"
            "  复杂的(E014 推理链断裂 / E015 概念错位 / E016 跨资产方向错)给 3 个不同方向。\n"
            "• 第一性原理：问句要指向「结论 / 前提 / 推理 / 数据」四件套中的某一环。\n"
            "• 不要问「为什么错了」这种宽泛问句 —— 要问"
            "「为什么看起来那个事件应该是利空/利多」「为什么你会假设跨资产会跟随」"
            "「为什么当时排除了这个反方假设」这种具体的。\n"
            "• 追问方向要多元化：别都问同一个维度（例：不要 3 个全问「假设」） —— "
            "要覆盖 假设 / 事件 / 数据 / 推理链 至少 2 个维度。\n"
            "• 输出 2-{} 个问句即可，不要硬凑 {} 个。\n"
            "\n"
            "———— 输入上下文 ————\n"
            "asset: {asset}\n"
            "action: {action}\n"
            "error_code: {error_code}\n"
            "用户当前已写的反思（可空）：\n"
            "{current}\n"
            "\n"
            "———— 当时 reasoning_path 摘要（2000 字符以内）————\n"
            "{reasoning_excerpt}\n"
            "\n"
            "———— 输出格式（JSON 数组）————\n"
            "返回 JSON 数组，每项是 1 句中文追问，例:[\"问句 1\", \"问句 2\", \"问句 3\"]。\n"
            "不要其他说明。\n"
        ).format(
            n_suggest=n_suggest,
            asset=asset or "?", action=action or "?",
            error_code=error_code or "?",
            current=current[:1500] or "(用户尚未写)",
            reasoning_excerpt=reasoning_excerpt or "(空)",
        )
        out = _rqr.call_llm_for_narrative(prompt)
        text = (out.get("text") or "").strip()
        questions: List[str] = []
        if text:
            try:
                import json as _json
                cleaned = text.strip()
                # LLM 偶尔会包在 ```json ``` 里,先剥掉
                if cleaned.startswith("```"):
                    cleaned = cleaned.strip("`")
                    if cleaned.lower().startswith("json"):
                        cleaned = cleaned[4:]
                parsed = _json.loads(cleaned)
                if isinstance(parsed, list):
                    questions = [str(x).strip() for x in parsed if str(x).strip()]
            except (ValueError, TypeError):
                pass
            if not questions:
                # 容错：按行拆问句（"1. ...\n2. ..." 或 "问句1\n问句2"）
                for line in text.split("\n"):
                    line = line.strip().lstrip("0123456789.、) ").strip()
                    if line and ("?" in line or "？" in line) and len(line) >= 8:
                        questions.append(line)
                    if len(questions) >= n_suggest:
                        break
        return {
            "decision_id": decision_id,
            "error_code": error_code,
            "questions": questions[:n_suggest],
            "model": out.get("model", ""),
            "error": out.get("error", "") if not questions else "",
        }
    finally:
        conn.close()


def _reflection_recent_sync(limit: int, code: Optional[str], origin: Optional[str]):
    from review import reflection_checkers as _rc
    errors = _rc.get_recent_errors(error_codes=[code] if code else None,
                                   limit=limit)
    if origin:
        errors = [e for e in errors if (e.get("origin") or "") == origin]
    # 统计: 各错误码出现次数
    code_stats: Dict[str, int] = {}
    origin_stats: Dict[str, int] = {"checker": 0, "llm": 0}
    severity_stats: Dict[str, int] = {"1": 0, "2": 0, "3": 0}
    for e in errors:
        c = e.get("error_code") or "?"
        code_stats[c] = code_stats.get(c, 0) + 1
        o = e.get("origin") or "checker"
        origin_stats[o] = origin_stats.get(o, 0) + 1
        s = str(e.get("severity") or 0)
        severity_stats[s] = severity_stats.get(s, 0) + 1
    # prompt 注入预览
    prompt_preview = _rc.format_errors_for_prompt(errors[:8])
    # v225: 跨资产 K 线快照 — 反思时不只盯当前品种，要看全市场形态
    cross_asset = {}
    try:
        import cross_asset_context as _cac
        from datetime import datetime as _dt, timezone as _tz
        # 用 errors 里最近一条 decision 的时间 + asset
        anchor_ts = None
        anchor_asset = "BTC"
        if errors:
            ts_raw = errors[0].get("created_at")
            # decision_errors.created_at 是 SQLite 'YYYY-MM-DD HH:MM:SS' 字符串
            try:
                anchor_ts = _dt.strptime(str(ts_raw), "%Y-%m-%d %H:%M:%S").replace(tzinfo=_tz.utc).isoformat()
            except Exception:
                anchor_ts = _dt.now(_tz.utc).isoformat()
        snap = _cac.cross_asset_snapshot(anchor_asset, ts_iso=anchor_ts)
        # 截短 symbols 详情防 payload 过大
        snap_light = {
            "anchor_ts": snap["anchor_ts"],
            "asset": snap["asset"],
            "macro_regime": snap["macro_regime"],
            "risk_off_count": snap["risk_off_count"],
            "risk_on_count": snap["risk_on_count"],
            "total_symbols": snap["total_symbols"],
            "warning": snap["warning"],
            "pct_only": snap["pct_only"],
        }
        cross_asset = snap_light
    except Exception as e:
        cross_asset = {"error": f"cross_asset snapshot failed: {e}"}
    # v225: 子原因精细化分解（把 E001 系列展开为细分子码，便于用户看真因）
    code_subkinds: Dict[str, int] = {}
    for e in errors:
        refs = e.get("evidence_refs") or {}
        if isinstance(refs, str):
            try:
                import json as _json
                refs = _json.loads(refs)
            except Exception:
                refs = {}
        sk = refs.get("sub_kind") if isinstance(refs, dict) else None
        if sk:
            key = f"{e.get('error_code')}/{sk}"
            code_subkinds[key] = code_subkinds.get(key, 0) + 1
    # v232.5 P0+: 错误码 → 累计 pnl 影响。让用户看到"这些错误码累计让你亏了多少期望值"。
    # 算法: JOIN decision_errors ↔ ai_decisions，过滤 settled=1，GROUP BY error_code
    #       累计 total_pnl / avg_pnl / n_settled / per-code 期望值贡献
    code_pnl_impact: List[Dict[str, Any]] = []
    try:
        conn = db.get_connection()
        conn.row_factory = sqlite3.Row
        try:
            rows = conn.execute(
                """
                SELECT
                  e.error_code                         AS code,
                  COUNT(DISTINCT e.decision_id)        AS n_errors,
                  COUNT(DISTINCT CASE WHEN d.settled = 1 THEN e.decision_id END) AS n_settled,
                  -- 同一个 decision 可能被打多个 error_code — 这里按"被这个 code 标记过"的决策平均 pnl
                  AVG(CASE WHEN d.settled = 1 AND d.forward_pnl IS NOT NULL THEN d.forward_pnl END) AS avg_pnl,
                  SUM(CASE WHEN d.settled = 1 AND d.forward_pnl IS NOT NULL THEN d.forward_pnl ELSE 0 END) AS total_pnl,
                  AVG(CASE WHEN d.settled = 1 AND d.forward_pnl > 0 THEN 1.0 ELSE 0.0 END) AS win_rate_when_coded,
                  MIN(CASE WHEN d.settled = 1 AND d.forward_pnl IS NOT NULL THEN d.forward_pnl END) AS worst_pnl,
                  MAX(CASE WHEN d.settled = 1 AND d.forward_pnl IS NOT NULL THEN d.forward_pnl END) AS best_pnl
                FROM decision_errors e
                LEFT JOIN ai_decisions d ON d.id = e.decision_id
                WHERE e.error_code IS NOT NULL AND e.error_code != ''
                GROUP BY e.error_code
                HAVING n_errors >= 1
                ORDER BY total_pnl ASC
                """,
            ).fetchall()
            for r in rows:
                code_pnl_impact.append({
                    "code": str(r["code"] or ""),
                    "n_errors": int(r["n_errors"] or 0),
                    "n_settled": int(r["n_settled"] or 0),
                    "avg_pnl": round(float(r["avg_pnl"]), 6) if r["avg_pnl"] is not None else None,
                    "total_pnl": round(float(r["total_pnl"] or 0.0), 6),
                    "win_rate_when_coded": round(float(r["win_rate_when_coded"]), 4) if r["win_rate_when_coded"] is not None else None,
                    "worst_pnl": round(float(r["worst_pnl"]), 6) if r["worst_pnl"] is not None else None,
                    "best_pnl": round(float(r["best_pnl"]), 6) if r["best_pnl"] is not None else None,
                })
        finally:
            try:
                conn.close()
            except Exception:
                pass
    except Exception as exc:
        code_pnl_impact = []
        print(f"[REFLECTION-RECENT] code_pnl_impact failed: {type(exc).__name__}: {exc}",
              flush=True)
    return {
        "total": len(errors),
        "errors": errors,
        "code_stats": code_stats,
        "origin_stats": origin_stats,
        "severity_stats": severity_stats,
        "code_subkinds": code_subkinds,        # v225: 子原因精细化
        "cross_asset_snapshot": cross_asset,    # v225: 跨资产快照
        "code_pnl_impact": code_pnl_impact,    # v232.5 P0+: 错误码 → pnl 影响
        "prompt_preview": prompt_preview,
        "limit": limit,
        "code_filter": code,
        "origin_filter": origin,
    }


def _reflection_clustering_sync(days: int, min_count: int, limit_per_group: int):
    from review import reflection_checkers as _rc
    conn = _rc._connect()
    try:
        return _rc.cluster_failures(
            days=days, min_count=min_count,
            limit_per_group=limit_per_group, conn=conn,
        )
    finally:
        try:
            conn.close()
        except Exception:
            pass


def _reflection_run_sync(params: Dict[str, Any]):
    from review import reflection_checkers as _rc
    import blindspot_reflector as _br
    import importlib
    # 简单参数透传（不重复实现 run_reflection.py 的逻辑，直接走同一函数）
    if "decision_id" in params and params["decision_id"]:
        did = int(params["decision_id"])
        # 单条
        conn = db.get_connection()
        try:
            row = conn.execute("SELECT * FROM ai_decisions WHERE id = ?", (did,)).fetchone()
            if not row:
                raise HTTPException(status_code=404, detail=f"decision_id={did} not found")
            dec = dict(row)
            errs = _rc.run_all_checkers(dec, conn=conn)
            inserted = _rc.save_errors(errs, conn=conn)
            llm_result = {}
            if not params.get("skip_llm"):
                llm_result = _br.reflect_and_persist(dec, conn=conn, auto_promote=bool(params.get("auto_promote")))
            return {
                "mode": "single",
                "decision_id": did,
                "checker_errors": len(errs),
                "checker_inserted": inserted,
                "checker_codes": [e["error_code"] for e in errs],
                "llm_result": llm_result,
            }
        finally:
            conn.close()
    # 批量走 run_reflection.py
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    import importlib.util
    here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    spec = importlib.util.spec_from_file_location("run_reflection", os.path.join(here, "run_reflection.py"))
    if spec is None or spec.loader is None:
        raise HTTPException(status_code=500, detail="run_reflection.py not found")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod.run(
        limit=int(params.get("limit", 50)),
        scope=params.get("scope", "recent"),
        skip_llm=bool(params.get("skip_llm")),
        auto_promote=bool(params.get("auto_promote")),
    )


def _reflection_stats_sync(days: int):
    conn = db.get_connection()
    try:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            """SELECT error_code, origin, severity, COUNT(*) as cnt
               FROM decision_errors
               WHERE created_at >= datetime('now', ?)
               GROUP BY error_code, origin, severity
               ORDER BY cnt DESC""",
            (f"-{days} days",),
        ).fetchall()
        # 汇总
        by_code: Dict[str, int] = {}
        by_origin: Dict[str, int] = {}
        by_severity: Dict[str, int] = {}
        for r in rows:
            rec = dict(r)
            by_code[rec["error_code"]] = by_code.get(rec["error_code"], 0) + rec["cnt"]
            by_origin[rec["origin"]] = by_origin.get(rec["origin"], 0) + rec["cnt"]
            by_severity[str(rec["severity"])] = by_severity.get(str(rec["severity"]), 0) + rec["cnt"]
        return {
            "days": days,
            "total": sum(r["cnt"] for r in rows),
            "by_code": by_code,
            "by_origin": by_origin,
            "by_severity": by_severity,
            "rows": [dict(r) for r in rows],
        }
    finally:
        conn.close()


def _stock_decisions_sync(days: int, limit: int,
                          include_errors: bool) -> Dict[str, Any]:
    conn = db.get_connection()
    try:
        conn.row_factory = sqlite3.Row
        cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
        rows = conn.execute(
            """SELECT id, target_asset, suggested_action, sentiment_score,
                      reasoning, created_at, is_correct, settled, forward_pnl,
                      entry_price, exit_price, market_category
               FROM ai_decisions
               WHERE market_category = 'STOCK' AND created_at >= ?
               ORDER BY id DESC LIMIT ?""",
            (cutoff, limit),
        ).fetchall()
        decisions: List[Dict[str, Any]] = []
        by_asset: Dict[str, int] = {}
        by_action: Dict[str, int] = {}
        for r in rows:
            rec = dict(r)
            rec["reasoning_length"] = len(rec.get("reasoning") or "")
            rec.pop("reasoning", None)
            asset = rec.get("target_asset") or "?"
            action = rec.get("suggested_action") or "?"
            by_asset[asset] = by_asset.get(asset, 0) + 1
            by_action[action] = by_action.get(action, 0) + 1
            rec["error_codes"] = []
            rec["error_count"] = 0
            decisions.append(rec)
        if include_errors and decisions:
            ids = [d["id"] for d in decisions]
            placeholders = ",".join("?" for _ in ids)
            err_rows = conn.execute(
                f"""SELECT decision_id, error_code FROM decision_errors
                    WHERE decision_id IN ({placeholders})
                      AND (origin = ? OR source = ?)""",
                (*ids, _STOCK_AUDIT_ORIGIN, _STOCK_AUDIT_ORIGIN),
            ).fetchall()
            for er in err_rows:
                for d in decisions:
                    if d["id"] == er["decision_id"]:
                        d["error_codes"].append(er["error_code"])
                        d["error_count"] += 1
                        break
        return {
            "total": len(decisions),
            "by_asset": by_asset,
            "by_action": by_action,
            "days": days,
            "limit": limit,
            "decisions": decisions,
        }
    finally:
        conn.close()


def _stock_audit_sync(params: Dict[str, Any]) -> Dict[str, Any]:
    from review import reflection_checkers as _rc
    conn = db.get_connection()
    try:
        conn.row_factory = sqlite3.Row
        decision_ids = params.get("decision_ids")
        if decision_ids:
            placeholders = ",".join("?" for _ in decision_ids)
            rows = conn.execute(
                f"""SELECT * FROM ai_decisions
                    WHERE id IN ({placeholders}) AND market_category = 'STOCK'""",
                tuple(decision_ids),
            ).fetchall()
            mode = "explicit_ids"
        else:
            days = min(max(int(params.get("days", _STOCK_AUDIT_DEFAULT_DAYS)), 1), 365)
            limit = min(max(int(params.get("limit", _STOCK_AUDIT_DEFAULT_LIMIT)), 1), 200)
            cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
            rows = conn.execute(
                """SELECT * FROM ai_decisions
                   WHERE market_category = 'STOCK' AND created_at >= ?
                   ORDER BY id DESC LIMIT ?""",
                (cutoff, limit),
            ).fetchall()
            mode = "recent"
        scanned = 0
        triggered = 0
        inserted = 0
        skipped_low_comp = 0
        by_code: Dict[str, int] = {}
        out_decisions: List[Dict[str, Any]] = []
        try:
            for row in rows:
                scanned += 1
                dec = dict(row)
                errs = _rc.run_all_checkers(dec, conn=conn)
                if not errs:
                    continue
                # v276: 强制打上 stock_reflection_panel 标签，便于 _stock_decisions_sync
                # 用 (origin=_STOCK_AUDIT_ORIGIN) 过滤；同时确保不会被现有 /api/reflection/recent
                # 路径统计到（那里用 origin='checker'）。
                # 注：所有 checker 默认会把 origin/source 写成具体值，必须强制覆盖
                for e in errs:
                    e["origin"] = _STOCK_AUDIT_ORIGIN
                    e["source"] = _STOCK_AUDIT_ORIGIN
                triggered += len(errs)
                # 用 cursor.rowcount 拿真实落库行数（save_errors 的 inserted 计数器只算循环
                # 次数，OR IGNORE 时与实际不同；用一次 executemany 拿真 rowcount）
                cur = conn.executemany(
                    """INSERT OR IGNORE INTO decision_errors
                       (decision_id, error_code, origin, severity, detail,
                        evidence_refs, source, weight, created_at)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, datetime('now'))""",
                    [
                        (
                            e.get("decision_id"),
                            e.get("error_code"),
                            _STOCK_AUDIT_ORIGIN,
                            int(e.get("severity", 2)),
                            str(e.get("detail", "")),
                            json.dumps(e.get("evidence_refs", {}), ensure_ascii=False),
                            _STOCK_AUDIT_ORIGIN,
                            0,
                        )
                        for e in errs
                    ],
                )
                inserted += cur.rowcount
                conn.commit()
                codes = [e["error_code"] for e in errs if e.get("error_code")]
                for c in codes:
                    by_code[c] = by_code.get(c, 0) + 1
                if any(c == "E019" for c in codes):
                    skipped_low_comp += 1
                out_decisions.append({
                    "id": dec.get("id"),
                    "asset": dec.get("target_asset"),
                    "action": dec.get("suggested_action"),
                    "error_codes": codes,
                    "error_count": len(codes),
                })
        except Exception as e:
            return {"mode": mode, "scanned": scanned, "error": str(e)}
        return {
            "mode": mode,
            "scanned": scanned,
            "checker_triggered": triggered,
            "inserted": inserted,
            "skipped_low_completeness": skipped_low_comp,
            "by_code": by_code,
            "decisions": out_decisions,
            "origin": _STOCK_AUDIT_ORIGIN,
        }
    finally:
        conn.close()
