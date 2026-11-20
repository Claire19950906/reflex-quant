# -*- coding: utf-8 -*-
"""admin 运维路由（结构切片，2026-11-17）。

从 api_server.py 搬来 id-reconcile 那 6 条：migrate / backfill / check / resolve /
alerts / status。逻辑逐字未改，只把 `@app.` 换成 `@router.`，_PydBaseModel 统一成 BaseModel。
依赖只有 db / id_reconcile。
"""
from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, Depends

import license_auth

from pydantic import BaseModel

import db
import id_reconcile

# v453 安全：这些路由全是「写」操作（重置/归档/备份/策略/凭证），
# full 模式默认要求 API Key；仅回环地址在 TRIDENT_LOCAL_TRUST=1 时免鉴权。
router = APIRouter(dependencies=[Depends(license_auth.require_api_key)])


def _api():
    """延迟回引 api_server（缓存/轮询助手仍在原文件）。"""
    import api_server
    return api_server


from pydantic import BaseModel as BaseModel


class _IdReconcileCheckBody(BaseModel):
    auto_quarantine: bool = True


class _IdReconcileResolveBody(BaseModel):
    trade_uuid: str
    note: str = ""
    resolver: str = "manual"
    skip_recheck: bool = False


@router.post("/api/admin/id-reconcile/migrate", tags=["admin", "id-reconcile"])
async def admin_id_reconcile_migrate():
    """建 alerts 表 + 给 5 链路表加 trade_uuid / quarantine 列。幂等。"""
    try:
        result = id_reconcile.ensure_schema()
        return {"ok": True, **result}
    except Exception as exc:
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}


@router.post("/api/admin/id-reconcile/backfill", tags=["admin", "id-reconcile"])
async def admin_id_reconcile_backfill():
    """backfill trade_uuid + 内嵌对账（双保险 #1）。"""
    try:
        result = id_reconcile.backfill_uuids()
        return {"ok": True, **result}
    except Exception as exc:
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}


@router.post("/api/admin/id-reconcile/check", tags=["admin", "id-reconcile"])
async def admin_id_reconcile_check(body: _IdReconcileCheckBody):
    """三字段对账 + 应急 quarantine。"""
    try:
        result = id_reconcile.check_mismatches(auto_quarantine=body.auto_quarantine)
        return {"ok": True, **result}
    except Exception as exc:
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}


@router.post("/api/admin/id-reconcile/resolve", tags=["admin", "id-reconcile"])
async def admin_id_reconcile_resolve(body: _IdReconcileResolveBody):
    """人工解锁一条 quarantine 记录。流程：重新跑对账 -> PASS 才解锁。"""
    try:
        result = id_reconcile.resolve(
            body.trade_uuid, note=body.note,
            resolver=body.resolver,
            auto_recheck=not body.skip_recheck)
        return {"ok": result.get("ok", False), **result}
    except Exception as exc:
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}


@router.get("/api/admin/id-reconcile/alerts", tags=["admin", "id-reconcile"])
async def admin_id_reconcile_alerts(
    limit: int = 50,
    type: Optional[str] = None,
    unresolved_only: bool = False,
):
    """列告警；默认最近 50 条。"""
    try:
        result = id_reconcile.list_alerts(
            limit=limit, alert_type=type, unresolved_only=unresolved_only)
        return {"ok": True, "count": len(result), "alerts": result}
    except Exception as exc:
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}


@router.get("/api/admin/id-reconcile/status", tags=["admin", "id-reconcile"])
def admin_id_reconcile_status():
    """概览：5 链路表 UUID 覆盖率 + 当前 quarantine 数 + 未处理 alert 数。"""
    try:
        conn = db.get_connection()
        try:
            def _count(sql, params=()):
                return conn.execute(sql, params).fetchone()[0]
            cov = {}
            for tbl in ("ai_decisions", "trade_reflections_v2",
                        "structured_reflections", "decision_errors",
                        "quick_sim_trades"):
                total = _count(f"SELECT COUNT(*) FROM {tbl}")
                has = _count(
                    f"SELECT COUNT(*) FROM {tbl} "
                    f"WHERE trade_uuid IS NOT NULL AND trade_uuid != ''")
                cov[tbl] = {"total": total, "with_uuid": has,
                            "missing_uuid": total - has}
            quarantine = _count(
                "SELECT COUNT(*) FROM trade_reflections_v2 "
                "WHERE quarantined_at IS NOT NULL")
            unresolved = _count(
                "SELECT COUNT(*) FROM id_reconcile_alerts "
                "WHERE resolved_at IS NULL AND alert_type != 'resolved'")
            by_type = {}
            for row in conn.execute(
                "SELECT alert_type, COUNT(*) FROM id_reconcile_alerts "
                "WHERE resolved_at IS NULL AND alert_type != 'resolved' "
                "GROUP BY alert_type"
            ).fetchall():
                by_type[row[0]] = row[1]
            return {
                "ok": True,
                "coverage": cov,
                "quarantined": quarantine,
                "unresolved_alerts": unresolved,
                "unresolved_by_type": by_type,
            }
        finally:
            conn.close()
    except Exception as exc:
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
