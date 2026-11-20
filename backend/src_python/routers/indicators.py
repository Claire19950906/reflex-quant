# -*- coding: utf-8 -*-
"""v188 技术指标 / 免费宏观聚合 / 技术状态风控路由（结构切片，2026-11-17）。

从 api_server.py 搬来 3 条端点：
  GET  /api/v188/indicators（列出已注册指标，可按 category 过滤）
  POST /api/v188/indicators/run（链式跑若干指标）
  GET  /api/v188/macro/snapshot（DXY/US10Y/白银/板块 ETF/Coinglass 资金费率/财报日历
       六源并发，任一失败不影响其余，全部失败也如实标 error 不造假）
  GET  /api/technical-state/risk（v412：仓位系数 + 止损 ATR 倍数，只读不写库）
"""
from __future__ import annotations

import asyncio
import importlib
import time
from typing import Any, Dict, Optional

from fastapi import APIRouter

import db

router = APIRouter()


def _api():
    """延迟回引 api_server（新闻/采集助手仍在原文件）。"""
    import api_server
    return api_server


def _api():
    """延迟回引 api_server（采集/报表助手仍在原文件）。"""
    import api_server
    return api_server


@router.get("/api/v188/indicators")
async def v188_indicators_list(category: Optional[str] = None):
    """v188+: 列出已注册技术指标 (可按 category 过滤)。"""
    try:
        import technical_indicators as _ti
        return {"ok": True, "count": len(_ti.INDICATORS),
                "indicators": _ti.list_indicators(category)}
    except Exception as exc:
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}


@router.post("/api/v188/indicators/run")
async def v188_indicators_run(body: Dict[str, Any]):
    """v188+: 链式跑若干技术指标。
    body: {"indicators": ["rsi", "macd", "bollinger_state"], "closes": [100,101,...]}
    """
    try:
        import technical_indicators as _ti
        names = body.get("indicators") or []
        closes = body.get("closes") or []
        pipe = _ti.IndicatorPipeline()
        for n in names:
            pipe.add(n)
        return {"ok": True, "results": pipe.run(closes)}
    except Exception as exc:
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}


@router.get("/api/v188/macro/snapshot")
async def v188_macro_snapshot():
    """v188+: 一次拉 6 个免费零延迟数据源 (DXY/US10Y/白银/板块ETF/Coinglass/财报)。
    注: 拉取顺序 DXY→国债→白银→板块ETF→Coinglass→财报, 平均 < 3s, 任一失败不影响其余。
    """
    out: Dict[str, Any] = {"ts": int(time.time()), "sources": {}}
    # 1. DXY
    try:
        out["sources"]["dxy"] = importlib.import_module("dollar_index_feed").fetch_dxy()
    except Exception as exc:
        out["sources"]["dxy"] = {"error": f"{type(exc).__name__}: {exc}"}
    # 2. 国债
    try:
        out["sources"]["treasury"] = importlib.import_module("treasury_yield_feed").fetch_yields()
    except Exception as exc:
        out["sources"]["treasury"] = {"error": f"{type(exc).__name__}: {exc}"}
    # 3. 白银
    try:
        out["sources"]["silver"] = importlib.import_module("silver_feed").fetch_silver()
    except Exception as exc:
        out["sources"]["silver"] = {"error": f"{type(exc).__name__}: {exc}"}
    # 4. 板块 ETF
    try:
        out["sources"]["sector_etfs"] = importlib.import_module("sector_etf_feed").fetch_sector_etfs()
    except Exception as exc:
        out["sources"]["sector_etfs"] = {"error": f"{type(exc).__name__}: {exc}"}
    # 5. Coinglass 资金费率
    try:
        out["sources"]["coinglass_funding"] = importlib.import_module("coinglass_free").fetch_funding_rates()
    except Exception as exc:
        out["sources"]["coinglass_funding"] = {"error": f"{type(exc).__name__}: {exc}"}
    # 6. 财报日历
    try:
        out["sources"]["earnings"] = importlib.import_module("earnings_calendar").fetch_earnings()
    except Exception as exc:
        out["sources"]["earnings"] = {"error": f"{type(exc).__name__}: {exc}"}
    out["ok"] = True
    return out


# ─── 2026-11-17 从 api_server 追加 ──────────

@router.get("/api/technical-state/risk")
async def get_technical_state_risk(asset: str = "BTC", timeframe: str = "4h"):
    """v412：当前风控档位（仓位系数 + 止损 ATR 倍数）。

    为什么要单独开一个接口：状态层的方向读数已经证明没有预测力（v408~v411），
    但"这段路有多颠"是实测可用的——前端下单/模拟下单需要的就是**仓位与止损**，
    而不是再来一个方向信号。这里只做只读读取，不写库、不参与任何决策分。

    返回里的 `risk.asof` 是最后一根 K 线的时间戳（毫秒），前端据此标注"读数时间"，
    避免把隔夜读数当成实时。
    """
    import technical_state as _ts

    def _read() -> Dict[str, Any]:
        conn = db.get_connection()
        conn.row_factory = __import__("sqlite3").Row
        last_close = None
        try:
            state = _ts.for_asset(asset.upper(), timeframe=timeframe)
            _lc = conn.execute(
                "SELECT close FROM kline_bars WHERE symbol=? AND timeframe=? "
                "ORDER BY ts DESC LIMIT 1",
                (asset.upper(), timeframe),
            ).fetchone()
            last_close = None if _lc is None else _lc[0]
        finally:
            conn.close()
        if "error" in state:
            return {"available": False, "error": state["error"]}
        meta = state.get("_meta") or {}
        risk = state.get("risk") or {}
        # candle_last_ts 在状态层是**秒**（与 external_indicators 口径一致），
        # 前端 JS 的 Date 要毫秒 —— 在这里统一换算，别让前端猜单位。
        _ts_raw = meta.get("candle_last_ts")
        try:
            _ts_ms = int(float(_ts_raw)) * (1000 if float(_ts_raw) < 1e12 else 1)
        except (TypeError, ValueError):
            _ts_ms = None
        bd = state.get("bias_detail") or {}
        return {
            "available": bool(risk.get("available")),
            "asset": meta.get("asset") or asset.upper(),
            "timeframe": meta.get("timeframe") or timeframe,
            "asof": _ts_ms,
            "bars": meta.get("bars"),
            # 最新收盘价：前端据此把"止损 ATR 倍数"换成具体止损价（没有价就只显示百分比）
            "last_close": last_close,
            "readout": state.get("readout") or "",
            "bias": bd.get("bias"),
            "coverage": bd.get("coverage"),
            "usable": bool(bd.get("usable")),
            # v479：sizing_evidence 一并带出 —— 前端要能说清"仓位系数不是收益信号"
            "risk": ({k: risk.get(k) for k in (
                "level", "size_factor", "stop_atr_multiple", "atr_pct",
                "expected_adverse_pct", "drivers", "note", "score",
                "sizing_evidence")} if risk else None),
        }

    return await asyncio.to_thread(_read)
