# -*- coding: utf-8 -*-
"""reasoning_review.py - P1 "推理审查"主面板后端

对一条 ai_decision 拆看 LLM 当时的"推理过程"是否完备。
不引入 severity/level/排序 - 一律平铺返回给 UI 显示。

6 维度:
  1. 8 段推理链完整性
  2. 反共识/对立观点是否出现
  3. 因果机制是否明确
  4. 跨资产传导方向是否一致
  5. 假设/前提是否明说
  6. 历史反例/历史样本是否被提及

数据缺失/不合格一项 - 全部信息性列出，不作为硬门禁。
"""
from __future__ import annotations
import json
import sqlite3
from typing import Any, Dict, List, Optional, Tuple

_ANALYSIS_STEPS: Tuple[str, ...] = (
    "总框架", "因素定位", "驱动力", "水位博弈",
    "跨资产资金", "反共识结论", "逻辑自检", "总结",
)

_COUNTER_CONSENSUS_KEYWORDS: Tuple[str, ...] = (
    "反共识", "反向", "然而", "但是", "不过", "尽管", "即便", "反之",
    "另一种", "相反", "对立", "差异化", "相反地", "反向看", "是否成立",
    "从反面", "反方观点", "反方", "反对", "反对者",
    "不利于", "不支持", "风险点", "风险提示", "警惕",
    "担忧", "顾虑", "误区", "误判", "不应", "不能", "否定",
    "但", "若不成立", "反向场景", "悲观情形", "乐观情形",
    "price-in", "priced in", "已反映", "已消化", "充分定价",
    "高估", "低估", "透支",
    "however", "but", "yet", "although", "though",
    "even though", "on the other hand", "counter", "contrary",
    "opposing", "opposite", "downside risk", "upside risk",
    "caveat", "bearish case", "bullish case", "alt view",
    "alternative view", "what if", "if wrong", "if this fails",
    "risk to view", "key risk", "main risk",
)

_ASSUMPTION_KEYWORDS: Tuple[str, ...] = (
    "假设", "前提", "假定", "如果", "若是", "若不",
    "条件为", "依赖", "依靠",
    "assuming", "assume", "provided", "given that",
    "conditional on", "on the basis", "depends on", "contingent",
)

_HISTORY_KEYWORDS: Tuple[str, ...] = (
    "历史", "过去", "类似", "上次", "曾经", "当初",
    "历史样本", "历史数据", "历史反例", "历史案例", "历史场景",
    "去年同期", "上次同期", "类似历史", "历史同期",
    "history", "historical", "previously", "last time",
    "in the past", "past data", "similar case", "analog",
)

_MECHANISM_KEYWORDS: Tuple[str, ...] = (
    "传导", "传导路径", "传导机制", "因为", "由于", "导致",
    "引发", "驱动", "驱动因素", "驱动逻辑", "作用机理",
    "transmission", "because", "due to", "lead to", "trigger",
    "mechanism", "driver", "proximate",
)


def _safe_json(raw):
    if raw is None:
        return None
    if isinstance(raw, (dict, list)):
        return raw
    if isinstance(raw, str):
        try:
            return json.loads(raw)
        except (json.JSONDecodeError, TypeError):
            return None
    return None


def _extract_reasoning(d):
    return (d.get("reasoning_path") or d.get("reasoning")
            or d.get("asset_reasoning") or "")


def _extract_factor_concepts(d):
    raw = d.get("factor_ranking")
    if not raw:
        return []
    data = _safe_json(raw)
    if not isinstance(data, list):
        return []
    out = []
    for it in data:
        if isinstance(it, dict):
            label = it.get("concept") or it.get("factor") or it.get("name")
            if isinstance(label, str) and label:
                out.append(label)
        elif isinstance(it, str) and it:
            out.append(it)
    return out


def _present_steps(reasoning):
    if not reasoning:
        return []
    return [s for s in _ANALYSIS_STEPS if s in reasoning]


def _keyword_hits(text, keywords):
    if not text:
        return []
    out = []
    for kw in keywords:
        if kw in text and kw not in out:
            out.append(kw)
    return out


def _cross_asset_direction(d):
    """检查跨资产方向一致性 - 从决策上下文 JSON (decision_context.assets) 提取。

    v170 表结构没有专门的 cross_asset_likely 列 — 跨资产信号嵌在 decision_context.assets
    里（key=资产 symbol，val=change_24h_pct）。仅作 24h pct 方向判定，不作硬门禁。
    """
    target = (d.get("target_asset") or "").upper()
    ctx = _safe_json(d.get("decision_context"))
    assets = {}
    if isinstance(ctx, dict):
        assets = ctx.get("assets") or {}
    if not isinstance(assets, dict) or not assets:
        return {"present": False, "target_asset": target,
                "items": [], "aligned_count": 0, "conflict_count": 0,
                "detail": "decision_context.assets 为空 - 未生成跨资产快照"}
    action = (d.get("suggested_action") or "").upper()
    expected_sign = 1 if action in ("BUY", "LONG") else (-1 if action in ("SELL", "SHORT") else 0)
    aligned = 0
    conflict = 0
    items = []
    for sym, info in assets.items():
        if not isinstance(info, dict):
            continue
        sym_up = sym.upper()
        if sym_up == target:
            continue
        pct = info.get("change_24h_pct")
        try:
            pct_f = float(pct) if pct is not None else None
        except (TypeError, ValueError):
            pct_f = None
        if pct_f is None or pct_f == 0:
            items.append({"symbol": sym, "pct_24h": pct_f,
                          "aligned": False, "conflicts": False, "neutral": True})
            continue
        # 简单方向匹配 - 涨跌与决策方向同向
        sign = 1 if pct_f > 0 else -1
        aligned_flag = sign == expected_sign
        conflict_flag = sign == -expected_sign and abs(pct_f) >= 1.0  # 仅显著 (>=1%) 才计冲突
        if aligned_flag:
            aligned += 1
        if conflict_flag:
            conflict += 1
        items.append({"symbol": sym, "pct_24h": pct_f,
                      "aligned": aligned_flag, "conflicts": conflict_flag,
                      "neutral": False})
    return {"present": True, "target_asset": target,
            "items": items, "aligned_count": aligned,
            "conflict_count": conflict,
            "detail": "跨资产 {} 条，方向对齐 {} 条，冲突 {} 条".format(
                len(items), aligned, conflict)}


def _to_float(v):
    try:
        return float(v) if v is not None else None
    except (TypeError, ValueError):
        return None


def _to_int(v):
    try:
        return int(v) if v is not None else None
    except (TypeError, ValueError):
        return None


def compute_review(decision):
    """对一条 ai_decision 进行推理审查。返回 6 维平铺结果 + 信息性 notes。"""
    reasoning = _extract_reasoning(decision)
    factors = _extract_factor_concepts(decision)
    steps_present = _present_steps(reasoning)
    steps_missing = [s for s in _ANALYSIS_STEPS if s not in steps_present]
    counter_kw = _keyword_hits(reasoning, _COUNTER_CONSENSUS_KEYWORDS)
    mech_kw = _keyword_hits(reasoning, _MECHANISM_KEYWORDS)
    assum_kw = _keyword_hits(reasoning, _ASSUMPTION_KEYWORDS)
    hist_kw = _keyword_hits(reasoning, _HISTORY_KEYWORDS)
    cad = _cross_asset_direction(decision)

    confidence = _to_float(decision.get("evidence_confidence"))
    pnl = _to_float(decision.get("forward_pnl"))
    is_correct_int = _to_int(decision.get("is_correct"))

    notes = []
    if not reasoning:
        notes.append("推理文本为空 - 可能未生成 reasoning 字段")
    if steps_missing:
        notes.append("8 段推理链缺 {} 段: {}".format(
            len(steps_missing), " / ".join(steps_missing)))
    if not counter_kw:
        notes.append("未出现反共识/反向思考关键词 - 可能未考虑对立面")
    if not mech_kw:
        notes.append("未出现因果机制关键词 - 可能未说明为什么")
    if not factors:
        notes.append("factor_ranking 为空 - 未量化概念等级")
    if not cad["present"]:
        notes.append("cross_asset_likely 为空 - 未说明跨资产传导")
    elif cad["conflict_count"] > 0:
        notes.append("跨资产方向冲突 {} 条 - 需重新审视传导逻辑".format(cad["conflict_count"]))
    if not assum_kw:
        notes.append("未出现假设/前提关键词 - 未明说推理的前提条件")
    if not hist_kw:
        notes.append("未出现历史/过去/类似关键词 - 未引用历史样本")
    if (confidence is not None and confidence >= 0.7
            and pnl is not None and pnl < 0 and is_correct_int == 0):
        notes.append("置信度 {:.2f} 但实现亏损 {:.4f} / 校准可能偏高".format(confidence, pnl))
    if (confidence is not None and confidence <= 0.4
            and pnl is not None and pnl > 0 and is_correct_int == 1):
        notes.append("置信度仅 {:.2f} 但实现盈利 {:.4f} / 反向校准机会".format(confidence, pnl))

    total_checks = 6
    passed = 0
    if not steps_missing:
        passed += 1
    if counter_kw:
        passed += 1
    if mech_kw:
        passed += 1
    if cad["present"]:
        passed += 1
    if assum_kw:
        passed += 1
    if hist_kw:
        passed += 1
    completion_score = round(passed / total_checks * 100, 1)

    return {
        "decision_id": int(decision.get("id") or 0),
        "asset": (decision.get("target_asset") or "").upper(),
        "action": (decision.get("suggested_action") or "").upper(),
        "reasoning_length": len(reasoning),
        "is_correct": is_correct_int,
        "forward_pnl": pnl,
        "steps": {
            "present": steps_present,
            "missing": steps_missing,
            "pct": round(len(steps_present) / len(_ANALYSIS_STEPS) * 100, 1),
        },
        "counter_consensus": {"present": bool(counter_kw), "keywords_hit": counter_kw[:8]},
        "causal_mechanism": {"present": bool(mech_kw), "keywords_hit": mech_kw[:8]},
        "factor_concepts": {"count": len(factors), "concepts": factors[:6]},
        "cross_asset_direction": cad,
        "assumption": {"present": bool(assum_kw), "keywords_hit": assum_kw[:8]},
        "historical_pattern": {"present": bool(hist_kw), "keywords_hit": hist_kw[:8]},
        "calibration": {"confidence": confidence, "pnl_realized": pnl, "is_correct": is_correct_int},
        "notes": notes,
        "completion_score": completion_score,
    }


_DECISION_COLS = (
    "id, target_asset, suggested_action, is_correct, forward_pnl, "
    "reasoning, reasoning_path, asset_reasoning, "
    "factor_ranking, decision_context, evidence_confidence, "
    "sentiment_score, event_type_code, created_at"
)


def get_review_for_decision(decision_id, conn):
    conn.row_factory = sqlite3.Row
    row = conn.execute(
        "SELECT " + _DECISION_COLS + " FROM ai_decisions WHERE id = ?",
        (int(decision_id),),
    ).fetchone()
    if not row:
        return None
    return compute_review(dict(row))


def list_recent_reviews(limit=50, days=7, only_failed=False, conn=None):
    if conn is None:
        return []
    conn.row_factory = sqlite3.Row
    limit = min(max(int(limit), 1), 200)
    days = min(max(int(days), 1), 90)
    where = ["created_at >= datetime('now', ?)"]
    params = ["-{} days".format(days)]
    if only_failed:
        where.append("(is_correct = 0 OR (forward_pnl IS NOT NULL AND forward_pnl < 0))")
    sql = ("SELECT " + _DECISION_COLS + " FROM ai_decisions WHERE "
           + " AND ".join(where) + " ORDER BY id DESC LIMIT ?")
    params.append(limit)
    rows = conn.execute(sql, params).fetchall()
    return [compute_review(dict(r)) for r in rows]


# v302+ P2 PR-16: 段位一致性快照 — 按天聚合 8 段命中率,支持 7~30 天热力图
# 设计: 一次拉所有 N 天内的决策(只用 8 段判定需要的字段),本地按 date(created_at) 分组
#   - 不复用 list_recent_reviews 是因为它返回的 dict 不含 created_at(compute_review 把它丢了)
#   - 不复用 compute_review 是因为历史快照不需要 6 维审查(只关心 8 段 in/out)
#   - 数据库负担: N=30,limit=1000 决策, ~50KB,远低于 review/list 单次 30 决策的负载
def segment_history(days=7, limit=1000, conn=None):
    """Return per-day, per-stage hit rate for the last N days.

    Output: [
        {"date": "2026-10-06", "count": 12,
         "stages": {"总框架": 1.0, "因素定位": 0.83, ..., "总结": 0.42}},
        ...
    ]
    - date 字符串用 date(created_at) 的本地日(ISO 格式)
    - count = 当天决策数(0 时 stages 全 0,UI 仍占位)
    - stages[k] = 当天该段非空比例 ∈ [0, 1]
    - days 上限 30,limit 上限 5000
    """
    if conn is None:
        return []
    days = min(max(int(days), 1), 30)
    limit = min(max(int(limit), 1), 5000)
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        "SELECT id, created_at, reasoning, reasoning_path, asset_reasoning, "
        "factor_ranking, decision_context "
        "FROM ai_decisions "
        "WHERE created_at >= datetime('now', ?) "
        "ORDER BY created_at DESC LIMIT ?",
        ("-{} days".format(days), limit),
    ).fetchall()
    # 按本地日聚合
    buckets: Dict[str, Dict[str, Any]] = {}
    for r in rows:
        created = r["created_at"] or ""
        date_str = created[:10]  # 'YYYY-MM-DD' from 'YYYY-MM-DD HH:MM:SS'
        # 严格校验:必须形如 'YYYY-MM-DD',否则整行跳过(防 created_at 损坏污染聚合)
        if (not date_str or len(date_str) != 10
                or not date_str[0:4].isdigit() or date_str[4] != "-"
                or date_str[7] != "-"):
            continue
        if date_str not in buckets:
            buckets[date_str] = {"count": 0, "hits": {k: 0 for k in _ANALYSIS_STEPS}}
        bucket = buckets[date_str]
        bucket["count"] += 1
        # 复用 _present_steps — 与 compute_review 完全一致的段位判定
        reasoning_text = _extract_reasoning(dict(r))
        present_set = set(_present_steps(reasoning_text))
        for k in _ANALYSIS_STEPS:
            if k in present_set:
                bucket["hits"][k] += 1
    # 转 list + 算 hit rate
    out = []
    for date_str in sorted(buckets.keys()):
        b = buckets[date_str]
        stages = {k: round(b["hits"][k] / b["count"], 4) if b["count"] > 0 else 0.0
                  for k in _ANALYSIS_STEPS}
        out.append({"date": date_str, "count": b["count"], "stages": stages})
    return out