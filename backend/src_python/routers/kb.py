# -*- coding: utf-8 -*-
"""知识库门面路由（结构切片，2026-11-17）。

从 api_server.py 搬来 v182.2 的 4 条 /kb/* 端点 + 4 个同步助手
（asset / dim / search / stats），全部薄封装 kb_facade，逻辑逐字未改。
"""
from __future__ import annotations

import asyncio
from typing import Any, Dict, List, Optional

from fastapi import APIRouter

router = APIRouter()


@router.get("/api/kb/asset/{asset}")
async def get_kb_asset(asset: str, since: str = "7d",
                       limit_per_kind: int = 20):
    """资产全息视图 — 聚合 13 类相关事实（错误/反思/洞察/因果/图谱/异动/回测/交易/K线/因子等）。

    asset: BTC / ETH / SOL / XAU / WTI ...
    since: 7d / 24h / 30d / 1w / all
    """
    return await asyncio.to_thread(_kb_asset_sync, asset, since, limit_per_kind)


@router.get("/api/kb/dim/{dim_code}")
async def get_kb_dim(dim_code: str, asset: Optional[str] = None,
                     since: str = "7d", limit: int = 20):
    """按反思维度检索 — dim_code ∈ {data, logic, causal, rules, market}。

    asset: 可选过滤；since: 时间窗口。
    """
    return await asyncio.to_thread(_kb_dim_sync, dim_code, asset, since, limit)


@router.get("/api/kb/search")
async def get_kb_search(q: str, dims: Optional[str] = None,
                        asset: Optional[str] = None,
                        since: str = "7d", limit: int = 20):
    """跨维度文本搜索 — LIKE 匹配 detail / rule / cause / effect / hint。

    dims: 逗号分隔 (data,logic,causal,rules,market)，None=全部。
    """
    dim_list = [d.strip() for d in dims.split(",")] if dims else None
    return await asyncio.to_thread(_kb_search_sync, q, dim_list, asset, since, limit)


@router.get("/api/kb/stats")
async def get_kb_stats(days: int = 7):
    """维度统计 + 资产概览 — 仪表盘热力图。"""
    return await asyncio.to_thread(_kb_stats_sync, days)


def _kb_asset_sync(asset: str, since: str, limit_per_kind: int) -> Dict[str, Any]:
    import kb_facade as _kb
    limit = min(max(limit_per_kind, 1), 100)
    return _kb.get_for_asset(asset, since=since, limit_per_kind=limit)


def _kb_dim_sync(dim_code: str, asset: Optional[str], since: str,
                 limit: int) -> Dict[str, Any]:
    import kb_facade as _kb
    limit = min(max(limit, 1), 100)
    items = _kb.get_dim(dim_code, asset=asset, since=since, limit=limit)
    return {"dim": dim_code, "asset": (asset or "").upper() if asset else None,
            "since": since, "count": len(items), "items": items}


def _kb_search_sync(q: str, dims: Optional[List[str]], asset: Optional[str],
                    since: str, limit: int) -> Dict[str, Any]:
    import kb_facade as _kb
    limit = min(max(limit, 1), 100)
    items = _kb.search(q, dims=dims, asset=asset, since=since, limit=limit)
    return {"q": q, "dims": dims, "asset": (asset or "").upper() if asset else None,
            "since": since, "count": len(items), "items": items}


def _kb_stats_sync(days: int) -> Dict[str, Any]:
    import kb_facade as _kb
    days = min(max(days, 1), 365)
    return {
        "days": days,
        "dim_stats": _kb.get_dim_stats(days=days),
        "asset_summary": _kb.get_asset_summary(days=days),
    }
