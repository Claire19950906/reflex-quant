# -*- coding: utf-8 -*-
"""样本工厂端点（v461，只读）。

  - /api/factory/report   把真实/回放样本按时间分段结算，回答"这格能不能开火"

口径在 `sample_factory.py`，router 只做缓存与参数。
"""
from __future__ import annotations

import threading
import time
from typing import Any, Dict, Tuple

from fastapi import APIRouter, Query

import sample_factory as sf

router = APIRouter()

_CACHE: Dict[Tuple[str, int, int, int], Tuple[float, Dict[str, Any]]] = {}
_LOCK = threading.Lock()


def _cached(key: Tuple[str, int, int, int], fn) -> Dict[str, Any]:
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
        if len(_CACHE) > 8:
            oldest = min(_CACHE.items(), key=lambda kv: kv[1][0])[0]
            _CACHE.pop(oldest, None)
    return rep


@router.get("/api/factory/report")
def factory_report(
    asset: str = Query("BTC", max_length=12),
    days: int = Query(60, ge=7, le=180),
    folds: int = Query(4, ge=2, le=8),
    cost: int = Query(2, ge=0, le=100),
):
    """分段结算报告：n、每段净期望、合并 t、最差段与晋升结论。"""
    rep = _cached((asset.upper(), days, folds, cost),
                  lambda: sf.build(asset=asset, days=days, folds=folds, cost_bps=cost))
    return {"ok": True, "data": rep}
