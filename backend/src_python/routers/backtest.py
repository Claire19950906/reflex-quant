# -*- coding: utf-8 -*-
"""策略回测路由（结构切片，2026-11-17）。

从 api_server.py 搬来 3 条端点：/api/backtest/run、/runs、/optimize。
strategy_store / backtest / _strategy_or_404 / LLM 优化器仍在 api_server，
router 用 _api() 回引（避免循环 import，也避免重复初始化 store）。
"""
from __future__ import annotations

import asyncio
import json
from typing import Any, Dict, Optional

from pydantic import BaseModel

from fastapi import APIRouter, HTTPException



class BacktestBody(BaseModel):
    strategy_id: int
    version_id: Optional[int] = None
    params: Optional[Dict[str, Any]] = None
    asset: str = ""
    limit: int = 500
    persist: bool = True


class OptimizeBody(BaseModel):
    strategy_id: int
    version_id: Optional[int] = None
    use_llm: bool = True


router = APIRouter()


def _api():
    """延迟回引 api_server（策略库、回测引擎与 LLM 客户端仍在原文件）。"""
    import api_server
    return api_server


@router.post("/api/backtest/run")
async def run_strategy_backtest(body: BacktestBody):
    strategy = _api()._strategy_or_404(body.strategy_id)
    if body.version_id:
        try:
            version = _api().strategy_store.get_version(body.version_id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        if version["strategy_id"] != body.strategy_id:
            raise HTTPException(status_code=400, detail="version does not belong to strategy")
    else:
        latest = strategy.get("latest_version")
        if not latest:
            raise HTTPException(status_code=400, detail="strategy has no version")
        version = latest
    params = _api().strategy_store.clamp_params(body.params or version["params"])
    report = await asyncio.to_thread(_api().backtest.run_backtest, params, asset=body.asset, limit=body.limit)
    run_id = None
    if body.persist:
        run_id = await asyncio.to_thread(
            _api().strategy_store.save_backtest_run, body.strategy_id, version["id"], params, report,
        )
    return {
        "run_id": run_id,
        "strategy": {"id": strategy["id"], "name": strategy["name"], "slug": strategy["slug"]},
        "version": {"id": version["id"], "version": version["version"]},
        "report": report,
    }


@router.get("/api/backtest/runs")
async def list_backtest_runs(strategy_id: int = 0, limit: int = 20):
    sid = strategy_id or None
    return await asyncio.to_thread(_api().strategy_store.list_backtest_runs, sid, limit)


@router.post("/api/backtest/optimize")
async def optimize_strategy(body: OptimizeBody):
    strategy = _api()._strategy_or_404(body.strategy_id)
    if body.version_id:
        version = _api().strategy_store.get_version(body.version_id)
    else:
        version = strategy.get("latest_version")
        if not version:
            raise HTTPException(status_code=400, detail="strategy has no version")
    report = await asyncio.to_thread(_api().backtest.run_backtest, version["params"])
    mode = "rules"
    note = ""
    if body.use_llm:
        try:
            llm = await asyncio.to_thread(_api()._llm_optimize_sync, version["params"], report)
            next_params, note, mode = llm["params"], llm["note"], "llm"
        except Exception:
            fallback = _api().backtest.optimize_params(version["params"], report)
            next_params, note, mode = fallback["params"], "；".join(fallback["notes"]), "rules"
    else:
        fallback = _api().backtest.optimize_params(version["params"], report)
        next_params, note, mode = fallback["params"], "；".join(fallback["notes"]), "rules"
    saved = await asyncio.to_thread(
        _api().strategy_store.add_version, body.strategy_id, next_params, source="llm" if mode == "llm" else "backtest", note=note,
    )
    return {
        "mode": mode,
        "baseline_report": {
            "sample_size": report["sample_size"],
            "taken": report["taken"],
            "winrate": report["winrate"],
            "total_pnl_usdt": report["total_pnl_usdt"],
            "max_drawdown_usdt": report["max_drawdown_usdt"],
            "insufficient_sample": report["insufficient_sample"],
        },
        "version": saved,
        "note": note,
    }
