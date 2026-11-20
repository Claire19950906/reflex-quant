# -*- coding: utf-8 -*-
"""总览 / 结构化反思飞轮路由（结构切片，2026-11-17）。

从 api_server.py 搬来 3 条端点：
  /api/dashboard/overview（对齐 dashboard/app.py 离线复盘：健康 + 信号统计 + 最近结算单）
  /api/structured_reflection/status（worker 健康 + 队列 pending/promoted/failed/skipped）
  /api/structured_reflections（列飞轮记录，可按 status/asset/verdict 过滤）
DB_PATH / _SSE_QUEUES / get_replay_stats / get_replay_signals / strategy_store
仍在 api_server，router 用 _api() 回引，避免循环 import。
"""
from __future__ import annotations

import asyncio
import json
import os
import time
from datetime import datetime as _dt
from typing import Any, Dict, List, Optional

from fastapi import APIRouter

import db

router = APIRouter()


def _api():
    """延迟回引 api_server（复盘统计与 sqlite 路径仍在原文件）。"""
    import api_server
    return api_server


@router.get("/api/dashboard/overview")
async def dashboard_overview():
    """对齐 dashboard/app.py 的离线复盘能力：信号统计 + 健康检查 + 最近已结算单。"""
    health = {
        "status": "ok",
        "db_path": _api().DB_PATH,
        "db_exists": os.path.exists(_api().DB_PATH),
        "sse_clients": len(_api()._SSE_QUEUES),
    }
    stats = await _api().get_replay_stats()
    recent = await _api().get_replay_signals(settled=1, limit=12)
    strategies = await asyncio.to_thread(_api().strategy_store.list_strategies, False)
    runs = await asyncio.to_thread(_api().strategy_store.list_backtest_runs, None, 5)
    return {
        "health": health,
        "replay": stats,
        "recent_signals": recent,
        "strategies": strategies,
        "recent_runs": runs,
        "streamlit": {"url": os.getenv("STREAMLIT_URL", ""), "source": "dashboard/app.py"},
        "exports": {"signals_xlsx": "/api/export/signals"},
    }


@router.get("/api/structured_reflection/status")
def structured_reflection_status():
    """Worker 健康检查 + 队列统计。"""
    from engine import structured_reflection as sr
    conn = db.get_connection()
    last_activity = None
    try:
        n_pending = conn.execute(
            "SELECT COUNT(*) AS n FROM structured_reflections WHERE status='pending'"
        ).fetchone()[0]
        n_promoted = conn.execute(
            "SELECT COUNT(*) AS n FROM structured_reflections WHERE status='promoted'"
        ).fetchone()[0]
        n_failed = conn.execute(
            "SELECT COUNT(*) AS n FROM structured_reflections WHERE status='failed'"
        ).fetchone()[0]
        n_skipped = conn.execute(
            "SELECT COUNT(*) AS n FROM structured_reflections WHERE status='skipped'"
        ).fetchone()[0]
        # 顶层 truth insight 数
        n_truth = conn.execute(
            "SELECT COUNT(*) AS n FROM review_insights WHERE origin='truth' AND active=1 AND manual_deactivated=0"
        ).fetchone()[0]
        last_activity = conn.execute(
            "SELECT MAX(updated_at) FROM structured_reflections"
        ).fetchone()[0]
    finally:
        conn.close()
    status = sr.get_worker_status()
    # v477：这个 worker 跑在 **engine 进程**里，而本接口在 bridge 进程里执行。
    # bridge 自己的 _worker_started_at 永远是 0 → 页面恒显示 "反思引擎未启动"，
    # 但实测引擎日志一直在打 "[STRUCT-REFLECT] 处理 N 笔"。用户看到的是一次
    # 纯假警报，还会据此以为整个反思链路挂了。
    # 这里用库里 updated_at 的推进判断"引擎侧是否真的在消费队列"。
    started_in_process = bool(status.get("started"))
    engine_active = False
    heart = None
    try:
        _hconn = db.get_connection()
        try:
            heart = _hconn.execute(
                "SELECT last_tick_at, last_status FROM worker_heartbeat "
                "WHERE worker_name='structured_reflection'").fetchone()
        finally:
            _hconn.close()
    except Exception:
        heart = None
    # 优先用心跳（v477 起 worker 每轮都写），心跳过期再退回"队列是否有推进"
    ref_ts = heart[0] if heart else None
    if not ref_ts and last_activity:
        ref_ts = last_activity
    if ref_ts:
        try:
            _last = _dt.strptime(str(ref_ts)[:19], "%Y-%m-%d %H:%M:%S")
            engine_active = (time.time() - _last.timestamp()) <= 600
        except Exception:
            engine_active = False
    status.update({
        "queue_pending": n_pending,
        "queue_promoted": n_promoted,
        "queue_failed": n_failed,
        "queue_skipped": n_skipped,
        "truth_insights_active": n_truth,
        "started_in_bridge_process": started_in_process,
        "engine_active": engine_active,
        "last_activity_at": str(last_activity) if last_activity else None,
        "status_source": ("bridge-process" if started_in_process
                          else "db-activity(engine)"),
    })
    # started 对前端语义是"这条链路在跑"，而不是"本进程起了线程"
    if not started_in_process and engine_active:
        status["started"] = True
    return status


@router.get("/api/structured_reflections")
def list_structured_reflections(
    status: Optional[str] = None,
    asset: Optional[str] = None,
    verdict: Optional[str] = None,
    limit: int = 50,
):
    """列出 pending/promoted/failed 的飞轮记录。

    可选过滤：status / asset / verdict。
    """
    conn = db.get_connection()
    try:
        sql = (
            "SELECT id, decision_id, news_id, asset, event_type_code, verdict, "
            "score_coverage, score_fabrication, entities_fabricated, events_missing, "
            "status, retry_count, last_error, promoted_insight_id, created_at, updated_at "
            "FROM structured_reflections"
        )
        wheres = []
        params: list = []
        if status:
            wheres.append("status = ?")
            params.append(status)
        if asset:
            wheres.append("asset = ?")
            params.append(asset)
        if verdict:
            wheres.append("verdict = ?")
            params.append(verdict)
        if wheres:
            sql += " WHERE " + " AND ".join(wheres)
        sql += " ORDER BY id DESC LIMIT ?"
        params.append(max(1, min(limit, 500)))
        cur = conn.execute(sql, params)
        cols = [d[0] for d in cur.description]
        rows = [{cols[i]: r[i] for i in range(len(cols))} for r in cur.fetchall()]
    finally:
        conn.close()
    return {"items": rows, "count": len(rows)}
