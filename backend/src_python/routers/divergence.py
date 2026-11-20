# -*- coding: utf-8 -*-
"""背离雷达路由（v2 · 2026-11-16）

把 divergence_engine 的公开指标背离能力接到前端：
  GET /api/divergence/assets                 可用标的
  GET /api/divergence/scan                   当前信号（含状态、交易计划）
  GET /api/divergence/bars                   图表数据 + pivot 标注
  GET /api/divergence/stats                  按等级 / 趋势上下文的历史校准

只读 ohlcv，不写库、不碰下单链路。
"""
from __future__ import annotations

import sqlite3
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, HTTPException, Query

import divergence_engine as de
import divergence_ledger as dl

router = APIRouter(tags=["divergence"])

# v480：统一走 db_location（支持 TRIDENT_DB_PATH 覆盖）
from db_location import DB_PATH

_CACHE: Dict[str, Any] = {}
_CACHE_TTL = 45.0


def _conn() -> sqlite3.Connection:
    if not DB_PATH.exists():
        raise HTTPException(status_code=500, detail="event bus db not found")
    conn = sqlite3.connect(str(DB_PATH), check_same_thread=False, timeout=30.0)
    conn.row_factory = sqlite3.Row
    # v385: 高并发写库下默认 busy_timeout=0 会取锁即 BUSY；抬到 30s 走等锁，
    #   避免背离扫描在 WAL 竞争里直接抛锁错或长时间僵在取锁阶段。
    conn.execute("PRAGMA busy_timeout=30000")
    return conn


def _cache_get(key: str):
    item = _CACHE.get(key)
    if item and time.time() - item[0] < _CACHE_TTL:
        return item[1]
    return None


def _cache_set(key: str, value: Any):
    if len(_CACHE) > 200:
        _CACHE.clear()
    _CACHE[key] = (time.time(), value)


def _norm_interval(interval: str) -> str:
    iv = (interval or "1d").lower()
    return "1d" if iv in ("1d", "d", "day", "daily") else iv


@router.get("/api/divergence/assets")
def assets(interval: str = "1d") -> Dict[str, Any]:
    iv = _norm_interval(interval)
    conn = _conn()
    try:
        if iv == "1d":
            rows = conn.execute(
                "SELECT asset, COUNT(*) n FROM ohlcv_daily GROUP BY asset "
                "HAVING n>=300 ORDER BY n DESC").fetchall()
        else:
            rows = conn.execute(
                "SELECT asset, COUNT(*) n FROM ohlcv_intraday WHERE interval=? "
                "GROUP BY asset HAVING n>=300 ORDER BY n DESC", (iv,)).fetchall()
    finally:
        conn.close()
    return {"ok": True, "interval": iv,
            "items": [{"asset": r["asset"], "bars": r["n"]} for r in rows]}


def _scan(asset: str, interval: str, limit: int):
    key = f"scan:{asset}:{interval}:{limit}"
    hit = _cache_get(key)
    if hit is not None:
        return hit
    conn = _conn()
    try:
        rows = de.load_bars(conn, asset, interval, limit)
    finally:
        conn.close()
    if len(rows) < 120:
        raise HTTPException(status_code=404, detail=f"{asset} {interval} 数据不足")
    sigs = de.scan_signals(rows, asset=asset, interval=interval)
    _cache_set(key, (rows, sigs))
    return rows, sigs


@router.get("/api/divergence/scan")
def scan(asset: str = "BTC", interval: str = "1d", limit: int = 1200,
         within: int = 120, min_grade: str = "C") -> Dict[str, Any]:
    iv = _norm_interval(interval)
    rows, sigs = _scan(asset.upper(), iv, max(300, min(limit, 4000)))
    rank = {"A": 0, "B": 1, "C": 2, "D": 3}
    last = len(rows) - 1
    out: List[Dict[str, Any]] = []
    for s in sigs:
        c = s["confirm_idx"]
        if c is None or last - c > within:
            continue
        if rank.get(s["grade"], 9) > rank.get(min_grade.upper(), 2):
            continue
        item = dict(s)
        item.pop("evidence_full", None)
        item["bars_since_confirm"] = last - c
        item["confirm_at"] = de._fmt_ts(s["confirm_ts"])
        item["pivot_a"]["at"] = de._fmt_ts(s["pivot_a"]["ts"])
        item["pivot_b"]["at"] = de._fmt_ts(s["pivot_b"]["ts"])
        item["status"] = de._status(rows, s)
        item["last_close"] = rows[last]["close"]
        item["risk_reward"] = round(abs(s["target"] - s["pivot_b"]["price"]) /
                                    max(1e-9, abs(s["invalidation"] - s["pivot_b"]["price"])), 2)
        # 历史稳定亏钱的组合直接把证据挂在信号上 —— 不隐藏、也不改分数，
        # 只让人在点开这条信号时先看到"这个组合过去 10 笔只赢 1 笔"。
        _ineff = de.ineffective_of(s)
        if _ineff:
            item["ineffective_note"] = (f"{_ineff['label']}：历史 {_ineff['n']} 笔命中 "
                                        f"{_ineff['hit_rate']}%、平均 {_ineff['avg_pnl']}%")
        out.append(item)
    out.sort(key=lambda x: (rank.get(x["grade"], 9), -x["score"]))
    return {"ok": True, "asset": asset.upper(), "interval": iv,
            "bars": len(rows), "last_ts": rows[last]["ts"],
            "last_close": rows[last]["close"], "count": len(out), "items": out}


@router.get("/api/divergence/bars")
def bars(asset: str = "BTC", interval: str = "1d", limit: int = 400,
         within: int = 200) -> Dict[str, Any]:
    iv = _norm_interval(interval)
    all_rows, sigs = _scan(asset.upper(), iv, 1200)
    tail = all_rows[-max(120, min(limit, 1200)):]
    offset = len(all_rows) - len(tail)
    pivots = []
    last = len(all_rows) - 1
    for s in sigs:
        c = s["confirm_idx"]
        if c is None or last - c > within:
            continue
        for tag, p in (("a", s["pivot_a"]), ("b", s["pivot_b"])):
            idx = p["idx"] - offset
            if 0 <= idx < len(tail):
                pivots.append({"idx": idx, "ts": p["ts"], "price": p["price"],
                               "type": s["type"], "kind": s["kind"],
                               "grade": s["grade"], "tag": tag})
    return {"ok": True, "asset": asset.upper(), "interval": iv,
            "bars": [{"ts": r["ts"], "open": r["open"], "high": r["high"],
                      "low": r["low"], "close": r["close"], "volume": r["volume"]}
                     for r in tail],
            "pivots": pivots}


@router.get("/api/divergence/stats")
def stats(asset: str = "BTC", interval: str = "1d", horizon: int = 10,
          limit: int = 1500) -> Dict[str, Any]:
    iv = _norm_interval(interval)
    key = f"stats:{asset.upper()}:{iv}:{horizon}:{limit}"
    hit = _cache_get(key)
    if hit is not None:
        return hit
    conn = _conn()
    try:
        rows = de.load_bars(conn, asset.upper(), iv, max(400, min(limit, 4000)))
    finally:
        conn.close()
    if len(rows) < 200:
        raise HTTPException(status_code=404, detail=f"{asset} {iv} 数据不足")
    res = de.evaluate(rows, horizon=horizon, asset=asset.upper(), interval=iv)
    payload = {"ok": True, **res}
    _cache_set(key, payload)
    return payload


@router.get("/api/divergence/confluence")
def confluence(asset: str = "BTC", within: int = 40) -> Dict[str, Any]:
    """跨周期共振：同一标的在 15m / 1h / 4h / 1d 上的最新背离方向。

    只取每个周期最近 within 根内确认的信号；两个以上周期同向 → resonance。
    数据不足的周期直接跳过，不补零、不猜测。
    """
    levels = ["1d", "4h", "1h", "15m"]
    items: List[Dict[str, Any]] = []
    for iv in levels:
        try:
            rows, sigs = _scan(asset.upper(), iv, 900)
        except HTTPException:
            continue
        last = len(rows) - 1
        cands = [s for s in sigs if s["confirm_idx"] is not None and last - s["confirm_idx"] <= within]
        if not cands:
            items.append({"interval": iv, "bias": "neutral", "latest": None})
            continue
        s = max(cands, key=lambda x: (x["confirm_idx"], x["score"]))
        items.append({
            "interval": iv,
            "bias": s["type"],
            "latest": {
                "type": s["type"], "kind": s["kind"], "grade": s["grade"],
                "score": s["score"], "bars_since_confirm": last - s["confirm_idx"],
                "context": s.get("context"),
            },
        })
    bear = sum(1 for x in items if x["bias"] == "bearish")
    bull = sum(1 for x in items if x["bias"] == "bullish")
    resonance = "bearish" if bear >= 2 and bear > bull else "bullish" if bull >= 2 and bull > bear else "none"
    return {"ok": True, "asset": asset.upper(), "items": items,
            "resonance": resonance, "bear_count": bear, "bull_count": bull}


# ───────────────── 样本外验证 / 本地台账 ─────────────────
_VAL_CACHE: Dict[str, Any] = {}
_VAL_TTL = 3600.0
_LAST_SYNC = 0.0
_SYNC_TTL = 600.0


@router.get("/api/divergence/validation")
def validation(horizon: int = Query(20, ge=3, le=60),
               train_ratio: float = Query(0.7, ge=0.5, le=0.9),
               cost_bps: float = Query(8.0, ge=0, le=100),
               interval: str = "1d") -> Dict[str, Any]:
    """时间外样本：前 train_ratio 只观察，后段独立结算，扣往返成本。"""
    iv = _norm_interval(interval)
    key = f"{iv}|{horizon}|{train_ratio}|{cost_bps}"
    hit = _VAL_CACHE.get(key)
    if hit and time.time() - hit[0] < _VAL_TTL:
        return hit[1]
    result = dl.validate(horizon=horizon, train_ratio=train_ratio,
                         cost_bps=cost_bps, intervals=(iv,))
    _VAL_CACHE[key] = (time.time(), result)
    return result


@router.get("/api/divergence/scoreboard")
def scoreboard(horizon: int = 10) -> Dict[str, Any]:
    """指标记分牌：按 方向/形态/等级 拆开命中率与净收益，回答"这个指标该不该留"。

    与 `ledger_stats()` 的区别：那边按 趋势上下文 分桶（回答"什么环境下好用"），
    会把看涨和看跌混在一起；实测看跌 +1.9% / 看涨 −1.06%，合并看是 +0.3%，
    结论会变成"勉强能用"，实际是一半赚钱一半送钱。
    """
    key = f"sb:{horizon}"
    hit = _cache_get(key)
    if hit is not None:
        return hit
    out = dl.scoreboard(horizon=horizon)
    _cache_set(key, out)
    return out


@router.get("/api/divergence/ledger")
def ledger(status: str = "all", limit: int = Query(200, ge=1, le=500),
           sync: int = 0) -> Dict[str, Any]:
    """本地台账：最近信号 + 到期结算。

    v498 修：默认不再在 GET 里做结算（sync 默认值 1→0）。
    原来 GET 首次请求会同步跑一遍 sync_ledger（全标的扫描，10s+），而背离雷达页
    把它放在 Promise.all 里 → 整页卡在"扫描中…"、0 信号、图表加载中。
    现在 GET 只读；要结算由页面按钮调 POST /ledger/sync，或显式传 sync=1。
    """
    global _LAST_SYNC
    if sync and time.time() - _LAST_SYNC > _SYNC_TTL:
        try:
            dl.sync_ledger(horizon=10, within_bars=90, intervals=("1d",))
            _LAST_SYNC = time.time()
        except Exception:
            pass
    return dl.ledger_list(status=status, limit=limit)


@router.post("/api/divergence/ledger/sync")
def ledger_sync(horizon: int = 10, within: int = 90, interval: str = "1d") -> Dict[str, Any]:
    global _LAST_SYNC
    out = dl.sync_ledger(horizon=horizon, within_bars=within,
                         intervals=(_norm_interval(interval),))
    _LAST_SYNC = time.time()
    return out


@router.get("/api/divergence/effective")
def effective(within: int = Query(90, ge=5, le=300)) -> Dict[str, Any]:
    """当前命中「已通过样本外检验」规则的信号（跨标的）。

    v385 性能：27 个达标标的串行重算全套指标 + pivot，纯扫描实测 ~8s，叠加
    1GB WAL 库锁竞争后单次 >12s，前端 fetch 一直挂死。这里：
      1) 缓存键带上全库最新日线 ts，K 线没更新时直接命中 45s 内存缓存；
      2) 单标的「bars+sigs」按 asset+最新 bar ts 缓存，重复请求不重算指标；
      3) 同步 def 路由由 FastAPI 自动丢线程池，阻塞不卡事件循环。
    """
    conn = _conn()
    try:
        # 全库最新日线时间戳，作为结果缓存的失效水位
        row = conn.execute("SELECT MAX(ts) FROM ohlcv_daily").fetchone()
        newest = int(row[0] or 0)
        key = f"eff|{within}|{newest}"
        hit = _cache_get(key)
        if hit:
            return hit
        rows = conn.execute(
            "SELECT asset, COUNT(*) n FROM ohlcv_daily GROUP BY asset HAVING n>=400").fetchall()
        items = []
        for r in rows:
            asset = r["asset"]
            bars, sigs = _scan_asset(asset, "1d", 1200, conn)
            last = len(bars) - 1
            for s in sigs:
                if not s.get("effective"):
                    continue
                if last - s["confirm_idx"] > within:
                    continue
                s["bars_since_confirm"] = last - s["confirm_idx"]
                s["confirm_at"] = de._fmt_ts(s["confirm_ts"])
                s["status"] = de._status(bars, s)
                s.pop("evidence", None)
                items.append(s)
        items.sort(key=lambda x: -x["confirm_idx"])
        out = {"ok": True, "count": len(items), "items": items,
               "rules": de.EFFECTIVE_RULES}
        _cache_set(key, out)
        return out
    finally:
        conn.close()


def _scan_asset(asset: str, interval: str, limit: int,
                conn: sqlite3.Connection) -> tuple:
    """单标的 bars+sigs，按 asset+最新 bar ts 缓存，避免每次全量重算指标。"""
    bar_row = conn.execute(
        "SELECT MAX(ts) FROM ohlcv_daily WHERE asset=?", (asset,)).fetchone()
    bar_ts = int(bar_row[0] or 0)
    key = f"asset:{asset}:{interval}:{limit}:{bar_ts}"
    hit = _cache_get(key)
    if hit is not None:
        return hit
    bars = de.load_bars(conn, asset, interval, limit)
    sigs = de.scan_signals(bars, asset=asset, interval=interval)
    _cache_set(key, (bars, sigs))
    return bars, sigs
