# -*- coding: utf-8 -*-
"""因果图谱改进候选路由（结构切片，2026-11-17）。

从 api_server.py 搬来 v202 段 4 的 5 条 /analysis/kg-* 端点。全部是
asyncio.to_thread(kg_improvements.xxx) 的薄封装，逻辑逐字未改。
"""
from __future__ import annotations

import asyncio
from typing import Optional

from fastapi import APIRouter

import kg_improvements

router = APIRouter()


@router.get("/api/analysis/kg-improvements")
async def list_kg_improvements(status: Optional[str] = None, limit: int = 20):
    """列出 KG 改进候选。
    status: pending | approved | rejected（缺省 = 全部）。limit 默认 20。
    """
    return await asyncio.to_thread(kg_improvements.list_proposals, status, limit)


@router.get("/api/analysis/kg-stats")
async def get_kg_stats():
    """KG 全图快照（节点/边/规则统计 + 候选审批计数）。"""
    return await asyncio.to_thread(kg_improvements.get_kg_stats)


@router.post("/api/analysis/kg-improvements/{proposal_id}/approve")
async def approve_kg_improvement(proposal_id: int):
    """批准一条候选。本版本仅更新 kg_improvements.status='approved'，
    真正写回 causal_graph / causal_chains 由 reconcile worker 异步应用。
    """
    return await asyncio.to_thread(
        kg_improvements.decide_proposal, proposal_id, "approve", ""
    )


@router.post("/api/analysis/kg-improvements/{proposal_id}/reject")
async def reject_kg_improvement(proposal_id: int):
    """驳回一条候选。仅更新 kg_improvements.status='rejected'。"""
    return await asyncio.to_thread(
        kg_improvements.decide_proposal, proposal_id, "reject", ""
    )


@router.post("/api/analysis/kg-improvements/auto-approve")
async def auto_approve_kg_improvements(
    min_confidence: float = 0.85, batch_max: int = 100, dry_run: bool = False,
):
    """v202 R2-fix: 一键自动审批 pending KG 候选（双重门：confidence + 频次）。

    默认 min_confidence=0.85，batch_max=100，dry_run=False（直接落库）。
    仅更新 status 字段，不写 asset_driver_edges / causal_chains（由
    reconcile worker 异步应用）。
    """
    return await asyncio.to_thread(
        kg_improvements.auto_approve_pending,
        min_confidence=float(min_confidence),
        batch_max=int(batch_max),
        dry_run=bool(dry_run),
    )
