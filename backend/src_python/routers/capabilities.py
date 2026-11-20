# -*- coding: utf-8 -*-
"""能力扩展路由（v2 · 2026-11-16）

/api/capabilities/scenario        情景树：事件 → 三条路径 + 持仓影响 + 动作
/api/capabilities/research/ingest 研报摄入：PDF/文本 → 结构化假设与信号 → 入库
/api/capabilities/research        研报列表
只读账本 + 独立 research_notes 表，不碰交易与策略参数。
"""
from __future__ import annotations

from typing import Any, Dict

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

import capabilities as cap

router = APIRouter(tags=["capabilities"])


class ScenarioBody(BaseModel):
    event: str = ""
    horizon_days: int = 3


class ResearchBody(BaseModel):
    filename: str = "note.txt"
    content_base64: str = ""


@router.post("/api/capabilities/scenario")
def scenario_tree(body: ScenarioBody) -> Dict[str, Any]:
    try:
        return cap.scenario_tree(body.event, horizon_days=body.horizon_days)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"scenario failed: {exc}") from exc


@router.post("/api/capabilities/research/ingest")
def research_ingest(body: ResearchBody) -> Dict[str, Any]:
    try:
        return cap.ingest_research(body.filename, body.content_base64)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"ingest failed: {exc}") from exc


@router.get("/api/capabilities/research")
def research_list(limit: int = 50) -> Dict[str, Any]:
    return {"ok": True, "items": cap.list_research(limit)}


@router.get("/api/capabilities/research/{note_id}")
def research_detail(note_id: int) -> Dict[str, Any]:
    item = cap.get_research(note_id)
    if not item:
        raise HTTPException(status_code=404, detail="note not found")
    return {"ok": True, "item": item}
