# -*- coding: utf-8 -*-
"""信念系统 / 知识缺口 / 外部数据接入路由（结构切片，2026-11-17）。

整块从 api_server.py 搬来（v223 建立的 Belief Management System + v225 Stage 5 外部数据接入），
共 14 条端点 + 2 个请求模型：
  /api/beliefs                      创建 / 列出
  /api/beliefs/{id}                 详情
  /api/beliefs/{id}/lineage         血缘
  /api/beliefs/{id}/decompose       世界观拆子链（旧版要手拆，这里是 LLM 自动拆）
  /api/beliefs/{id}/verify          子链验证
  /api/beliefs/{id}/revise          改置信度/状态/regime
  /api/beliefs/revise-all           批量改
  /api/knowledge-gaps               全量知识缺口扫描（Stage 3）
  /api/beliefs/{id}/knowledge-gaps  单条缺口
  /api/beliefs/{id}/external-coverage 外部指标覆盖度
  /api/external/fetch-and-ingest    拉外部数据并 ingest（Stage 5）
  /api/external/ingest              裸 ingest
  /api/external/indicators          已 ingest 指标列表

这些端点全部走函数内惰性导入（belief_store / external_data），块内没有依赖 api_server 的
任何模块级对象，所以是整块搬、零改动。
"""
from __future__ import annotations

import asyncio
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

import belief_store

router = APIRouter()


# === v223：Belief Management System (世界观 → 子链 → 自动分解 → 合成 verdict) ===
# 旧版：v222 的 hypothesis endpoint 只能输入一条具体 chain；
#       抽象世界观（"美联储预期是大部分涨跌根因"）用户需要手动拆 5 条子链。
# 新版：用户输入 worldview → 系统 LLM 自动拆 3-7 条可测子链 → 跑 verify →
#       合成 verdict (CONFIRMED/CONTRADICTED/PARTIAL/UNVERIFIED) + regime_map +
#       trading_rule_candidates。同时自动跑 conflict detector 找出与已有信念的张力。

class BeliefBody(BaseModel):
    """Create a new belief (worldview or chain)."""
    thesis: str
    level: str = "worldview"
    asset: str = ""
    parent_id: Optional[int] = None
    confidence: float = 0.5
    regime_tags: List[str] = []
    note: str = ""


class BeliefReviseBody(BaseModel):
    """Revise a belief's confidence/status/regime."""
    confidence: Optional[float] = None
    status: Optional[str] = None
    regime_tags: Optional[List[str]] = None
    note: str = ""


@router.post("/api/beliefs")
async def beliefs_create(body: BeliefBody):
    """Create a new belief + auto conflict detection vs existing."""
    try:
        return await asyncio.to_thread(
            belief_store.create_belief,
            body.thesis, level=body.level, asset=body.asset,
            parent_id=body.parent_id, confidence=body.confidence,
            regime_tags=body.regime_tags, note=body.note,
        )
    except HTTPException:
        raise
    except Exception as exc:
        import traceback as _tb
        print(f"[beliefs/create] ERROR: {exc}\n{_tb.format_exc()}", flush=True)
        raise HTTPException(status_code=400, detail=f"create failed: {str(exc)[:200]}") from exc


@router.get("/api/beliefs")
async def beliefs_list(
    level: Optional[str] = None,
    status: Optional[str] = None,
    asset: Optional[str] = None,
    limit: int = 100,
):
    """List beliefs; optional filters."""
    return await asyncio.to_thread(
        belief_store.list_beliefs, level=level, status=status,
        asset=asset, limit=limit,
    )


@router.get("/api/beliefs/{belief_id}")
async def beliefs_get(belief_id: int):
    """Fetch one belief + sub_chains + edges."""
    out = await asyncio.to_thread(belief_store.get_belief, belief_id)
    if not out:
        raise HTTPException(status_code=404, detail=f"belief {belief_id} not found")
    return out


@router.get("/api/beliefs/{belief_id}/lineage")
async def beliefs_lineage(belief_id: int, depth: int = 3):
    """Recursive lineage tree: parent / children / conflicts."""
    out = await asyncio.to_thread(belief_store.get_lineage, belief_id, depth)
    if not out:
        raise HTTPException(status_code=404, detail=f"belief {belief_id} not found")
    return out


@router.post("/api/beliefs/{belief_id}/decompose")
async def beliefs_decompose(
    belief_id: int,
    max_sub: int = 7,
    lookback_days: int = 30,
    auto_verify: bool = True,
):
    """LLM-split worldview into testable sub-chains; immediately verify."""
    try:
        return await asyncio.to_thread(
            belief_store.decompose_worldview, belief_id,
            max_sub=max_sub, lookback_days=lookback_days,
            auto_verify=auto_verify,
        )
    except HTTPException:
        raise
    except Exception as exc:
        import traceback as _tb
        print(f"[beliefs/decompose] ERROR: {exc}\n{_tb.format_exc()}", flush=True)
        raise HTTPException(status_code=400, detail=f"decompose failed: {str(exc)[:200]}") from exc


@router.post("/api/beliefs/{belief_id}/verify")
async def beliefs_verify(belief_id: int, lookback_days: int = 30):
    """Re-verify (re-synthesize) a worldview's verdict."""
    try:
        return await asyncio.to_thread(
            belief_store._synthesize_from_children, belief_id,
            lookback_days, True,
        )
    except HTTPException:
        raise
    except Exception as exc:
        import traceback as _tb
        print(f"[beliefs/verify] ERROR: {exc}\n{_tb.format_exc()}", flush=True)
        raise HTTPException(status_code=400, detail=f"verify failed: {str(exc)[:200]}") from exc


@router.post("/api/beliefs/{belief_id}/revise")
async def beliefs_revise(belief_id: int, body: BeliefReviseBody):
    """Revise confidence/status/regime; writes history row."""
    try:
        return await asyncio.to_thread(
            belief_store.revise_belief, belief_id,
            confidence=body.confidence, status=body.status,
            regime_tags=body.regime_tags, note=body.note,
            actor="user",
        )
    except HTTPException:
        raise
    except Exception as exc:
        import traceback as _tb
        print(f"[beliefs/revise] ERROR: {exc}\n{_tb.format_exc()}", flush=True)
        raise HTTPException(status_code=400, detail=f"revise failed: {str(exc)[:200]}") from exc


@router.post("/api/beliefs/revise-all")
async def beliefs_revise_all(lookback_days: int = 30):
    """Scan all active worldviews; auto-adjust confidence by evidence."""
    return await asyncio.to_thread(belief_store.revise_all, lookback_days)


@router.get("/api/knowledge-gaps")
async def knowledge_gaps_all(lookback_days: int = 30):
    """Stage 3: 扫所有 active worldviews，按 coverage_score 升序（最缺证据的在前）。"""
    return await asyncio.to_thread(
        belief_store.detect_all_gaps, lookback_days)


@router.get("/api/beliefs/{belief_id}/knowledge-gaps")
async def belief_knowledge_gaps(belief_id: int, lookback_days: int = 30):
    """Stage 3: 扫描 worldview 缺证据的 regime + 建议采集数据。"""
    return await asyncio.to_thread(
        belief_store.detect_knowledge_gaps, belief_id, lookback_days)


# v225 Stage 5 — External Data Ingest
@router.post("/api/external/fetch-and-ingest")
async def external_fetch_and_ingest(
    asset: str,
    yf_symbol: str,
    lookback_days: int = 30,
    regime_tags: Optional[List[str]] = None,
):
    """一键：从 Yahoo Finance 抓数据并写入 external_indicators 表。
    regime_tags 不传则按 INDICATOR_REGIMES 自动推断。"""
    import external_data as ed
    return await asyncio.to_thread(
        ed.fetch_and_ingest, asset, yf_symbol, lookback_days, regime_tags)


@router.post("/api/external/ingest")
async def external_ingest(
    asset: str,
    indicator: str,
    records: List[Dict[str, Any]],
    source: str = "manual",
    regime_tags: Optional[List[str]] = None,
):
    """手动 ingest 任意数据。records 形如 [{ts, value}, ...]。"""
    import external_data as ed
    return await asyncio.to_thread(
        ed.ingest_external, asset, indicator, records, source, regime_tags, None)


@router.get("/api/external/indicators")
async def external_list_indicators(
    asset: Optional[str] = None,
    indicator: Optional[str] = None,
    limit: int = 200,
):
    """列出已 ingest 的外部指标。"""
    import external_data as ed
    return await asyncio.to_thread(
        ed.list_indicators, asset, indicator, limit)


@router.get("/api/beliefs/{belief_id}/external-coverage")
async def belief_external_coverage(belief_id: int):
    """Stage 5: 用 external_indicators 覆盖 worldview 的 regime_tags 缺口。"""
    import external_data as ed
    return await asyncio.to_thread(
        ed.coverage_for_worldview, belief_id)
