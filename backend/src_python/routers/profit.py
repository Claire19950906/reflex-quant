# -*- coding: utf-8 -*-
"""成本–持有期匹配端点（v423，只读）。

回答一个比"加信号"更根本的问题：**同样的信号，换持有期/换成本档，净期望能不能转正？**

11-18 实测（2030 笔历史信号、真实 K 线重结算）：
  20m 毛 -0.023% / 1h +0.004% / 4h -0.091% / 1d +0.040% / 3d -0.053%
  → 所有持有期的毛优势都在 0 附近，胜率 46–49%；成本只要 >0，净期望就是负。
本端点把这件事变成可复检的数字，避免下一次又靠"感觉信号变多了"来庆祝。
"""
from __future__ import annotations

import sqlite3
import threading
import time
from typing import Any, Dict, List, Optional, Tuple

from fastapi import APIRouter, Query

import cost_horizon_eval as ce
import db

router = APIRouter()

# 结算一次要读十几万行 K 线（库里 800MB、引擎一直在写），实测 8–14s。
# 这个页面是"复检器"，结论几分钟内不会变，所以按 (days, asset, bars_days) 缓存 5 分钟，
# 第一次慢、之后秒开；缓存不跨进程、不落盘，重启即失效。
_CACHE: Dict[Tuple[int, str, int], Tuple[float, Dict[str, Any]]] = {}
_CACHE_TTL = 300.0
_CACHE_LOCK = threading.Lock()


def _ro_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(f"file:{db.get_db_path()}?mode=ro", uri=True, timeout=30.0)
    conn.row_factory = sqlite3.Row
    return conn


def _scan(days: int, asset: Optional[str], bars_days: int) -> Dict[str, Any]:
    key = (days, asset or "", bars_days)
    now = time.time()
    with _CACHE_LOCK:
        hit = _CACHE.get(key)
    if hit and now - hit[0] < _CACHE_TTL:
        cached = dict(hit[1])
        cached["cached"] = True
        cached["cache_age_s"] = round(now - hit[0], 1)
        return cached
    rep = _scan_uncached(days, asset, bars_days)
    rep["cached"] = False
    with _CACHE_LOCK:
        _CACHE[key] = (time.time(), rep)
        if len(_CACHE) > 32:
            oldest = min(_CACHE.items(), key=lambda kv: kv[1][0])[0]
            _CACHE.pop(oldest, None)
    return rep


def _scan_uncached(days: int, asset: Optional[str], bars_days: int) -> Dict[str, Any]:
    now = int(time.time())
    conn = _ro_conn()
    try:
        entries = ce.load_entries(conn, since_ts=now - days * 86400, asset=asset)
        assets = sorted({e["asset"] for e in entries})
        bars: Dict[str, Dict[str, List]] = {}
        for a in assets:
            bars[a] = {}
            for _label, _sec, tf in ce.HORIZONS:
                bars[a][tf] = ce.load_bars(conn, a, tf, now - bars_days * 86400)
    finally:
        conn.close()
    rep = ce.evaluate(entries, bars)
    rep["window_days"] = days
    rep["assets"] = assets
    rep["generated_at"] = now
    rep["missing_bars"] = [a for a in assets if not any(bars[a].values())]
    return rep


@router.get("/api/profit/scan")
async def profit_scan(
    days: int = Query(60, ge=7, le=180),
    asset: Optional[str] = Query(None),
    bars_days: int = Query(120, ge=30, le=365),
):
    """成本 × 持有期 的净期望矩阵（只读，不发网络请求）。"""
    rep = _scan(days, (asset or "").upper() or None, bars_days)
    best = rep.get("best_cell")
    rep["headline"] = (
        "当前数据里没有任何 (持有期, 成本) 组合能在扣费后为正且显著。"
        if not best or (best["net"]["avg"] or 0) <= 0 or not _significant(best["net"])
        else "存在正期望格子：%s / %dbps（净 %+.3f%%/笔，t=%s），但样本 %d 笔，仍需样本外验证。"
             % (best["horizon"], best["cost_bps"], best["net"]["avg"],
                best["net"]["t_stat"], best["net"]["n"])
    )
    return {"ok": True, "data": rep}


def _significant(s: Dict[str, Any]) -> bool:
    t = s.get("t_stat")
    return bool(t is not None and t >= 2.0 and (s.get("avg") or 0) > 0)
