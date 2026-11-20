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
    # v497 新增：库里真实存在、原来完全没有词表的族
    "ipo_listing":      ["IPO", "上市", "首次公开", "挂牌", "新股"],
    "supply_chain":     ["供应链", "断供", "物流", "缺货", "交货"],
    "macro_speech":     ["讲话", "发言", "演讲", "表态", "记者会"],
    "trade_tariff":     ["关税", "贸易战", "加征", "tariff"],
    "merger_acquisition": ["并购", "收购", "合并", "merger", "acquisition"],
    "hack_attack":      ["黑客", "被盗", "攻击", "漏洞", "exploit"],
    "chip_capacity":    ["产能", "晶圆", "制程", "封测", "chip"],
    "technology_breakthrough": ["突破", "技术迭代", "新架构", "量产"],
    "crypto_etf":       ["ETF", "现货ETF", "基金申购", "被动资金"],
    "ai_capex":         ["资本开支", "CAPEX", "算力投入", "扩建"],
    "credit_stress":    ["信用", "违约", "利差", "兑付", "债务风险"],
}

# v497: 库里 event_type_code 是**细分码**（geopolitical_conflict / earnings_release /
# ipo_listing …），而关键词表用的是**族名**（geopolitical / earnings）。原实现
# `_COMPILED.get(code)` 查不到就直接用 re.escape(code) 去匹配 reasoning —— 中文
# reasoning 里永远不可能出现 "geopolitical_conflict" 这个英文码，于是
# score_coverage 恒为 0、verdict 恒为 'partial'，飞轮收到的全是假阳性。
# 这里补一张"细分码 → 族名"的解析层，族名找不到再退回字面码。
_CODE_FAMILY = {
    "geopolitical_conflict": "geopolitical",
    "geopolitical_easing": "geopolitical",
    "earnings_release": "earnings",
    "rate_hike": "rate_hike",
    "rate_cut": "rate_cut",
    "cpi": "macro_data",
    "cpi_release": "macro_data",
    "nfp": "macro_data",
    "macro_speech": "macro_speech",
    "regulation_policy": "regulatory",
    "supply_chain": "supply_chain",
    "natural_disaster": "natural_disaster",
    "trade_tariff": "trade_tariff",
    "merger_acquisition": "merger_acquisition",
    "ipo_listing": "ipo_listing",
    "hack_attack": "hack_attack",
    "opec_cut": "opec",
    "opec_increase": "opec",
    "oil_inventory": "supply_demand",
    "chip_capacity": "chip_capacity",
    "technology_breakthrough": "technology_breakthrough",
    "crypto_etf": "crypto_etf",
    "ai_capex": "ai_capex",
    "central_bank_gold": "central_bank",
    "cb_official": "central_bank",
    "credit_stress": "credit_stress",
}


def _patterns_for(code: str):
    """细分码 → 关键词族 → 编译后的正则列表；实在没有就退回字面匹配。"""
    if not code:
        return []
    if code in _COMPILED:
        return _COMPILED[code]
    fam = _CODE_FAMILY.get(code) or code.split("_")[0]
    if fam in _COMPILED:
        return _COMPILED[fam]
    return [re.compile(re.escape(code), re.IGNORECASE)]


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
        patterns = _patterns_for(code)
        hit = any(p.search(reasoning_text) for p in patterns)
        (mentioned if hit else missing).append(code)
    return mentioned, missing


def _check_fabrication(reasoning_text, structured_payload):
    """Detect entities claimed in reasoning but absent in payload.

    v497 修正：原实现会拿 reasoning 里的资产词（黄金/原油/BTC…）去和 payload 全文
    比 —— 只要 payload 里没逐字出现"黄金"两个字就记一次编造。可 payload 里经常
    只有 events 和快照字段，根本没有资产词表，于是"提到黄金"直接算编造，
    再除以事件数（通常 1）得出 3~5 的 fabrication_score → 一律 fabricated。
    这是**没有可比基准时给出结论**，比不给结论更糟。

    新规则：只有 payload 真的带了资产上下文（snapshot / assets / asset_prices）
    才判定编造；否则返回空，表示"这一项本次无法评估"。
    """
    if not reasoning_text or not isinstance(structured_payload, dict):
        return []
    if not any(k in structured_payload for k in ("snapshot", "assets", "asset_prices")):
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
            out["verdict_reason"] = ("no_reasoning" if not reasoning_text
                                    else "no_events_in_payload")
            return out


        mentioned, missing = _check_coverage(reasoning_text, event_types)
        fabricated = _check_fabrication(reasoning_text, structured_payload)

        coverage_score = (len(mentioned) / n) if n else 0.0
        # 一个事件最多算"全错"：不 clamp 的话 5 个资产词 × 1 个事件 = 500% 编造率
        fabrication_score = min(1.0, len(fabricated) / max(n, 1))

        out["events_mentioned"] = mentioned
        out["events_missing"] = missing
        out["entities_fabricated"] = fabricated
        out["score_coverage"] = round(coverage_score, 4)
        out["score_fabrication"] = round(fabrication_score, 4)
        # v497：**没有主张 ≠ 主张不实**。降级/共识不足的路径只写一句"共识不足故
        # 不给方向单"，既没引用任何事件类型、也没给任何数字；对这种 reasoning 判
        # partial 会让它全部流进真实性飞轮，把飞轮喂成"LLM 天天造数据"的假象。
        # 判据不是字数，而是"有没有可被证伪的内容"：既没命中事件类型、又没有
        # 任何数字，就记 ok + no_event_claim，不进飞轮。
        # 只看"像市场主张"的数字：三位以上整数（价格）、小数、百分比。
        # 投票计数（1/1、2/3）不算主张，否则降级路径永远判 partial。
        if not mentioned and not re.search(r"\d{3,}|\d+\.\d+|%", reasoning_text):
            out["events_missing"] = event_types
            out["verdict"] = "ok"
            out["verdict_reason"] = "no_event_claim"
            out["score_coverage"] = round(coverage_score, 4)
            out["score_fabrication"] = round(fabrication_score, 4)
            return out

        out["verdict"] = _verdict_of(coverage_score, fabrication_score, n)
        out["verdict_reason"] = (
            "fabrication" if out["verdict"] == "fabricated"
            else ("low_coverage" if out["verdict"] == "partial" else "covered"))

        # v174: 若时间锚 score 极低(< 0.25)且有真实事件,verdict 降级为 partial
        # 这让前端 dashboard 能筛出"reasoning 完全没引用事件时间"的样本
        try:
            ta = out["v174_time_anchor"]
            if n > 0 and not ta.get("time_anchor_in_reasoning") and ta.get("structured_time_fields", {}).get("snapshot_has_news_ts"):
                # 已有时间锚数据但 LLM reasoning 完全没引用 → 降级
                out["verdict"] = "partial"
                out["verdict_reason"] = "time_anchor_missing"
                out["v174_time_anchor"]["anchor_missing_reason"] = "structured_input 提供 news_ts/price_at_news_ts 但 LLM reasoning 未显式引用"
        except Exception:
            pass
    except Exception:
        pass

    return out


def serialize_check(check_dict):
    """Convert check dict to compact JSON string for DB column."""
    return json.dumps(check_dict, ensure_ascii=False, default=str)
