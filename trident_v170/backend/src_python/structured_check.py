"""Structured Check Layer (v173+) - bypass module, does NOT modify LLM reasoning.

Responsibilities:
  check_reasoning_vs_structured_input() - after LLM returns reasoning,
    compare reasoning text mentions with structured_input events.

Output JSON in ai_decisions.structured_check:
  {
    "check_at": <unix_ts>,
    "events_in_payload": <int>,
    "events_mentioned": [<type_code>, ...],
    "events_missing": [<type_code>, ...],
    "entities_fabricated": [<str>, ...],
    "score_coverage": <float 0-1>,
    "score_fabrication": <float 0-1>,
    "verdict": "ok" | "partial" | "fabricated",
    "v174_time_anchor": { ... },    # v174 新增 - 时间结构化校验
  }

v174 新增时间结构化校验维度:
  - time_anchor_in_reasoning: bool (LLM 是否在 reasoning 中显式引用事件时间)
  - time_anchor_score: float 0-1 (信号密度:含数字时间戳 + 含时间相关词 + snap_news_delta 出现在 reasoning)
  - snap_price_in_reasoning: bool (LLM 是否引用了发布时刻价或当前价的具体数值)
  - structured_time_fields: {snapshot_has_news_ts, snapshot_has_price_at_news_ts, snapshot_has_delta_pct}
"""
from __future__ import annotations

import json
import re
import time
from typing import Any, Dict, List, Optional


# v174: 时间锚关键词 — LLM reasoning 中显式提到时间相关概念
_TIME_ANCHOR_KEYWORDS = [
    r"\d{1,2}[:\u003a]\d{2}",  # 11:30 / 03:50
    r"\d{4}-\d{1,2}-\d{1,2}",   # 2026-09-21
    r"发布[于在]",
    r"新[闻聞][时時间間]",
    r"事件[时時间間]",
    r"\d+\s*分[钟鐘]前",
    r"\d+\s*小时[前以內]?",
    r"价[格][明显显].*[动變化]",
    r"price[\-\s]?in",
    r"price[\-\s]?in[\-\s]?对[比比较]",
    r"price[\-\s]?in[\-\s]?程度",
    r"price[\-\s]?in[\-\s]?价",
    r"price[\-\s]?in[\-\s]?完成",
    r"\d+\.\d+\s*%",
    r"上涨\s*\d",
    r"下跌\s*\d",
    r"涨[跌幅]\s*\d",
    r"异动[时點]",
    r"异[动動][起時][时點]",
    r"全网[最早][时時间間]",
    r"入[库庫][时時间間]",
    r"发[布布][时時间間]",
]

_TIME_ANCHOR_COMPILED = [re.compile(p, re.IGNORECASE) for p in _TIME_ANCHOR_KEYWORDS]


def _check_time_anchor(reasoning_text, structured_payload):
    """v174: 检查 LLM reasoning 是否显式引用事件时间 + 价格。

    Returns dict with:
      - time_anchor_in_reasoning (bool)
      - time_anchor_score (float 0-1, 信号密度)
      - time_anchor_hits (list[str], 命中的关键词样本)
      - snap_price_in_reasoning (bool): reasoning 是否出现具体价格数字
      - structured_time_fields (dict): structured_input.snapshot 中时间字段是否齐全
    """
    out = {
        "time_anchor_in_reasoning": False,
        "time_anchor_score": 0.0,
        "time_anchor_hits": [],
        "snap_price_in_reasoning": False,
        "structured_time_fields": {
            "snapshot_has_news_ts": False,
            "snapshot_has_price_at_news_ts": False,
            "snapshot_has_delta_pct": False,
        },
    }
    if not reasoning_text:
        return out

    text = reasoning_text
    hits = []
    for pat in _TIME_ANCHOR_COMPILED:
        m = pat.search(text)
        if m:
            hits.append(m.group(0)[:32])  # 截断,防爆

    # structured_input 时间字段齐全度
    snap = structured_payload.get("snapshot") or []
    if snap:
        first = snap[0] or {}
        out["structured_time_fields"]["snapshot_has_news_ts"] = bool(first.get("news_ts"))
        out["structured_time_fields"]["snapshot_has_price_at_news_ts"] = bool(first.get("price_at_news_ts"))
        out["structured_time_fields"]["snapshot_has_delta_pct"] = "delta_pct_news_to_now" in first

    # 价格数字是否在 reasoning 中(形如 67000 / 3800.5)
    _price_num = re.findall(r"\b\d{3,5}(?:\.\d+)?\b", text)
    out["snap_price_in_reasoning"] = len(_price_num) >= 2  # 至少 2 个价格数字

    out["time_anchor_in_reasoning"] = len(hits) > 0
    out["time_anchor_score"] = min(1.0, len(hits) / 4.0)  # 4 个信号 = 满分
    out["time_anchor_hits"] = hits[:8]  # 上限 8 个
    return out


def _now() -> int:
    return int(time.time())


_COVERAGE_KEYWORDS = {
    "rate_hike":        ["加息", "上调", "rate hike", "hike"],
    "rate_cut":         ["降息", "下调", "rate cut", "cut"],
    "geopolitical":     ["地缘", "战争", "冲突", "局势", "geopolit"],
    "sanctions":        ["制裁", "在锡", "sanction"],
    "earnings":         ["营收", "年报", "earnings"],
    "macro_data":       ["CPI", "非农", "NFP", "GDP", "宏观数据", "macro"],
    "regulatory":       ["监管", "反型", "备案", "regulat"],
    "central_bank":     ["中央银行", "联准", "FOMC", "central bank"],
    "supply_demand":    ["供需", "产量", "缺货", "supply"],
    "natural_disaster": ["地震", "台风", "洪水", "灾害"],
    "policy":           ["政策", "发布", "出台", "policy"],
    "trade_balance":    ["贸易顺差", "贸易额", "trade"],
    "opec":             ["OPEC", "佛波", "佛罗斯"],
    "fed":              ["美联储", "联邦储备", "Fed", "FOMC"],
    "ecb":              ["欧中行", "ECB"],
    "pboc":             ["人行", "PBOC", "人民银行"],
    "rba":              ["澳洲央行", "RBA"],
    "boj":              ["日本银行", "BOJ"],
}

_ASSET_KEYWORDS = ["BTC", "ETH", "XAU", "WTI", "能源", "黄金", "油", "加密", "油价"]


def _compile_patterns():
    compiled = {}
    for code, pats in _COVERAGE_KEYWORDS.items():
        compiled[code] = [re.compile(p, re.IGNORECASE) for p in pats]
    return compiled


_COMPILED = _compile_patterns()


def _check_coverage(reasoning_text, event_types):
    """Check which payload event types are mentioned in reasoning."""
    if not reasoning_text or not event_types:
        return [], list(event_types or [])

    mentioned, missing = [], []
    for code in event_types:
        patterns = _COMPILED.get(code) or [re.compile(re.escape(code), re.IGNORECASE)]
        hit = any(p.search(reasoning_text) for p in patterns)
        (mentioned if hit else missing).append(code)
    return mentioned, missing


def _check_fabrication(reasoning_text, structured_payload):
    """Detect entities claimed in reasoning but absent in payload."""
    if not reasoning_text:
        return []

    payload_text = json.dumps(structured_payload, ensure_ascii=False)
    fabricated = []
    seen = set()

    for asset in _ASSET_KEYWORDS:
        if asset in seen:
            continue
        if asset in reasoning_text and asset not in payload_text:
            if len(asset) <= 1:
                continue
            fabricated.append(asset)
            seen.add(asset)

    return fabricated


def _verdict_of(coverage_score, fabrication_score, n_events):
    """Decide overall verdict."""
    if n_events == 0:
        return "ok"
    if fabrication_score > 0.3:
        return "fabricated"
    if coverage_score >= 0.7:
        return "ok"
    return "partial"


def check_reasoning_vs_structured_input(reasoning_text, structured_payload):
    """Main entry. Pure function. Returns dict ready for ai_decisions.structured_check."""
    out = {
        "check_at": _now(),
        "events_in_payload": 0,
        "events_mentioned": [],
        "events_missing": [],
        "entities_fabricated": [],
        "score_coverage": 0.0,
        "score_fabrication": 0.0,
        "verdict": "ok",
        "v174_time_anchor": _check_time_anchor("", {}),  # v174: 默认空,后面会覆盖
    }
    if not isinstance(structured_payload, dict):
        return out

    try:
        events = structured_payload.get("events") or []
        event_types = []
        for e in events:
            tc = (e or {}).get("type_code") or ""
            if tc and tc not in event_types:
                event_types.append(tc)

        n = len(event_types)
        out["events_in_payload"] = n

        # v174: 时间结构化校验 — 即使 reasoning_text 为空或 n=0 也跑一次,
        # 这样 snapshot 的时间字段齐全度也能被记录(便于前端 dashboard 监控)
        try:
            out["v174_time_anchor"] = _check_time_anchor(reasoning_text or "", structured_payload)
        except Exception:
            pass

        if not reasoning_text or n == 0:
            out["events_missing"] = event_types
            out["verdict"] = "partial" if n > 0 else "ok"
            return out

        mentioned, missing = _check_coverage(reasoning_text, event_types)
        fabricated = _check_fabrication(reasoning_text, structured_payload)

        coverage_score = (len(mentioned) / n) if n else 0.0
        fabrication_score = (len(fabricated) / max(n, 1))

        out["events_mentioned"] = mentioned
        out["events_missing"] = missing
        out["entities_fabricated"] = fabricated
        out["score_coverage"] = round(coverage_score, 4)
        out["score_fabrication"] = round(fabrication_score, 4)
        out["verdict"] = _verdict_of(coverage_score, fabrication_score, n)

        # v174: 若时间锚 score 极低(< 0.25)且有真实事件,verdict 降级为 partial
        # 这让前端 dashboard 能筛出"reasoning 完全没引用事件时间"的样本
        try:
            ta = out["v174_time_anchor"]
            if n > 0 and not ta.get("time_anchor_in_reasoning") and ta.get("structured_time_fields", {}).get("snapshot_has_news_ts"):
                # 已有时间锚数据但 LLM reasoning 完全没引用 → 降级
                out["verdict"] = "partial"
                out["v174_time_anchor"]["anchor_missing_reason"] = "structured_input 提供 news_ts/price_at_news_ts 但 LLM reasoning 未显式引用"
        except Exception:
            pass
    except Exception:
        pass

    return out


def serialize_check(check_dict):
    """Convert check dict to compact JSON string for DB column."""
    return json.dumps(check_dict, ensure_ascii=False, default=str)
