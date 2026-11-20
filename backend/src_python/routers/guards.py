# -*- coding: utf-8 -*-
"""开仓闸端点（v446，只读）。

三件事：
  1. /api/guards/entry/scan     —— 2030 笔历史信号分持有期的闸门验证（有缓存）
  2. /api/guards/entry/shadow   —— 影子账本 4h 的口径回放（小样本，仅参考）
  3. /api/guards/entry/check    —— 单条实时读数：位置分位 / 是否同向 / 该不该拦

口径全部在 `entry_guard.py` 里，router 只做参数与缓存，不重算。
"""
from __future__ import annotations

import threading
import time
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

from fastapi import APIRouter, Query

import entry_guard as eg

router = APIRouter()

_CACHE: Dict[Tuple[str, int], Tuple[float, Dict[str, Any]]] = {}
_CACHE_TTL = 600.0
_LOCK = threading.Lock()


def _cached(kind: str, days: int) -> Dict[str, Any]:
    key = (kind, days)
    now = time.time()
    with _LOCK:
        hit = _CACHE.get(key)
    if hit and now - hit[0] < _CACHE_TTL:
        out = dict(hit[1])
        out["cached"] = True
        out["cache_age_s"] = round(now - hit[0], 1)
        return out
    if kind == "shadow":
        rep = eg.scan(days=days)
    else:
        rep = eg.scan_signals(days=days)
    rep["cached"] = False
    with _LOCK:
        _CACHE[key] = (time.time(), rep)
        if len(_CACHE) > 16:
            oldest = min(_CACHE.items(), key=lambda kv: kv[1][0])[0]
            _CACHE.pop(oldest, None)
    return rep


def _config() -> Dict[str, Any]:
    return {
        "mode": eg.MODE,
        "enabled": True,
        "buy_max_pos": eg.BUY_MAX_POS,
        "sell_min_pos": eg.SELL_MIN_POS,
        "window_min": eg.WINDOWS_MIN[0],
        "confirm_window_min": eg.CONFIRM_WINDOW_MIN,
        "confirm_min_move_pct": eg.CONFIRM_MIN_MOVE_PCT,
        "min_ticks": eg.MIN_TICKS,
        "min_conclusion_n": eg.MIN_CONCLUSION_N,
        "env": {
            "ENTRY_GUARD_MODE": "shadow | enforce | off",
            "ENTRY_GUARD_ENABLED": "1 | 0",
        },
    }


@router.get("/api/guards/entry/config")
async def entry_guard_config():
    """当前闸门参数与模式（只读）。"""
    return {"ok": True, "data": _config()}


@router.get("/api/guards/entry/check")
async def entry_guard_check(
    asset: str = Query(..., min_length=2),
    action: str = Query(..., pattern="^(BUY|SELL|buy|sell)$"),
    ts: Optional[float] = Query(None, description="决策时间戳（秒）；缺省=现在"),
):
    """单条实时读数：位置分位、前 30 分钟同向与否、该不该拦。"""
    conn = eg._ro_conn()
    try:
        info = eg.evaluate(conn, asset, action.upper(), ts=ts)
    finally:
        conn.close()
    return {"ok": True, "data": info}


@router.get("/api/guards/entry/shadow")
async def entry_guard_shadow(days: int = Query(7, ge=1, le=30)):
    """影子账本（4h 已结算）口径回放 —— 小样本，只作参考。"""
    rep = _cached("shadow", days)
    return {"ok": True, "data": rep}


@router.get("/api/guards/entry/scan")
async def entry_guard_scan(days: int = Query(60, ge=7, le=180)):
    """2030 笔历史信号 × 20m/1h/4h × 位置/同向的验证表（只读，不发网络请求）。"""
    rep = _cached("signals", days)
    return {"ok": True, "data": rep}
