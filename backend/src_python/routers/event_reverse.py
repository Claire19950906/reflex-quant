#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""routers/event_reverse.py — 时刻→事件反向检索 HTTP 端点（v490 P1-2 · 需求 #6）

GET /api/attribution/reverse?asset=WTI&ts=<unix秒>
  · asset 可空（不限定品种）；ts 可空（默认当前时刻）；
  · 返回同期行情（临界点/快慢形态）+ 候选事件（含已归因标注、方向、时间差、资金佐证）。
纯只读，不调 LLM、不写库。
"""
from __future__ import annotations

import asyncio
import time
from typing import Any, Dict, Optional

from fastapi import APIRouter, Query

import db
import event_reverse_lookup

router = APIRouter()


def _sync(asset: str, target_ts: int, lookback: int, lookahead: int) -> Dict[str, Any]:
    conn = db.get_connection()
    try:
        return event_reverse_lookup.lookup(
            conn, asset or "", target_ts,
            lookback_sec=lookback, lookahead_sec=lookahead,
        )
    finally:
        conn.close()


@router.get("/api/attribution/reverse")
async def reverse_lookup(
    asset: Optional[str] = Query(default="", description="品种代码，空=不限定"),
    ts: Optional[int] = Query(default=None, description="目标时刻 unix 秒，默认 now"),
    lookback: int = Query(default=event_reverse_lookup.LOOKBACK_SEC, ge=60, le=86400),
    lookahead: int = Query(default=event_reverse_lookup.LOOKAHEAD_SEC, ge=0, le=3600),
):
    target_ts = int(ts) if ts else int(time.time())
    return await asyncio.to_thread(_sync, asset or "", target_ts, lookback, lookahead)
