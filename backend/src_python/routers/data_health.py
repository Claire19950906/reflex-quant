# -*- coding: utf-8 -*-
"""数据健康路由（结构切片，2026-11-17）。

从 api_server.py 搬来 P2 的 4 条 /data-health/* 端点 + 3 个同步助手
（summary / recent / record）。summary 与 recent 的助手在原文件里被拆到
别处（P3 反思段内），这里收拢到一处。逻辑逐字未改。
"""
from __future__ import annotations

import asyncio
import sqlite3
from typing import Any, Dict, Optional

from fastapi import APIRouter

import db

router = APIRouter()


@router.get("/api/data-health/summary")
async def get_data_health_summary(asset: Optional[str] = None, days: int = 7):
    """P2 数据健康聚合 - 返回按 (asset, feed_kind) 分组的健康率/最常用源/最近失败。"""
    return await asyncio.to_thread(_data_health_summary_sync,
                                    asset, int(days))


@router.get("/api/data-health/recent")
async def get_data_health_recent(asset: Optional[str] = None,
                                  feed_kind: Optional[str] = None,
                                  status: Optional[str] = None,
                                  limit: int = 100):
    """P2 数据健康原始流水 - 最近 N 条 attempt。"""
    return await asyncio.to_thread(_data_health_recent_sync,
                                    asset, feed_kind, status, int(limit))


@router.get("/api/data-health/policy")
async def get_data_health_policy():
    """返回 feed_redund_policy 全量 - 让前端展示每个 (asset, kind) 的 fallback 链。"""
    import feed_redund_policy as _frp
    return {
        "policy": _frp.FEED_REDUND_POLICY,
        "assets": _frp.known_assets(),
        "timeout_ms": _frp.SOURCE_TIMEOUT_MS,
        "max_drift_pct": _frp.EXOG_MAX_PRICE_DRIFT_PCT,
    }


@router.post("/api/data-health/record")
async def post_data_health_record(body: Dict[str, Any]):
    """P2 数据健康 — 手动记录一次 attempt (供 ingest / ai_worker 调用 或 调试)。"""
    return await asyncio.to_thread(_data_health_record_sync, body or {})


def _data_health_summary_sync(asset, days):
    import data_health_journal as _dhj
    conn = db.get_connection()
    try:
        return _dhj.summary(asset=asset, days=days, conn=conn)
    finally:
        conn.close()


def _data_health_recent_sync(asset, feed_kind, status, limit):
    import data_health_journal as _dhj
    conn = db.get_connection()
    try:
        # v309 P1: db.get_connection() **不设** row_factory (默认 tuple),
        # 下面 `dict(r)` 会抛 "TypeError: object is not iterable" → HTTP 500。
        # api_server.py 其他 20+ 处都显式 `conn.row_factory = sqlite3.Row`,
        # 这里漏了 —— 前端 data-health-panel.tsx 调 /api/data-health/recent 直接 500。
        conn.row_factory = sqlite3.Row
        _dhj.ensure_table(conn)
        limit = min(max(int(limit or 100), 1), 500)
        where = ["1=1"]
        params = []
        if asset:
            where.append("asset = ?")
            params.append(str(asset).upper())
        if feed_kind:
            where.append("feed_kind = ?")
            params.append(str(feed_kind).upper())
        if status:
            where.append("status = ?")
            params.append(str(status).lower())
        sql = ("SELECT id, asset, feed_kind, source, status, latency_ms, value, error, ts "
               "FROM feed_health WHERE " + " AND ".join(where)
               + " ORDER BY id DESC LIMIT ?")
        params.append(limit)
        rows = conn.execute(sql, params).fetchall()
        out = []
        for r in rows:
            out.append(dict(r))
        return {"limit": limit, "asset": asset, "feed_kind": feed_kind,
                "status": status, "count": len(out), "items": out}
    finally:
        conn.close()


def _data_health_record_sync(body):
    import data_health_journal as _dhj
    conn = db.get_connection()
    try:
        _dhj.ensure_table(conn)
        aid = _dhj.record_attempt(
            asset=str(body.get("asset") or "?").upper(),
            feed_kind=str(body.get("feed_kind") or "?").upper(),
            source=str(body.get("source") or "?").lower(),
            status=str(body.get("status") or "failed").lower(),
            latency_ms=int(body.get("latency_ms") or 0),
            value=body.get("value"),
            error=body.get("error"),
            conn=conn,
        )
        return {"id": aid, "ok": aid > 0}
    finally:
        conn.close()
