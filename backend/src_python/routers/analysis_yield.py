# -*- coding: utf-8 -*-
"""分析产能体检端点（v414，只读）。

为什么单独开一条：
  `/api/v1/scorecard` 讲的是**绩效**（真实纸面成交，口径在 quick_sim_trades），
  这里讲的是**分析产能**（决策终态，口径在 analysis/contract.py）。两者分母不同，
  混进一个端点迟早会被当成同一个数。

口径：所有数字由 `analysis_yield` 现算，不读缓存、不写库；连库用 `mode=ro`。
"""
from __future__ import annotations

import sqlite3
from typing import Annotated, Any, Dict

from fastapi import APIRouter, Query

import db
from analysis_yield import (channel_quality, gate_funnel, load_rows, queue_health,
                            report, retry_candidates)

router = APIRouter()


def _ro_conn() -> sqlite3.Connection:
    """只读连接：本端点永远不该写库，用 mode=ro 从物理上保证。"""
    conn = sqlite3.connect(f"file:{db.get_db_path()}?mode=ro", uri=True, timeout=30.0)
    conn.row_factory = sqlite3.Row
    return conn


def _yield(days: int) -> Dict[str, Any]:
    conn = _ro_conn()
    try:
        rows = load_rows(conn, days)
        rep = report(rows, days=days)
        retry = retry_candidates(conn, days)
        channels = channel_quality(conn, days)
        queue = queue_health(conn, hours=min(24, days * 24))
        funnel = gate_funnel(conn, days)
    finally:
        conn.close()
    rep["retry"] = {"n": len(retry), "items": retry[:50]}
    rep["channels"] = channels
    rep["queue"] = queue
    rep["funnel"] = funnel
    return rep


@router.get("/api/analysis/yield")
async def analysis_yield(
    days: Annotated[int, Query(ge=1, le=90)] = 7,
):
    """近 N 天决策终态分布 + 分层指标 + 处置口径（只读）。"""
    try:
        return {"ok": True, "data": _yield(int(days))}
    except Exception as exc:
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}


@router.get("/api/analysis/yield/retry-list")
async def analysis_yield_retry(
    days: Annotated[int, Query(ge=1, le=90)] = 7,
    limit: Annotated[int, Query(ge=1, le=1000)] = 200,
):
    """模型侧失败、且之后没分析成功的新闻清单（按 news_id 去重）。"""
    try:
        conn = _ro_conn()
        try:
            items = retry_candidates(conn, int(days), int(limit))
        finally:
            conn.close()
        return {"ok": True, "n": len(items), "data": items}
    except Exception as exc:
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
