# -*- coding: utf-8 -*-
"""事件原子 / 影响曲线 / 维度表路由（结构切片，2026-11-17）。

从 api_server.py 搬来 8 条端点：event_atoms 列表与详情、impact_curves 列表与
按原子查询、impact_stat、dim/assets、dim/event_types、atom/summary。
逻辑逐字未改；_serialize_event_atom_row 从 routers/news.py 取，
_format_time 走 common.time_utils.format_time。
"""
from __future__ import annotations

import asyncio
import time
from typing import Any, Dict, List, Optional

import aiosqlite
from fastapi import APIRouter, HTTPException

import config
import dim_tables as _dim_tables
import event_atom as _event_atom
import event_impact_curve as _event_impact_curve
from common.time_utils import format_time as _format_time
from routers.news import _serialize_event_atom_row


router = APIRouter()


@router.get("/api/event_atoms")
async def list_event_atoms(
    type_code: Optional[str] = None,
    asset_code: Optional[str] = None,
    news_id: Optional[int] = None,
    since_ts: Optional[int] = None,
    limit: int = 50,
):
    """事件原子列表（按 type_code / asset_code / 时间 / news_id 过滤）。默认 50 条，最大 500。"""
    limit = max(1, min(int(limit or 50), 500))
    sql = [
        "SELECT ea.id, ea.raw_news_id, ea.event_type_id, ea.asset_id,",
        "       det.code AS event_type_code, det.name_zh AS event_type_zh,",
        "       det.category AS event_category,",
        "       da.code  AS asset_code,",
        "       ea.event_time, ea.event_time_confidence, ea.event_time_source,",
        "       ea.magnitude_value, ea.magnitude_unit, ea.actor,",
        "       ea.direction, ea.direction_confidence, ea.direction_source,",
        "       ea.evidence_count, ea.parser_version, ea.cluster_id,",
        "       ea.reasoning, ea.created_at",
        "FROM event_atom ea",
        "JOIN dim_event_type det ON det.id = ea.event_type_id",
        "LEFT JOIN dim_asset da ON da.id = ea.asset_id",
        "WHERE 1=1",
    ]
    params: List[Any] = []
    if type_code:
        sql.append("AND det.code = ?"); params.append(str(type_code))
    if asset_code:
        sql.append("AND da.code = ?"); params.append(str(asset_code))
    if news_id is not None:
        sql.append("AND ea.raw_news_id = ?"); params.append(int(news_id))
    if since_ts is not None:
        sql.append("AND ea.event_time >= ?"); params.append(int(since_ts))
    sql.append("ORDER BY ea.event_time DESC, ea.id DESC LIMIT ?")
    params.append(limit)
    async with aiosqlite.connect(config.DB_PATH, timeout=20) as conn:
        conn.row_factory = aiosqlite.Row
        cur = await conn.execute("\n".join(sql), params)
        rows = await cur.fetchall()
        await cur.close()
    items = [_serialize_event_atom_row(dict(r)) for r in rows]
    return {"items": items, "count": len(items),
            "filters": {"type_code": type_code, "asset_code": asset_code,
                        "news_id": news_id, "since_ts": since_ts, "limit": limit}}


@router.get("/api/event_atoms/{atom_id}")
async def get_event_atom(atom_id: int):
    """单事件原子详情 + 同新闻候选时间 + 影响曲线（4 窗口聚合）。"""
    async with aiosqlite.connect(config.DB_PATH, timeout=20) as conn:
        conn.row_factory = aiosqlite.Row
        cur = await conn.execute(
            """SELECT ea.*, det.code AS event_type_code, det.name_zh AS event_type_zh,
                       det.category AS event_category, da.code AS asset_code
                FROM event_atom ea
                JOIN dim_event_type det ON det.id = ea.event_type_id
                LEFT JOIN dim_asset da ON da.id = ea.asset_id
                WHERE ea.id = ?""",
            (int(atom_id),),
        )
        row = await cur.fetchone()
        await cur.close()
        if row is None:
            raise HTTPException(status_code=404, detail="event_atom not found")
        atom = _serialize_event_atom_row(dict(row))
        cur = await conn.execute(
            """SELECT id, candidate_ts, tz, source, confidence, reasoning,
                       is_primary, parser_version, created_at
                FROM event_time_candidates
                WHERE raw_news_id = ?
                ORDER BY is_primary DESC, confidence DESC, id""",
            (atom["raw_news_id"],),
        )
        cand_rows = await cur.fetchall()
        await cur.close()
        candidates: List[Dict[str, Any]] = []
        for r in cand_rows:
            c = dict(r)
            c["candidate_ts"] = _format_time(c.get("candidate_ts"))
            c["created_at"]   = _format_time(c.get("created_at"))
            c["confidence"]   = round(float(c["confidence"]), 3) if c.get("confidence") is not None else None
            c["is_primary"]   = bool(c.get("is_primary"))
            candidates.append(c)
    curve = await asyncio.to_thread(
        _event_impact_curve.aggregate_impact_curve,
        event_atom_id=int(atom_id), asset_code=atom.get("asset_code") or None,
    )
    for wl, point in curve.items():
        if isinstance(point, dict) and point.get("sample_ts"):
            point["sample_ts_iso"] = _format_time(point["sample_ts"])
    return {"atom": atom, "candidates": candidates, "impact_curve": curve}


@router.get("/api/event_impact_curves")
async def list_event_impact_curves(
    asset_code: Optional[str] = None,
    type_code: Optional[str] = None,
    limit: int = 100,
):
    """影响曲线列表（按 sample_ts DESC），最大 1000。"""
    limit = max(1, min(int(limit or 100), 1000))
    async with aiosqlite.connect(config.DB_PATH, timeout=20) as conn:
        conn.row_factory = aiosqlite.Row
        cur = await conn.execute(
            """SELECT eic.event_atom_id, eic.asset_id, eic.window_label,
                       eic.delta_pct, eic.price, eic.volume_ratio,
                       eic.sample_ts, eic.note,
                       a.code AS asset_code,
                       det.code AS event_type_code
                FROM event_impact_curve eic
                LEFT JOIN dim_asset a       ON a.id = eic.asset_id
                LEFT JOIN event_atom ea      ON ea.id = eic.event_atom_id
                LEFT JOIN dim_event_type det ON det.id = ea.event_type_id
                ORDER BY eic.sample_ts DESC, eic.event_atom_id LIMIT ?""",
            (limit,),
        )
        rows = await cur.fetchall()
        await cur.close()
    items = []
    for r in rows:
        d = dict(r)
        d["sample_ts"] = _format_time(d.get("sample_ts"))
        if asset_code and d.get("asset_code") != asset_code:
            continue
        if type_code and d.get("event_type_code") != type_code:
            continue
        items.append(d)
    return {"items": items, "count": len(items),
            "filters": {"asset_code": asset_code, "type_code": type_code, "limit": limit}}


@router.get("/api/event_impact_curves/by_atom/{atom_id}")
async def get_event_impact_curve_by_atom(atom_id: int):
    """单个事件原子的完整影响曲线（按窗口排序）。"""
    rows = await asyncio.to_thread(_event_impact_curve.get_impact_curve, event_atom_id=int(atom_id))
    for r in rows:
        r["sample_ts"] = _format_time(r.get("sample_ts"))
    aggregated = await asyncio.to_thread(
        _event_impact_curve.aggregate_impact_curve, event_atom_id=int(atom_id)
    )
    for wl, p in aggregated.items():
        if isinstance(p, dict) and p.get("sample_ts"):
            p["sample_ts_iso"] = _format_time(p["sample_ts"])
    return {"atom_id": int(atom_id), "rows": rows, "aggregated": aggregated}


@router.get("/api/event_impact_stat")
async def get_event_impact_stat(days: int = 7):
    """每日聚合统计（event_impact_stat 表）。默认 7 天，最多 90 天。"""
    days = max(1, min(int(days or 7), 90))
    async with aiosqlite.connect(config.DB_PATH, timeout=20) as conn:
        conn.row_factory = aiosqlite.Row
        cur = await conn.execute(
            """SELECT date_local, impact_curve_filled, event_atoms_total,
                       fallback_used, avg_window_minutes, clock_jumps,
                       date_epoch_start, created_at
                FROM event_impact_stat ORDER BY date_local DESC LIMIT ?""",
            (days,),
        )
        rows = await cur.fetchall()
        await cur.close()
    return {"items": [dict(r) for r in rows], "count": len(rows), "days": days}


@router.get("/api/event_dim/assets")
async def get_dim_assets():
    """维度表 - 资产列表（用于前端下拉/标签着色）。"""
    rows = await asyncio.to_thread(_dim_tables.list_assets)
    return {"items": rows, "count": len(rows)}


@router.get("/api/event_dim/event_types")
async def get_dim_event_types():
    """维度表 - 事件类型列表（含 direction_bias 默认方向偏差）。"""
    rows = await asyncio.to_thread(_dim_tables.list_event_types)
    return {"items": rows, "count": len(rows)}


@router.get("/api/event_atom/summary")
async def get_event_atom_summary():
    """事件原子汇总：按 type_code / asset_code / category 聚合最近 7 天计数。"""
    since_ts = int((time.time() - 7 * 86400))
    by_type = await asyncio.to_thread(_event_atom.count_events_by_type, since_ts=since_ts)
    by_asset = {}
    by_cat = {}
    async with aiosqlite.connect(config.DB_PATH, timeout=20) as conn:
        conn.row_factory = aiosqlite.Row
        cur = await conn.execute(
            """SELECT a.code AS code, COUNT(*) AS cnt
               FROM event_atom ea LEFT JOIN dim_asset a ON a.id = ea.asset_id
               WHERE ea.event_time >= ? GROUP BY a.code""",
            (since_ts,),
        )
        for r in await cur.fetchall():
            by_asset[r["code"] or "UNKNOWN"] = int(r["cnt"])
        await cur.close()
        cur = await conn.execute(
            """SELECT det.category AS category, COUNT(*) AS cnt
               FROM event_atom ea JOIN dim_event_type det ON det.id = ea.event_type_id
               WHERE ea.event_time >= ? GROUP BY det.category""",
            (since_ts,),
        )
        for r in await cur.fetchall():
            by_cat[r["category"] or "UNKNOWN"] = int(r["cnt"])
        await cur.close()
    return {
        "by_type_code": by_type,
        "by_asset_code": by_asset,
        "by_category": by_cat,
        "window_days": 7,
        "since_ts": since_ts,
    }

def __getattr__(name: str):
    """懒读库路径（v475）：`DB_PATH` 不再在 import 时快照。

    原来这里写的是 `DB_PATH = config.DB_PATH` —— 只要本模块是在**别人把
    config.DB_PATH 改坏的那一刻**被首次 import，之后整场测试/整个进程就一直在
    读那个库（典型症状：test_structured_api_v172 单跑绿、全量 6 条全红 count=0）。
    改成属性访问时现读，`db.get_db_path()` 的语义与之一致。
    """
    if name == "DB_PATH":
        return config.DB_PATH
    raise AttributeError(name)
