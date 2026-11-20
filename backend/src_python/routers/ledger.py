# -*- coding: utf-8 -*-
"""账本端点（v462，只读）。

  - /api/ledger/overview   模拟盘账户 + 持仓标记 + 成交 + 对账提醒

口径在 `ledger.py`，router 只做 60 秒缓存。
"""
from __future__ import annotations

import threading
import time
from typing import Any, Dict, Tuple

from fastapi import APIRouter

import ledger as lm

router = APIRouter()

_CACHE: Dict[Tuple[str, int], Tuple[float, Dict[str, Any]]] = {}
_LOCK = threading.Lock()


@router.get("/api/ledger/overview")
def ledger_overview(order_limit: int = 50) -> Dict[str, Any]:
    key = ("overview", int(order_limit))
    now = time.time()
    with _LOCK:
        hit = _CACHE.get(key)
    if hit and now - hit[0] < 60.0:
        out = dict(hit[1])
        out["cached"] = True
        out["cache_age_s"] = round(now - hit[0], 1)
        return {"ok": True, "data": out}
    rep = lm.build(order_limit=order_limit)
    rep["cached"] = False
    with _LOCK:
        _CACHE[key] = (time.time(), rep)
    return {"ok": True, "data": rep}
