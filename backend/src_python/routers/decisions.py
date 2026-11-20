# -*- coding: utf-8 -*-
"""决策多周期结算读数路由（结构切片，2026-11-17）。

从 api_server.py 搬来 v342 那一段 2 条端点（decision/{id}/horizons、horizons/stats）
及两个同步助手。助手在段外无调用点，随段搬走，不留回引。
"""
from __future__ import annotations

import asyncio
import sqlite3
import time
from typing import Any, Dict, Optional

from fastapi import APIRouter, HTTPException

import aiosqlite
import db
import config


router = APIRouter()


# ─── v342: 多周期结算读数（15m/1h/4h/1d/5d） ───────────────────────────
# 主口径（5 天）不在这些端点里重算，只读 `decision_outcomes_horizon` 的另存读数，
# 解决"这套系统擅长 1 小时还是 5 天"这个此前无法回答的问题。


def _decision_horizons_sync(decision_id: int) -> dict:
    """单条决策的各周期读数 + 决策头信息。"""
    import multi_horizon_settle as _mhs
    conn = db.get_connection()
    conn.row_factory = sqlite3.Row
    try:
        head = conn.execute(
            "SELECT id, asset, intended_action, final_action, decision_type, "
            "       entry_price, decision_ts, decision_source, score, confidence, status "
            "FROM trade_decisions WHERE id=?", (int(decision_id),)).fetchone()
        if head is None:
            return {"ok": False, "error": "decision not found"}
        return {"ok": True, "decision": dict(head),
                "horizons": _mhs.read_horizons(conn, int(decision_id))}
    finally:
        conn.close()


@router.get("/api/decision/{decision_id}/horizons", tags=["decision"])
async def get_decision_horizons(decision_id: int):
    """一条决策在 15m/1h/4h/1d/5d 上的收益与是否判赢（取不到价如实标 no_price）。"""
    return await asyncio.to_thread(_decision_horizons_sync, decision_id)


def _horizon_stats_sync(days: int, asset: Optional[str]) -> dict:
    """按周期聚合：样本 / 已结算 / 胜数 / 胜率 / 平均净收益。

    只统计 status='settled' 的行；no_price / not_executable / implausible 单列计数，
    不混进胜率分母（否则"没取到价"会被算成"判断错"）。
    """
    conn = db.get_connection()
    conn.row_factory = sqlite3.Row
    try:
        cutoff = int(time.time()) - days * 86400
        params: list = [cutoff]
        where = "decision_ts >= ?"
        if asset:
            where += " AND asset = ?"
            params.append(str(asset).strip().upper())
        rows = conn.execute(
            "SELECT horizon, horizon_sec, status, COUNT(*) AS n, "
            "       SUM(CASE WHEN hit=1 THEN 1 ELSE 0 END) AS wins, "
            "       AVG(CASE WHEN status='settled' THEN pnl_pct END) AS avg_pnl, "
            "       AVG(CASE WHEN status='settled' THEN outcome_pct END) AS avg_outcome "
            "FROM decision_outcomes_horizon WHERE " + where + " "
            "GROUP BY horizon, horizon_sec ORDER BY horizon_sec ASC",
            tuple(params)).fetchall()
        if not rows:
            return {"ok": True, "days": days, "asset": asset, "horizons": [],
                    "note": "暂无多周期结算读数（等下一趟结算或历史库无分钟线）"}
        out = []
        status_totals: dict = {}
        for r in rows:
            status_totals[r["status"] or "unknown"] = status_totals.get(r["status"] or "unknown", 0) + int(r["n"])
        # 逐周期合并 settled 与非 settled（同一周期可能有多行不同 status）
        merged: dict = {}
        for r in rows:
            key = r["horizon"]
            item = merged.setdefault(key, {
                "horizon": key, "horizon_sec": int(r["horizon_sec"]),
                "settled": 0, "wins": 0, "hit_rate": None,
                "avg_pnl_pct": None, "avg_outcome_pct": None,
                "no_price": 0, "not_executable": 0, "implausible": 0,
            })
            n = int(r["n"] or 0)
            if (r["status"] or "") == "settled":
                item["settled"] = n
                item["wins"] = int(r["wins"] or 0)
                item["avg_pnl_pct"] = round(float(r["avg_pnl"]), 3) if r["avg_pnl"] is not None else None
                item["avg_outcome_pct"] = round(float(r["avg_outcome"]), 3) if r["avg_outcome"] is not None else None
            elif (r["status"] or "") in ("no_price", "not_executable", "implausible"):
                item[(r["status"] or "")] = n
        for key, item in merged.items():
            if item["settled"]:
                item["hit_rate"] = round(item["wins"] / item["settled"], 4)
            out.append(item)
        return {"ok": True, "days": days, "asset": asset, "horizons": out,
                "status_totals": status_totals}
    finally:
        conn.close()


@router.get("/api/horizons/stats", tags=["decision"])
async def get_horizon_stats(days: int = 30, asset: Optional[str] = None):
    """按周期看胜率与平均净收益：回答"这套判断在哪一个周期上真的有效"。"""
    days = min(max(1, int(days)), 365)
    return await asyncio.to_thread(_horizon_stats_sync, days, asset)


# ─── 决策已读标记（2026-11-17 从 api_server 追加）───────────────

@router.post("/api/decisions/mark-all-read")
async def mark_all_decisions_read():
    """把 ai_decisions.status='UNREAD' 批量改为 'REVIEWED'（用户已查看过 dashboard）。

    dashboard 红点显示 UNREAD 数；用户点 dashboard 后自动调一次清零。
    返回：{marked_count: N}
    """
    try:
        async with aiosqlite.connect(config.DB_PATH, timeout=10) as conn:
            cur = await conn.execute(
                "UPDATE ai_decisions SET status='REVIEWED' WHERE status='UNREAD'"
            )
            marked = cur.rowcount if hasattr(cur, "rowcount") else 0
            await conn.commit()
        return {"marked_count": marked}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"mark-decisions-read failed: {e}")


@router.post("/api/decisions/{decision_id}/mark-read")
async def mark_decision_read(decision_id: int):
    """访问单条决策详情时调：UNREAD → REVIEWED。"""
    try:
        async with aiosqlite.connect(config.DB_PATH, timeout=5) as conn:
            await conn.execute(
                "UPDATE ai_decisions SET status='REVIEWED' WHERE id=? AND status='UNREAD'",
                (decision_id,),
            )
            await conn.commit()
        return {"marked_id": decision_id}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"mark-decision-read failed: {e}")

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
