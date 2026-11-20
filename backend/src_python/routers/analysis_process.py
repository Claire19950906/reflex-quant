# -*- coding: utf-8 -*-
"""分析全过程路由（结构切片，2026-11-17）。

从 api_server.py 搬来 3 条 /analysis-process/* 端点：左栏列表、单条新闻全链路
生命周期、模块+DB 双健康检查。全部薄封装 analysis_process，逻辑逐字未改。
"""
from __future__ import annotations

import asyncio

from fastapi import APIRouter, HTTPException

import analysis_process

router = APIRouter()


@router.get("/api/analysis-process/recent")
async def get_analysis_process_recent(limit: int = 20, only_with_decisions: bool = False):
    """最近 N 条新闻 + 关键指标,给 /analysis-process 左栏列表用。

    Args:
        limit: 最多返回条数 (默认 20,后端硬上限 200)
        only_with_decisions: True 只列出至少有 1 个 ai_decision 的新闻
    """
    limit = max(1, min(int(limit), 200))
    items = await asyncio.to_thread(analysis_process.list_recent, limit, bool(only_with_decisions))
    return {"items": items, "count": len(items), "limit": limit}


@router.get("/api/analysis-process/news/{news_id}")
async def get_analysis_process_news(news_id: int):
    """单条新闻的全过程数据 —— Gate / Reasoning / Trades / Reflections / Insights。

    返回 dict 包含以下顶级键:
      news / gate / decisions / primary_decision_id / reasoning / llm_call_meta
      quick_sim / paper_run / trade_reflections / structured_reflections
      attribution_reviews / news_trade_review / review_insights / experience_entries / causal_chain_links
    """
    if news_id <= 0:
        raise HTTPException(status_code=400, detail="news_id must be > 0")
    data = await asyncio.to_thread(analysis_process.get_lifecycle, int(news_id))
    if data.get("news") is None:
        raise HTTPException(status_code=404, detail=f"news_id {news_id} not found")
    return data


@router.get("/api/analysis-process/healthcheck")
async def get_analysis_process_healthcheck():
    """模块就绪 + DB 连通性就绪的双重检查。"""
    return await asyncio.to_thread(analysis_process.healthcheck)
