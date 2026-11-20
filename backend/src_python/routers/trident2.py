# -*- coding: utf-8 -*-
"""Trident 2.0 功能层路由（64 项能力统一出口）。

设计要点：
1. 所有端点是**只读或纸面**：run 系列只写 t2_* 账本，不触碰真实订单；
2. 首次请求时对当前库执行 ``trident2.store.ensure_schema``（14+1 张 t2_* 表，幂等）；
3. 单条功能失败只让那一条失败（pipeline 已捕获），接口本身不 500；
4. 参数一律透传给功能函数，功能函数自己校验，缺参数时返回 not_configured。
"""
from __future__ import annotations

import sqlite3
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Body, HTTPException, Query
from pydantic import BaseModel

import db
import trident2
from trident2 import catalog, pipeline, registry, store

router = APIRouter()

_schema_ready: set = set()


def _conn() -> sqlite3.Connection:
    conn = db.get_connection()
    conn.row_factory = sqlite3.Row
    key = getattr(conn, "database", None) or "memory"
    if key not in _schema_ready:
        store.ensure_schema(conn)
        _schema_ready.add(key)
    return conn


def _close(conn: sqlite3.Connection) -> None:
    try:
        conn.close()
    except Exception:
        pass


# ── 目录 / 状态 ─────────────────────────────────────────────────────────
@router.get("/api/trident2/features", tags=["trident2"])
async def list_features(priority: str = "", category: str = "") -> Dict[str, Any]:
    """64 项候选功能总目录：是否已实现、最近一次运行结果。"""
    conn = _conn()
    try:
        snap = pipeline.status_snapshot(conn)
        items = snap.get("items", [])
        if priority:
            items = [i for i in items if (i.get("priority") or "") == priority.upper()]
        if category:
            items = [i for i in items if (i.get("category") or "") == category]
        return {"ok": True, "version": trident2.__version__,
                "summary": catalog.summary(), "features": items}
    finally:
        _close(conn)


@router.get("/api/trident2/status", tags=["trident2"])
async def status() -> Dict[str, Any]:
    conn = _conn()
    try:
        snap = pipeline.status_snapshot(conn)
        return {"ok": True, "implemented": len(registry.implemented()),
                "missing": sorted(registry.missing()), "snapshot": snap}
    finally:
        _close(conn)


@router.get("/api/trident2/spec/{feature_id}", tags=["trident2"])
async def spec(feature_id: str) -> Dict[str, Any]:
    s = registry.spec(feature_id)
    if not s:
        raise HTTPException(status_code=404, detail=f"{feature_id} 不在目录中")
    return {"ok": True, "spec": s, "implemented": feature_id in registry.implemented()}


# ── 运行 ────────────────────────────────────────────────────────────────
class RunBody(BaseModel):
    kwargs: Dict[str, Any] = {}


@router.post("/api/trident2/run/{feature_id}", tags=["trident2"])
async def run_feature(feature_id: str, body: Optional[RunBody] = Body(default=None)) -> Dict[str, Any]:
    """跑单个功能（结果落 t2_results，告警按 F35 分级去重）。"""
    if feature_id not in catalog.BY_ID:
        raise HTTPException(status_code=404, detail=f"{feature_id} 不在 64 项目录中")
    conn = _conn()
    try:
        kwargs = (body.kwargs if body else {}) or {}
        r = pipeline.run_one(conn, feature_id, **kwargs)
        return {"ok": r.status != "error", "result": r.to_dict()}
    finally:
        _close(conn)


@router.post("/api/trident2/run", tags=["trident2"])
async def run_all(priority: str = Query(default="P0"),
                  feature_ids: Optional[List[str]] = Query(default=None)) -> Dict[str, Any]:
    """批量巡检（默认只跑 P0，避免一次压满库）。"""
    conn = _conn()
    try:
        if feature_ids:
            results = pipeline.run_many(conn, feature_ids)
        else:
            results = pipeline.run_all(conn, priority=priority or None)
        return {"ok": True, "count": len(results),
                "results": [r.to_dict() for r in results]}
    finally:
        _close(conn)


# ── 结果 / 告警 ─────────────────────────────────────────────────────────
@router.get("/api/trident2/results", tags=["trident2"])
def results(feature_id: str = "", limit: int = 50) -> Dict[str, Any]:
    conn = _conn()
    try:
        if feature_id:
            rows = store.recent_results(conn, feature_id, limit=limit)
        else:
            rows = [dict(r) for r in conn.execute(
                "SELECT * FROM t2_results ORDER BY id DESC LIMIT ?", (limit,))]
        return {"ok": True, "results": rows}
    finally:
        _close(conn)


@router.get("/api/trident2/alerts", tags=["trident2"])
async def alerts(limit: int = 50, level: str = "") -> Dict[str, Any]:
    conn = _conn()
    try:
        return {"ok": True, "alerts": store.alerts(conn, level=level or None, limit=limit)}
    finally:
        _close(conn)


# ── 纸面账本 ────────────────────────────────────────────────────────────
@router.get("/api/trident2/account", tags=["trident2"])
async def account() -> Dict[str, Any]:
    conn = _conn()
    try:
        acct = store.account(conn)
        return {"ok": True, "account": acct,
                "note": "paper 账本；LIVE_TRADING 未启用时任何功能都不会写真实订单"}
    finally:
        _close(conn)


@router.get("/api/trident2/positions", tags=["trident2"])
def positions(status: str = "open") -> Dict[str, Any]:
    conn = _conn()
    try:
        rows = [dict(r) for r in conn.execute(
            "SELECT * FROM t2_positions WHERE status=? ORDER BY id DESC LIMIT 100", (status,))]
        return {"ok": True, "positions": rows}
    finally:
        _close(conn)


@router.get("/api/trident2/orders", tags=["trident2"])
def orders(open_only: bool = False, limit: int = 100) -> Dict[str, Any]:
    conn = _conn()
    try:
        if open_only:
            rows = [dict(r) for r in conn.execute(
                "SELECT * FROM t2_orders WHERE status IN ('open','pending') ORDER BY id DESC LIMIT ?",
                (int(limit),))]
        else:
            rows = [dict(r) for r in conn.execute(
                "SELECT * FROM t2_orders ORDER BY id DESC LIMIT ?", (int(limit),))]
        return {"ok": True, "orders": rows}
    finally:
        _close(conn)


# ── 交互：自然语言改单（只出草稿） ──────────────────────────────────────
class AmendBody(BaseModel):
    text: str
    confirm: bool = False


@router.post("/api/trident2/nl-amend", tags=["trident2"])
async def nl_amend(body: AmendBody) -> Dict[str, Any]:
    """F17：把我一句人话变成订单草稿；永远 dry_run，不执行。"""
    conn = _conn()
    try:
        r = pipeline.run_one(conn, "F17", text=body.text, confirm=body.confirm)
        return {"ok": r.status == "ok", "result": r.to_dict()}
    finally:
        _close(conn)
