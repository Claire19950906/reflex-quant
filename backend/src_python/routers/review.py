# -*- coding: utf-8 -*-
"""review 域路由（结构切片，2026-11-17）。

从 api_server.py 搬来 7 条只依赖 review_agent 的端点：归因队列/统计/重置/撤销重置/
人工纠错/自动复核/历史报告/生效经验。逻辑逐字未改，只把 `@app.` 换成 `@router.`。

`/api/review/run` 刻意**没有**搬：它依赖 api_server 内的 _run_review_sync（该函数还被
12977 行后台任务复用），留给后续单独抽出。
"""
from __future__ import annotations

import asyncio
import sqlite3
from typing import Any, Dict, List, Literal, Optional

import aiosqlite
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

import config
import db
from review import review_agent
from common.ttl_cache import ttl_get, ttl_put


router = APIRouter()


def _utc_ts_to_space_iso(ts: int) -> str:
    """延迟导入 api_server 的同名实现，避免 router ← api_server 循环 import。"""
    from api_server import _utc_ts_to_space_iso as _f
    return _f(ts)


def _resolve_window_with_log(hours, from_ts, to_ts, caller: str = ""):
    """延迟导入 api_server 的同名实现，避免循环 import。"""
    from api_server import _resolve_window_with_log as _f
    return _f(hours, from_ts, to_ts, caller)


class AttributionFeedback(BaseModel):
    status: Literal["agree", "corrected"]
    corrected_code: Optional[int] = None
    corrected_text: str = ""
    note: str = ""


@router.get("/api/review/attributions")
async def get_attributions(status: str = "pending", limit: int = 200):
    """归因复核队列：待复核(pending)/已确认(agree)/已纠错(corrected)/全部(all)。"""
    if status not in ("pending", "agree", "corrected", "all"):
        raise HTTPException(status_code=400, detail="status must be pending|agree|corrected|all")
    return await asyncio.to_thread(review_agent.list_attributions, status, limit)


@router.get("/api/review/attributions/stats")
async def get_attribution_stats():
    """跨来源失败归因统一汇总统计：失败总数 / 来源分布 / 各失败原因占比（哪类失败最多）。

    reset 状态 = 用户手动归档的记录，不计入统计（数据保留可恢复）。
    """
    return await asyncio.to_thread(review_agent.attribution_stats)


class AttributionResetRequest(BaseModel):
    scope: str = "all"        # all / news / review
    days: int = 0             # 0=全部；>0 仅重置 N 天前的
    confirm: bool = False     # 必须为 True 才会执行


@router.post("/api/review/attributions/reset")
async def post_reset_attributions(payload: AttributionResetRequest):
    """重置 pending 失败归因（软删除 = human_status='reset'，数据保留可恢复）。

    二次确认：payload.confirm 必须为 True，避免误操作。
    适用场景：用户看到大量 pending 归因想"清空眼不见心不烦"，但保留数据便于人工复核时再放出来。
    """
    if not payload.confirm:
        raise HTTPException(status_code=400, detail="confirm=true required")
    if payload.scope not in ("all", "news", "review"):
        raise HTTPException(status_code=400, detail="scope must be all|news|review")
    if payload.days < 0 or payload.days > 3650:
        raise HTTPException(status_code=400, detail="days must be 0..3650")
    return await asyncio.to_thread(
        review_agent.reset_attributions, payload.scope, payload.days
    )


class AttributionUnresetRequest(BaseModel):
    scope: str = "all"        # all / news / review
    days: int = 0             # 0=全部；>0 仅撤销 N 天前的
    confirm: bool = False     # 必须为 True 才会执行


@router.post("/api/review/attributions/unreset")
async def post_unreset_attributions(payload: AttributionUnresetRequest):
    """撤销重置（reset → pending），重新放入复核队列。

    二次确认：payload.confirm 必须为 True，避免误操作。
    与 reset 互为反向操作。适用场景：用户重置后想恢复部分归因继续复核。
    """
    if not payload.confirm:
        raise HTTPException(status_code=400, detail="confirm=true required")
    if payload.scope not in ("all", "news", "review"):
        raise HTTPException(status_code=400, detail="scope must be all|news|review")
    if payload.days < 0 or payload.days > 3650:
        raise HTTPException(status_code=400, detail="days must be 0..3650")
    return await asyncio.to_thread(
        review_agent.unreset_attributions, payload.scope, payload.days
    )


@router.post("/api/review/attributions/{attribution_id}/feedback")
def attribution_feedback(attribution_id: int, payload: AttributionFeedback):
    """人工复核/纠错一条归因；纠错且 code 不同会自动回灌经验规则（review_insights）。"""
    conn = db.get_connection()
    conn.row_factory = sqlite3.Row
    try:
        result = review_agent.apply_attribution_feedback(
            conn, attribution_id, payload.status,
            payload.corrected_code, payload.corrected_text, payload.note,
        )
    finally:
        conn.close()
    if not result.get("ok"):
        raise HTTPException(status_code=404, detail=result.get("error") or "纠错失败")
    return result


@router.post("/api/review/attributions/auto-review")
async def auto_review_attributions(
    min_confidence: float = 0.85, batch_max: int = 200,
    only_source: Optional[str] = None, dry_run: bool = False,
):
    """v202 R3-fix: 一键自动通过高置信度 pending 归因复核（human_status=auto_reviewed）。

    仅标 human_status='auto_reviewed' + reviewed_at，保留 corrected_code/text 留给人工。
    不触发 rule_pushed（留给 reconcile worker）。
    """
    return await asyncio.to_thread(
        review_agent.auto_review_attributions,
        min_confidence=float(min_confidence),
        batch_max=int(batch_max),
        only_source=only_source,
        dry_run=bool(dry_run),
    )


@router.get("/api/review/reports")
async def get_review_reports(limit: int = 5):
    """历史复盘报告列表。"""
    return await asyncio.to_thread(review_agent.latest_reports, None, min(max(limit, 1), 20))


@router.get("/api/review/insights")
async def get_review_insights():
    """生效中的复盘经验 + 最近被停用的经验（供前端展示空态原因，避免误以为经验丢失）。

    active_items 每条含 origin：real=历史交易复盘 / quick_sim=快速评测提炼 / rule=归因规则，
    供提示词库等页面区分来源展示。
    """
    active_items = await asyncio.to_thread(review_agent.load_active_insight_items)
    insights = [item["rule"] for item in active_items]
    # 200 条上限：停用经验要能在前端逐条查看并手动删除，10 条会看不全
    deactivated = await asyncio.to_thread(review_agent.load_deactivated_insights, None, 200)
    # 停用总数：提示库面板在"0 条生效"时要能说明原因（经验被停用且不会自动恢复），
    # 否则用户会以为面板坏了 —— 曾因自动停用一次性停掉全部经验而困惑。
    deactivated_total = await asyncio.to_thread(review_agent.count_insights, False)
    return {
        "insights": insights,
        "active_items": active_items,
        "deactivated_recent": deactivated,
        "active_total": len(insights),
        "deactivated_total": deactivated_total,
        # 快速评测持仓时长（分钟）—— 供前端动态展示"X 分钟模拟"，
        # 避免把时长写死在文案里（改配置后文案不再过期）。
        "quick_sim_horizon_minutes": config.QUICK_SIM_HORIZON_MINUTES,
    }


# ── 复盘经验库（insights）5 条端点（2026-11-17 切片）─────────────────────
# 从 api_server.py 搬来 /review/insights/all、/count、/{id}/toggle、
# DELETE /{id}、DELETE /by-origin/{origin}。逻辑逐字未改。
# 依赖：common.ttl_cache（原 api_server 的 _ttl_get/_ttl_put，已下沉）、
#       _utc_ts_to_space_iso / _resolve_window_with_log 从 api_server 反向导入。

@router.get("/api/review/insights/all")
async def get_all_review_insights(
    hours: Optional[int] = None,
    from_ts: Optional[int] = None,
    to_ts: Optional[int] = None,
):
    """全部复盘经验（含生效与停用，带 id/origin/样本数），供手动清理用。

    v180 P0+:支持 hours / from_ts / to_ts 时间窗过滤(与 /api/events/today 、
    /api/review/snapshot 同口径)。三个都为空时退化为"全期"。

    与 /api/review/insights 的区别：那个只给"当前生效 + 最近停用"用于展示；
    这个给窗口内的全量，让用户能按来源（快速评测/历史交易）逐条删除。
    """
    from quick_sim import _resolve_window
    lo, hi = _resolve_window(hours, from_ts, to_ts)
    all_items = await asyncio.to_thread(review_agent.load_all_insight_items)
    if lo is None and hi is None:
        return all_items
    # v309 P2 修复: 同上 —— review_insights.created_at 是**空格分隔** UTC,
    # 不能用 'T' 分隔的 cutoff 去比 (字节序 ' ' < 'T' → 当天的行被漏掉)。
    lo_iso = _utc_ts_to_space_iso(lo) if lo is not None else None
    hi_iso = _utc_ts_to_space_iso(hi) if hi is not None else None
    filtered = []
    for item in all_items:
        ts = str(item.get("created_at") or "")
        if lo_iso is not None and ts < lo_iso:
            continue
        if hi_iso is not None and ts > hi_iso:
            continue
        filtered.append(item)
    return filtered


@router.get("/api/review/insights/count")
async def get_review_insights_count(
    hours: Optional[int] = None,
    from_ts: Optional[int] = None,
    to_ts: Optional[int] = None,
) -> Dict[str, Any]:
    """窗口内经验规则生效/停用计数(只返回数字,不带明细,供顶部卡片用)。

    v180 P0+:与 /api/events/today、/api/review/snapshot 同一时段口径。
    三个参数都为空时退化为"全期"计数。
    """
    _ck8 = "inscnt:%s:%s:%s" % (hours, from_ts, to_ts)
    _hit8 = ttl_get(_ck8, 8.0)
    if _hit8 is not None:
        return _hit8

    from quick_sim import _resolve_window
    lo, hi, lo_iso, hi_iso = _resolve_window_with_log(
        hours, from_ts, to_ts, caller="get_review_insights_count")
    # v309 P2 修复: review_insights.created_at 是 **空格分隔** 的 UTC
    # (db.py DEFAULT = datetime('now')), 而 _resolve_window_with_log 返回的
    # lo_iso/hi_iso 是 **'T' 分隔** 的 (给 ai_decisions 用的)。
    # 字节序 ' '(0x20) < 'T'(0x54) → 'T' cutoff 会把 cutoff 当天的行整片漏掉。
    # 这里改用空格分隔 cutoff, 与落库格式一致 → 字典序 == 时间序。
    where_parts = ["1=1"]
    params: List[Any] = []
    if lo is not None:
        where_parts.append("created_at >= ?")
        params.append(_utc_ts_to_space_iso(lo))
    if hi is not None:
        where_parts.append("created_at <= ?")
        params.append(_utc_ts_to_space_iso(hi))
    where_sql = " AND ".join(where_parts)
    try:
        async with aiosqlite.connect(config.DB_PATH, timeout=10) as conn:
            cur = await conn.execute(
                f"SELECT active, COUNT(*) FROM review_insights WHERE {where_sql} GROUP BY active",
                tuple(params),
            )
            rows = await cur.fetchall()
            await cur.close()
    except aiosqlite.OperationalError:
        return {"active": 0, "deact": 0, "total": 0, "window": {"from_ts": lo, "to_ts": hi}}
    active = 0
    deact = 0
    for r in rows:
        # r[0] 是 INTEGER,可能是 0/1 或 None
        n = int(r[1] or 0)
        if r[0] == 1:
            active = n
        else:
            deact += n
    _payload = {
        "active": active,
        "deact": deact,
        "total": active + deact,
        "window": {"from_ts": lo, "to_ts": hi},
    }
    ttl_put(_ck8, _payload)
    return _payload


@router.patch("/api/review/insights/{insight_id}/toggle")
async def toggle_review_insight(insight_id: int):
    """手动启用/停用一条经验（1↔0）。

    背景：自动停用是单向的（只评估 active=1 的经验，停用后永不重评），
    故需要手动出口把有价值的经验挑回来。
    """
    result = await asyncio.to_thread(review_agent.toggle_insight, insight_id)
    if not result.get("ok"):
        raise HTTPException(status_code=404, detail=result.get("error") or "切换失败")
    return result


@router.delete("/api/review/insights/{insight_id}")
async def delete_review_insight(insight_id: int):
    """删除一条经验规则（含生效与停用）。

    背景：重置模拟盘会保留复盘经验（设计如此），所以需要单独的手动清理入口
    —— 尤其来源=quick_sim 的经验，其底层快速评测样本可能已被重置删除。
    """
    result = await asyncio.to_thread(review_agent.delete_insight, insight_id)
    if not result.get("ok"):
        raise HTTPException(status_code=404, detail=result.get("error") or "删除失败")
    return result


@router.delete("/api/review/insights/by-origin/{origin}")
async def delete_review_insights_by_origin(origin: str):
    """按来源批量删除经验（real / quick_sim / rule）。"""
    result = await asyncio.to_thread(review_agent.delete_insights_by_origin, origin)
    if not result.get("ok"):
        raise HTTPException(status_code=400, detail=result.get("error") or "删除失败")
    return result

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
