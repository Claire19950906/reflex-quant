# -*- coding: utf-8 -*-
"""只读视图与决策动作路由（结构切片，2026-11-17）。

从 api_server.py 搬来 /api/v1 那一段 11 条端点（含中间夹着的 /api/nlq）：
成绩单、K线覆盖度、决策动作/静音/解除、反事实回看、记忆检索与统计、自然语言问答。
点内原本都是惰性 import（scorecard_share / counterfactual / decision_actions / nlq），
原样保留，不改成顶层导入以拖慢启动。
"""
from __future__ import annotations

from typing import Any, Dict

from fastapi import APIRouter, Body

import db

router = APIRouter()


@router.get("/api/v1/scorecard")
def scorecard_data():
    """v386 · 成绩单数据（供 /scorecard 页与外部只读展示复用同一口径）。"""
    try:
        import scorecard_share as _sc
        with db.get_connection() as conn:
            return {"ok": True, "data": _sc.collect(conn)}
    except Exception as exc:
        return {"ok": False, "error": f"{type(exc).__name__}: {str(exc)[:160]}"}


@router.get("/api/v1/kline/coverage/{symbol}")
def kline_coverage(symbol: str, recent_hours: int = 72):
    """v384 · 某品种库内 K 线周期覆盖（供前端禁用不存在的周期，避免空白图表）。

    返回每个周期最近 N 小时内的根数 + 数量 + 最近时间；recommended 为「有数据的最细周期」。
    """
    try:
        sym = str(symbol or "").upper().strip()
        sym = {"GOLD": "XAU", "OIL": "WTI"}.get(sym, sym)
        since_ms = int((__import__("time").time() - max(1, int(recent_hours)) * 3600) * 1000)
        with db.get_connection() as conn:
            rows = conn.execute(
                """SELECT timeframe, COUNT(*), MAX(ts)
                     FROM kline_bars WHERE symbol=?
                    GROUP BY timeframe""",
                (sym,),
            ).fetchall()
            recent = conn.execute(
                """SELECT timeframe, COUNT(*) FROM kline_bars
                    WHERE symbol=? AND ts>=? GROUP BY timeframe""",
                (sym, since_ms),
            ).fetchall()
        counts = {str(r[0]): int(r[1] or 0) for r in rows}
        recent_map = {str(r[0]): int(r[1] or 0) for r in recent}
        order = ["1m", "3m", "5m", "10m", "15m", "30m", "1h", "4h", "1d"]
        # 「可用」= 近期有 ≥8 根；分钟级要求近期有数据，否则视为该品种没有分钟线
        usable = [tf for tf in order if recent_map.get(tf, 0) >= 8]
        recommended = usable[0] if usable else (min(counts, key=counts.get) if counts else "1d")
        return {
            "ok": True, "symbol": sym, "recent_hours": recent_hours,
            "timeframes": {tf: {"bars": counts.get(tf, 0), "recent": recent_map.get(tf, 0)}
                           for tf in order if counts.get(tf)},
            "usable": usable,
            "recommended": recommended,
        }
    except Exception as exc:
        return {"ok": False, "error": f"{type(exc).__name__}: {str(exc)[:160]}"}


@router.get("/api/v1/decisions/actions")
def decision_actions_list(limit: int = 30):
    """v383 · 处置账列表（谁在什么时候静默/忽略/反手）。"""
    try:
        from signals import decision_actions as _da
        with db.get_connection() as conn:
            return {"ok": True, "items": _da.list_recent(conn, limit=limit)}
    except Exception as exc:
        return {"ok": False, "error": f"{type(exc).__name__}: {str(exc)[:160]}"}


@router.get("/api/v1/decisions/muted")
def decision_muted_assets():
    """v383/v388 · 当前静默中的品种；v388 起附 `silences`（带行 id，可逐条撤销）。"""
    try:
        from signals import decision_actions as _da
        with db.get_connection() as conn:
            return {"ok": True, "items": _da.muted_assets(conn), "silences": _da.active_silences(conn)}
    except Exception as exc:
        return {"ok": False, "error": f"{type(exc).__name__}: {str(exc)[:160]}"}


@router.post("/api/v1/decisions/unsilence")
def decision_unsilence(payload: Dict[str, Any] = Body(default={})):
    """v388 · 撤销静默：body 给 {"action_id": 12} 或 {"asset": "XAU"}。

    置过期而非删行，保留「谁在什么时候撤销过」的痕迹。
    """
    try:
        from signals import decision_actions as _da
        aid = int((payload or {}).get("action_id") or 0)
        asset = str((payload or {}).get("asset") or "")
        if not aid and not asset:
            return {"ok": False, "error": "需要 action_id 或 asset"}
        with db.get_connection() as conn:
            return _da.unsilence(conn, action_id=aid, asset=asset)
    except Exception as exc:
        return {"ok": False, "error": f"{type(exc).__name__}: {str(exc)[:160]}"}


@router.post("/api/v1/decisions/{decision_id}/action")
def decision_action(decision_id: int, payload: Dict[str, Any] = Body(default={})):
    """v383 · 一键处置：silence_asset / ignore_decision / reverse。

    body: {"action": "...", "duration_min": 60, "note": "..."}
    """
    try:
        from signals import decision_actions as _da
        action = str((payload or {}).get("action") or "")
        duration = int((payload or {}).get("duration_min") or 0)
        note = str((payload or {}).get("note") or "")
        with db.get_connection() as conn:
            res = _da.record(conn, decision_id, action, duration,
                             note, source=str((payload or {}).get("source") or "ui"),
                             scope=str((payload or {}).get("scope") or "asset"),
                             event_type=str((payload or {}).get("event_type") or ""))
        return res
    except Exception as exc:
        return {"ok": False, "error": f"{type(exc).__name__}: {str(exc)[:160]}"}


@router.get("/api/nlq")
def nlq_ask(q: str = ""):
    """v381 · 自然语言问库（白名单只读查询，不让 LLM 自由写 SQL）。"""
    try:
        import nlq as _nlq
        if not (q or "").strip():
            return {"ok": False, "error": "缺少 q 参数（例如 q=黄金最近为什么跌）"}
        return _nlq.ask(q)
    except Exception as exc:
        return {"ok": False, "error": f"{type(exc).__name__}: {str(exc)[:160]}"}


@router.get("/api/v1/counterfactual/recent")
def counterfactual_recent(limit: int = 30):
    """v380 · 最近可重放的决策（反事实页选择器）。"""
    try:
        import counterfactual as _cf
        return {"ok": True, "items": _cf.recent_decisions(min(max(int(limit or 30), 1), 200))}
    except Exception as exc:
        return {"ok": False, "error": f"{type(exc).__name__}: {str(exc)[:160]}"}


@router.get("/api/v1/counterfactual/{decision_id}")
def counterfactual_detail(decision_id: int, horizon: int = 0):
    """v380 · 单笔决策的四条路径重放（actual / skip / reverse / delay / half）。"""
    try:
        import counterfactual as _cf
        res = _cf.four_legs(decision_id, horizon_minutes=(horizon or None))
        return res
    except Exception as exc:
        return {"ok": False, "reason": "exception", "error": f"{type(exc).__name__}: {str(exc)[:160]}"}


@router.get("/api/v1/memory/search")
def memory_search(q: str = "", asset: str = "", event: str = "", k: int = 5):
    """v379 · 语义记忆检索（只读）。

    给前端「记忆检索」与排障用：传当前新闻文本，返回按相似度召回的历史反思 top-k。
    与 prompt 注入共用同一套索引（60s TTL），所以这里看到的召回结果就是模型看到的。
    """
    try:
        import semantic_memory as _sm
        k = max(1, min(int(k or 5), 20))
        idx = _sm.get_index()
        items = idx.search(q or "", asset=asset or "", event_type=event or "", k=k)
        return {
            "ok": True,
            "docs": idx.n_docs,
            "query": {"q": q, "asset": asset, "event": event, "k": k},
            "items": [
                {
                    "doc_id": d.get("doc_id"),
                    "source": d.get("source"),
                    "asset": d.get("asset"),
                    "event_type": d.get("event_type"),
                    "ts": d.get("ts"),
                    "similarity": d.get("similarity"),
                    "outcome": d.get("outcome"),
                    "text": _sm._snippet(d.get("text") or "", 160),
                }
                for d in items
            ],
        }
    except Exception as exc:
        return {"ok": False, "error": f"{type(exc).__name__}: {str(exc)[:160]}"}


@router.get("/api/v1/memory/stats")
def memory_stats():
    """v379 · 记忆语料规模（前端只读展示：多少条记忆、多少词）。"""
    try:
        import semantic_memory as _sm
        idx = _sm.get_index()
        by_source: Dict[str, int] = {}
        for d in idx.docs:
            by_source[d.get("source") or "?"] = by_source.get(d.get("source") or "?", 0) + 1
        return {"ok": True, "docs": idx.n_docs, "vocab": len(idx.idf), "by_source": by_source}
    except Exception as exc:
        return {"ok": False, "error": f"{type(exc).__name__}: {str(exc)[:160]}"}
