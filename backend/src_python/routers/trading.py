# -*- coding: utf-8 -*-
"""交易管理器路由（结构切片，2026-11-17）。

从 api_server.py 搬来 4 条端点：/api/trading/manager 的 start / stop / status / logs。
鉴权与进程句柄仍在 api_server（_TRADING_MANAGER 在 lifespan 关闭流程里也要 stop），
这里通过 _api() 延迟回引，行为不变：默认 fail-closed，未显式开
TRADING_MANAGER_ENABLED=1 一律 503；token 不符 401。
"""
from __future__ import annotations

import asyncio

from fastapi import APIRouter, Header, HTTPException

import config

router = APIRouter()


def _api():
    """延迟回引 api_server（_TRADING_MANAGER / _authorize_trading_manager 仍在原文件）。"""
    import api_server
    return api_server


@router.post("/api/trading/manager/start")
async def start_trading_manager(x_trading_token: str | None = Header(default=None)):
    _api()._authorize_trading_manager(x_trading_token)
    if not config.BINANCE_API_KEY or not config.BINANCE_API_SECRET:
        raise HTTPException(status_code=400, detail="BINANCE_API_KEY / BINANCE_API_SECRET 未配置")
    return await asyncio.to_thread(_api()._TRADING_MANAGER.start)


@router.post("/api/trading/manager/stop")
async def stop_trading_manager(x_trading_token: str | None = Header(default=None)):
    _api()._authorize_trading_manager(x_trading_token)
    return await asyncio.to_thread(_api()._TRADING_MANAGER.stop)


@router.get("/api/trading/manager/status")
async def trading_manager_status(x_trading_token: str | None = Header(default=None)):
    _api()._authorize_trading_manager(x_trading_token)
    return _api()._TRADING_MANAGER.status()


@router.get("/api/trading/manager/logs")
async def trading_manager_logs(tail: int = 200, x_trading_token: str | None = Header(default=None)):
    _api()._authorize_trading_manager(x_trading_token)
    lines = await asyncio.to_thread(_api()._TRADING_MANAGER.logs, tail)
    return {"lines": [line.rstrip("\r\n") for line in lines]}
