# -*- coding: utf-8 -*-
"""history 域路由（结构切片，2026-11-17）。

从 api_server.py 整体搬来 6 条 /api/history/news-trades 端点：列表 / pending-count /
rejudge-batch / 单条详情 / AI 复盘 / 单条重判。逻辑逐字未改，只把 `@app.` 换成 `@router.`，
**注册顺序保持原样** —— pending-count 与 rejudge-batch 必须排在 `{news_id}` 之前，
否则前者会被当成 news_id 捕获（FastAPI 按声明顺序匹配，v309 P1 修过这个 bug）。
"""
from __future__ import annotations

import asyncio
from typing import Optional

from fastapi import APIRouter, HTTPException

import history_review

router = APIRouter()


@router.get("/api/history/news-trades")
async def history_news_trades(
    hours: int = 72,
    limit: int = 300,
    from_ts: Optional[int] = None,
    to_ts: Optional[int] = None,
):
    """已结束交易的新闻列表（按最近结算时间倒序）。

    v180 P0+：新增 from_ts / to_ts（UTC 秒）支持自定义时段，与 hours 互斥。
    前端 PeriodSelector 选「今日/近7天/近30天/自定义」时，常规时段用 hours 计算，
    自定义时段直接传 UTC 秒对。
    """
    return await asyncio.to_thread(
        history_review.news_trade_list, hours, limit, from_ts, to_ts
    )


# v180 P0+：批量 / 统计 类端点必须在 {news_id} 之前注册，
# 否则 `pending-count` 会被 {news_id} 路径捕获（FastAPI 路由按声明顺序匹配）。
@router.get("/api/history/news-trades/pending-count")
async def history_news_trade_pending_count(hours: Optional[int] = None,
                                           from_ts: Optional[int] = None,
                                           to_ts: Optional[int] = None):
    """统计窗口内 pending 交易数（不修改数据）— 用于前端顶部徽章。

    参数同 news-trades 列表接口：hours 与 from_ts/to_ts 互斥。
    """
    return await asyncio.to_thread(
        history_review.count_pending_news, hours, from_ts, to_ts
    )


@router.post("/api/history/news-trades/rejudge-batch")
async def history_news_trade_rejudge_batch(hours: Optional[int] = None,
                                           from_ts: Optional[int] = None,
                                           to_ts: Optional[int] = None,
                                           limit: int = 200):
    """批量重判定窗口内所有 pending 交易（v180 P0+）。

    用法：
      - 单个 news：POST /api/history/news-trades/{news_id}/rejudge
      - 整个时段：POST /api/history/news-trades/rejudge-batch?hours=24
      - 自定义窗口：?from_ts=...&to_ts=...

    返回：
      {
        "window": {...},
        "summary": {news, retried, updated, still_pending},
        "news": [{news_id, retried, updated, still_pending}, ...]
      }
    """
    return await asyncio.to_thread(
        history_review.rejudge_pending_batch, hours, from_ts, to_ts, int(limit)
    )


@router.get("/api/history/news-trades/{news_id}")
async def history_news_trade_detail(news_id: int):
    """单条新闻的交易详情（新闻原文 + 交易明细 + 聚合 + 已缓存 AI 总结）。"""
    detail = await asyncio.to_thread(history_review.news_trade_detail, news_id)
    if not detail:
        raise HTTPException(status_code=404, detail="news not found")
    return detail


@router.post("/api/history/news-trades/{news_id}/review")
async def history_news_trade_review(news_id: int):
    """AI 归因总结单条新闻的交易（成功/失败原因 + 整体总结），结果缓存。"""
    return await asyncio.to_thread(history_review.run_news_review, news_id)


@router.post("/api/history/news-trades/{news_id}/rejudge")
async def history_news_trade_rejudge(news_id: int):
    """对单条新闻下所有未判定或判定失败的交易重新结算（v180 P0+）。

    流程：
      1. 找出该 news 下 settled=1 且 verdict ∈ {NULL, 'BREAKEVEN'} 的 quick_sim_trades
         （即 _derive_pending_reason 返回非空 reason 的卡住交易）
      2. 用当前最新行情重新计算 exit_price/pnl_pct/verdict 并写回
      3. 返回 {news_id, retried: n, updated: [...], still_pending: [...]} 给前端

    用于：用户在前端看到「待判定 + reason=no_price_data」时点「重新判定」，
    K线到齐后再走一次结算即可。
    """
    return await asyncio.to_thread(history_review.rejudge_pending, news_id)
