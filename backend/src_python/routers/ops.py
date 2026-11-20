# -*- coding: utf-8 -*-
"""运维 / 回测 / 交易日志路由（结构切片，2026-11-17）。

从 api_server.py 搬来 9 条：v188/admin 的 daily-archive 与 db-stats、
v189/backtest 的 active_run / archive / cleanup、v189/journal 的
record / rollup / {date} / 列表。逻辑逐字未改，点内惰性 import
（daily_archive / run_journal 等）原样保留。
"""
from __future__ import annotations

import sqlite3
from typing import Any, Dict, Optional

from fastapi import APIRouter, Depends

import license_auth

import db

# v453 安全：这些路由全是「写」操作（重置/归档/备份/策略/凭证），
# full 模式默认要求 API Key；仅回环地址在 TRIDENT_LOCAL_TRUST=1 时免鉴权。
router = APIRouter(dependencies=[Depends(license_auth.require_api_key)])


@router.post("/api/v188/admin/daily-archive")
async def v188_daily_archive(days: int = 90):
    """手动触发归档: 把 >N 天的 backtest_runs 标记 archived=1。"""
    try:
        import daily_archive as _da
        return _da.archive_old_runs(days=int(days))
    except Exception as exc:
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}


@router.get("/api/v188/admin/db-stats")
async def v188_db_stats():
    """每张表行数 + run_id 分布 + db 文件大小。"""
    try:
        import daily_archive as _da
        return _da.db_stats()
    except Exception as exc:
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}


# v189: 回测每日重置 + 反思日记 + 归档
@router.get("/api/v189/backtest/active_run")
async def v189_active_run():
    """v189: 当前活跃 run_id。"""
    try:
        import daily_archive as _da
        return {"ok": True, "active_run_id": _da.get_active_run_id()}
    except Exception as exc:
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}


@router.post("/api/v189/backtest/archive")
async def v189_archive(asset_class: str = "all", archive_days: int = 1):
    """v189: 手动触发归档 (默认 archive_days=1 = 昨日及更早)。
    通常由定时任务在 UTC 00:00 自动调用。
    """
    try:
        import daily_archive as _da
        return _da.daily_archive(asset_class, archive_days)
    except Exception as exc:
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}


@router.post("/api/v189/backtest/cleanup")
async def v189_cleanup(days: int = 90, dry_run: bool = False):
    """v189: 清理 90 天前的 archived 数据。"""
    try:
        import daily_archive as _da
        return _da.cleanup_old_runs(days, dry_run)
    except Exception as exc:
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}


@router.post("/api/v189/journal/record")
async def v189_journal_record(body: Dict[str, Any]):
    """v189: 记录单条反思周期日志 (供 paper_reflection_worker 调用)。"""
    try:
        from review import reflection_journal as _rj
        rid = _rj.record_cycle(
            decision_id=int(body.get("decision_id", 0)),
            run_id=str(body.get("run_id", "")),
            reflection_id=body.get("reflection_id"),
            outcome=str(body.get("outcome", "pending")),
            llm_invoked=int(body.get("llm_invoked", 0)),
            latency_ms=body.get("latency_ms"),
            ontology_signal=str(body.get("ontology_signal", "")),
            error=str(body.get("error", "")),
        )
        return {"ok": rid is not None, "cycle_id": rid}
    except Exception as exc:
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}


@router.post("/api/v189/journal/rollup")
async def v189_journal_rollup(date: Optional[str] = None, run_id: Optional[str] = None):
    """v189: 滚动归档 (每日 UTC 23:55 自动调用)。"""
    try:
        from review import reflection_journal as _rj
        return _rj.rollup_daily(date, run_id)
    except Exception as exc:
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}


@router.get("/api/v189/journal/{date}")
async def v189_journal_summary(date: str, run_id: Optional[str] = None):
    """v189: 取指定日期的反思日报。"""
    try:
        from review import reflection_journal as _rj
        return _rj.gen_summary(date, run_id)
    except Exception as exc:
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}


@router.get("/api/v189/journal")
def v189_journal_recent(days: int = 30, run_id: Optional[str] = None):
    """v189: 取最近 N 天反思日报列表(供前端日历热力图)。"""
    import db as _db
    conn = _db.get_connection()
    try:
        # v189-fix: dict(r) 要求 row_factory=sqlite3.Row, 否则抛
        # "ValueError: dictionary update sequence element #0 has length 10; 2 is required"
        conn.row_factory = sqlite3.Row
        rows = conn.execute("""
            SELECT journal_date, run_id, n_decisions, n_wins, n_losses, win_rate,
                   pnl_usdt, unknown_rate, llm_invoked_count, ontology_breakdown,
                   top_experiences, top_missed_signals, created_at
              FROM reflection_daily_journal
             WHERE date(journal_date) >= date('now', ?)
               AND (run_id=? OR ? IS NULL)
             ORDER BY journal_date DESC LIMIT ?
        """, (f"-{int(days)} days", run_id, run_id, int(days))).fetchall()
        items = [dict(r) for r in rows]
        import json as _json
        for it in items:
            try:
                it["ontology_breakdown"] = _json.loads(it.get("ontology_breakdown") or "{}")
            except Exception:
                pass
        return {"ok": True, "days": int(days), "items": items}
    except Exception as exc:
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
    finally:
        conn.close()


# v188+ 技术指标注册表 + Pipeline
