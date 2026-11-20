# -*- coding: utf-8 -*-
"""推理审查 / 自述 / 8 段断点路由（结构切片，2026-11-17）。

从 api_server.py 搬来 P1「推理审查」3 条、P3「LLM 自述」3 条、v275+「8 段
断点按需取原文」1 条，共 7 条端点 + 7 个同步助手。顺序照原样保留
（narrative/list 早于 narrative/{decision_id}）。逻辑逐字未改。
"""
from __future__ import annotations

import asyncio
import sqlite3
from typing import Any, Dict

from fastapi import APIRouter, HTTPException

import db

router = APIRouter()


@router.get("/api/reasoning/review/list")
async def get_reasoning_review_list(limit: int = 30, days: int = 7,
                                    only_failed: bool = False):
    """P1 「推理审查」 — 批量返回最近 N 条决策的 6 维审查结果。"""
    return await asyncio.to_thread(_reasoning_review_list_sync,
                                    int(limit), int(days), bool(only_failed))


@router.get("/api/reasoning/review/segment-history")
async def get_reasoning_review_segment_history(days: int = 7, limit: int = 1000):
    """P2 「段位一致性快照」 — 按天聚合 8 段命中率,前端热力图用。

    days 1~30(默认 7),limit 1~5000(默认 1000)。
    返回: {"days": N, "baseline": {stage: rate, ...}, "items": [{date, count, stages}, ...]}
      - baseline = 整个时间窗(N 天)内的平均命中率,用于前端计算 delta 着色
      - items 按时序升序(老→新),前端从左到右绘制时间轴
    """
    return await asyncio.to_thread(_reasoning_review_segment_history_sync,
                                    int(days), int(limit))


@router.get("/api/reasoning/review/{decision_id}")
async def get_reasoning_review(decision_id: int):
    """P1 「推理审查」 — 对单条 ai_decision 做 6 维审查，返回全部平铺（无 severity）。"""
    return await asyncio.to_thread(_reasoning_review_single, int(decision_id))


@router.get("/api/reasoning/narrative/list")
async def get_reasoning_narrative_list(limit: int = 50, days: int = 7,
                                        only_failed: bool = True):
    """P3 拉自述列表（已生成过 + 默认仅失败样本）。"""
    return await asyncio.to_thread(_narrative_list_sync,
                                    int(limit), int(days), bool(only_failed))


@router.get("/api/reasoning/narrative/{decision_id}")
async def get_reasoning_narrative(decision_id: int):
    """P3 取已生成的自述 — 未生成返回 has_narrative=false。"""
    return await asyncio.to_thread(_narrative_get_sync, int(decision_id))


@router.post("/api/reasoning/narrative/{decision_id}/generate")
async def post_reasoning_narrative_generate(decision_id: int, body: Dict[str, Any] = None):
    """P3 触发 LLM 生成第一人称自述 — 走 ai_worker.chat reflection 池。"""
    body = body or {}
    return await asyncio.to_thread(_narrative_generate_sync,
                                    int(decision_id), bool(body.get("force_regen", False)))


@router.get("/api/reasoning/decision/{decision_id}/text")
async def get_reasoning_decision_text(decision_id: int):
    """v275+: 取单条 ai_decision 的完整推理文本 — 供 8 段 dot 按需展开。"""
    return await asyncio.to_thread(_reasoning_decision_text_sync, int(decision_id))


def _reasoning_review_single(decision_id: int):
    import reasoning_review as _rr
    conn = db.get_connection()
    try:
        out = _rr.get_review_for_decision(decision_id, conn)
        if out is None:
            raise HTTPException(status_code=404, detail=f"decision {decision_id} not found")
        return out
    finally:
        conn.close()


def _reasoning_review_list_sync(limit: int, days: int, only_failed: bool):
    import reasoning_review as _rr
    conn = db.get_connection()
    try:
        items = _rr.list_recent_reviews(limit=limit, days=days,
                                        only_failed=only_failed, conn=conn)
        return {"limit": limit, "days": days, "only_failed": only_failed,
                "count": len(items), "items": items}
    finally:
        conn.close()


def _reasoning_review_segment_history_sync(days: int, limit: int):
    """P2 「段位一致性快照」 — 计算 baseline + items,baseline 用于前端 delta 着色。"""
    import reasoning_review as _rr
    conn = db.get_connection()
    try:
        items = _rr.segment_history(days=days, limit=limit, conn=conn)
        # baseline = 总命中率加权(每段 = sum(hit) / sum(count))
        #   用加权(而不是均值)是为了让"决策多的一天"占比更大,反映真实"最近的基线"
        if not items:
            return {"days": days, "baseline": {}, "items": []}
        total_count = sum(it["count"] for it in items)
        if total_count == 0:
            return {"days": days, "baseline": {}, "items": items}
        baseline: Dict[str, float] = {}
        for stage in _rr._ANALYSIS_STEPS:
            weighted_hit = sum(it["stages"].get(stage, 0.0) * it["count"] for it in items)
            baseline[stage] = round(weighted_hit / total_count, 4)
        return {"days": days, "baseline": baseline, "items": items}
    finally:
        conn.close()


def _narrative_get_sync(decision_id):
    import reasoning_quality_report as _rqr
    conn = db.get_connection()
    try:
        n = _rqr.get_narrative_for_decision(decision_id, conn)
        return {"decision_id": decision_id, "has_narrative": n is not None, "narrative": n}
    finally:
        conn.close()


def _narrative_generate_sync(decision_id, force_regen):
    import reasoning_quality_report as _rqr
    import reasoning_review as _rr
    conn = db.get_connection()
    try:
        if not force_regen:
            existing = _rqr.get_narrative_for_decision(decision_id, conn)
            if existing and existing.get("narrative"):
                return {"decision_id": decision_id, "cached": True, "narrative": existing}
        review = _rr.get_review_for_decision(decision_id, conn)
        if review is None:
            raise HTTPException(status_code=404, detail=f"decision {decision_id} not found")
        conn.row_factory = sqlite3.Row
        drow = conn.execute(
            "SELECT * FROM ai_decisions WHERE id = ?", (int(decision_id),)
        ).fetchone()
        if not drow:
            raise HTTPException(status_code=404, detail=f"decision {decision_id} not found")
        decision = dict(drow)
        prompt = _rqr.build_prompt(decision, review)
        out = _rqr.call_llm_for_narrative(prompt)
        text = (out.get("text") or "").strip()
        if not text:
            return {"decision_id": decision_id, "error": out.get("error", "empty"),
                    "cached": False, "model": out.get("model", "")}
        nid = _rqr.save_narrative(decision_id=decision_id, review_dict=review,
                                   narrative=text, model=out.get("model", ""),
                                   prompt_tokens=out.get("prompt_tokens", 0),
                                   completion_tokens=out.get("completion_tokens", 0),
                                   conn=conn)
        saved = _rqr.get_narrative_for_decision(decision_id, conn)
        return {"decision_id": decision_id, "cached": False, "id": nid,
                "model": out.get("model", ""), "narrative": saved}
    finally:
        conn.close()


def _narrative_list_sync(limit, days, only_failed):
    import reasoning_quality_report as _rqr
    conn = db.get_connection()
    try:
        items = _rqr.list_narratives(limit=limit, days=days,
                                      only_failed=only_failed, conn=conn)
        return {"limit": limit, "days": days, "only_failed": only_failed,
                "count": len(items), "items": items}
    finally:
        conn.close()


def _reasoning_decision_text_sync(decision_id: int):
    """同步实现 — 8 段 dot 点击按需加载原文。

    present/missing 判定复用 reasoning_review._present_steps(同一函数),
    保证前端 dot 颜色与 review/list 返回完全一致,不重复实现。
    """
    import reasoning_review as _rr
    conn = db.get_connection()
    try:
        conn.row_factory = sqlite3.Row
        drow = conn.execute(
            "SELECT id, target_asset, suggested_action, reasoning, "
            "reasoning_path, asset_reasoning FROM ai_decisions WHERE id = ?",
            (int(decision_id),),
        ).fetchone()
        if not drow:
            raise HTTPException(status_code=404, detail=f"decision {decision_id} not found")
        d = dict(drow)
        # 复用 reasoning_review 的 present/missing 判定 — 前端不重复实现
        full_text = (d.get("reasoning_path") or d.get("reasoning")
                     or d.get("asset_reasoning") or "")
        present = _rr._present_steps(full_text) if full_text else []
        all_steps = list(_rr._ANALYSIS_STEPS)
        missing = [s for s in all_steps if s not in present]
        # 段关键词在文本中的位置索引 — 前端用于高亮
        segment_positions = []
        for s in all_steps:
            idx = full_text.find(s)
            segment_positions.append({
                "segment": s,
                "present": s in present,
                "char_index": idx if idx >= 0 else None,
            })
        return {
            "decision_id": int(d.get("id") or 0),
            "asset": (d.get("target_asset") or "").upper(),
            "action": (d.get("suggested_action") or "").upper(),
            "reasoning_path": d.get("reasoning_path") or "",
            "reasoning": d.get("reasoning") or "",
            "asset_reasoning": d.get("asset_reasoning") or "",
            "full_text": full_text,
            "full_text_length": len(full_text),
            "segments": {
                "all": all_steps,
                "present": present,
                "missing": missing,
                "positions": segment_positions,
            },
        }
    finally:
        conn.close()
