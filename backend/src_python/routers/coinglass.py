# -*- coding: utf-8 -*-
"""Coinglass 数据路由（结构切片，2026-11-17）。

从 api_server.py 搬来 v189 的 4 条：lsr/latest、lsr/history、health、
lsr/ranking。逻辑逐字未改，点内惰性 import（coinglass_client 等）原样保留。
"""
from __future__ import annotations

import time
from typing import Any, Dict, List, Optional

from fastapi import APIRouter

import config

router = APIRouter()


@router.get("/api/v189/coinglass/lsr/latest")
def coinglass_lsr_latest(
    symbol: str = "",
    mode: str = "",
    limit: int = 20,
):
    """v189-7: Coinglass LSR 最新样本 (symbol/mode 可选, 默认全量).

    返回: { ts, samples: [...] }
    """
    from feeds import coinglass_lsr as _cg_lsr  # 延迟 import, 模块未启用时不加载
    sym = symbol.upper().strip() or None
    md = mode.lower().strip() or None
    if md and md not in ("trader", "whale"):
        return {
            "ok": False,
            "error": f"invalid mode: {mode!r}, 必须是 trader 或 whale",
        }
    samples = _cg_lsr.get_latest(symbol=sym, mode=md, limit=limit)
    return {
        "ok": True,
        "ts": int(time.time()),
        "filter": {"symbol": sym or "*", "mode": md or "*", "limit": limit},
        "count": len(samples),
        "samples": samples,
        "config": {
            "enabled": bool(config.COINGLASS_ENABLED),
            "key_masked": config.coinglass_key_masked(),
            "expires_at": config.COINGLASS_EXPIRES_AT,
            "poll_seconds": int(config.COINGLASS_LSR_POLL_SECONDS),
            "symbols": list(config.COINGLASS_SYMBOLS),
        },
    }


@router.get("/api/v189/coinglass/lsr/history")
def coinglass_lsr_history(
    symbol: str,
    mode: str = "trader",
    minutes: int = 60,
    limit: int = 500,
):
    """v189-7: Coinglass LSR 历史曲线 (since_ts=now-minutes*60)."""
    from feeds import coinglass_lsr as _cg_lsr
    if mode not in ("trader", "whale"):
        return {
            "ok": False,
            "error": f"invalid mode: {mode!r}, 必须是 trader 或 whale",
        }
    if not symbol or not symbol.strip():
        return {"ok": False, "error": "symbol is required (e.g. BTC)"}
    minutes = max(1, min(60 * 24 * 7, int(minutes)))  # 1min ~ 7d
    since_ts = int(time.time()) - minutes * 60
    samples = _cg_lsr.get_history(
        symbol=symbol.strip(), mode=mode, since_ts=since_ts, limit=limit
    )
    return {
        "ok": True,
        "ts": int(time.time()),
        "filter": {
            "symbol": symbol.upper().strip(),
            "mode": mode,
            "since_ts": since_ts,
            "minutes": minutes,
            "limit": limit,
        },
        "count": len(samples),
        "samples": samples,
    }


@router.get("/api/v189/coinglass/health")
def coinglass_health(minutes: int = 60):
    """v189-7: 健康度 + watcher 实时状态 + 配额 / key 过期日."""
    from feeds import coinglass_lsr as _cg_lsr
    minutes = max(1, min(60 * 24, int(minutes)))
    since_ts = int(time.time()) - minutes * 60
    summary = _cg_lsr.get_health_summary(since_ts=since_ts)
    summary["config"] = {
        "enabled": bool(config.COINGLASS_ENABLED),
        "key_masked": config.coinglass_key_masked(),
        "expires_at": config.COINGLASS_EXPIRES_AT,
        "poll_seconds": int(config.COINGLASS_LSR_POLL_SECONDS),
        "symbols": list(config.COINGLASS_SYMBOLS),
        "rate_warn_remaining": int(config.COINGLASS_RATE_WARN_REMAINING),
    }
    return summary


@router.get("/api/v189/coinglass/lsr/ranking")
def coinglass_lsr_ranking(
    mode: str = "trader",
    sort: str = "delta_2m",
    order: str = "desc",
    limit: int = 50,
    offset: int = 0,
    minutes: int = 30,
):
    """v189-7: 排行榜 — 各 (mode, symbol) 最近一条样本, 按 sort 字段排序.

    不走上游配额 (standard 不支持 ranking 端点), 纯 SQL.
    sort 字段: ratio / delta_2m / delta_30m / delta_4h.
    默认窗口 30min, 避免 24h 老数据混入.
    """
    if mode not in ("trader", "whale"):
        return {
            "ok": False,
            "error": f"invalid mode: {mode!r}, 必须是 trader 或 whale",
        }
    allowed_sort = {"ratio", "delta_2m", "delta_30m", "delta_4h", "ts"}
    if sort not in allowed_sort:
        return {
            "ok": False,
            "error": f"invalid sort: {sort!r}, 允许 {sorted(allowed_sort)}",
        }
    if order not in ("asc", "desc"):
        return {"ok": False, "error": f"invalid order: {order!r}"}
    limit = max(1, min(int(limit), 200))
    offset = max(0, int(offset))
    minutes = max(1, min(int(minutes), 1440))
    from feeds import coinglass_lsr as _cg_lsr
    try:
        rows = _cg_lsr.get_ranking(
            mode=mode, sort=sort, order=order,
            limit=limit, offset=offset, since_minutes=minutes,
        )
    except Exception as exc:
        return {"ok": False, "error": f"{type(exc).__name__}: {str(exc)[:160]}"}
    return {
        "ok": True,
        "ts": int(time.time()),
        "filter": {
            "mode": mode, "sort": sort, "order": order,
            "limit": limit, "offset": offset, "minutes": minutes,
        },
        "count": len(rows),
        "rows": rows,
    }
