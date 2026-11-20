# -*- coding: utf-8 -*-
"""提示词库（采集入库过滤）路由（结构切片，2026-11-17）。

从 api_server.py 搬来 /api/filter/keywords 的 3 条端点 + KeywordPayload 模型。
全部薄封装 realtime_filter，逻辑逐字未改。
"""
from __future__ import annotations

import asyncio
from typing import Literal

from fastapi import APIRouter
from pydantic import BaseModel

import realtime_filter

router = APIRouter()


class KeywordPayload(BaseModel):
    kind: Literal["junk", "core"]
    keyword: str
    note: str = ""


@router.get("/api/filter/keywords")
async def list_filter_keywords():
    """全部过滤提示词（JUNK 噪声词 / CORE 高相关词，含内置与停用标记）。"""
    return await asyncio.to_thread(realtime_filter.list_keywords)


@router.post("/api/filter/keywords")
async def add_filter_keyword(payload: KeywordPayload):
    """新增过滤提示词（立即生效）。"""
    return await asyncio.to_thread(realtime_filter.add_keyword, payload.kind, payload.keyword, payload.note)


@router.delete("/api/filter/keywords/{keyword_id}")
async def delete_filter_keyword(keyword_id: int):
    """停用（软删除）过滤提示词（立即生效）。"""
    return await asyncio.to_thread(realtime_filter.deactivate_keyword, keyword_id)
