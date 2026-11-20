# -*- coding: utf-8 -*-
"""信号可执行卡片端点（v422，只读）。

为什么单独开：决策表里已经有方向性结论（实测近 3 天 BUY 10 / SELL 18），
但"能不能做、做多少、错了在哪停"没有任何接口回答。这里只补这一层，
不重新推理、不改任何决策。

口径：
  - 方向取 ai_decisions 里 `suggested_action / doubao_action / evidence_action /
    original_action` 任一为 BUY/SELL 的行（四个字段口径不同，会一并回显）。
  - 价格/波动全部来自 `kline_bars`（只读）；K 线不足时卡片明确标 estimated。
  - 仓位是**风险平价**：单笔风险占总权益 risk_pct%（默认 1），
    仓位% = risk_pct / 止损距离%。不是"建议满仓"，也不是投资建议。
"""
from __future__ import annotations

import sqlite3
import time
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Query

import db
import signal_cards

router = APIRouter()

_ACTION_FIELDS = ("suggested_action", "doubao_action", "evidence_action", "original_action")
_DIRECTIONS = ("BUY", "SELL", "LONG", "SHORT")
# K 线优先级：日线定结构，退到 4h/1h 时自动标 stale 阈值更紧
_TF_ORDER = ("1d", "4h", "1h", "15m")

# 聚合标的没有自己的 K 线（实测库里只有 26 个 symbol），用可交易的代理指数代替，
# 并在卡片上标 proxy=True，避免让人以为 SPX 的价格就是"美股"的价格。
_PROXY = {
    "US_STOCK": "SPX",
    "HK_STOCK": "TENCENT",
}


def _ro_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(f"file:{db.get_db_path()}?mode=ro", uri=True, timeout=30.0)
    conn.row_factory = sqlite3.Row
    return conn


def _pick_action(row: sqlite3.Row) -> Optional[tuple]:
    """返回 (方向, 来源字段)。优先级：doubao（终审）> suggested > evidence > original。"""
    for field in ("doubao_action", "suggested_action", "evidence_action", "original_action"):
        v = str(row[field] or "").strip().upper()
        if v in _DIRECTIONS:
            return (("BUY" if v in ("BUY", "LONG") else "SELL"), field)
    return None


def _load_bars(conn: sqlite3.Connection, symbol: str, timeframe: str, limit: int = 60) -> List[tuple]:
    cur = conn.execute(
        """
        SELECT ts, high, low, close FROM kline_bars
        WHERE symbol = ? AND timeframe = ?
        ORDER BY ts DESC LIMIT ?
        """,
        (symbol, timeframe, limit),
    )
    rows = cur.fetchall()
    cur.close()
    # 返回升序，兼容毫秒/秒（signal_cards 内部会归一）
    return [(r[0], r[1], r[2], r[3]) for r in rows]


def _bars_for(conn: sqlite3.Connection, symbol: str) -> tuple:
    for tf in _TF_ORDER:
        bars = _load_bars(conn, symbol, tf)
        if len(bars) >= 5:
            return bars, tf
    return [], _TF_ORDER[0]


@router.get("/api/signals/actionable")
def signals_actionable(
    days: int = Query(3, ge=1, le=30),
    limit: int = Query(30, ge=1, le=200),
    risk_pct: float = Query(1.0, ge=0.1, le=10.0),
    atr_mult: float = Query(1.5, ge=0.5, le=5.0),
):
    """方向性决策 → 可执行卡片（止损 / 仓位 / 目标 / 失效条件）。"""
    since = int(time.time()) - days * 86400
    now_ts = int(time.time())
    conn = _ro_conn()
    try:
        cur = conn.execute(
            """
            SELECT d.id, d.ts, d.target_asset, d.news_id,
                   d.suggested_action, d.doubao_action, d.evidence_action, d.original_action,
                   d.evidence_confidence, d.trade_gate_reason, d.analysis_degraded_reason,
                   d.reasoning, d.structured_check,
                   COALESCE(n.content, '') AS head, COALESCE(n.source, '') AS source
            FROM ai_decisions d
            LEFT JOIN raw_news n ON n.id = d.news_id
            WHERE d.ts >= ?
              AND COALESCE(d.decision_kind, '') <> 'routing_miss'
              AND UPPER(COALESCE(d.target_asset, 'NONE')) NOT IN ('', 'NONE')
            ORDER BY d.ts DESC
            LIMIT 4000
            """,
            (since,),
        )
        rows = cur.fetchall()
        cur.close()

        directional = []
        for r in rows:
            picked = _pick_action(r)
            if not picked:
                continue
            directional.append((r, picked))

        by_asset_total: Dict[str, int] = {}
        for r, _p in directional:
            a = str(r["target_asset"] or "").upper()
            by_asset_total[a] = by_asset_total.get(a, 0) + 1

        cards: List[Dict[str, Any]] = []
        for r, (direction, field) in directional[: limit * 5]:
            asset = str(r["target_asset"] or "").upper()
            proxy = _PROXY.get(asset)
            bars, tf = _bars_for(conn, proxy or asset)
            decision = {
                "decision_id": r["id"],
                "ts": r["ts"],
                "at": time.strftime("%Y-%m-%d %H:%M", time.localtime(int(r["ts"] or 0))),
                "asset": asset,
                "action": direction,
                "action_field": field,
                "actions_raw": {k: (r[k] or "") for k in _ACTION_FIELDS},
                "confidence": r["evidence_confidence"],
                "gate_reason": r["trade_gate_reason"] or "",
                "degraded": r["analysis_degraded_reason"] or "",
                "news_id": r["news_id"],
                "news_source": r["source"] or "",
                "news_head": (r["head"] or "").strip().replace("\n", " ")[:160],
                "why": (r["reasoning"] or "").strip().replace("\n", " ")[:300],
            }
            card = signal_cards.build_card(decision, bars, tf=tf,
                                           risk_pct=risk_pct, atr_mult=atr_mult,
                                           now_ts=now_ts)
            if proxy:
                card["proxy"] = proxy
                if card["market"].get("bars"):
                    card["market"]["source"] = "kline_bars:%s（代理 %s）" % (tf, proxy)
            cards.append(card)
    finally:
        conn.close()

    # 能执行的排前面，其次按决策时间新→旧；算不出的沉到最后但**不隐藏**
    cards.sort(key=lambda c: (0 if c["execute_ready"] else 1, -(int(c["ts"] or 0))))

    # 同一品种同一方向的多条决策（实测 ETH 22:34 与 23:21 两条），指标完全一样。
    # 保留最新一条，其余只记个数 —— 否则页面会被"同一张卡"刷屏。
    collapsed: Dict[tuple, Dict[str, Any]] = {}
    deduped: List[Dict[str, Any]] = []
    for c in cards:
        key = (c["asset"], c["action"])
        if key in collapsed:
            collapsed[key]["same_direction_count"] = int(
                collapsed[key].get("same_direction_count", 1)) + 1
            continue
        c["same_direction_count"] = 1
        collapsed[key] = c
        deduped.append(c)
    dup_count = sum(int(c["same_direction_count"]) - 1 for c in deduped)
    cards = deduped
    ready = [c for c in cards if c["execute_ready"]]
    return {
        "ok": True,
        "data": {
            "window_days": days,
            "directional_total": len(directional),
            "returned": len(cards[:limit]),
            "ready": len(ready),
            "not_ready": len(cards) - len(ready),
            "duplicates_collapsed": dup_count,
            "by_asset": dict(sorted(by_asset_total.items(), key=lambda kv: -kv[1])[:15]),
            "params": {"risk_pct": risk_pct, "atr_mult": atr_mult},
            "cards": cards[:limit],
            "generated_at": now_ts,
            "disclaimer": (
                "止损/仓位是按 ATR 与风险平价算出来的**机械口径**，用于把结论落到可执行的数字；"
                "不构成投资建议。estimated=true 表示 K 线不足、用了 2% 兜底。"
            ),
        },
    }
