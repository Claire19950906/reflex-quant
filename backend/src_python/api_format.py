"""纯函数：行 → 前端事件对象的格式化与清洗。

2026-11-20: 从 api_server.py 抽出（只移不改）。这组函数原本混在 9 千行的大文件里，
无法单独测试；抽出后可在 tests/test_api_format.py 里直接断言，不用起 HTTP 服务。
"""

import json
import re

import event_status
from typing import Any, Dict

from common.time_utils import format_time as _format_time

def _safe(row, key, default=None):
    """sqlite3.Row safe access — no .get() method.

    v321 性能修复：原先写成 `row[key] if key in row.keys() else default`，
    但 sqlite3.Row.keys() **每次调用都会新建一个完整列名 list**。
    _row_to_event 对每行调用 _safe 约 78 次 → 4380 行 = 34 万次调用
    → 每次都重建一次列名列表 → 光这一项就吃掉 ~18s 中的绝大部分，
    导致 /api/events 缓存每 60s 过期后下一个请求阻塞十几秒（看起来像服务挂了）。

    改成 try/except：sqlite3.Row 缺列抛 IndexError、dict 缺键抛 KeyError，
    两者语义与原来的 `key in row.keys()` 完全等价，但每行查询降为 O(1)。
    """
    try:
        return row[key]
    except (IndexError, KeyError):
        return default


def _normalize_analysis_status(status: Any, has_decision: bool, is_noise: bool = False,
                                gate_pending: bool = False) -> str:
    """事件分析状态归一 —— 唯一实现见 event_status.resolve_analysis_status()。

    v317 fix：原先这里是 api_server 私有一份推导逻辑，bridge_server 另有一份，
    两边对同一行数据能给出不同状态（实测 496 行 raw_news=FAILED / news_events=DONE）。
    现在两个 API 都调用 event_status 这个唯一真相源。

    参数 is_noise：噪声行派生为 SKIPPED（噪声不会被 AI 处理，不该显示"待 AI 分析"）。

    v321 fix：**is_noise 必须由调用方显式传入**。原先本函数有默认值 False，
    而 _row_to_event / _fetch_event_snapshot 两处调用都漏传 → 1550 条噪声行
    在 8000 端口显示"待 AI 分析"，在 8100 端口显示"噪声已跳过"（bridge 传了）。
    这就是"同一批数据两个端口状态不一致"的残留。

    参数 gate_pending：门卫(LLM)未判定（gate_unavailable=1）→ GATE_PENDING。
    这类行不会被主分析认领，等 replay_deferred_gate.py 回放，绝不该显示"待 AI 分析"。
    """
    return event_status.resolve_analysis_status(
        status, has_decision=has_decision, is_noise=is_noise,
        gate_pending=gate_pending,
    )


def _parse_peer_reviews(raw: Any) -> list:
    """解析 peer_reviews（验证模型结果 JSON 字符串）→ 数组；异常返回 []。"""
    if raw is None:
        return []
    if isinstance(raw, list):
        return raw
    if isinstance(raw, str) and raw.strip():
        try:
            parsed = json.loads(raw)
            return parsed if isinstance(parsed, list) else []
        except (json.JSONDecodeError, TypeError):
            return []
    return []


# 历史遗留前缀清洗：早期"HOLD 强分改写"机制给 reasoning 加的"由观望转看多/看空"标注
# （现在 AI 观望就观望，不再转方向 → 展示层去掉旧前缀，不污染历史库）
_HOLD_OVERRIDE_RE = re.compile(
    r"^(?:评分较强\([^)]*\)，)?由观望转(?:看多|看空)[；;]?\s*"
)


def _strip_hold_override(text: Any) -> str:
    """去掉 reasoning 中旧的"由观望转…"前缀（遗留数据清洗，仅展示层）。"""
    if not text:
        return ""
    return _HOLD_OVERRIDE_RE.sub("", str(text))


def gate_reason_fallback(gate_reason: str) -> str:
    """历史信号未记录 no_trade_reason 时，用闸门拒绝原因兜底。

    '证据充分，允许输出方向性结论' 表示闸门通过（不是拒绝原因）→ 返回空；
    其它非空闸门结论（置信度不足/方向矛盾等）都是"未建仓"的说明。
    """
    reason = (gate_reason or "").strip()
    if not reason or reason.startswith("证据充分"):
        return ""
    return reason


def _row_to_event(row) -> Dict[str, Any]:
    """Map a raw-news row with an optional decision to the frontend ApiEvent shape."""
    decision_id = _safe(row, "decision_id")
    analysis_status = _normalize_analysis_status(
        _safe(row, "analysis_status"),
        decision_id is not None,
        is_noise=bool(_safe(row, "is_noise", 0) or 0),
        gate_pending=int(_safe(row, "gate_unavailable", 0) or 0) == 1,
    )
    reason = _strip_hold_override(_safe(row, "reason")) or ""
    reason = re.sub(r"^\[.*?\]\s*", "", reason)
    if decision_id is None:
        if analysis_status == "FAILED":
            reason = "AI 分析失败"
        elif analysis_status == "DONE":
            reason = "历史记录已重置删除（重置模拟盘时清空交易）"
        else:
            reason = "待 AI 分析"
    market = (_safe(row, "market_category") or "").upper()
    # v1 重构：从 market_classifier 取生产白名单（无 OTHER）。
    # 任何不在白名单的行（含历史脏数据 'OTHER' / 空串）→ 标记 unclassified=1，
    # 前端根据这个 flag 渲染「待复核」徽章，**不**用模糊分类蒙混。
    from market.market_classifier import (
        MARKET_CATEGORIES_PROD,
        is_debug_other_allowed,
    )
    if market == "OTHER" and not is_debug_other_allowed():
        market = ""
        unclassified = True
    elif market and market not in MARKET_CATEGORIES_PROD:
        market = ""
        unclassified = True
    else:
        unclassified = not bool(market)  # 空串也视为待复核
    # ── Parse extra_models_consensus JSON → individual model fields ──
    raw_consensus = _safe(row, "extra_models_consensus") or ""
    if isinstance(raw_consensus, str):
        raw_consensus = raw_consensus.strip()
    try:
        consensus = json.loads(raw_consensus) if raw_consensus else {}
    except (json.JSONDecodeError, TypeError):
        consensus = {}
    if not isinstance(consensus, dict):
        consensus = {}

    news_id = _safe(row, "news_id", _safe(row, "id"))
    return {
        "id": news_id,
        "news_id": news_id,
        "decision_id": decision_id,
        "analysis_status": analysis_status,
        "paper_trading_run_id": _safe(row, "paper_trading_run_id"),
        "strategy_id": _safe(row, "strategy_id"),
        "strategy_version_id": _safe(row, "strategy_version_id"),
        "agent_model_id": _safe(row, "agent_model_id"),
        "evidence_confidence": _safe(row, "evidence_confidence"),
        "evidence_action": (_safe(row, "evidence_action") or "HOLD").upper(),
        "trade_gate_reason": _safe(row, "trade_gate_reason") or "",
        # 未建仓原因：优先用 ai_worker 记录的；历史信号没记录时回退到闸门拒绝原因
        "no_trade_reason": _safe(row, "no_trade_reason") or (
            gate_reason_fallback(_safe(row, "trade_gate_reason") or "")
        ),
        "timestamp": _format_time(_safe(row, "timestamp")),
        "published_at": _format_time(_safe(row, "published_at")) if _safe(row, "published_at") else "",
        # 全网原始溯源时间：同内容指纹在全网最早被采集的时刻（跨源去重后拉平到全局最早）
        "first_seen_ts": _format_time(_safe(row, "first_seen_ts")) if _safe(row, "first_seen_ts") else "",
        # 全网溯源：AI 自由回答的自然语言文本（无确切信息为"无法确认"或空串）
        "net_origin_info": (_safe(row, "net_origin_info") or "").strip(),
        # 全网溯源：AI 直接给出的结构化最早时间（5 层校验通过才落库；AMBIGUOUS/空串时前端不渲染）
        "cross_net_first_seen_ts": _format_time(_safe(row, "cross_net_first_seen_ts")) if _safe(row, "cross_net_first_seen_ts") else "",
        # 全网溯源：cross_net_first_seen_ts 的依据来源（"XX社首发"/"候选1:…/候选2:…"）
        "cross_net_source": (_safe(row, "cross_net_source") or "").strip(),
        "ai_time": _format_time(_safe(row, "created_at")) if decision_id is not None else "——",
        "source": _safe(row, "source") or "FinancialJuice",
        "news_text": re.sub(r'\[hash:[a-fA-F0-9]+\]\s*', '', (_safe(row, "news_text") or "")[:200]),
        "action": (_safe(row, "action") or "HOLD").upper(),
        "score": round(_safe(row, "score"), 2) if _safe(row, "score") is not None else 0.0,
        "reason": reason[:80],
        "market_category": market,
        # v1 重构：未归类标记（True 时前端展示「待复核」徽章，禁止作为正常信号处理）
        "unclassified": unclassified,
        "target_asset": (_safe(row, "target_asset") or "NONE").upper(),
        "parent_id": _safe(row, "parent_id"),
        "child_count": _safe(row, "child_count") or 0,
        # 完整推理链（八段+因果链，多品种分行后最长 ~560 字实测）——保留足够长度，
        # 否则前端详情后半段（跨资产/反共识/逻辑自检）被切掉显示"没内容"
        "reasoning_path": (_safe(row, "reasoning_path") or "")[:1500],
        # 品种相关推理逻辑（AI 逐品种解释"为什么该新闻关联此品种"；仅主行有值）
        "asset_reasoning": (_safe(row, "asset_reasoning") or "")[:500],
        "vip_tag": _safe(row, "vip_tag") or "",
        "entry_price": _safe(row, "entry_price"),
        "exit_price": _safe(row, "exit_price"),
        # 分级止盈：部分平仓价 / 已平比例 / 该部分盈亏%（剩余仓位按最终 exit_price 结算）
        "partial_exit_price": _safe(row, "partial_exit_price"),
        "partial_exit_ratio": _safe(row, "partial_exit_ratio") or 0.0,
        "partial_pnl_pct": _safe(row, "partial_pnl_pct") or 0.0,
        # 影响评估（0-10）：舆情热度 / 市场影响力 / 对该金融产品影响程度
        "heat_score": _safe(row, "heat_score"),
        "impact_score": _safe(row, "impact_score"),
        "asset_impact": _safe(row, "asset_impact"),
        # 多模型交叉验证：验证模型独立分析结果（[{model_label, action, score, reasoning}]）
        "peer_reviews": _parse_peer_reviews(_safe(row, "peer_reviews")),
        # ── 需求5 新增子模块输出：关注度得分 / 弹性窗参数 / 原始数据戳 / 硬数据截图 ──
        "attention_score": _safe(row, "attention_score"),
        "elasticity_window": (_safe(row, "elasticity_window") or ""),
        "data_anchor_ts": (_safe(row, "data_anchor_ts") or ""),
        "hard_data_evidence": (_safe(row, "hard_data_evidence") or ""),
        "max_price": _safe(row, "max_price"),
        "min_price": _safe(row, "min_price"),
        "max_price_time": _safe(row, "max_price_time") or 0,
        "min_price_time": _safe(row, "min_price_time") or 0,
        "entry_time": _safe(row, "entry_time") or "",
        "is_correct": _safe(row, "is_correct") or "",
        "settled": _safe(row, "settled") or 0,
        "doubao_action": _safe(row, "doubao_action") or "HOLD",
        "doubao_reasoning": _safe(row, "doubao_reasoning") or "",
        # 修复：A2 — ai_decisions 表里没有 deepseek_action 列，统一从 extra_models_consensus JSON 取真实值。
        "deepseek_action": (consensus.get("DeepSeek", {}).get("action") if isinstance(consensus.get("DeepSeek"), dict) else None) or "HOLD",
        "deepseek_reasoning": (consensus.get("DeepSeek", {}).get("reasoning") if isinstance(consensus.get("DeepSeek"), dict) else None) or "",
        "gemini_action": (consensus.get("Gemini", {}).get("action") if isinstance(consensus.get("Gemini"), dict) else None) or "HOLD",
        "gemini_reasoning": (consensus.get("Gemini", {}).get("reasoning") if isinstance(consensus.get("Gemini"), dict) else None) or "",
        "grok_action": (consensus.get("Grok", {}).get("action") if isinstance(consensus.get("Grok"), dict) else None) or "HOLD",
        "grok_reasoning": (consensus.get("Grok", {}).get("reasoning") if isinstance(consensus.get("Grok"), dict) else None) or "",
        # ── Phase 0: 元数据字段 ──
        "prediction_type": _safe(row, "prediction_type") or "continuation",
        "event_phase": _safe(row, "event_phase") or "mid",
        "event_strength": _safe(row, "event_strength") or "medium",
        "market_confirmation": _safe(row, "market_confirmation") or "unknown",
        "direct_catalyst": _safe(row, "direct_catalyst") or 0,
        "expected_horizon": _safe(row, "expected_horizon") or "1-3d",
        "invalidation_condition": _safe(row, "invalidation_condition") or "",
        "decision_context": _safe(row, "decision_context") or "{}",
        # ── Phase 0.5: 结果追踪指标 ──
        "mfe_pct": _safe(row, "mfe_pct"),
        "mae_pct": _safe(row, "mae_pct"),
        "forward_pnl": _safe(row, "forward_pnl"),
        "mfe_time_mins": _safe(row, "mfe_time_mins"),
        "chatgpt_action": (consensus.get("ChatGPT", {}).get("action") if isinstance(consensus.get("ChatGPT"), dict) else None) or "HOLD",
        "chatgpt_reasoning": (consensus.get("ChatGPT", {}).get("reasoning") if isinstance(consensus.get("ChatGPT"), dict) else None) or "",
        "cluster_size": _safe(row, "cluster_size") or 1,
        # ── v172 增量：事件结构化输出层（让前端能看到机器内部的结构化思考结果）──
        # event_struct: 规则确定性抽出的事件原子列表 JSON（event_struct.extract_events）
        "event_struct": _safe(row, "event_struct") or "",
        # structured_input: LLM 调用前拼好的 6 字段 JSON payload（meta/events/snapshot/relations/baseline/text_summary）
        "structured_input": _safe(row, "structured_input") or "",
        # structured_check: LLM 输出 reasoning 与 structured_input 的反向引用比对
        #                   JSON（verdict/mentioned/missing/fabricated/check_at 等）
        "structured_check": _safe(row, "structured_check") or "",
        # event_type_code: v174 强制字段 — 从 dim_event_type 25 个 code 中选 1
        "event_type_code": _safe(row, "event_type_code") or "",
        # snapshot_proximity: v174 强制字段 — reasoning 引用的快照距事件时间分钟差
        #                     文本："5"/"30"/"N/A"，前端用 TZ 渲染时用
        "snapshot_proximity": _safe(row, "snapshot_proximity") or "",
    }
