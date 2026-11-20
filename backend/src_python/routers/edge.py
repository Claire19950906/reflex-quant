# -*- coding: utf-8 -*-
"""结构性优势体检端点（v424，只读）。

和 v423 的 /api/profit/scan 的区别：
  - /profit/scan 检验的是"新闻信号 + 持有期 + 成本"；
  - 本端点检验的是"结构性指标（资金费率/持仓/多空比）+ 分位极端 + 持有期"。

每行同时给三件事，缺一不可：
  1. 扣费净收益（净>0）
  2. t 值（≥2）
  3. 时机置换 p（<0.05）：把信号时点换成同期任意时点，能否复现
再加一列"无条件基线"，用来排除"这段行情本来就是单边，做空随便赚"。
"""
from __future__ import annotations

import sqlite3
import threading
import time
from typing import Any, Dict, Optional, Tuple

from fastapi import APIRouter, Query

import carry_yield as cy
import db
import structural_edge as se

router = APIRouter()

_HORIZON_MAP = {
    "8h": 28_800,
    "1d": 86_400,
    "3d": 259_200,
}

# 和 /profit/scan 一样：一次要跑置换检验，第一次慢，之后秒开
_CACHE: Dict[Tuple, Tuple[float, Dict[str, Any]]] = {}
_CACHE_TTL = 300.0
_LOCK = threading.Lock()


def _ro_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(f"file:{db.get_db_path()}?mode=ro", uri=True, timeout=30.0)
    return conn


def _run(kind: str, assets: str, days: int, q_low: float, q_high: float,
         horizons: str, k: int) -> Dict[str, Any]:
    hs = tuple(_HORIZON_MAP[h.strip().lower()] for h in horizons.split(",")
               if h.strip().lower() in _HORIZON_MAP) or (86_400,)
    alist = tuple(a.strip().upper() for a in assets.split(",") if a.strip())
    key = (kind, alist, days, q_low, q_high, hs, k)
    now = time.time()
    with _LOCK:
        hit = _CACHE.get(key)
    if hit and now - hit[0] < _CACHE_TTL:
        out = dict(hit[1]); out["cached"] = True
        out["cache_age_s"] = round(now - hit[0], 1)
        return out
    conn = _ro_conn()
    try:
        rep = se.evaluate_with_baseline(
            conn, kind, alist, int(time.time()) - days * 86_400,
            horizons_s=hs, q_low=q_low, q_high=q_high, k=k,
        )
    finally:
        conn.close()
    rep["cached"] = False
    rep["window_days"] = days
    rep["headline"] = _headline(rep)
    with _LOCK:
        _CACHE[key] = (time.time(), rep)
        if len(_CACHE) > 32:
            _CACHE.pop(min(_CACHE.items(), key=lambda kv: kv[1][0])[0], None)
    return rep


def _headline(rep: Dict[str, Any]) -> str:
    rows = rep.get("rows") or []
    if not rows:
        return "没有可结算的信号：该指标在窗口内没有足够的序列或 K 线。"
    strong = [r for r in rows if r.get("has_edge")]
    best = rows[0]
    if strong:
        r = strong[0]
        return ("有三重检验（净>0 + t≥2 + 时机 p<0.05）都通过的格子："
                "%s %s %s，净 %+.3f%%/笔、t=%s、p=%s、%d 笔。"
                % (r["asset"], r["horizon"], r["direction"], r["net"]["avg"],
                   r["net"]["t_stat"], r["timing_p"], r["n"]))
    if best.get("timing_p") is not None and best["timing_p"] < 0.05:
        return ("时点有信息但样本不足：%s %s %s 净 %+.3f%%/笔、t=%s、p=%s，只有 %d 笔（需 ≥30），"
                "只能算线索、不能上实盘。" % (best["asset"], best["horizon"], best["direction"],
                                       best["net"]["avg"], best["net"]["t_stat"],
                                       best["timing_p"], best["n"]))
    return "没有任何格子同时满足净>0、t≥2、时机 p<0.05；该指标在窗口内不构成可验证优势。"


def _carry_run(assets: str, windows: str) -> Dict[str, Any]:
    alist = tuple(a.strip().upper() for a in assets.split(",") if a.strip())
    wlist = tuple(int(w) for w in windows.split(",") if w.strip().isdigit()) or (30, 90, 320)
    key = ("carry", alist, wlist)
    now = time.time()
    with _LOCK:
        hit = _CACHE.get(key)
    if hit and now - hit[0] < _CACHE_TTL:
        out = dict(hit[1]); out["cached"] = True
        return out
    conn = _ro_conn()
    try:
        rep = cy.evaluate(conn, alist, wlist)
    finally:
        conn.close()
    rep["cached"] = False
    with _LOCK:
        _CACHE[key] = (time.time(), rep)
    return rep


@router.get("/api/edge/carry")
async def edge_carry(
    assets: str = Query("BTC,ETH,SOL"),
    windows: str = Query("30,90,320", description="统计窗口（天），逗号分隔"),
):
    """资金费率套息：不吃方向，靠收 8 小时费率租金。给年化%、正费率占比、回本天数。"""
    return {"ok": True, "data": _carry_run(assets, windows)}


@router.get("/api/edge/scan")
async def edge_scan(
    kind: str = Query("funding", description="结构性指标：funding / lsratio / oi"),
    assets: str = Query("BTC,ETH,SOL"),
    days: int = Query(90, ge=30, le=365),
    q_low: float = Query(0.2, gt=0.0, lt=0.5),
    q_high: float = Query(0.8, gt=0.5, lt=1.0),
    horizons: str = Query("8h,1d"),
    k: int = Query(2000, ge=500, le=5000),
):
    """结构性指标优势体检：极端分位 → 方向 → 真实 K 线结算 → t + 置换 p。"""
    if q_high <= q_low:
        return {"ok": False, "error": "q_high 必须大于 q_low"}
    return {"ok": True, "data": _run(kind, assets, days, q_low, q_high, horizons, k)}
