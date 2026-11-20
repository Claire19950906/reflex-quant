# -*- coding: utf-8 -*-
"""时序读库路由（结构切片，2026-11-17）。

从 api_server.py 搬来 5 条 /timeseries/* 端点：ticks / ohlc / factors / news /
winrate。全部是 `asyncio.to_thread(timeseries.xxx, ...)` 的薄封装，逻辑逐字未改。
"""
from __future__ import annotations

import asyncio

from fastapi import APIRouter, HTTPException

import timeseries

router = APIRouter()


@router.get("/api/timeseries/ticks/{symbol}")
async def get_timeseries_ticks(symbol: str, limit: int = 500):
    items = await asyncio.to_thread(timeseries.query_ticks, symbol, None, None, limit)
    return {"symbol": symbol.upper(), "count": len(items), "items": items}


@router.get("/api/timeseries/ohlc/{symbol}")
async def get_timeseries_ohlc(symbol: str, interval: str = "1m", limit: int = 200):
    if interval not in timeseries.BUCKET_SECONDS:
        raise HTTPException(status_code=400, detail=f"interval must be one of {sorted(timeseries.BUCKET_SECONDS)}")
    items = await asyncio.to_thread(timeseries.resample_ohlc, symbol, interval, limit)
    return {"symbol": symbol.upper(), "interval": interval, "count": len(items), "items": items}


@router.get("/api/timeseries/factors")
async def get_timeseries_factors(asset: str | None = None, limit: int = 100):
    items = await asyncio.to_thread(timeseries.query_factor_history, asset, limit)
    return {"asset": (asset or "ALL").upper(), "count": len(items), "items": items}


@router.get("/api/timeseries/news")
async def get_timeseries_news(asset: str | None = None, limit: int = 2000):
    items = await asyncio.to_thread(timeseries.query_news_events, None, None, asset, limit)
    return {"asset": (asset or "ALL").upper(), "count": len(items), "items": items}


@router.get("/api/timeseries/winrate")
async def get_timeseries_winrate(asset: str | None = None, window: int = 20):
    return await asyncio.to_thread(timeseries.rolling_winrate, asset, window)
