# -*- coding: utf-8 -*-
"""事件路由（结构切片，2026-11-17）。

搬来 5 条端点：
  /api/events/heatmap（asset × hour-bucket 密度网格，30s 缓存）
  /api/events（最新非噪声新闻 + 最新决策，走 15 天窗口缓存，失败降级直查）
  /api/events/today（窗口内真实计数，避免前端拿列表截断数当信号数）
  /api/events/stream（SSE 推送，keepalive 1s）
  /api/events/filter-stream（门卫双流 passed/filtered，SQL only 零 token）
缓存对象与 _fetch_event_rows 仍在 api_server，router 用 _api() 回引。
"""
from __future__ import annotations

import asyncio
import sqlite3
import time
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

import aiosqlite
from fastapi import APIRouter, Request
from fastapi.responses import StreamingResponse

import config
import db

TZ_SHANGHAI = config.TZ_SHANGHAI

router = APIRouter()


def _api():
    """延迟回引 api_server（缓存/轮询助手仍在原文件）。"""
    import api_server
    return api_server


_EVENT_HEATMAP_CACHE: Dict[str, Any] = {"expires": 0.0, "value": None}


_EVENT_HEATMAP_TTL_S = 30.0


def _event_heatmap_sync(hours: int = 24, limit_assets: int = 12) -> Dict[str, Any]:
    """聚合最近 N 小时的事件热力图数据。"""
    hours = max(1, min(int(hours), 168))
    limit_assets = max(1, min(int(limit_assets), 30))
    now_ts = int(time.time())
    since_ts = now_ts - hours * 3600
    bucket_size = 3600
    now_bucket_start = (now_ts // bucket_size) * bucket_size
    first_bucket_start = ((since_ts // bucket_size) * bucket_size)
    bucket_count = (now_bucket_start - first_bucket_start) // bucket_size + 1
    if bucket_count < 1:
        bucket_count = 1
    bucket_labels: List[str] = []
    for i in range(bucket_count):
        bs = first_bucket_start + i * bucket_size
        bd = datetime.fromtimestamp(bs, TZ_SHANGHAI)
        bucket_labels.append(bd.strftime("%m-%d %H"))
    assets: List[str] = []
    cells: Dict[str, Dict[int, Dict[str, float]]] = {}
    try:
        with db.get_connection() as conn:
            conn.row_factory = sqlite3.Row
            asset_rows = conn.execute(
                """SELECT ne.asset AS asset
                   FROM news_events ne
                   WHERE ne.is_noise = 0 AND ne.asset != 'NONE' AND ne.ts >= ?
                   GROUP BY ne.asset
                   ORDER BY COUNT(*) DESC LIMIT ?""",
                (since_ts, limit_assets),
            ).fetchall()
            assets = [str(r["asset"] or "").upper() for r in asset_rows if r["asset"]]
            if not assets:
                return {
                    "hours": hours, "bucket_count": bucket_count, "buckets": bucket_labels,
                    "assets": [], "cells": {}, "max_count": 0, "max_score": 0.0,
                    "since_ts": since_ts, "now_ts": now_ts,
                    "generated_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
                }
            placeholders = ",".join("?" for _ in assets)
            ev_rows = conn.execute(
                f"""SELECT ne.asset AS asset, ne.ts AS ts, ne.score AS score,
                           COALESCE((SELECT ne2.action FROM news_events ne2
                            WHERE ne2.asset=ne.asset AND ne2.ts=ne.ts
                            ORDER BY ABS(ne2.score) DESC LIMIT 1), 'HOLD') AS action
                    FROM news_events ne
                    WHERE ne.is_noise = 0 AND ne.asset != 'NONE' AND ne.ts >= ?
                      AND ne.asset IN ({placeholders})
                    ORDER BY ne.ts DESC""",
                [since_ts, *assets],
            ).fetchall()
            for r in ev_rows:
                a = str(r["asset"] or "").upper()
                ts = int(r["ts"] or 0)
                idx = max(0, min(bucket_count - 1, (ts - first_bucket_start) // bucket_size))
                bucket_key = first_bucket_start + idx * bucket_size
                cells.setdefault(a, {}).setdefault(
                    bucket_key, {"count": 0, "score_sum": 0.0, "abs_score_sum": 0.0, "buy": 0, "sell": 0}
                )
                cell = cells[a][bucket_key]
                sc = float(r["score"] or 0.0)
                cell["count"] += 1
                cell["score_sum"] += sc
                cell["abs_score_sum"] += abs(sc)
                act = str(r["action"] or "HOLD").upper()
                if act == "BUY":
                    cell["buy"] += 1
                elif act == "SELL":
                    cell["sell"] += 1
        cell_list: Dict[str, List[Dict[str, float]]] = {}
        max_count = 0
        max_score = 0.0
        bucket_keys = [first_bucket_start + i * bucket_size for i in range(bucket_count)]
        for a in assets:
            row: List[Dict[str, float]] = []
            for bs in bucket_keys:
                c = cells.get(a, {}).get(bs, {"count": 0, "score_sum": 0.0, "abs_score_sum": 0.0, "buy": 0, "sell": 0})
                row.append({
                    "ts": bs,
                    "count": int(c["count"]),
                    "score_sum": round(float(c["score_sum"]), 3),
                    "abs_score_sum": round(float(c["abs_score_sum"]), 3),
                    "buy": int(c["buy"]),
                    "sell": int(c["sell"]),
                })
                if c["count"] > max_count:
                    max_count = int(c["count"])
                if c["abs_score_sum"] > max_score:
                    max_score = float(c["abs_score_sum"])
            cell_list[a] = row
    except sqlite3.OperationalError:
        return {
            "hours": hours, "bucket_count": bucket_count, "buckets": bucket_labels,
            "assets": [], "cells": {}, "max_count": 0, "max_score": 0.0,
            "since_ts": since_ts, "now_ts": now_ts,
            "generated_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        }
    return {
        "hours": hours, "bucket_count": bucket_count, "buckets": bucket_labels,
        "assets": assets, "cells": cell_list,
        "max_count": max_count, "max_score": round(max_score, 3),
        "since_ts": since_ts, "now_ts": now_ts,
        "generated_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
    }


def _cached_event_heatmap(hours: int, limit_assets: int) -> Dict[str, Any]:
    now = time.monotonic()
    v = _EVENT_HEATMAP_CACHE["value"]
    if v is None or now >= _EVENT_HEATMAP_CACHE["expires"]:
        v = _event_heatmap_sync(hours, limit_assets)
        _EVENT_HEATMAP_CACHE["value"] = v
        _EVENT_HEATMAP_CACHE["expires"] = now + _EVENT_HEATMAP_TTL_S
    return v


@router.get("/api/events/heatmap")
async def get_events_heatmap(hours: int = 24, limit_assets: int = 12) -> Dict[str, Any]:
    """事件热力图：asset × hour-bucket 事件密度。

    给归因页"事件热力图"用 — 前端按 (count, abs_score_sum) 着色，
    鼠标悬停显示该 cell 的 BUY/SELL 计数和平均 score。
    默认最近 24 小时、桶大小 1 小时、Top 12 个资产。30s 缓存。
    """
    return await asyncio.to_thread(
        _cached_event_heatmap,
        max(1, min(int(hours), 168)),
        max(1, min(int(limit_assets), 30)),
    )


# ─── 2026-11-17 从 api_server 追加 ──────────

@router.get("/api/events")
async def get_events(
    limit: int = 2000,
    hours: Optional[int] = None,
    from_ts: Optional[int] = None,
    to_ts: Optional[int] = None,
) -> List[Dict[str, Any]]:
    """Return the latest non-noise news rows with their latest decision.

    v170.5+: 走 _EVENTS_15D_CACHE（启动期预加载 now±15d 窗口，60s TTL 增量刷新）。
    缓存为空 / DB 不可用 / preload 失败 → 降级走 _api()._fetch_event_rows 直查 DB。
    v362+: 带 hours / from_ts / to_ts 窗口时直查 DB 按 raw_news.ts 过滤，不走全量缓存。
    """
    capped = max(1, min(int(limit), config.EVENTS_LIST_MAX))
    lo, hi, _, _ = _api()._resolve_window_with_log(hours, from_ts, to_ts, caller="get_events")
    if lo is not None or hi is not None:
        try:
            return await _api()._fetch_event_rows(limit=capped, lo_ts=lo, hi_ts=hi)
        except aiosqlite.OperationalError:
            return []
    try:
        cached = await asyncio.to_thread(_api()._events_15d_get_cached, capped)
        if cached:
            return cached
        # 缓存空 → 降级
        return await _api()._fetch_event_rows(limit=capped)
    except aiosqlite.OperationalError:
        return []


@router.get("/api/events/today")
async def get_today_events(
    hours: Optional[int] = None,
    from_ts: Optional[int] = None,
    to_ts: Optional[int] = None,
) -> Dict[str, Any]:
    """窗口内真实计数,避免前端用列表截断条数当"信号数"。

    v180 P0+:支持 hours / from_ts / to_ts 三种入口(与 /api/review/snapshot、
    /api/history/* 同口径)。三个都为空时退化到"上海日"(保持旧前端兼容)。
    """
    today = datetime.now(TZ_SHANGHAI).strftime("%Y-%m-%d")
    # v180 P0+:归一化窗口(与 quick_sim._resolve_window / /api/review/snapshot 同口径)
    # v302+ P0+:走 _resolve_window_with_log 输出 DEBUG（env WINDOW_DEBUG=1 开启）
    lo, hi, lo_iso, hi_iso = _api()._resolve_window_with_log(
        hours, from_ts, to_ts, caller="get_today_events")
    has_window = (lo is not None) or (hi is not None)
    # 默认查询条件
    # v302+ P0+【修复】改用 ts(INTEGER UTC 秒)做窗口过滤，而不是 timestamp(TEXT 混合格式)。
    # 旧代码用 timestamp 字符串做字典序比较有两个坑:
    #   1) 部分行 timestamp 是 'YYYY-MM-DD HH:MM:SS'(Beijing 朴素串),
    #      部分行是 'YYYY-MM-DDTHH:MM:SS+08:00'(ISO 带偏移)；
    #   2) 即便统一成 'T' 分隔,Beijing 时刻字符串和 UTC lo/hi 串不能直接比:
    #      Beijing 16:00 昨天 字符串 '2026-10-09T16:00:00+08:00' >= UTC lo
    #      '2026-10-09T16:00:00'(Beijing 今天 00:00 的 UTC 表示)=TRUE,
    #      把北京昨天傍晚的事件漏进"今日"窗口。
    # ts 列是 UTC unix 秒 INTEGER,跟 lo/hi 同口径,直接整数比较永远安全。
    # 没 ts 列的旧库降级到 timestamp(带已知的 T-vs-space 风险,与 v302 之前的实现同)。
    news_cond = "is_noise = 0"
    news_params: List[Any] = []
    # 默认查询条件
    news_ts_cond = ""  # 适用于 ts INTEGER 列存在的"已上报"
    news_ts_params: List[Any] = []
    if not has_window:
        # v469（2026-11-19 修空态）：timestamp 列采集时写入的是 unix 秒数字符串
        # （如 '1795070012'），不是 Beijing 日期字面串；旧代码 substr(timestamp,1,10)
        # 拿秒串前 10 位去比 'YYYY-MM-DD' 永远不匹配 → news_count/analyzed_count 恒为 0，
        # 前端事件流/新闻计数空态。改用 ts(INTEGER UTC 秒) 按上海日聚合，与窗口分支同口径。
        news_cond += " AND date(datetime(ts, 'unixepoch', '+8 hours')) = ?"
        news_params.append(today)
    else:
        if lo is not None:
            news_ts_cond += " AND ts >= ?"
            news_ts_params.append(lo)
        if hi is not None:
            news_ts_cond += " AND ts <= ?"
            news_ts_params.append(hi)
        # 暂时把 ts 条件并入 news_cond(主查询);params 拆开拼接
        news_cond += news_ts_cond
        news_params.extend(news_ts_params)
    sig_cond = "suggested_action IN ('BUY','SELL')"
    sig_params: List[Any] = []
    if not has_window:
        # 旧前端兼容：substr(created_at,1,10) 仍可用（AI 决策 created_at 标 UTC 比较一致）。
        sig_cond += " AND substr(created_at, 1, 10) = ?"
        sig_params.append(today)
    else:
        # v302+ P0+【修复】改用 ts(INTEGER UTC 秒)做窗口过滤，与 raw_news.ts 同口径。
        # 同 news_cond 的 T-vs-space 风险：created_at 同样是 Beijing 字面串，
        # 直接字典序与 UTC lo/hi 串比会把昨天傍晚 ai_decisions 漏进今日窗口。
        if lo is not None:
            sig_cond += " AND ts >= ?"
            sig_params.append(lo)
        if hi is not None:
            sig_cond += " AND ts <= ?"
            sig_params.append(hi)
    # v300+ "今日信号" 新鲜度：只算最近 N 小时内的 BUY/SELL。
    # FRESH_SIGNAL_HOURS=0 → 关掉过滤，live_signal_count == signal_count。
    # 设计意图：早场 LLM 健康时生成的决策，过了这个窗口就视作"陈旧信号"，
    # 前端 StatCards 大字读 live_signal_count，LLM/数据源全挂时如实归零，
    # 历史 36 条仍在 signal_count / /review 全量可见。
    live_sig_cond = sig_cond
    live_sig_params: List[Any] = list(sig_params)
    fresh_h = int(getattr(config, "FRESH_SIGNAL_HOURS", 4) or 0)
    if fresh_h > 0:
        # v302+ P0+【修复】改用 ts INTEGER 直接比（不再走 datetime('now', ?) 字典序比较），
        # 保证和 sig_cond 同口径，避免 fresh_h 过滤与窗口过滤之间的格式漂移。
        import time as _t
        fresh_lo = int(_t.time()) - fresh_h * 3600
        live_sig_cond += " AND ts >= ?"
        live_sig_params.append(fresh_lo)
    settled_cond = "settled = 1"
    settled_params: List[Any] = []
    if not has_window:
        settled_cond += " AND substr(datetime(exit_ts, 'unixepoch'), 1, 10) = ?"
        settled_params.append(today)
    else:
        if lo is not None:
            settled_cond += " AND exit_ts >= ?"
            settled_params.append(lo)
        if hi is not None:
            settled_cond += " AND exit_ts <= ?"
            settled_params.append(hi)
    try:
        async with aiosqlite.connect(config.DB_PATH, timeout=20) as connection:
            cursor = await connection.execute(
                f"""
                SELECT
                    COUNT(*) AS news_count,
                    COALESCE(SUM(CASE WHEN status = 'DONE' THEN 1 ELSE 0 END), 0) AS analyzed_count
                FROM raw_news
                WHERE {news_cond}
                """,
                tuple(news_params),
            )
            row = await cursor.fetchone()
            await cursor.close()
            cursor = await connection.execute(
                "SELECT COUNT(*) FROM raw_news WHERE is_noise = 0"
            )
            total_row = await cursor.fetchone()
            await cursor.close()
            cursor = await connection.execute(
                f"SELECT COUNT(*) FROM ai_decisions WHERE {sig_cond}",
                tuple(sig_params),
            )
            signal_row = await cursor.fetchone()
            await cursor.close()
            # v471: 顺带按 suggested_action 拆出多/空计数，前端卡片直连权威口径，
            # 避免靠 /api/events 列表（limit 截断 + HOLD 占大头）本地重数导致少计。
            cursor = await connection.execute(
                f"""
                SELECT COALESCE(SUM(CASE WHEN suggested_action = 'BUY' THEN 1 ELSE 0 END), 0),
                       COALESCE(SUM(CASE WHEN suggested_action = 'SELL' THEN 1 ELSE 0 END), 0)
                FROM ai_decisions WHERE {sig_cond}
                """,
                tuple(sig_params),
            )
            split_row = await cursor.fetchone()
            await cursor.close()
            cursor = await connection.execute(
                f"SELECT COUNT(*) FROM ai_decisions WHERE {live_sig_cond}",
                tuple(live_sig_params),
            )
            live_signal_row = await cursor.fetchone()
            await cursor.close()
            cursor = await connection.execute(
                f"SELECT COUNT(*) FROM quick_sim_trades WHERE {settled_cond}",
                tuple(settled_params),
            )
            settled_row = await cursor.fetchone()
            await cursor.close()
    except aiosqlite.OperationalError:
        return {
            "today": today,
            "window": {"from_ts": lo, "to_ts": hi},
            "news_count": 0,
            "analyzed_count": 0,
            "total_count": 0,
            "signal_count": 0,
            "signal_long": 0,
            "signal_short": 0,
            "live_signal_count": 0,
            "live_signal_window_hours": int(getattr(config, "FRESH_SIGNAL_HOURS", 4) or 0),
            "settled_today": 0,
        }
    return {
        "today": today,
        # v180 P0+: 透传窗口,前端可显示时段标签
        "window": {"from_ts": lo, "to_ts": hi},
        "news_count": int(row[0] or 0),
        "analyzed_count": int(row[1] or 0),
        "total_count": int(total_row[0] or 0),
        "signal_count": int(signal_row[0] or 0),
        "signal_long": int(split_row[0] or 0),
        "signal_short": int(split_row[1] or 0),
        # v300+ 新鲜度：仅最近 N 小时 BUY/SELL，LLM/数据源全挂时如实归零。
        "live_signal_count": int(live_signal_row[0] or 0),
        "live_signal_window_hours": int(getattr(config, "FRESH_SIGNAL_HOURS", 4) or 0),
        "settled_today": int(settled_row[0] or 0),
    }


@router.get("/api/events/filter-stream")
async def get_gatekeeper_filter_stream(limit: int = 30) -> Dict[str, Any]:
    """门卫筛选双流：passed (被门卫晒进来) + filtered (被门卫没晒进来)。

    轻量级：SQL only，零 LLM token；前端 15s 轮询一次。
    每条记录只返回 id/source/content_preview/timestamp/relevance_score/status，
    不返回 reasoning / ai_decisions，避免泄出大字段。
    """
    try:
        return await asyncio.to_thread(_api()._cached_gatekeeper_filter, max(5, min(int(limit), 100)))
    except sqlite3.OperationalError:
        # 表/列还没初始化（极早启动）
        return {
            "passed": [],
            "filtered": [],
            "counts": {"passed_today": 0, "filtered_today": 0, "passed_total": 0, "filtered_total": 0},
# 2026-11-17: 事件热力图整簇已迁到 routers/events.py。

        }


@router.get("/api/events/stream")
async def sse_stream(request: Request):
    q: asyncio.Queue = asyncio.Queue(maxsize=256)
    _api()._SSE_QUEUES.append(q)

    async def event_generator():
        try:
            while True:
                if await request.is_disconnected():
                    break
                try:
                    msg = await asyncio.wait_for(q.get(), timeout=1.0)
                    yield f"data: {msg}\n\n"
                except asyncio.TimeoutError:
                    yield ": keepalive\n\n"
        except asyncio.CancelledError:
            pass
        finally:
            try:
                _api()._SSE_QUEUES.remove(q)
            except ValueError:
                pass

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
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
