# -*- coding: utf-8 -*-
"""复盘 run / 快照 / 导出路由（结构切片，2026-11-17）。

从 api_server.py 搬来 4 条端点：
  POST /api/review/run（触发一次反思跑批，同步阻塞式）
  GET  /api/review/signals、/snapshot（顶部总览，与模拟盘同源同口径）、/export.xlsx
助手 _run_review_sync 与 _fetch_event_rows（8 处调用）仍在 api_server，用 _api() 回引。
"""
from __future__ import annotations

import asyncio
import sqlite3
from datetime import datetime, timezone
from typing import Any, Dict, Optional

from fastapi import APIRouter, HTTPException, Query, Response
from fastapi.responses import JSONResponse

import config
from review import review_agent
from review import review_snapshot


router = APIRouter()


def _api():
    """延迟回引 api_server（反思跑批助手仍在原文件）。"""
    import api_server
    return api_server


@router.post("/api/review/run")
async def run_review(
    scope: str = "week",
    hours: Optional[int] = None,
    from_ts: Optional[int] = None,
    to_ts: Optional[int] = None,
):
    """手动触发 AI 复盘（today | week | all）。

    v290 P0+（反思系统时段统一）：新增 hours/from_ts/to_ts 窗口参数，
    与全局 PeriodSelector 同源同窗 —— 只要传了窗口，scope 就被忽略，
    SQL 严格按 created_at ∈ [lo, hi] 过滤。
    传参优先级：hours > from_ts/to_ts > scope（向后兼容）。
    """
    if scope not in ("today", "week", "all"):
        raise HTTPException(status_code=400, detail="scope must be today|week|all")
    return await asyncio.to_thread(_api()._run_review_sync, scope, hours, from_ts, to_ts)


@router.get("/api/review/signals")
async def get_review_signals():
    """提炼的参考信号（品种/方向/阶段/强度/入场时机胜率），供 AI 分析新新闻参考。"""
    return await asyncio.to_thread(review_agent.derive_reference_signals)


@router.get("/api/review/snapshot")
async def get_review_snapshot(
    hours: Optional[int] = None,
    days: Optional[int] = None,      # 8100 那边的历史参数，删掉它那份实现后仍需兼容
    from_ts: Optional[int] = None,
    to_ts: Optional[int] = None,
):
    """反思系统顶部总览数据 —— 与模拟盘同源同口径，保证两处数字一致。

    paper      = /api/replay/stats 的 overall（真实持仓 ai_decisions，实时、全量）
    quick_sim  = quick_sim.snapshot() 的 overall（快速评测 quick_sim_trades，
                  v180 P0+ 支持 hours / from_ts / to_ts 时段过滤）
    window     = 时段内的信号 / 已结算（v180 P0+：与时段同步；旧 today 字段保留
                  兼容老前端）

    时段过滤（前端 PeriodSelector 选定后透传）：
    - hours=24/168/720   → 今日/近7天/近30天
    - from_ts=&to_ts=    → 自定义 UTC 秒对（与 hours 互斥）
    """
    if hours is None and days is not None:
        hours = int(days) * 24           # days 是旧参数，换算成 hours（与 8100 原实现一致）
    _ck = "snap:%s:%s:%s" % (hours, from_ts, to_ts)
    _hit = _api()._ttl_get(_ck, 4.0)
    if _hit is not None:
        return _hit

    # v310 P2: 走共享实现 review_snapshot.build_snapshot() ——
    # 8000 (api_server) 与 8100 (bridge_server) 用**同一份代码 + 同一个 DB**,
    # 杜绝两边各算各的、导致"真实持仓战绩"两边对不上。
    # 旧实现在这里自己拼 paper/quick_sim/today/window 四段; bridge 侧的本地简化版
    # 数据源用的是 trade_reflections_v2 且忽略时间窗口 → 前端显示 2208 笔 /
    # 胜率 13.35% / 平均 +117.71% 这种假数据 (实为污染列的平均值)。
    def _build() -> dict:
        conn = sqlite3.connect(str(config.DB_PATH), timeout=20)
        conn.row_factory = sqlite3.Row
        try:
            return review_snapshot.build_snapshot(
                conn, hours=hours, from_ts=from_ts, to_ts=to_ts
            )
        finally:
            conn.close()

    _res = await asyncio.to_thread(_build)
    _api()._ttl_put(_ck, _res)
    return _res


@router.get("/api/review/export.xlsx")
async def export_review_xlsx():
    """v170 Phase D：导出反思系统全量数据为 .xlsx，5 sheets:
    Overview / Trades / Reflections / Insights / Experiences。

    单只读连接 + 纯 SQL 聚合（无 LLM 调用），< 2s 完成。
    """
    try:
        from review.review_excel_export import build_review_xlsx
        payload = await asyncio.to_thread(build_review_xlsx)
    except Exception as exc:
        return JSONResponse(
            status_code=500,
            content={"ok": False, "error": f"export failed: {exc!r}"},
        )
    ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M")
    filename = f"trident_review_{ts}.xlsx"
    return Response(
        content=payload,
        media_type=(
            "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
        ),
        headers={
            "Content-Disposition": (
                f'attachment; filename="{filename}"'
            ),
            "Cache-Control": "no-store",
        },
    )

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
