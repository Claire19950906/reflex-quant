# -*- coding: utf-8 -*-
"""聚合类 GET 的短 TTL 缓存（结构切片，2026-11-17）。

从 api_server.py 的 v372 段整段搬出。原注释保留：

实测最近 2000 次请求里 /api/review/snapshot 470 次、/api/events/today 420 次、
/api/review/insights/count 173 次 —— 前端多个页面各自定时轮询，每次都全量查库，
而库里同时有决策/行情在写，是 database is locked 的主要来源。
这些接口都是"看板聚合"，4 秒内的重复请求直接复用结果完全可接受。
"""
from __future__ import annotations

import time
from typing import Any, Dict

_TTL_CACHE: Dict[str, Any] = {}
_TTL_MAX = 256


def ttl_get(key: str, ttl: float) -> Any:
    ent = _TTL_CACHE.get(key)
    if ent and (time.time() - ent[0]) < ttl:
        return ent[1]
    return None


def ttl_put(key: str, value: Any) -> None:
    if len(_TTL_CACHE) > _TTL_MAX:
        _TTL_CACHE.clear()
    _TTL_CACHE[key] = (time.time(), value)
