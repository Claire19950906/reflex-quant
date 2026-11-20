# -*- coding: utf-8 -*-
"""因果链库 / 因果假设路由（结构切片，2026-11-17）。

从 api_server.py 搬来 8 条端点 + 4 个请求模型：
  /api/causal/chains          GET（v283+ 带弱 ETag；v339 P3 默认 lite 字段集，?full=1 拿全文）
                              POST（新建）
  /api/causal/chains/{id}     PUT（改）/ DELETE
  /api/causal/chains/bulk-toggle     v283+ 批量启停（ids+enabled 或 scope）
  /api/causal/chains/{id}/weight     PATCH 只改权重（列表行内改权用）
  /api/causal/chains/{id}/toggle     PATCH 翻转启停
  /api/causal-graph/hypothesis       v222 用户主动立因果假设 → upsert 节点 + 立刻核验

注意两条顺序敏感的路径：/bulk-toggle 与 /{chain_id}/weight、/{chain_id}/toggle 是不同
method 或不同段数，未与 /{chain_id} 冲突；但 /bulk-toggle 必须排在 /{chain_id} 的分段匹配
之前（POST vs PUT 不同 method，实际不冲突，保持原顺序即可）。

_now_iso 在本文件内自带一份（与 id_reconcile / crowd_sources / capability_acceptance
既有约定一致，项目里本来就是每个模块各留一份，避免为一个 3 行函数造共享层）。
"""
from __future__ import annotations

import asyncio
import json
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, HTTPException, Request, Response
from pydantic import BaseModel

import causal_chains
import causal_graph
import chain_prior
import config
import db

router = APIRouter()


def _with_evidence(rows: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """给链列表补证据字段（n / 命中率 / 区间 / 基准率 / 超额 / 档位）。

    v417：前端因果链管理页此前只有"触发词 + 权重"，看一条链准不准要另开
    `/api/causal/quality` 或手工查库 —— 证据必须跟列表一起下发。
    失败时原样返回（列表可用性优先于附加字段）。
    """
    if not rows:
        return rows
    conn = None
    try:
        conn = db.get_connection()
        return chain_prior.annotate_chains(conn, rows)
    except Exception:
        return rows
    finally:
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass


def _now_iso() -> str:
    """完整 ISO 北京时间串（秒级，含 +08:00 时区），与 api_server 侧同名函数逐字一致。"""
    return datetime.now(config.TZ_SHANGHAI).isoformat(timespec="seconds")


# -- 因果链库（宏观因果知识注入 [因果链分析] 段）-----------------------------

class CausalChainBody(BaseModel):
    name: str = ""
    cause: str = ""
    effect: str = ""
    trigger_words: str = ""
    note: str = ""
    mechanism: str = ""
    precondition: str = ""
    invalidation: str = ""
    weight: Optional[float] = None
    # v283+ · 来源追踪:区分"手填 / 哪个 panel 投的 / 哪个 auto 流水线"
    # manual              = 纯手填(默认)
    # manual_from_panel   = 从 panel 推送(cause 前缀带来源)
    # auto_real/auto_quick_sim/auto_mixed = 自动提炼(已存在)
    origin: str = "manual"


class BulkToggleBody(BaseModel):
    """v283+ · 批量启停。ids=[]+scope='all_enabled/all_disabled/all' 时按 scope 操作。"""
    ids: List[int] = []
    scope: str = ""  # 'all_enabled' / 'all_disabled' / 'all'(全表)
    enabled: Optional[int] = None  # None=翻转,0=全停,1=全启


class WeightBody(BaseModel):
    weight: float = 1.0


class HypothesisBody(BaseModel):
    """v222：用户主动输入因果假设（手填 chain → 立即核验）。"""
    asset: str
    label: str  # 节点名，如「DXY 走强」「美联储鹰派」
    kind: str = "core"  # core / condition / asset
    is_core: bool = True
    confidence: float = 0.5
    chain: List[str] = []  # 因果链短语数组，每句 ≤40 字
    note: str = ""
    auto_verify: bool = True  # 是否立即调 verify_hypothesis 跑核验
    lookback_days: int = 30  # 核验回看天数


@router.get("/api/causal/chains")
async def causal_chains_list(request: Request, response: Response):
    """v283+ · 带 ETag:客户端下次传 If-None-Match 即返回 304(零带宽)。

    v339 P3: 默认返回 lite 字段集（不含 mechanism/precondition/invalidation/note，
    这 4 个长文本占 1 MB 里的 558 KB 而列表不渲染）；需要全文传 `?full=1`。
    可选 `?limit=N` 只取最近 N 条。
    """
    full = (request.query_params.get("full") or "").lower() in ("1", "true", "yes")
    try:
        limit = max(0, int(request.query_params.get("limit") or 0))
    except (TypeError, ValueError):
        limit = 0
    rows = await asyncio.to_thread(
        lambda: causal_chains.list_chains(lite=not full, limit=limit)
    )
    rows = await asyncio.to_thread(_with_evidence, rows)
    # 弱 ETag:基于 id+updated_at,确保任何写操作都让缓存失效
    import hashlib
    # 变体标识必须进签名，否则先取 lite、再带同一 If-None-Match 取 full 会拿到 304 旧体
    # v417: 证据（hit/miss/confidence）会随 outcome 评估变化而 updated_at 不变，
    # 必须一起进签名，否则客户端会把"昨天的命中率"缓存到今天。
    sig_src = ("full" if full else "lite") + f":{limit}|" + "|".join(
        f"{r.get('id')}:{r.get('updated_at','')}:{r.get('hit_count')}:"
        f"{r.get('miss_count')}:{r.get('confidence')}" for r in rows)
    etag = '"' + hashlib.md5(sig_src.encode()).hexdigest()[:16] + '"'
    response.headers["ETag"] = etag
    response.headers["Cache-Control"] = "private, max-age=0, must-revalidate"
    # 客户端带 If-None-Match → 304(Not Modified)
    if_none_match = request.headers.get("if-none-match")
    if if_none_match and if_none_match == etag:
        return Response(status_code=304, headers={"ETag": etag})
    return rows


@router.post("/api/causal/chains")
async def causal_chains_add(body: CausalChainBody):
    try:
        return await asyncio.to_thread(
            causal_chains.add_chain, body.cause, body.effect,
            name=body.name, trigger_words=body.trigger_words, note=body.note,
            mechanism=body.mechanism, precondition=body.precondition,
            invalidation=body.invalidation,
            weight=body.weight if body.weight is not None else 1.0,
            origin=body.origin,  # v283+ · 来源透传
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.put("/api/causal/chains/{chain_id}")
async def causal_chains_update(chain_id: int, body: CausalChainBody):
    try:
        return await asyncio.to_thread(
            causal_chains.update_chain, chain_id, body.cause, body.effect,
            name=body.name, trigger_words=body.trigger_words, note=body.note,
            mechanism=body.mechanism, precondition=body.precondition,
            invalidation=body.invalidation,
            weight=body.weight,
            origin=body.origin,  # v283+
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/api/causal/chains/bulk-toggle")
async def causal_chains_bulk_toggle(body: BulkToggleBody):
    """v283+ · 批量启停:ids=[..]+enabled=0/1/None,或 scope='all_enabled/all_disabled/all'。"""
    if not body.scope and not body.ids:
        raise HTTPException(status_code=400, detail="ids 或 scope 至少传一个")
    return await asyncio.to_thread(
        causal_chains.bulk_toggle_chains,
        ids=body.ids, scope=body.scope, enabled=body.enabled,
    )


@router.patch("/api/causal/chains/{chain_id}/weight")
async def causal_chains_set_weight(chain_id: int, body: WeightBody):
    """只更新权重（列表行内改权重用）。"""
    return await asyncio.to_thread(causal_chains.set_weight, chain_id, body.weight)


@router.delete("/api/causal/chains/{chain_id}")
async def causal_chains_del(chain_id: int):
    return await asyncio.to_thread(causal_chains.delete_chain, chain_id)


@router.patch("/api/causal/chains/{chain_id}/toggle")
async def causal_chains_toggle(chain_id: int):
    return await asyncio.to_thread(causal_chains.toggle_chain, chain_id)


# === v222：用户主动输入因果假设 ===
# 旧版：因果图谱完全由 AI 自动生成；用户无法直接表达「我怀疑 X 才是 BTC 下跌的根因」。
# 新版：用户提供 (asset, label, chain[]) → 系统 _upsert_node + 立即 verify_hypothesis 跑回看 30 天事实，
#       给出 CONFIRMED / CONTRADICTED / UNCONFIRMED + 证据条目数 + 命中的 fact_ids。
#       用户可以拿这个结果去对照实际走势，养成「先立假设再让系统证伪」的决策习惯。
@router.post("/api/causal-graph/hypothesis")
def causal_graph_hypothesis(body: HypothesisBody):
    """用户主动输入因果假设：upsert 节点 + 立即跑核验，返回 {node_id, status, ...}。"""
    try:
        asset = (body.asset or "").upper().strip()
        label = (body.label or "").strip()
        if not asset or not label:
            raise HTTPException(status_code=400, detail="asset 与 label 必填")
        kind = body.kind if body.kind in ("core", "condition", "asset") else "core"
        is_core = 1 if body.is_core else 0
        confidence = max(0.0, min(1.0, float(body.confidence)))
        # 构造 evidence JSON（与 _serialize_evidence 兼容）
        ev_payload: Dict[str, Any] = {
            "chain": list(body.chain or []),
            "user_input": True,
            "note": body.note or "",
            "submitted_at": _now_iso(),
        }
        ev_text = json.dumps(ev_payload, ensure_ascii=False)
        # upsert
        conn = db.get_connection()
        try:
            nid = causal_graph._upsert_node(
                conn, asset, label, kind, bool(is_core), confidence, evidence=ev_text,
            )
        finally:
            try:
                conn.close()
            except Exception:
                pass
        if nid is None:
            raise HTTPException(status_code=400, detail=f"label 不合法（{label}）")
        # 核验
        result: Dict[str, Any] = {
            "node_id": int(nid),
            "asset": asset,
            "label": label,
            "kind": kind,
            "is_core": bool(is_core),
            "user_input": True,
        }
        if body.auto_verify:
            try:
                # 回看 N 天的事实
                since_ts = (datetime.now() - timedelta(days=int(body.lookback_days))).isoformat()
            except Exception:
                since_ts = None
            facts = causal_graph._extract_facts(asset, since_ts=since_ts)
            verify_result = causal_graph.verify_hypothesis(int(nid), facts)
            result["verify"] = verify_result
            # 友好状态
            new_status = verify_result.get("new_status")
            if new_status == "CONFIRMED":
                result["status_zh"] = "已确认"
            elif new_status == "CONTRADICTED":
                result["status_zh"] = "已反驳"
            else:
                result["status_zh"] = "待验证"
        return result
    except HTTPException:
        raise
    except Exception as exc:
        import traceback as _tb
        print(f"[causal-graph/hypothesis] ERROR: {exc}\n{_tb.format_exc()}", flush=True)
        raise HTTPException(status_code=500, detail=f"hypothesis failed: {str(exc)[:200]}") from exc
