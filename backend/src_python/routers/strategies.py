# -*- coding: utf-8 -*-
"""策略库路由（结构切片，2026-11-17）。

从 api_server.py 搬来 8 条端点 + 3 个请求模型 + _strategy_or_404：
  /api/strategies（GET 列表 / POST 新建）、/current、/{id}、/{id}/activate、
  /{id}/versions、/{id}/archive、/meta/defaults

注册顺序有讲究：/api/strategies/meta/defaults 必须排在 /api/strategies/{strategy_id} 之前，
否则 "meta" 会被当成 strategy_id 走 422。这里保持原顺序，没有重新排序。

_strategy_or_404 被回测/优化器端点也用（api_server 侧留同名别名导入），
所以它搬到这里而不是留在入口文件——否则 router 得反查 api_server 形成环。
"""
from __future__ import annotations

import asyncio
from typing import Any, Dict, Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

import strategy_store

router = APIRouter()


class StrategyCreateBody(BaseModel):
    name: str
    description: str = ""
    params: Dict[str, Any] = {}


class StrategyVersionBody(BaseModel):
    params: Dict[str, Any]
    note: str = ""
    source: str = "manual"


class StrategyActivateBody(BaseModel):
    version_id: Optional[int] = None


def _strategy_or_404(strategy_id: int) -> Dict[str, Any]:
    try:
        return strategy_store.get_strategy(strategy_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.get("/api/strategies")
async def list_strategies(include_archived: bool = False):
    return await asyncio.to_thread(strategy_store.list_strategies, include_archived)


@router.get("/api/strategies/current")
async def get_current_strategy():
    return await asyncio.to_thread(strategy_store.get_current_strategy)


@router.post("/api/strategies/{strategy_id}/activate")
async def activate_strategy(strategy_id: int, body: StrategyActivateBody):
    try:
        return await asyncio.to_thread(
            strategy_store.set_current_strategy, strategy_id, body.version_id,
        )
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.post("/api/strategies")
async def create_strategy(body: StrategyCreateBody):
    try:
        return await asyncio.to_thread(
            strategy_store.create_strategy, body.name, body.description, body.params,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.get("/api/strategies/meta/defaults")
async def strategy_defaults():
    return {
        "defaults": strategy_store.default_params(),
        "bounds": {
            "signal_threshold": {"min": 0.0, "max": 0.9},
            "notional_usdt": {"min": 5, "max": 10000},
            "leverage": {"min": 1, "max": 20},
            "trailing_callback_rate": {"min": 0.1, "max": 5},
            "holding_horizon_minutes": {"min": 15, "max": 240},
        },
        "assets": sorted(a for a in strategy_store.ALLOWED_ASSETS if a),
        "strengths": [item for item in strategy_store.ALLOWED_STRENGTH if item],
    }


@router.get("/api/strategies/{strategy_id}")
async def get_strategy(strategy_id: int):
    return _strategy_or_404(strategy_id)


@router.post("/api/strategies/{strategy_id}/versions")
async def add_strategy_version(strategy_id: int, body: StrategyVersionBody):
    _strategy_or_404(strategy_id)
    try:
        return await asyncio.to_thread(
            strategy_store.add_version, strategy_id, body.params,
            source=body.source, note=body.note,
        )
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.post("/api/strategies/{strategy_id}/archive")
async def archive_strategy(strategy_id: int):
    try:
        await asyncio.to_thread(strategy_store.archive_strategy, strategy_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return {"archived": True, "id": strategy_id}
