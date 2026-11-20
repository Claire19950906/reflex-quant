# -*- coding: utf-8 -*-
"""单押注检测 + 正期望停单端点（v449，只读）。

  - /api/bets/scan             13 笔仓位到底几注独立方向；相关性矩阵与聚簇
  - /api/policy/expectancy     今日允许开火的格子；没有就明确"停单"

口径在 `bets.py` / `expectancy_policy.py`，router 只做缓存与参数。
"""
from __future__ import annotations

import threading
import time
from typing import Any, Dict, Tuple

from fastapi import APIRouter, Query

import bets as bm
import expectancy_policy as ep

router = APIRouter()

_CACHE: Dict[Tuple[str, int, int], Tuple[float, Dict[str, Any]]] = {}
_LOCK = threading.Lock()


def _cached(kind: str, a: int, b: int, fn) -> Dict[str, Any]:
    key = (kind, a, b)
    now = time.time()
    with _LOCK:
        hit = _CACHE.get(key)
    if hit and now - hit[0] < 600.0:
        out = dict(hit[1])
        out["cached"] = True
        out["cache_age_s"] = round(now - hit[0], 1)
        return out
    rep = fn()
    rep["cached"] = False
    with _LOCK:
        _CACHE[key] = (time.time(), rep)
        if len(_CACHE) > 16:
            oldest = min(_CACHE.items(), key=lambda kv: kv[1][0])[0]
            _CACHE.pop(oldest, None)
    return rep


@router.get("/api/bets/scan")
def bets_scan(window_days: int = Query(60, ge=20, le=180)):
    """未平仓仓位按相关性簇合并，回答"几笔仓位 = 几注独立下注"。"""
    rep = _cached("bets", window_days, 0,
                  lambda: bm.scan(window_days=window_days))
    return {"ok": True, "data": rep}


@router.get("/api/policy/expectancy")
def policy_expectancy(
    days: int = Query(60, ge=7, le=180),
    cost: int = Query(30, ge=0, le=100),
):
    """当前成本档下，哪些持有期/品种是正期望；一个都没有就返回停单。"""
    rep = _cached("expectancy", days, cost,
                  lambda: ep.build(days=days, cost_bps=cost))
    return {"ok": True, "data": rep}
