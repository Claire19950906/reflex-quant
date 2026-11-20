# -*- coding: utf-8 -*-
"""LLM 调用统计路由（结构切片，2026-11-17）。

从 api_server.py 搬来 v187 的 5 条 /llm-stats 端点：by-purpose / by-key /
by-call-site / timeline / summary。纯读库聚合，逻辑逐字未改。
"""
from __future__ import annotations

import asyncio
import time

from fastapi import APIRouter

import db

router = APIRouter()


@router.get("/api/v187/llm-stats/by-purpose")
async def llm_stats_by_purpose(days: float = 1.0):
    """v187: 按 purpose 聚合 token 用量(默认 24h)。"""
    days = max(0.01, min(7.0, days))
    since_ts = int(time.time() - days * 86400)
    return await asyncio.to_thread(db.query_ai_call_stats_by_purpose, since_ts)


@router.get("/api/v187/llm-stats/by-key")
async def llm_stats_by_key(days: float = 1.0):
    """v187: 按 key_alias 聚合(看每通道独立消耗)。"""
    days = max(0.01, min(7.0, days))
    since_ts = int(time.time() - days * 86400)
    return await asyncio.to_thread(db.query_ai_call_stats_by_key, since_ts)


@router.get("/api/v187/llm-stats/by-call-site")
async def llm_stats_by_call_site(days: float = 1.0):
    """v187: 按 call_site 聚合(看哪个 endpoint 最费 token)。"""
    days = max(0.01, min(7.0, days))
    since_ts = int(time.time() - days * 86400)
    return await asyncio.to_thread(db.query_ai_call_stats_by_call_site, since_ts)


@router.get("/api/v187/llm-stats/timeline")
async def llm_stats_timeline(days: float = 1.0, bucket_seconds: int = 3600):
    """v187: 按时间桶聚合(默认 1 小时 / 24 桶)。"""
    days = max(0.01, min(7.0, days))
    bucket_seconds = max(60, min(86400, bucket_seconds))
    since_ts = int(time.time() - days * 86400)
    rows = await asyncio.to_thread(db.query_ai_call_stats_timeline, since_ts, bucket_seconds)
    return {"bucket_seconds": bucket_seconds, "buckets": rows}


@router.get("/api/v187/llm-stats/summary")
async def llm_stats_summary(days: float = 1.0):
    """v187: 总览 — 一次返回所有 4 个聚合 + 总 token/调用数,便于前端 dashboard 一把抓。"""
    days = max(0.01, min(7.0, days))
    since_ts = int(time.time() - days * 86400)
    purpose_rows = await asyncio.to_thread(db.query_ai_call_stats_by_purpose, since_ts)
    key_rows = await asyncio.to_thread(db.query_ai_call_stats_by_key, since_ts)
    site_rows = await asyncio.to_thread(db.query_ai_call_stats_by_call_site, since_ts)
    timeline_rows = await asyncio.to_thread(db.query_ai_call_stats_timeline, since_ts, 3600)
    # 总计
    total_in = sum(r.get("tokens_in", 0) for r in purpose_rows)
    total_out = sum(r.get("tokens_out", 0) for r in purpose_rows)
    total_calls = sum(r.get("calls", 0) for r in purpose_rows)
    return {
        "window_days": days,
        "since_ts": since_ts,
        "totals": {
            "tokens_in": total_in,
            "tokens_out": total_out,
            "calls": total_calls,
        },
        "by_purpose": purpose_rows,
        "by_key": key_rows,
        "by_call_site": site_rows,
        "timeline": timeline_rows,
    }
