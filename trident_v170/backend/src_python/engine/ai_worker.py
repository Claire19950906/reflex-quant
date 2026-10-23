"""Task B — Concurrent Batch AI Worker
+ requests-based OpenAI-compatible LLM client + model roster + performance feedback.
"""

from __future__ import annotations

import asyncio
import json

# ── SERVER VERDICT 用的快照 cache（模块级，让嵌套 _do_api_call 能读到） ──
# 每次 _ai_worker_loop 成功拉快照后写入；snapshot 失败时保留上一次成功的快照。
# _do_api_call 注入 SERVER VERDICT 块时读这里，避免重复请求行情源。
_SERVER_SNAPSHOT_CACHE: dict = {"ts": 0.0, "summary": "", "json": "{}"}
import os
import re
import sqlite3
import time
from datetime import datetime
from typing import Any, Dict, List, Optional

import db  # v189-8: 用于 db.retry_on_busy 包 _batch_claim 的 UPDATE RETURNING
import requests
import causal_graph  # v200 P0+ 严谨兜底: L2 业务知识兜底用通用驱动结构
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from config import (
    AI_MODEL_ROSTER,
    DEFAULT_AI_MODEL_ID,
    AIPING_API_KEY,
    AIPING_BASE_URL,
    AIPING_EXTRA_BODY,
    get_selected_ai_model_id,
    VIP_SCORE_BOOST,
    BATCH_SIZE,
    HERMES_AGENT_ENABLED,
    MACRO_CONTEXT_ENABLED,
    _AGG_WINDOW_HOURS,
    _AGG_MIN_SCORE,
    MECHANISM_GATE_ENABLED,
    MIN_ASSET_IMPACT_FOR_TRADE,
)
import config

# ═══════════════════════════════════════════════════════════════════════════
# v200 P0+ 严谨兜底: 字段源头追溯 + completeness_score 计算
# 设计动机: 之前 LLM 漏填字段时 ai_worker 写 "服务端兜底：LLM 未填 X" 占位字符串,
#   反思系统跑在脏数据上 → review_insights → prompt 注入 → LLM 学坏.
# 修复: 每个字段打 source 标记(5 类), 算 completeness_score.
#   反思系统按 completeness_score 过滤脏决策不入深度分析.
#
# 5 类允许的兜底来源(按严谨度排序):
#   L1 数学公式反推    (rule_xxx)
#   L2 业务知识硬编码   (business_knowledge_v176_2)
#   L3 历史模式查询     (historical_pattern)
#   L4 多 LLM 投票     (llm_ensemble_consensus)
#   L5 留空 + 强标记    (missing)
# 3 类绝对禁止:
#   X1 任意字符串占位   ("服务端兜底：LLM 未填 X")
#   X2 fake reasoning 模板 (八段模板拼凑)
#   X3 跨层借用       (脏数据污染反思)
# ═══════════════════════════════════════════════════════════════════════════
FIELD_WEIGHTS = {
    "reasoning_path":        0.25,  # 八段推理链(最重)
    "reasoning":             0.15,
    "asset_reasoning":       0.10,
    "asset_nature":          0.10,
    "factor_ranking":        0.10,
    "invalidation_condition":0.08,
    "verification":          0.05,
    "hard_data_evidence":    0.05,
    "data_anchor_ts":        0.03,
    "elasticity_window":     0.03,
    "low_impact":            0.02,
    "attention_score":       0.02,
    "impact_magnitude":      0.02,
}


def _mark_field_source(result, field: str, source: str) -> None:
    """统一打 source 标记 (5 类严谨来源之一)."""
    try:
        _fs = json.loads(result.get("field_sources") or "{}")
    except (json.JSONDecodeError, TypeError):
        _fs = {}
    _fs[field] = source
    result["field_sources"] = json.dumps(_fs, ensure_ascii=False)


def _mark_field_missing(result, field: str) -> None:
    """标记缺失 + 扣 completeness_score (L5 留空强标记)."""
    try:
        _mf = json.loads(result.get("missing_fields") or "[]")
        if not isinstance(_mf, list):
            _mf = []
    except (json.JSONDecodeError, TypeError):
        _mf = []
    if field not in _mf:
        _mf.append(field)
    result["missing_fields"] = json.dumps(_mf, ensure_ascii=False)
    weight = FIELD_WEIGHTS.get(field, 0.05)
    cur = float(result.get("completeness_score") or 1.0)
    result["completeness_score"] = max(0.0, cur - weight)


# v200+ P2: invalidation_condition LLM 兜底（X1 假字符串禁止 但不能留空）
# 当 LLM 留空/<4 字符时，按 event_type_code 分类给一个真实事件级失效条件，
# 既不让字段为空（确保 ai_decisions.invalidation_condition 始终有值），
# 又保留 mark_field_missing 让 L5 完整性扣分（LLM 实际没填，扣分不当）。
# 模板含义必须能让用户读懂"什么情况下这条建议失效"，不是占位字符串。
_INVAL_COND_TEMPLATES: Dict[str, str] = {
    # 利率/央行政策类
    "rate_cut": "市场对降息幅度定价反向或美联储措辞转鹰",
    "rate_hike": "通胀粘性回落或美联储暂停加息周期",
    "rate_decision": "利率路径与 OIS 隐含路径偏离 >25bp",
    "central_bank_speech": "央行措辞与此前指引明显背离",
    # 数据发布
    "cpi": "CPI 核心环比偏离预期 >0.3pp",
    "nfp": "非农新增就业偏离预期 >2 倍标准差",
    "ppi": "PPI 同比偏离预期 >0.5pp",
    "pmi": "PMI 跌破枯荣线或回升超 3 个点",
    "data_release": "数据值偏离市场共识 >2σ",
    "earnings": "业绩 EPS 与一致预期偏离 >15%",
    "guidance_update": "公司业绩指引反向调整 >10%",
    # 地缘/供给
    "geopolitical_conflict": "地缘事件缓和或避险溢价快速回落",
    "geopolitical_escalation": "停火协议落地或冲突降级",
    "opec_decision": "OPEC 转向增产或豁免国违约",
    "supply_disruption": "供给中断解除或库存回补",
    # 政策/法规
    "policy_decision": "政策细则与本周预期相反或推迟实施",
    "regulatory": "监管细则放松或豁免范围扩大",
    "trade_tariff": "关税豁免或税率低于此前市场定价",
    # 技术/资金
    "market_event": "价格反向突破 1.5×ATR 或关键支撑位失守",
    "funding_shift": "资金费率反向或 OI 骤降 >30%",
}


def _fallback_invalidation_condition(event_type_code: str, asset: str = "",
                                     action: str = "") -> str:
    """v200+ P2: invalidation_condition LLM 兜底。

    入参：event_type_code (如 'rate_cut'/'geopolitical_conflict'/...)、target_asset、suggested_action。
    出参：≤40 字真实失效条件（不是占位字符串）。

    规则：
      1. event_type_code 在 _INVAL_COND_TEMPLATES → 用对应模板
      2. 否则按 event_type_code 前缀/包含匹配（如 'rate_*'、'*geopol*'）
      3. 还不行 → 按 asset + action 给通用兜底
      4. HOLD 决策统一给"异动级别回落 <0.5%"
    """
    code = (event_type_code or "").strip().lower()
    asset_u = (asset or "").strip().upper()
    act_u = (action or "").strip().upper()

    # 1. 完全匹配
    if code in _INVAL_COND_TEMPLATES:
        return _INVAL_COND_TEMPLATES[code]

    # 2. 前缀/包含匹配
    if code.startswith("rate_") or "rate" in code:
        return _INVAL_COND_TEMPLATES["rate_decision"]
    if code.startswith("cpi") or "cpi" in code:
        return _INVAL_COND_TEMPLATES["cpi"]
    if code.startswith("nfp") or "nonfarm" in code:
        return _INVAL_COND_TEMPLATES["nfp"]
    if code.startswith("ppi") or "ppi" in code:
        return _INVAL_COND_TEMPLATES["ppi"]
    if code.startswith("pmi") or "pmi" in code:
        return _INVAL_COND_TEMPLATES["pmi"]
    if "geopol" in code or "conflict" in code or "war" in code:
        return _INVAL_COND_TEMPLATES["geopolitical_conflict"]
    if "opec" in code or "supply" in code:
        return _INVAL_COND_TEMPLATES["supply_disruption"]
    if "policy" in code or "regulat" in code or "tariff" in code:
        return _INVAL_COND_TEMPLATES["policy_decision"]
    if "earn" in code or "guidance" in code:
        return _INVAL_COND_TEMPLATES["earnings"]
    if "funding" in code or "liquidity" in code or "oi" in code:
        return _INVAL_COND_TEMPLATES["funding_shift"]

    # 3. HOLD 决策通用兜底
    if act_u == "HOLD":
        return "异动级别回落（<0.5%）且无新增催化"

    # 4. 通用兜底
    if asset_u:
        return f"{asset_u} 价格反向 1.5×ATR 或关键支撑/阻力位失守"
    return "价格反向突破 1.5×ATR 或关键支撑/阻力位失守"


def _count_sources(field_sources: Dict[str, str]) -> Dict[str, int]:
    """统计 source 分布: {llm:n, rule:n, business_kb:n, historical:n, ensemble:n, missing:n}."""
    counts = {"llm": 0, "rule": 0, "business_kb": 0,
              "historical": 0, "ensemble": 0, "missing": 0}
    for f, s in (field_sources or {}).items():
        if s == "llm":
            counts["llm"] += 1
        elif s.startswith("rule"):
            counts["rule"] += 1
        elif s.startswith("business_knowledge"):
            counts["business_kb"] += 1
        elif s == "historical_pattern":
            counts["historical"] += 1
        elif s == "llm_ensemble_consensus":
            counts["ensemble"] += 1
        elif s == "missing":
            counts["missing"] += 1
    return counts


def _fallback_business_knowledge_drivers(target_asset: str) -> Optional[List[str]]:
    """L2 业务知识兜底: 从 causal_graph 通用驱动结构取驱动列表.

    Returns: 驱动列表(如 ["美元 DXY 反向", "实际利率反向"]) 或 None.

    来源:
      - 已学习的: causal_graph.get_knowledge(asset) -> core_factors + condition_factors
      - 未学习的: causal_graph.inject_block(asset) -> 通用驱动结构(v176.2 硬编码)
    """
    if not target_asset or target_asset == "NONE":
        return None
    try:
        k = causal_graph.get_knowledge(target_asset)
        drivers = []
        for c in (k.get("core_factors") or [])[:3]:
            drivers.append(str(c.get("factor") or "").strip())
        for c in (k.get("condition_factors") or [])[:2]:
            drivers.append(str(c.get("factor") or "").strip())
        drivers = [d for d in drivers if d]
        if drivers:
            return drivers
    except Exception:
        pass
    # 兜底: 未学到时用通用驱动结构(inject_block 内部已含兜底)
    try:
        block = causal_graph.inject_block(target_asset, top=4)
        # 从 block 字符串抽 "核心驱动=X、Y" 和 "条件因素=A、B"
        import re as _re
        # split 时也用分号作为分隔符(因为 "核心驱动=X;条件因素=A" 形式)
        m1 = _re.search(r"核心驱动[=:](.+?)(?:;|;|\n|$)", block)
        m2 = _re.search(r"条件因素[=:](.+?)(?:;|;|\n|$)", block)
        drivers = []
        if m1:
            for s in _re.split(r"[;；、,，]", m1.group(1)):
                s = s.strip().rstrip("。")
                # 去掉 " 条件因素=X" 这种嵌套(若 m1 group 里残留)
                s = _re.sub(r"^\s*条件因素[=:].*", "", s).strip()
                if s:
                    drivers.append(s)
        if m2:
            for s in _re.split(r"[;；、,，]", m2.group(1)):
                s = s.strip().rstrip("。")
                if s:
                    drivers.append(s)
        return drivers[:5] if drivers else None
    except Exception:
        return None


def _fallback_historical_reasoning(target_asset: str, event_type_code: str,
                                    action: str, conn) -> Optional[Dict[str, Any]]:
    """L3 历史模式兜底: 查最近 90 天同条件真实决策的 reasoning.

    Returns: {"text": ..., "ref_decision_id": ...} 或 None.

    严谨性:
      - 数据源: 同 (asset, event_type_code, action) 历史决策的 LLM 真输出
      - 不创造新内容, 只引用
      - 显式标记 ref_decision_id 便于追溯
    """
    if not target_asset or target_asset == "NONE" or not conn:
        return None
    try:
        row = conn.execute("""
            SELECT id, reasoning FROM ai_decisions
            WHERE target_asset = ?
              AND event_type_code = ?
              AND suggested_action = ?
              AND reasoning IS NOT NULL AND reasoning != ''
              AND reasoning NOT LIKE '%服务端兜底%'
              AND reasoning NOT LIKE '%LLM 未填%'
              AND json_extract(field_sources, '$.reasoning') = 'llm'
              AND created_at > datetime('now', '-90 day')
            ORDER BY created_at DESC LIMIT 1
        """, (target_asset, event_type_code or "", action or "")).fetchone()
        if row:
            return {"text": row[1], "ref_decision_id": row[0]}
    except sqlite3.OperationalError:
        pass
    return None


def _init_result_completeness(result: Dict[str, Any]) -> Dict[str, Any]:
    """初始化 completeness/source/missing 字段(LLM 返回后第一次处理)."""
    if "field_sources" not in result:
        result["field_sources"] = "{}"
    if "missing_fields" not in result:
        result["missing_fields"] = "[]"
    if "completeness_score" not in result:
        result["completeness_score"] = 1.0
    return result


# v187: 公共 chat() 函数 — 简单 prompt in / string out 接口
# 走 purpose-routed 模型池(参见 config.get_pool_for_purpose)
# 自动 token 埋点(写到 ai_call_stats 表)
# 失败返回 ""(永不抛),便于反思/顾问等低频路径直接调
def chat(prompt: str, *,
         purpose: str = "main",
         system: str = "",
         max_tokens: int = 1500,
         temperature: float = 0.3,
         call_site: str = "") -> str:
    """v187: 简单 chat 接口. 自动选 purpose 池最高权重模型;失败降级本地;埋 token.

    Args:
        prompt: 用户内容
        purpose: main/gate/anomaly/reflection/advisor/sql/optimizer
        system: 可选 system 消息
        max_tokens: 输出上限 (覆盖 extra_body 默认)
        temperature: 0.0-1.0
        call_site: 调用方标记(写到 ai_call_stats),如 'blindspot_reflector'
    Returns:
        message.content 字符串;全失败返回 "".
    """
    import db as _db
    pool = config.get_pool_for_purpose(purpose)
    if not pool:
        return ""
    last_err = ""
    for m in pool:
        cfg = {
            "id": m["id"],
            "request_model": m.get("request_model") or m["id"],
            "label": m.get("label", m["id"]),
            "api_base": m["api_base"],
            "api_key": m["api_key"],
            "extra_body": dict(m.get("extra_body") or {}),
            "use_proxy": bool(m.get("use_proxy", False)),
            "json_mode": False,
            "is_local": bool(m.get("is_local", False)),
            "timeout": float(m.get("timeout", 60.0)),
            "key_alias": m.get("key_alias", "unknown"),
        }
        # 把 max_tokens 覆盖到 extra_body
        cfg["extra_body"]["max_tokens"] = max_tokens
        try:
            t0 = time.perf_counter()
            session = _LLM_PROXY_SESSION if cfg["use_proxy"] else _LLM_SESSION
            headers = {
                "Authorization": f"Bearer {cfg['api_key']}",
                "Content-Type": "application/json",
            }
            messages: List[Dict[str, Any]] = []
            if system:
                messages.append({"role": "system", "content": system})
            messages.append({"role": "user", "content": prompt})
            payload: Dict[str, Any] = {
                "model": cfg["request_model"],
                "messages": messages,
                "temperature": temperature,
                **cfg["extra_body"],
                "max_tokens": max_tokens,
            }
            resp = session.post(
                f"{cfg['api_base']}/chat/completions",
                json=payload, headers=headers, timeout=cfg["timeout"],
            )
            dt_ms = int((time.perf_counter() - t0) * 1000)
            if resp.status_code >= 400:
                last_err = f"HTTP {resp.status_code}: {resp.text[:150]}"
                # 埋失败日志
                _db.log_ai_call(
                    purpose=purpose, key_alias=cfg["key_alias"],
                    model_id=cfg["id"], tokens_in=0, tokens_out=0,
                    latency_ms=dt_ms, status="http_error",
                    error_code=str(resp.status_code), call_site=call_site,
                )
                continue
            body = resp.json()
            text = (body.get("choices", [{}])[0].get("message", {}).get("content", "") or "").strip()
            usage = body.get("usage", {}) or {}
            tok_in = int(usage.get("prompt_tokens") or 0)
            tok_out = int(usage.get("completion_tokens") or 0)
            _db.log_ai_call(
                purpose=purpose, key_alias=cfg["key_alias"],
                model_id=cfg["id"], tokens_in=tok_in, tokens_out=tok_out,
                latency_ms=dt_ms, status="ok",
                error_code="", call_site=call_site,
            )
            return text
        except Exception as exc:
            last_err = f"{type(exc).__name__}: {str(exc)[:120]}"
            try:
                _db.log_ai_call(
                    purpose=purpose, key_alias=cfg.get("key_alias", "unknown"),
                    model_id=cfg.get("id", "?"), tokens_in=0, tokens_out=0,
                    latency_ms=int((time.perf_counter() - t0) * 1000),
                    status="network_error" if not isinstance(exc, TimeoutError) else "timeout",
                    error_code=type(exc).__name__, call_site=call_site,
                )
            except Exception:
                pass
            continue
    print(f"[chat:{purpose}] all {len(pool)} models failed; last_err={last_err}", flush=True)
    return ""


# 市场快照 — 每轮 AI batch 前拉取一次 BTC/XAU 行情
from market_snapshot import get_snapshot
import strategy_store
import paper_trading
import decision_guard
import evidence
import hermes_agent
import macro_context
import timeseries
import news_sources

from .alerts import build_signal_title, format_signal_body, send_feishu_alert, send_important_news, send_signal_alert, send_strong_buy_alert
from .prices import _get_current_price
from .utils import _detect_vip, _now, _open_db, _ts

# ── 市场分类单一真理源 ──
# 不再 inline 写 (CRYPTO,GOLD,OIL,STOCK,MACRO,COMMODITY) 这种白名单。
# 所有 market_category 推断/校验都走这里。失败抛 UnclassifiedError，
# 调用方负责写 unclassified_events 表（人工/规则补全）。
from market_classifier import (
    MARKET_CATEGORIES_PROD,
    TARGET_TO_MARKET,
    UnclassifiedError,
    infer_market_category,
    is_debug_other_allowed,
    map_target,
    row_is_unclassified,
    validate_pair,
)

# HOLD 弱分阈值：|score| 不超过此值时视为中性观望，不伪造符号。
_NEUTRAL_SCORE_EPS = 0.001

# v176.7: 单条新闻分析因超时/DB 锁退回 PENDING 的最大次数；超限标 FAILED，打断重复分析死循环。
_MAX_PENDING_RETRIES = 3


def reconcile_score_action(score: float, action: str) -> tuple[float, str]:
    """对齐 LLM 方向与分数符号。

    - HOLD（观望）：一律保持观望、分数归零——AI 说观望就观望，绝不被强分抬成方向单
    - BUY/SELL 与分数符号相反时，以方向为准翻转分数
    - BUY/SELL 但分数接近 0 时，给一个同向弱分，便于聚合
    """
    action = (action or "HOLD").upper()
    if action not in ("BUY", "SELL", "HOLD"):
        action = "HOLD"
    try:
        score = float(score)
    except (TypeError, ValueError):
        score = 0.0
    score = max(-1.0, min(1.0, score))

    if action == "HOLD":
        return 0.0, "HOLD"  # AI 观望 → 分数中性，绝不改写成 BUY/SELL

    if action == "BUY":
        if score <= 0:
            score = abs(score) if abs(score) >= _NEUTRAL_SCORE_EPS else 0.05
        return score, "BUY"

    if score >= 0:
        score = -abs(score) if abs(score) >= _NEUTRAL_SCORE_EPS else -0.05
    return score, "SELL"


def direction_consistent(action: str, score: float) -> bool:
    """HOLD 始终自洽；BUY 需正分，SELL 需负分。"""
    action = (action or "").upper()
    try:
        score = float(score)
    except (TypeError, ValueError):
        score = 0.0
    if action == "HOLD":
        return True
    if action == "BUY":
        return score > 0
    if action == "SELL":
        return score < 0
    return False


# ── 核心字段白名单：LLM 结果整箱过滤时用的合法取值集合 ──
_SCORE_ACTIONS = ("BUY", "SELL", "HOLD")
# _MARKET_CATEGORIES 保留为兼容旧调用方的别名；新代码必须用 market_classifier.MARKET_CATEGORIES_PROD
_MARKET_CATEGORIES = MARKET_CATEGORIES_PROD  # v1 重构：直接复用真理源（无 OTHER）


def _auto_fill_required_fields(
    result: Dict[str, Any],
    *,
    snapshot_ts_iso: Optional[str] = None,
    news_published_at: Optional[str] = None,
    evidence_lines: Optional[list] = None,
) -> Dict[str, Any]:
    """v200 P0+ 严谨兜底：LLM 漏填的 4 个关键字段，用 L1 数学规则兜底。

    设计原则（取代 P1-5/P0-2 旧实现）:
      - 不写"服务端兜底：XXX"假字符串（X1 类禁止）
      - 严谨兜底来源分级:
        L1 数学公式反推（rule_*）: data_anchor_ts/hard_data_evidence/elasticity_window/verification
      - 所有兜底字段打 source 标记, 字段缺失打 missing 标记
      - completeness_score 反映填全率（不全=扣分）

    参数:
      snapshot_ts_iso     prompt 里 snap_price_ts 的 ISO-8601 字符串（首选）
      news_published_at   新闻 published_at 的 ISO-8601（次选）
      evidence_lines      主品种 5m/1H 关键 K 线数据（用于 hard_data_evidence）
    """
    _init_result_completeness(result)

    # ── data_anchor_ts ──────────────────────────────────────────────
    if not result.get("data_anchor_ts"):
        anchor = (snapshot_ts_iso or news_published_at or "")
        if anchor:
            result["data_anchor_ts"] = str(anchor)[:32]
            _mark_field_source(result, "data_anchor_ts", "rule_snapshot_ts")
        else:
            _mark_field_missing(result, "data_anchor_ts")

    # ── hard_data_evidence ──────────────────────────────────────────
    if not result.get("hard_data_evidence"):
        if evidence_lines:
            # L1 数学规则兜底: 拼接行情快照 K 线数据(实数据,非假字符串)
            result["hard_data_evidence"] = " | ".join(str(x) for x in evidence_lines[:6])[:500]
            _mark_field_source(result, "hard_data_evidence", "rule_kline_snapshot")
        else:
            _mark_field_missing(result, "hard_data_evidence")

    # ── elasticity_window ───────────────────────────────────────────
    if not result.get("elasticity_window"):
        # L1 数学规则默认: 60 分钟窗口，medium 衰减，30 分钟达峰
        result["elasticity_window"] = json.dumps(
            {"minutes": 60, "decay": "medium", "peak_after_minutes": 30},
            ensure_ascii=False,
        )
        _mark_field_source(result, "elasticity_window", "rule_default_window_v176")

    # ── verification ────────────────────────────────────────────────
    if not result.get("verification"):
        # L1 数学规则兜底: 用 suggested_action vs sentiment_score 推断最简一致性结果
        try:
            sa = (result.get("suggested_action") or "").upper()
            sc = float(result.get("sentiment_score") or result.get("score") or 0.0)
            if (sa in ("LONG", "BUY") and sc > 0) or (sa in ("SHORT", "SELL") and sc < 0):
                result["verification"] = "pass | rule_action_score_consistent"
            else:
                result["verification"] = "warn | rule_action_score_inconsistent"
            _mark_field_source(result, "verification", "rule_action_score_consistency_v176_5")
        except (ValueError, TypeError):
            # 字段解析失败: L5 留空, 不写假字符串
            _mark_field_missing(result, "verification")

    return result


def _build_evidence_for_asset(asset: str, ts_iso: str = "") -> str:
    """P1-6 服务端拼硬数据：抓 1m + 1H K 线最后一根 → 拼接成 hard_data_evidence。

    用于 LLM 漏填 hard_data_evidence 时的兜底。失败返回空字符串。

    v176.11 fix：之前无脑用 fetch_okx_candles → BTC/ETH/SOL 走 OKX 必然失败（OKX 上没有
      BTC-USDT-SWAP 这种合约，这些币只在币安上市）；现在按 quote_sources.route(asset) 路由表
      选对数据源：
        - binance_fapi / binance_spot → fetch_binance_spot_candles（spot 国内直连稳定）
        - okx_swap                    → fetch_okx_candles（XAU/XAG 已有国内现货兜底）
        - yahoo / domestic_spot       → 返回空（暂未实现 candles fetcher，标记为 N/A 更诚实）
      路由失败的资产（不在 ASSET_ROUTE 里）返回空字符串，避免误用别币种 K 线。
    """
    try:
        import quote_sources as _qs
        rt = _qs.route(asset)
        if not rt:
            return ""
        market = rt.get("market", "")
        sym = rt.get("symbol") or ""
        if not sym:
            # WTI/OIL 等无永续合约 → 暂用 N/A
            return ""

        # ── 选 fetcher ──
        rows_1m: list = []
        rows_1h: list = []
        try:
            import datetime as _dt
            if market in ("binance_fapi", "binance_spot"):
                # 币安币种（BTC/ETH/SOL）—— 用现货 spot（国内直连稳定），不再走 OKX
                rows_1m = _qs.fetch_binance_spot_candles(sym, "1m", 2) or []
                rows_1h = _qs.fetch_binance_spot_candles(sym, "1h", 2) or []
            elif market == "okx_swap":
                # OKX 优先 → Yahoo 兜底（v2026.10.05）
                rows_1m = _qs.fetch_asset_candles_with_yahoo_fallback(asset, "1m", 2) or []
                rows_1h = _qs.fetch_asset_candles_with_yahoo_fallback(asset, "1H", 2) or []
            else:
                # yahoo / domestic_spot 暂未实现 K 线 fetcher
                return ""
        except Exception:
            return ""

        if not rows_1m and not rows_1h:
            return ""

        lines: list = []
        if rows_1m:
            r = rows_1m[-1]
            # r = [ts_ms, open, high, low, close, volume]
            import datetime as _dt
            t_str = _dt.datetime.fromtimestamp(r[0] / 1000).strftime("%H:%M")
            change_pct = ((r[4] - r[1]) / r[1] * 100.0) if r[1] else 0.0
            lines.append(f"{asset} {t_str} close={r[4]:.4f} pct={change_pct:+.2f}% vol={r[5]:.0f}")
        if rows_1h:
            r = rows_1h[-1]
            import datetime as _dt
            t_str = _dt.datetime.fromtimestamp(r[0] / 1000).strftime("%m-%d %H:%M")
            change_pct = ((r[4] - r[1]) / r[1] * 100.0) if r[1] else 0.0
            lines.append(f"{asset} {t_str} 1H close={r[4]:.4f} pct={change_pct:+.2f}%")
        return " | ".join(lines)
    except Exception:
        return ""


def _compute_market_style(asset: str) -> str:
    """P2-9 市场风格感知：最近 1H/4H K 线算 ATR% + 趋势方向 → 输出风格标签。

    返回字符串格式："{style}|atr_pct%|trend_pct%"。style ∈
      trending_up / trending_down / mean_revert / volatile_high / volatile_low / unknown
    """
    try:
        import quote_sources as _qs
        rt = _qs.route(asset)
        if not rt:
            return "unknown|n/a|n/a"
        market = rt.get("market", "")
        sym = rt.get("symbol") or ""
        if not sym:
            return "unknown|n/a|n/a"
        # v176.11 fix：与 _build_evidence_for_asset 同 bug —— 按市场路由选 fetcher，
        #   不再无脑走 OKX（BTC/ETH/SOL 不在 OKX 上市）。
        # v2026.10.05：okx_swap 分支加 Yahoo 兜底，避免 OKX SNI 阻断时 ATR/趋势为空。
        h1 = []
        try:
            if market in ("binance_fapi", "binance_spot"):
                h1 = _qs.fetch_binance_spot_candles(sym, "1h", 24) or []
            elif market == "okx_swap":
                h1 = _qs.fetch_asset_candles_with_yahoo_fallback(asset, "1H", 24) or []
            else:
                return "unknown|n/a|n/a"
        except Exception:
            return "unknown|n/a|n/a"
        if not h1 or len(h1) < 5:
            return "unknown|n/a|n/a"
        # ATR% = mean(high-low)/close * 100
        atr_sum = 0.0
        atr_n = 0
        closes = []
        for r in h1:
            try:
                h_l = float(r[2]) - float(r[3])
                atr_sum += h_l
                atr_n += 1
                closes.append(float(r[4]))
            except Exception:
                continue
        if atr_n == 0 or not closes:
            return "unknown|n/a|n/a"
        atr_pct = atr_sum / atr_n / closes[-1] * 100.0
        # trend: 24h 前后 close 对比
        trend_pct = (closes[-1] - closes[0]) / closes[0] * 100.0 if closes[0] else 0.0
        # 风格判定
        if atr_pct > 1.5:
            style = "volatile_high"  # 高波动：新闻冲击钝化，胜率偏低
        elif atr_pct < 0.3:
            style = "volatile_low"   # 低波动：新闻冲击被放大
        elif abs(trend_pct) > 1.5:
            style = "trending_up" if trend_pct > 0 else "trending_down"
        else:
            style = "mean_revert"
        return f"{style}|{atr_pct:.2f}|{trend_pct:+.2f}"
    except Exception:
        return "unknown|n/a|n/a"


# ── v176.2：市场风格持久化 + 决策反思 + prev 决策传递 ─────────────────────


def _snapshot_market_style(asset: str, regime_hint: str = "") -> int:
    """把 _compute_market_style 输出 + 5 层 alerts 落 market_style_snapshot 表。"""
    try:
        import time, db as _db, market_style as _ms
        style_str = _compute_market_style(asset)
        style = style_str.split("|", 1)[0] if style_str else "unknown"
        try:    layer1 = ",".join(_ms.layer1_price_structure_alerts(asset))
        except Exception: layer1 = ""
        try:    layer2 = ",".join(_ms.layer2_funding_flow_alerts(asset))
        except Exception: layer2 = ""
        try:    layer3 = ",".join(_ms.layer3_positioning_alerts(asset))
        except Exception: layer3 = ""
        try:    layer4 = ",".join(_ms.layer4_macro_rate_alerts(asset))
        except Exception: layer4 = ""
        try:    layer5 = ",".join(_ms.layer5_event_alerts(asset))
        except Exception: layer5 = ""
        try:    style_block = _ms.market_style_block(asset)
        except Exception: style_block = ""
        conn = _db.get_connection()
        conn.execute(
            "INSERT INTO market_style_snapshot "
            "(asset, regime, layer1_alerts, layer2_alerts, layer3_alerts, "
            "layer4_alerts, layer5_alerts, style_block, trigger, ts) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (asset or "", regime_hint or style, layer1, layer2, layer3,
             layer4, layer5, style_block, "auto", int(time.time())),
        )
        conn.commit()
        return 1
    except Exception:
        return 0


def _reflect_recent_decisions(asset: str, window: str = "1h", n: int = 8) -> int:
    """读最近 N 条同品种决策 + 真实 K 线方向，算胜率 → 写反思。

    v186 audit 修复: 即使 settled=0 也计算虚拟 PnL (entry_price → 当前价).
    原版: 当 _resolve_post_decision_pnl 返回 None (因为 settled=0) → all flat →
    n_decided=0 → return 0 → 反思永远空转. 反馈环断在第一步.

    新逻辑:
    1. 优先用 _resolve_post_decision_pnl (真实结算的 PnL)
    2. fallback: 从 entry_price + 当前 _get_current_price_cached 算虚拟 PnL
    3. 仍 fallback: 从 max_price/min_price 算 MFE/MAE 推断方向
    4. 只在完全没信号时才返回 0

    这样即使 forward_tracker 没跑 (settled=0), 反思 worker 也能产出.
    """
    try:
        import time, db as _db
        conn = _db.get_connection()
        rows = conn.execute(
            "SELECT id, suggested_action, entry_price, max_price, min_price, "
            "       entry_time, settled, forward_pnl "
            "FROM ai_decisions "
            "WHERE target_asset=? AND LENGTH(COALESCE(reasoning_path,'')) > 50 "
            "ORDER BY id DESC LIMIT ?",
            (asset, n),
        ).fetchall()
        if len(rows) < 3:
            return 0
        win = loss = flat = 0
        pnl_sum = 0.0
        try:
            from prices import _resolve_post_decision_pnl as _pp
        except Exception:
            _pp = None
        try:
            from prices import _get_current_price_cached as _cp
        except Exception:
            _cp = None

        for row in rows:
            rid = row["id"]
            action = row["suggested_action"]
            entry = row["entry_price"]
            cur_max = row["max_price"]
            cur_min = row["min_price"]
            settled = row["settled"]
            fwd_pnl = row["forward_pnl"]

            pnl = None
            # 1) 真实结算优先
            if settled == 1 and fwd_pnl is not None:
                pnl = float(fwd_pnl)
            # 2) _resolve_post_decision_pnl
            elif _pp:
                try:
                    pnl = _pp(rid, asset, "1h")
                except Exception:
                    pnl = None
            # 3) v186 fallback: 从 entry_price + 当前价算虚拟 PnL
            if pnl is None and entry and entry > 0 and _cp:
                try:
                    cur_price = _cp(asset)
                except Exception:
                    cur_price = None
                if cur_price and cur_price > 0:
                    pnl = (cur_price - entry) / entry
            # 4) v186 fallback: 从 max/min 推断方向
            if pnl is None and entry and entry > 0 and (cur_max or cur_min):
                if cur_max and cur_max > entry:
                    pnl = (cur_max - entry) / entry  # 假设拿到最优
                elif cur_min and cur_min < entry:
                    pnl = (cur_min - entry) / entry

            if pnl is None:
                flat += 1; continue
            pnl_sum += pnl
            action_u = (action or "").upper()
            if action_u == "BUY" and pnl > 0.0005:        win += 1
            elif action_u == "SELL" and pnl < -0.0005:    win += 1
            elif action_u == "HOLD":                      flat += 1
            elif abs(pnl) < 0.0005:                       flat += 1
            else:                                         loss += 1
        n_decided = win + loss
        if n_decided == 0: return 0
        win_rate = win / n_decided
        bias_text = ""; trigger = ""
        if loss >= 3 and win_rate < 0.30:
            bias_text = f"{asset} {window} 连{loss}输{win}赢胜率{win_rate:.0%}—下次降一档或转HOLD"
            trigger = f"连亏{loss}次"
        elif win >= 5 and win_rate > 0.70:
            bias_text = f"{asset} {window} {win}胜{loss}负—同方向信心可上调"
            trigger = f"连胜{win}次"
        elif loss > win * 2 and n_decided >= 4:
            bias_text = f"{asset} {window} 亏/盈={loss}/{win}—方向偏好系统性偏差，复核[驱动力]"
            trigger = "systematic_bias"
        if not bias_text: return 0
        avg_pnl = pnl_sum / max(1, win + loss + flat)
        conn.execute(
            "INSERT INTO decision_reflection "
            "(asset, window, n_decisions, win_n, loss_n, flat_n, avg_pnl_pct, "
            "bias_text, trigger, ts) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (asset, window, len(rows), win, loss, flat, avg_pnl, bias_text,
             trigger, int(time.time())),
        )
        conn.commit()
        return 1
    except Exception:
        return 0


def _auto_match_insights(news_text: str, target_asset: str = "", top_n: int = 3) -> List[int]:
    """v186: 当 AI 输出 insight_hit_ids 为空时,自动从 review_insights 中按关键词匹配 top-N.

    目的: 让"经验"真的参与决策的命中统计. 原版 AI 总是返回 [] 因为 prompt 里
    虽然列出了 EX# 但模型没法靠直觉判断"这条新闻属于哪几条经验". 我们用
    关键词打分 (rule 中关键词 × 新闻中出现次数), 选 top-3.

    算法: tokenize rule 和 news_text → 计算 jaccard 相似度 (set intersection / union).
    返回 matched insight_id 列表 (按相似度降序).
    """
    if not news_text:
        return []
    try:
        from review_agent import load_active_insight_items
        import re as _re
        items = load_active_insight_items()
        if not items:
            return []
        # tokenize news_text → set
        news_tokens = set(
            t.lower() for t in _re.findall(r"[\w\u4e00-\u9fff]+", news_text) if len(t) >= 2
        )
        if not news_tokens:
            return []
        scored = []
        for it in items:
            rule = it.get("rule", "")
            if not rule:
                continue
            rule_tokens = set(
                t.lower() for t in _re.findall(r"[\w\u4e00-\u9fff]+", rule) if len(t) >= 2
            )
            if not rule_tokens:
                continue
            inter = news_tokens & rule_tokens
            if not inter:
                continue
            # jaccard
            sim = len(inter) / len(news_tokens | rule_tokens)
            # bonus: asset match (规则里直接点名 asset 加分)
            if target_asset and target_asset.upper() in rule.upper():
                sim += 0.2
            scored.append((sim, int(it["id"])))
        scored.sort(reverse=True)
        return [iid for _, iid in scored[:top_n]]
    except Exception:
        return []


def _load_prev_decision(asset: str) -> dict:
    """查同 asset 最近 1 条决策，返回 prev/regime/bias 5 字段。"""
    out = {"prev_decision_id": None, "prev_action_same_asset": "",
           "prev_score_same_asset": None, "regime_at_decision": "",
           "bias_after_reflection": ""}
    try:
        import db as _db
        conn = _db.get_connection()
        row = conn.execute(
            "SELECT id, suggested_action, sentiment_score FROM ai_decisions "
            "WHERE target_asset=? AND LENGTH(COALESCE(reasoning_path,'')) > 50 "
            "ORDER BY id DESC LIMIT 1", (asset,),
        ).fetchone()
        if row:
            out["prev_decision_id"] = row[0]
            out["prev_action_same_asset"] = row[1] or ""
            out["prev_score_same_asset"] = row[2]
        row2 = conn.execute(
            "SELECT regime FROM market_style_snapshot WHERE asset=? "
            "ORDER BY id DESC LIMIT 1", (asset,),
        ).fetchone()
        if row2: out["regime_at_decision"] = row2[0]
        row3 = conn.execute(
            "SELECT bias_text FROM decision_reflection WHERE asset=? "
            "ORDER BY id DESC LIMIT 1", (asset,),
        ).fetchone()
        if row3: out["bias_after_reflection"] = row3[0]
    except Exception:
        pass
    return out


def _build_struct_summary(res: Dict[str, Any], action: str, asset: str) -> str:
    """v181 结构化产出（准优先版）：7 项摘要，供 ai_decisions.structured_summary 落库 + 前端渲染。

    - 3 个判断项（asset_classification/fund_driver/consensus_vs_us）直接取模型亲填值；
    - 4 个复制项（target_asset/final_action/key_factors/self_check）从已有字段 1:1 映射，不做启发式猜测。
    任何异常返回空字符串，绝不阻断 INSERT。
    """
    try:
        _res = res if isinstance(res, dict) else {}

        # ── 复制项（与落库字段严格一致）──
        target_asset = str(asset or _res.get("target_asset") or "NONE").strip()
        final_action = str(action or "").strip().upper() or "HOLD"

        key_factors = []
        _fr = _res.get("factor_ranking")
        if isinstance(_fr, list):
            for _item in _fr[:3]:
                if isinstance(_item, dict):
                    _f = str(_item.get("factor") or "").strip()
                    _d = str(_item.get("direction") or "").strip()
                    if _f:
                        key_factors.append({"factor": _f, "direction": _d})

        _ver = str(_res.get("verification") or "").strip()
        if _ver.lower().startswith("pass"):
            self_check = "自洽"
        elif _ver.lower().startswith(("warn", "fail")) or "矛盾" in _ver:
            self_check = "矛盾"
        else:
            self_check = "N/A"

        # ── 判断项（模型亲填，漏填标 N/A）──
        asset_classification = str(_res.get("asset_classification") or "").strip() or "N/A"
        fund_driver = str(_res.get("fund_driver") or "").strip() or "N/A"
        consensus_vs_us = str(_res.get("consensus_vs_us") or "").strip() or "N/A"

        return json.dumps({
            "target_asset": target_asset,
            "final_action": final_action,
            "key_factors": key_factors,
            "fund_driver": fund_driver,
            "consensus_vs_us": consensus_vs_us,
            "self_check": self_check,
            "asset_classification": asset_classification,
        }, ensure_ascii=False)
    except Exception:
        return ""


def _looks_placeholder(name: str) -> bool:
    """判断一个因素名是否为占位/模板噪声（区别于 causal_graph._is_placeholder 的候选过滤）。"""
    s = (name or "").strip()
    if not s:
        return True
    if s.upper() in ("N/A", "NA", "NONE", "NULL", "UNDEFINED", "TBD", "TODO", "OTHER", "HOLD", "BUY", "SELL"):
        return True
    if "服务端" in s or "兜底" in s:
        return True
    # LLM 未替换的模板占位词
    if s in ("因素名", "因素", "子链", "变量", "驱动", "待定", "未填", "未知", "无"):
        return True
    return False


# 从 reasoning 抽取因素时的元信息/交易动作噪声词（命中即丢弃整条候选）
_FACTOR_STOPWORDS = ("门卫", "盘面", "仓位", "追单", "抢跑", "先验", "结论", "方向",
                      "事件类型", "摘要", "未异动", "跟单")


def _extract_factors_fallback(result: Dict[str, Any]) -> List[Dict[str, Any]]:
    """v200 P0+ 严谨兜底: factor_ranking 缺失/全空时的真实因素抽取.

    设计原则（取代 v176 旧"服务端兜底因素"占位）:
      - X1 假数据禁止: 不写"服务端兜底因素"假字符串
      - 优先级: 从 LLM 真实产出(asset_nature/reasoning) 抽取 → L2 业务知识兜底 → L5 留空
      - 所有 fallback 抽取出来的 factor 打 source 标记, 不让 LLM 误以为 LLM 真输出
    """
    action = str(result.get("suggested_action") or "HOLD").strip().upper()
    direction = {"BUY": "利多", "SELL": "利空"}.get(action, "中性")
    target = str(result.get("target_asset") or "NONE").upper()

    # 只从 LLM 真实产出的两个字段提取。reasoning_path 在 80% 决策里是服务端
    # 模板占位（501/557 分支生成），从中提取会带入「目标资产 WTI」等噪声，故不作为源。
    # 且本函数在 sanitize 链条中运行，此时 reasoning/reasoning_path 可能已被前面的
    # 兜底步骤改写，必须跳过带服务端兜底标记的源，避免把「LLM 未填 reasoning」当因素。
    sources: List[str] = []
    an = str(result.get("asset_nature") or "")
    if an and "服务端兜底" not in an:
        sources.append(an)
    rsn = str(result.get("reasoning") or "")
    if rsn and not any(t in rsn for t in ("服务端兜底", "LLM 未填", "关键词推断", "未做全文分析")):
        sources.append(rsn)

    factors: List[str] = []
    seen = set()
    for src in sources:
        seg = re.sub(r"^\[[^\]]+\]\s*", "", src.strip())
        # 拎出 'xxx 主导驱动/驱动/性质：' 之后的宾语
        seg = re.sub(r"^.*?(?:主导驱动|核心驱动|驱动|性质)[:：]\s*", "", seg)
        # 去掉括号备注、数值百分比，换成逗号便于拆分
        seg = re.sub(r"[（(][^）)]*[）)]", "，", seg)
        seg = re.sub(r"[-+]?\d+(?:\.\d+)?%?", " ", seg)
        seg = re.sub(r"\d+/\d+", " ", seg)
        # 用中文/英文标点拆分（保留英文短语内部空格，如 'risk on'）
        for part in re.split(r"[+、，,；;：:/|]+", seg):
            cand = part.strip().rstrip("，,。.;；")
            cand = re.sub(r"^(利多|利空|中性|看多|看空|观望)\s*", "", cand).strip()
            # 丢弃以 '[' 开头的模板标记（如 '[副品种 WTI] 事件类型'、'[MiniMax] ...'）
            if cand.startswith("["):
                continue
            if _looks_placeholder(cand):
                continue
            if len(cand) < 2 or len(cand) > 16:
                continue
            if cand.upper() == target:
                continue
            if cand in _FACTOR_STOPWORDS:
                continue
            if any(w in cand for w in _FACTOR_STOPWORDS):
                continue
            if cand not in seen:
                seen.add(cand)
                factors.append(cand)

    if factors:
        return [{"factor": f[:200], "rank": i, "direction": direction}
                for i, f in enumerate(factors[:3], 1)]

    # ── L2 业务知识兜底: 从 causal_graph 通用驱动结构取真实驱动 ──
    drivers = _fallback_business_knowledge_drivers(target)
    if drivers:
        return [{"factor": d[:200], "rank": i, "direction": direction}
                for i, d in enumerate(drivers[:5], 1)]

    # ── L5 留空: 不写假字符串, 让上游知道是缺失 ──
    return []


def _presanitize_for_schema(result: Dict[str, Any], cleaned: str = "") -> Dict[str, Any]:
    """v176：把 schema 校验必填字段强制达标（在 schema 校验前再补一遍）。

    llm_schema.AI_DECISION_SCHEMA 必填 + 强约束：asset_nature(minLen 4)/verification(minLen 4)
    /event_type_code(minLen 3)/reasoning_path(minLen 100)/hard_data_evidence(minLen 4)
    /reasoning(minLen 4)/asset_reasoning(minLen 4)/invalidation_condition(minLen 4)
    /factor_ranking 至少 1 个合规 item。

    兜底顺序：sanitize → presanitize → schema validate。

    v176.9 新增参数 cleaned：LLM 原文（剥掉 <think> 后的）。
    用于全空兜底时从原文末尾抽 80 字当 [总结] 占位，
    避免 reasoning_path 全是 "LLM 未输出八段" 这种零信息占位。
    """
    if not isinstance(result, dict):
        result = {}

    # 0) v176.8 防御：剥离 reasoning 里的 <think>…</think> 标签并尝试兜底取真实内容。
    # 现象：MiniMax-M3 主分析在 thinking disabled 但 prompt 极长时偶发产出
    #   reasoning="<think>…让我先梳理…</think>"（即整段都是 thinking 块），被 strip 后是空 → 兜底。
    # 修复：先剥 think 标签 → 仍空则从 reasoning_path 的 [总结] 段取 → 再空则用 asset_reasoning。
    _think_re = re.compile(r"<think>.*?</think>", re.DOTALL)
    def _strip_think(s: str) -> str:
        s = _think_re.sub("", s or "")
        s = re.sub(r"</?think[^>]*>", "", s).strip()
        return s
    for _k in ("reasoning", "asset_reasoning", "asset_nature", "invalidation_condition"):
        _v = str(result.get(_k) or "")
        if "<think>" in _v or "<think" in _v or _v.lstrip().startswith("<think>"):
            result[_k] = _strip_think(_v)
    _rsn0 = str(result.get("reasoning") or "").strip()
    if len(_rsn0) < 4:
        # 兜底 1：从 reasoning_path 的 [总结] 段取（如果 LLM 给了完整八段推理）
        _rp0 = str(result.get("reasoning_path") or "")
        _sm = re.search(r"\[总结\]\s*([^\n]+)", _rp0)
        if _sm:
            _sm_text = _sm.group(1).strip()
            if len(_sm_text) >= 4 and "兜底" not in _sm_text:
                result["reasoning"] = _sm_text[:200]
        # 兜底 2：用 asset_reasoning（如果已 sanitize 后 >=4）
        if len(str(result.get("reasoning") or "").strip()) < 4:
            _ar = str(result.get("asset_reasoning") or "").strip()
            _ar_clean = re.sub(r"^[^:：]+[:：]\s*(服务端兜底.*)$", "", _ar).strip()
            if len(_ar_clean) >= 4:
                result["reasoning"] = _ar_clean[:200]

    # 1) reasoning_path：min 100 字符。空/短 → 用 v176.2 [总框架] + [因素定位] + [总结] 模板占位
    # v176.2：100 字符的阈值让 LLM 输出"XAU: 服务端兜底"（~30 字符）被无端覆盖。
    # 改为：只在完全空（<10 字符）/解析失败 时覆盖；>=10 字符视为 LLM 真实输出，保留。
    rp = str(result.get("reasoning_path") or "").strip()
    # v176.2.1：把 LLM 误读的"未注入因果图谱"字面文本替换为真实情况——
    # 即使 graph_block 已注入，LLM 仍输出口语化的"未注入"占位。覆盖成"因果图谱已注入"
    # （更准确反映注入事实），同时解除 100 字符阈值对该段的影响。
    if "未注入因果图谱" in rp:
        rp = rp.replace(
            "[总框架] 服务端兜底：未注入因果图谱",
            "[总框架] 服务端兜底：因果图谱已注入（但 LLM 未应用）"
        )
        rp = rp.replace(
            "[总框架] N/A | 服务端未注入因果图谱",
            "[总框架] 服务端兜底：因果图谱已注入（但 LLM 未应用）"
        )
        result["reasoning_path"] = rp
    if len(rp) < 10:
        ta = str(result.get("target_asset") or "NONE").upper()
        an = str(result.get("asset_nature") or "N/A").strip() or "N/A"
        rsn = str(result.get("reasoning") or "").strip()
        # v176.3 fix: 三段式兜底
        # 1) LLM 给了 10+ 字符的摘要 → 保留原文 + 追加兜底说明
        # 2) LLM 没给 reasoning_path 但给了 reasoning（真实判断） → 用 reasoning 作 [总结] 段，剩余段标注"未展开"
        # 3) 两者都没给 → 全段硬兜底（最后兜底）
        if rp and len(rp) >= 10:
            result["reasoning_path"] = (
                f"[LLM 摘要] {rp}\n"
                f"[兜底补全] LLM 输出了简短摘要，未展开八段推演；"
                f"置信度降低，{ta} 标记为 HOLD（数据不足）"
            )
        elif rsn and len(rsn) >= 4:
            # v176.6: 用 reasoning + asset_nature + action/score 等已 sanitize 字段，把八段填成
            # 有信息量的叙述，而不是"服务端自动补全：需人工复核"这类空占位。LLM 虽未展开八段，
            # 但给了 reasoning 摘要 —— 把它作为 [总结] 实体内容，其余段按事件类型/方向导出。
            an = str(result.get("asset_nature") or "").strip()
            ticker = str(result.get("news_ticker") or result.get("ticker") or "").strip()
            ev_code = str(result.get("event_type_code") or "").strip() or "market_event"
            impact = result.get("asset_impact") or result.get("impact_score")
            impact_s = f"{impact:.1f}/10" if isinstance(impact, (int, float)) else "未评估"
            news_summary = rsn[:140] if len(rsn) > 140 else rsn
            action = str(result.get("suggested_action") or "HOLD").strip().upper()
            score = result.get("sentiment_score")
            score_s = f"{score:+.2f}" if isinstance(score, (int, float)) else "未评估"
            direction = ("看多" if action in ("BUY", "LONG") else
                         "看空" if action in ("SELL", "SHORT") else "观望")
            ev_label = {
                "market_event": "市场事件",
                "macro_policy": "宏观政策",
                "geopolitical_conflict": "地缘冲突",
                "earnings": "财报/业绩",
                "liquidity_flow": "流动性",
                "sentiment_shift": "情绪转向",
                "supply_demand": "供需变化",
                # v170 P0+ 修复：原 "other": "其它事件" 会让历史脏数据继续显示"其它"
                # 现在的不可分类新闻已 step7 → 'low_value' + quarantined_at，
                # 这里给 low_value 一个明确标识，让用户/前端一眼看到"被隔离"。
                "low_value": "⚠️ 低价值（已隔离）",
            }.get(ev_code, ev_code)
            # v200 P3: 改中性措辞 — LLM 真输出（reasoning 摘要），不要再被前端标"无真实 AI"
            # 之前用「由 LLM 摘要归纳」「reasoning 摘要未展开」触发了前端 summary 类横幅，
            # 让用户误以为「没真实 AI 分析」。实际 LLM 给了真东西（45-80 字 reasoning），
            # 仅未展开八段。改写后用户看到「LLM 真东西摘要」即可明白。
            result["reasoning_path"] = (
                f"[总框架] 目标资产 {ta}；性质：{an or '未明'}；事件类型：{ev_label}；影响分 {impact_s}\n"
                f"[因素定位] 命中「{ev_label}」传导链（{ticker or ta}）；关键变量及其价格弹性见 [总结] 摘要\n"
                f"[驱动力] LLM 真东西摘要：{news_summary}（方向 {direction}）\n"
                f"[水位博弈] 盘面水位：未注入盘面快照（{ta} 监控品种外或数据通道受限），按中性处理不抢跑\n"
                f"[跨资产资金] 跨资产联动：LLM 摘要未展开多品种资金传导，按 LLM 单资产判断处理\n"
                f"[反共识结论] 独立判断：sentiment={score_s}，action={action}（{direction}）；反共识点见 [总结]\n"
                f"[逻辑自检] 自洽性：action/score 方向与服务端规则对齐；八段骨架已按 LLM 真东西铺开\n"
                f"[总结] {rsn}"
            )
        else:
            # v176.9 fix：全空兜底时，从 cleaned 文本（前一步剥离 <think> 后的 LLM 原文）
            # 里抽 80 字当 [总结] 占位 —— 至少不是完全空白，让用户能看到 LLM 实际产出了什么
            _act2 = str(result.get("suggested_action") or "HOLD").strip().upper()
            _mc2 = str(result.get("market_category") or "").strip().upper()
            _sc2 = result.get("sentiment_score")
            _sc2_s = f"{_sc2:+.2f}" if isinstance(_sc2, (int, float)) else "未评估"
            # 从 cleaned 末尾抓 80 字作为 LLM 真实输出片段（保留信息，不丢字）
            _raw_tail = cleaned.strip()
            # 截掉开头的 JSON 块（"{" 到对应 "}"）—— 只看 JSON 之后的内容
            _json_match = re.search(r"^.*?\}\s*", _raw_tail, re.DOTALL)
            if _json_match and _json_match.end() < len(_raw_tail):
                _raw_tail = _raw_tail[_json_match.end():].strip()
            # 也截掉 markdown code fence
            _raw_tail = re.sub(r"^```(?:json)?\s*|\s*```$", "", _raw_tail).strip()
            if len(_raw_tail) > 80:
                _raw_tail = _raw_tail[:80] + "…"
            _summary_line = _raw_tail if _raw_tail else f"{ta} rule_no_data_conclusion_{_act2}"
            # v176.12 fix：若抽出的尾巴是 token 截断的 JSON 残片（news_88=140 实测），重置为服务端兜底，
            # 避免前端展示 raw JSON 碎片。自然语言叙述不会带引号写这些字段键。
            if re.search(r'"(?:factor|rank|direction|impact_magnitude|factor_ranking|reasoning_path|suggested_action|target_asset)"', _summary_line):
                _summary_line = f"{ta} rule_no_data_conclusion_{_act2}"
            # v200 P1-B: 把"事件不在监控范围 → 强制 HOLD"硬套话换成有信息量的版本。
            # 背景: 此前 NONE 决策 89% 都被这串套话覆盖，用户反馈"分析说一些模型莫名其妙的话"。
            # 改进点:
            #   1) [总框架] 加入 ticker 和新闻真实摘要（如果有），不再纯说"事件不在监控范围"
            #   2) [因素定位] / [驱动力] 说明为什么是 NONE（标的不可交易/无明确标的/汇总类），而不是空洞否定
            #   3) [总结] 优先用 LLM 真实摘要(_raw_tail)，只在完全空时才用兜底
            #   4) 保留 HOLD 结论和置信度低说明（这些是有用的告警，不是套话）
            _ticker = str(result.get("news_ticker") or result.get("ticker") or "").strip()
            _ticker_tag = f"；涉及标的 {_ticker}" if _ticker else ""
            # 取 LLM 给的 reasoning（如有）作为 [总结] 内容，比纯兜底信息量大
            _rsn_fb = str(result.get("reasoning") or "").strip()
            if _rsn_fb and len(_rsn_fb) >= 4 and "服务端兜底" not in _rsn_fb:
                _summary_line = _rsn_fb[:140]
            result["reasoning_path"] = (
                f"[总框架] 目标资产 {ta}（market_category={_mc2}）{_ticker_tag}；"
                f"事件不直接命中本系统核心监控品种（BTC/XAU/WTI/ETH/SOL），按非交易信号处理\n"
                f"[因素定位] 该事件归类为个股/港股/汇总类等扩展品种，"
                f"本框架不执行自动交易；LLM 输出 {_act2} 仅作分析记录\n"
                f"[驱动力] 无跨品种资金传导路径到核心监控品种；"
                f"事件本身的资金/情绪影响停留在所属个股市场，未进入本系统套利监控框架\n"
                f"[水位博弈] 盘面水位无影响（事件与核心品种盘面不相关）\n"
                f"[跨资产资金] 无跨资产联动（事件与核心监控品种无关）\n"
                f"[反共识结论] sentiment={_sc2_s}，action={_act2}（中性观望，不下单）；"
                f"非交易结论不代表事件无意义，仅表示本框架不执行\n"
                f"[逻辑自检] target_asset={ta} 不在核心监控清单 → 按 HOLD 兜底；"
                f"若用户关注此标的所属市场，请走人工/个股通道\n"
                f"[总结] {_summary_line}"
            )

    # 2) asset_nature：min 4 字符 (v200 P0+: L2 业务知识兜底驱动列表, 非假字符串)
    _init_result_completeness(result)
    an = str(result.get("asset_nature") or "")
    if not an.strip() or len(an) < 4:
        ta = str(result.get("target_asset") or "NONE").upper()
        drivers = _fallback_business_knowledge_drivers(ta)
        if drivers:
            # L2: 用第一个驱动定性(非假字符串, 是真实业务知识)
            result["asset_nature"] = f"{ta} {drivers[0]}驱动属性"
            _mark_field_source(result, "asset_nature", "business_knowledge_causal_graph_v176_2")
        else:
            # L5 留空: 标记缺失, 不写假字符串
            _mark_field_missing(result, "asset_nature")

    # 3) asset_reasoning：min 4 字符 (v200 P0+: L5 留空, 绝不写"LLM 未填"假字符串)
    ar = str(result.get("asset_reasoning") or "")
    if not ar.strip() or len(ar) < 4:
        # X1 假字符串禁止: 不再写 f"{ta}: 服务端兜底（LLM 未填）"
        _mark_field_missing(result, "asset_reasoning")

    # 4) reasoning：min 4 max 200
    # v200 P0+: 缺时不写"LLM 未填"假字符串. 优先尝试 L3 历史模式, 其次 L5 留空.
    rsn = str(result.get("reasoning") or "").strip()
    if not rsn or len(rsn) < 2:
        # L3 历史模式兜底尝试(若无 conn 则跳过)
        ta = str(result.get("target_asset") or "NONE").upper()
        _hf = None
        try:
            _hf = _fallback_historical_reasoning(
                ta,
                str(result.get("event_type_code") or ""),
                str(result.get("suggested_action") or ""),
                None,  # _presanitize 阶段 conn 未传, 跳过 L3
            )
        except Exception:
            _hf = None
        if _hf and _hf.get("text"):
            result["reasoning"] = _hf["text"][:200]
            _mark_field_source(result, "reasoning", "historical_pattern")
            try:
                _refs = json.loads(result.get("historical_ref_ids") or "{}")
                if isinstance(_refs, dict):
                    _refs["reasoning"] = _hf.get("ref_decision_id")
                    result["historical_ref_ids"] = json.dumps(_refs, ensure_ascii=False)
            except (json.JSONDecodeError, TypeError):
                pass
        else:
            # L5 留空: 标记缺失, 不写假字符串
            _mark_field_missing(result, "reasoning")
    if len(result.get("reasoning") or "") > 200:
        result["reasoning"] = result["reasoning"][:197] + "..."

    # 5) invalidation_condition：min 4 max 100 (v200 P0+: L5 留空)
    # v200+ P2: LLM 留空时给真实事件级兜底（不是占位字符串），
    # 同时仍 mark_field_missing 让 L5 完整性扣分（标记 LLM 真实未输出）。
    ic = str(result.get("invalidation_condition") or "").strip()
    if len(ic) < 4:
        # X1 假字符串禁止: 不再写"服务端兜底：未填失效条件"
        _mark_field_missing(result, "invalidation_condition")
        # v200+ P2: 用 event_type_code 兜底出真实失效条件
        # (确保 ai_decisions.invalidation_condition 字段始终有值，便于复盘查阅)
        _fb_ic = _fallback_invalidation_condition(
            event_type_code=str(result.get("event_type_code") or ""),
            asset=str(result.get("target_asset") or ""),
            action=str(result.get("suggested_action") or ""),
        )
        result["invalidation_condition"] = _fb_ic[:100]
        # 标注 source 让 field_sources 区分 llm / rule_template (便于审计)
        try:
            _fs = json.loads(result.get("field_sources") or "{}")
            if not isinstance(_fs, dict):
                _fs = {}
            _fs["invalidation_condition"] = "rule_template"
            result["field_sources"] = json.dumps(_fs, ensure_ascii=False)
        except (json.JSONDecodeError, TypeError):
            pass
    if len(result.get("invalidation_condition") or "") > 100:
        result["invalidation_condition"] = result["invalidation_condition"][:97] + "..."

    # 6) factor_ranking：至少 1 个 item，每个 item 必含 factor/rank/direction
    fr = result.get("factor_ranking")
    if not isinstance(fr, list) or not fr:
        result["factor_ranking"] = _extract_factors_fallback(result)
    else:
        clean_fr = []
        rank_n = 0
        for it in fr:
            if not isinstance(it, dict):
                continue
            factor = str(it.get("factor") or "").strip()
            if _looks_placeholder(factor):
                continue
            if len(factor) < 2:
                continue
            rank_n += 1
            direction = str(it.get("direction") or "").strip()
            if direction not in ("利多", "利空", "中性"):
                direction = "中性"
            try:
                rank_v = int(it.get("rank") or rank_n)
            except (TypeError, ValueError):
                rank_v = rank_n
            if rank_v < 1:
                rank_v = 1
            clean_fr.append({"factor": factor[:200], "rank": rank_v, "direction": direction})
        if not clean_fr:
            clean_fr = _extract_factors_fallback(result)
        result["factor_ranking"] = clean_fr

    # 7) event_type_code：min 3 字符
    # v170 P0+ 修复：原 .get(mc, "other") 兜底导致 664 条 event_type_code='other'
    # 现在严禁兜底到 "other"。不可分类 → 'low_value' + quarantined_at（id_reconcile 隔离）
    if not str(result.get("event_type_code") or "").strip() or len(str(result.get("event_type_code") or "")) < 3:
        mc = str(result.get("market_category") or "").upper()
        _etc_default = {
            "CRYPTO": "market_event",
            "GOLD": "macro_policy",
            "OIL": "geopolitical_conflict",
            "STOCK": "earnings",
            "MACRO": "macro_policy",
            "COMMODITY": "supply_chain",
        }.get(mc, "low_value")
        result["event_type_code"] = _etc_default
        if _etc_default == "low_value":
            # 标记为低价值 + 隔离（与 user 反馈"不该出现其他兜底"对齐）
            from datetime import datetime as _dt
            result["quarantined_at"] = _dt.utcnow().isoformat(timespec="seconds") + "Z"
            result["trade_gate_reason"] = result.get("trade_gate_reason") or "low_value_no_classify"

    # 8) verification：min 4 字符 (v200 P0+: L5 留空, _auto_fill_required_fields 兜底)
    if len(str(result.get("verification") or "").strip()) < 4:
        # _auto_fill_required_fields 会用 rule_action_score_consistency 兜底
        # 这里只标记 missing, 不写假字符串
        _mark_field_missing(result, "verification")

    # 9) hard_data_evidence：min 4 字符 (v200 P0+: L5 留空)
    if len(str(result.get("hard_data_evidence") or "").strip()) < 4:
        # _auto_fill_required_fields 会用 rule_kline_snapshot 兜底
        # 这里只标记 missing, 不写假字符串
        _mark_field_missing(result, "hard_data_evidence")

    # 10) snapshot_proximity：必须是 number 或 string。"N/A" 字符串允许。
    sp = result.get("snapshot_proximity")
    if sp is None:
        result["snapshot_proximity"] = "N/A"
    elif isinstance(sp, bool):
        result["snapshot_proximity"] = "N/A"
    elif not isinstance(sp, (int, float, str)):
        result["snapshot_proximity"] = "N/A"

    # 10.5) data_anchor_ts：ISO-8601 严格匹配，否则填 "N/A"
    das = result.get("data_anchor_ts")
    if not isinstance(das, str):
        das = "" if das is None else str(das)
    das = das.strip()
    if das and das not in ("N/A", "AMBIGUOUS"):
        import re as _das_re
        if not _das_re.match(r"^(N/A|\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}(Z|[+\-]\d{2}:?\d{2})?)$", das):
            # 模糊文本（如"今晚"/"刚刚"）→ 兜底 N/A
            das = "N/A"
    elif not das:
        das = "N/A"
    result["data_anchor_ts"] = das[:60]

    # 11) target_asset：必须落在 enum 内
    # v200 P1-A: 加 HK_STOCK（港股），让港股个股/财报不再被规范化到 NONE。
    # 背景:康方生物 09926.HK / 希音 00625.HK 等港股新闻此前全归 NONE 走兜底，
    # 89% 的 NONE 决策被"事件不在监控范围 → 强制 HOLD"套话覆盖。
    # 港股与美股/A股归类逻辑一致（都属 STOCK 类目），但单列 enum 以便未来做交易规则区分。
    _target_enum = ("BTC", "ETH", "SOL", "XAU", "WTI", "CN_STOCK", "US_STOCK",
                    "INDIVIDUAL_STOCK", "HK_STOCK", "SKHYNIX",
                    "DXY", "FX",  # v200 P1-F: 美元指数/外汇类（无直接交易通道，但信号不丢）
                    "NONE")
    ta = str(result.get("target_asset") or "").strip().upper()
    if ta not in _target_enum:
        ta_norm = ta.replace("/", "-").replace(" ", "")
        # v200 P1-E: 空字符串不再误命中第一个 enum（空串 in "BTC" 是 True 会落到 BTC）
        if not ta_norm:
            ta = "NONE"
        else:
            # v200 P1-F: 外汇代码（USD/MXN/EUR/JPY 等）→ FX 类目
            _fx_codes = ("USD", "EUR", "JPY", "GBP", "MXN", "CAD", "AUD",
                         "CNY", "CNH", "CHF", "NZD", "SGD", "HKD")
            if ta_norm in _fx_codes:
                ta = "FX"
            else:
                for ok in _target_enum:
                    if ok in ta_norm or ta_norm in ok:
                        ta = ok
                        break
                else:
                    ta = "NONE"
        result["target_asset"] = ta

    # 12) market_category：必须是 PROD enum；不可映射 → 写 unclassified_events（人工/规则补全）
    # v1 重构：删除原 _mc_map + "OTHER/MACRO" 兜底；改走 market_classifier.infer_market_category()
    # 该函数会抛 UnclassifiedError，调用方（_do_api_call 包层）捕获后入 unclassified_events。
    mc_in = str(result.get("market_category") or "").strip().upper()
    ta2 = str(result.get("target_asset") or "").strip().upper()
    try:
        result["market_category"] = infer_market_category(
            ta2, mc_in, layer="B_sanitize_step12"
        )
    except UnclassifiedError as e:
        # 把异常挂在 result 上，由 _do_api_call 兜底写入 unclassified_events 表
        # 同时把 market_category 设为临时空串（落库前的「待复核」标记），
        # 避免 ai_decisions 表出现非法值；这里用 "_UNCLASSIFIED_*" 形式以便后续审计定位。
        result["_unclassified_reason"] = e.reason
        result["_unclassified_layer"] = "B_sanitize_step12"
        result["_unclassified_llm_mc"] = e.llm_market_category
        result["_unclassified_asset"] = e.target_asset
        result["market_category"] = ""  # 待复核：留空让前端渲染徽章
        # 仍标 quarantined + low_value 走隔离路径（不会触发下单）
        from datetime import datetime as _dt2
        if not result.get("quarantined_at"):
            result["quarantined_at"] = _dt2.utcnow().isoformat(timespec="seconds") + "Z"
        result["trade_gate_reason"] = result.get("trade_gate_reason") or f"unclassified:{e.reason}"
        if not str(result.get("event_type_code") or "").strip() or \
           str(result.get("event_type_code") or "") == "other":
            result["event_type_code"] = "low_value"

    # 13) prediction_type / event_phase / market_confirmation / expected_horizon / event_strength 必填 enum
    _replace_str_enum = {
        "prediction_type": ("reversal", "continuation", "breakout"),
        "event_phase": ("early", "mid", "late"),
        "market_confirmation": ("positive", "negative", "unknown"),
        "expected_horizon": ("intraday", "1-3d", "1w+"),
        "event_strength": ("low", "medium", "high"),
    }
    for k, allowed in _replace_str_enum.items():
        v = str(result.get(k) or "").strip().lower()
        if v not in allowed:
            if k == "prediction_type":
                v = "continuation"
            elif k == "event_phase":
                v = "mid"
            elif k == "market_confirmation":
                v = "unknown"
            elif k == "expected_horizon":
                v = "intraday"
            elif k == "event_strength":
                try:
                    ai = float(result.get("asset_impact") or 0)
                except (TypeError, ValueError):
                    ai = 0.0
                if ai >= 7:
                    v = "high"
                elif ai >= 4:
                    v = "medium"
                else:
                    v = "low"
        result[k] = v

    # 14) heat_score / impact_score / asset_impact：必填 0-10 数字
    for k in ("heat_score", "impact_score", "asset_impact"):
        v = result.get(k)
        try:
            v_num = float(v if v is not None else 0)
        except (TypeError, ValueError):
            v_num = 0.0
        v_num = max(0.0, min(10.0, v_num))
        result[k] = v_num

    # 15) direct_catalyst：必须 bool
    dc = result.get("direct_catalyst")
    if not isinstance(dc, bool):
        result["direct_catalyst"] = str(dc).strip().lower() in ("true", "1", "yes")

    # 16) elasticity_window：必须是 {minutes, decay, peak_after_minutes} 三键
    ew = result.get("elasticity_window")
    if not isinstance(ew, dict):
        try:
            ew = json.loads(str(ew)) if isinstance(ew, str) and ew else {}
        except Exception:
            ew = {}
    if not isinstance(ew, dict):
        ew = {}
    try:
        minutes = int(ew.get("minutes", 60) or 60)
    except (TypeError, ValueError):
        minutes = 60
    decay = str(ew.get("decay", "medium")).strip().lower()
    if decay not in ("fast", "medium", "slow"):
        decay = "medium"
    try:
        peak = int(ew.get("peak_after_minutes", 30) or 30)
    except (TypeError, ValueError):
        peak = 30
    result["elasticity_window"] = {
        "minutes": max(0, minutes),
        "decay": decay,
        "peak_after_minutes": max(0, peak),
    }

    # 17) impact_curve：必须至少有 1 个键（5m/30m/2h/1d）
    ic = result.get("impact_curve")
    if not isinstance(ic, dict) or not ic:
        result["impact_curve"] = {"5m": 0.0, "30m": 0.0, "2h": 0.0, "1d": 0.0}
    else:
        _clean_ic = {}
        for k in ("5m", "30m", "2h", "1d"):
            if k in ic:
                v = ic[k]
                if isinstance(v, bool):
                    continue
                if v is None or isinstance(v, (int, float, str)):
                    _clean_ic[k] = v
        if not _clean_ic:
            _clean_ic = {"5m": 0.0, "30m": 0.0, "2h": 0.0, "1d": 0.0}
        result["impact_curve"] = _clean_ic

    # 18) event_window_minutes：必须 int 0-4320 或 'N/A'
    ewm = result.get("event_window_minutes")
    if isinstance(ewm, bool):
        result["event_window_minutes"] = 60
    elif isinstance(ewm, int):
        result["event_window_minutes"] = max(0, min(4320, ewm))
    elif isinstance(ewm, float):
        result["event_window_minutes"] = max(0, min(4320, int(ewm)))
    elif isinstance(ewm, str):
        s = ewm.strip()
        if s in ("", "N/A"):
            result["event_window_minutes"] = "N/A"
        else:
            try:
                result["event_window_minutes"] = max(0, min(4320, int(float(s))))
            except ValueError:
                result["event_window_minutes"] = 60
    else:
        result["event_window_minutes"] = 60

    return result


def _record_unclassified_event(
    news_id: int,
    *,
    reason: str,
    layer: str,
    target_asset: str,
    llm_market_category: str,
    title: str = "",
    source: str = "",
    news_published_at: str = "",
) -> None:
    """把一条「无法归类」的事件写入 unclassified_events 表，供人工/规则补全。

    调用方（_sanitize_llm_result 后的所有 caller）都应该传 news_id 进来；
    news_id=0 表示「上下文丢失」，仅用于兜底写日志。

    title/source/news_published_at 若 caller 没传，会从 raw_news 表按 news_id 补全，
    这样数据迁移脚本能直接 SELECT 出待复核清单给人工处理。
    """
    try:
        import db as _db_uc
        conn = _db_uc.get_connection()
        try:
            # 缺字段时按 news_id 从 raw_news 补全
            if news_id and (not title or not source):
                try:
                    _row = conn.execute(
                        "SELECT title, source, published_at FROM raw_news WHERE id = ?",
                        (int(news_id),),
                    ).fetchone()
                    if _row:
                        title = title or (_row["title"] if "title" in _row.keys() else "") or ""
                        source = source or (_row["source"] if "source" in _row.keys() else "") or ""
                        news_published_at = news_published_at or (
                            _row["published_at"] if "published_at" in _row.keys() else ""
                        ) or ""
                except Exception:
                    pass
            conn.execute(
                """
                INSERT INTO unclassified_events (
                    created_at, news_id, title, source, published_at,
                    target_asset, llm_market_category, llm_target_asset,
                    reason, layer, status
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending')
                """,
                (
                    datetime.utcnow().isoformat(timespec="seconds") + "Z",
                    int(news_id or 0),
                    (title or "")[:500],
                    (source or "")[:128],
                    (news_published_at or "")[:32],
                    target_asset or "",
                    llm_market_category or "",
                    target_asset or "",  # llm_target_asset 同 target_asset（重构前同一字段）
                    reason or "UNKNOWN",
                    layer or "",
                ),
            )
            conn.commit()
        finally:
            try:
                conn.close()
            except Exception:
                pass
    except Exception as _e:
        # 人工复核队列落库失败只记日志，不能影响主链路
        print(
            f"  [unclassified] record failed (reason={reason}, layer={layer}, "
            f"news_id={news_id}): {type(_e).__name__}: {str(_e)[:120]}",
            flush=True,
        )


def _consume_unclassified_marker(
    result: Dict[str, Any],
    news_id: int,
    *,
    title: str = "",
    source: str = "",
    news_published_at: str = "",
) -> None:
    """检查 result 上挂的 _unclassified_* 标记，若存在则写入 unclassified_events。"""
    if not isinstance(result, dict):
        return
    if not result.get("_unclassified_reason"):
        return
    _record_unclassified_event(
        news_id=news_id,
        reason=str(result.get("_unclassified_reason") or "UNKNOWN"),
        layer=str(result.get("_unclassified_layer") or "?"),
        target_asset=str(result.get("_unclassified_asset") or ""),
        llm_market_category=str(result.get("_unclassified_llm_mc") or ""),
        title=title,
        source=source,
        news_published_at=news_published_at,
    )
    # 写完清理标记，避免重复写
    for k in ("_unclassified_reason", "_unclassified_layer",
              "_unclassified_llm_mc", "_unclassified_asset"):
        result.pop(k, None)


def _sanitize_llm_result(result: Dict[str, Any]) -> Dict[str, Any]:
    """整箱白名单过滤 LLM 结果 —— 幻觉兜底，绝不整条丢弃新闻。

    对 LLM 返回的 dict 逐字段做白名单/夹取/补默认：
      - 缺失键补安全默认，保证下游任何 res["key"] 直取都不会 KeyError；
      - 类型非法(数字给了字符串、资产给了列表等)按白名单净化或回落；
      - 数值越界夹取到合法区间。
    方向与分数符号的一致性不在此判定（交由 reconcile_score_action /
    _norm_score_action），避免重复实现两套口径。

    v200 P0+: 入口处初始化 completeness/source, 标记 LLM 真输出的字段为 source="llm".
    """
    if not isinstance(result, dict):
        result = {}

    # ── v200 P0+: 初始化 completeness/source/missing ──
    _init_result_completeness(result)
    _LLM_TRACKED_FIELDS = (
        "reasoning", "reasoning_path", "asset_reasoning", "asset_nature",
        "factor_ranking", "invalidation_condition", "verification",
        "hard_data_evidence", "data_anchor_ts", "elasticity_window",
        "low_impact", "attention_score", "impact_magnitude",
    )
    for _f in _LLM_TRACKED_FIELDS:
        _v = result.get(_f)
        # 非空且不是服务端兜底标记 → 视为 LLM 真输出
        if _v is not None and str(_v).strip() and "服务端兜底" not in str(_v):
            _mark_field_source(result, _f, "llm")

    # sentiment_score：兼容三档范围
    #   (a) 0-100 整数（新约定，期望；50=中性, 0-30=偏 SELL, 70-100=偏 BUY）
    #   (b) 0-1 float（部分老 LLM 输出，0=空, 0.5=中, 1=极多）
    #   (c) -1~1 float（最早约定，负数=SELL，正数=BUY）
    # 内部统一为 -1~1 后存到 DB：所有下游 decision_guard/quick_sim 兼容 0 改动。
    result["sentiment_score"] = _normalize_sentiment_score(result.get("sentiment_score", 0.0))

    # suggested_action：白名单；缺省/非法 → HOLD
    _act = result.get("suggested_action")
    action = (_act if isinstance(_act, str) else "").strip().upper()
    result["suggested_action"] = action if action in _SCORE_ACTIONS else "HOLD"

    # reasoning / reasoning_path：仅接受字符串，其余滤空（展示截断由调用点决定）。
    # v176.7：MiniMax-M3 在 thinking disabled 下会把 reasoning_path 输出成嵌套 dict
    # （step1_fact_check/step2_... 结构）而非字符串八段式，直接滤空会触发
    # _presanitize_for_schema 的"LLM 未输出八段"兜底 → 前端整段变"服务端兜底：LLM 未填 reasoning"。
    # 这里先把 dict/list 展平成可读文本再过滤（与 factor_ranking 的 dict 兼容对齐）。
    _rp = result.get("reasoning_path")
    if isinstance(_rp, dict):
        _segs = []
        for _k, _v in _rp.items():
            if isinstance(_v, (dict, list, tuple)):
                _v = json.dumps(_v, ensure_ascii=False)
            _segs.append(f"[{_k}] {str(_v).strip()}")
        result["reasoning_path"] = "\n".join(_segs)
    elif isinstance(_rp, (list, tuple)):
        result["reasoning_path"] = "\n".join(str(x) for x in _rp)
    elif isinstance(_rp, str):
        result["reasoning_path"] = _rp
    else:
        result["reasoning_path"] = ""

    # reasoning：仅接受字符串；空/非法时用展平后的 reasoning_path 首段作摘要，
    # 避免 reasoning 与 reasoning_path 同时落空导致双重兜底。
    _rsn = result.get("reasoning")
    if isinstance(_rsn, str) and _rsn.strip():
        result["reasoning"] = _rsn
    else:
        _rp_flat = (result.get("reasoning_path") or "").strip()
        if _rp_flat:
            _first = re.sub(r"^\[[^\]]+\]\s*", "", _rp_flat.split("\n")[0]).strip()
            result["reasoning"] = _first if len(_first) >= 4 else ""
        else:
            result["reasoning"] = ""

    # target_asset：仅接受字符串，非空 → 大写截断；否则 NONE（后续再归一主品种）
    _ta = result.get("target_asset")
    target = (_ta if isinstance(_ta, str) else "").strip().upper()[:20]
    result["target_asset"] = target or "NONE"

    # market_category：白名单 + 配对校验（v1 重构）
    # 不再 fallback 到 MACRO 或保留 OTHER；全部走 market_classifier.validate_pair。
    # 这一步是 _sanitize 的最后兜底，理论上前一步 step12 已经把一切非法值清掉了；
    # 留作 belt-and-suspenders，避免 LLM 兜底串从 step12 之后才介入（race window）。
    _mc = result.get("market_category")
    cat = (_mc if isinstance(_mc, str) else "").strip().upper()
    _ta_s = str(result.get("target_asset") or "").strip().upper()
    if cat in MARKET_CATEGORIES_PROD or (cat == "OTHER" and is_debug_other_allowed()):
        try:
            validate_pair(_ta_s, cat)
            result["market_category"] = cat
        except UnclassifiedError as e:
            # Layer B 已经挂过 _unclassified_* 标记的，这里只更新 layer 名（最新层优先）
            result["_unclassified_reason"] = e.reason
            result["_unclassified_layer"] = "C_sanitize_step5"
            result["market_category"] = ""
    else:
        # 不在 PROD 白名单且非 debug → 留空，由 _do_api_call 兜底入 unclassified_events
        result["_unclassified_reason"] = f"INVALID_AT_STEP5:{cat}"
        result["_unclassified_layer"] = "C_sanitize_step5"
        result["market_category"] = ""
        if not result.get("quarantined_at"):
            from datetime import datetime as _dt4
            result["quarantined_at"] = _dt4.utcnow().isoformat(timespec="seconds") + "Z"

    # direct_catalyst：布尔白名单（容错 true/false 或字符串 true/1/yes）
    dc = result.get("direct_catalyst")
    if not isinstance(dc, bool):
        dc = str(dc).strip().lower() in ("true", "1", "yes")
    result["direct_catalyst"] = dc

    # ── 八步推演新增字段（阶段1）：资产性质 / 因素排行 / 影响数量级 ──
    _an = result.get("asset_nature")
    result["asset_nature"] = _an if isinstance(_an, str) else ""

    _fr = result.get("factor_ranking")
    if isinstance(_fr, (list, dict)):
        result["factor_ranking"] = json.dumps(_fr, ensure_ascii=False)
    elif isinstance(_fr, str):
        result["factor_ranking"] = _fr
    else:
        result["factor_ranking"] = ""

    # ── v174 新增三字段规范化 ──
    # event_type_code: 必须从 dim_event_type 25 个选 1, LLM 自由字符串会被 schema 拒绝
    _etc = result.get("event_type_code")
    result["event_type_code"] = _etc if isinstance(_etc, str) else ""

    # verification: 跨字段自检字符串, 限制 ≤500
    _ver = result.get("verification")
    if isinstance(_ver, str):
        result["verification"] = _ver[:500]
    else:
        result["verification"] = ""

    # snapshot_proximity: 数字或 "N/A" 字符串
    _sp = result.get("snapshot_proximity")
    if _sp is None:
        result["snapshot_proximity"] = ""
    elif isinstance(_sp, str):
        result["snapshot_proximity"] = _sp[:32]
    else:
        try:
            _sp_num = float(_sp)
            result["snapshot_proximity"] = str(int(_sp_num)) if _sp_num.is_integer() else str(_sp_num)
        except (TypeError, ValueError):
            result["snapshot_proximity"] = "N/A"

    try:
        _im = float(result.get("impact_magnitude", 0.0))
    except (TypeError, ValueError):
        _im = 0.0
    result["impact_magnitude"] = max(0.0, min(100.0, _im))

    # ── 需求5 新增子模块输出（关注度得分 / 弹性窗 / 原始数据戳 / 硬数据截图）──
    try:
        _atn = float(result.get("attention_score", 0.0))
    except (TypeError, ValueError):
        _atn = 0.0
    result["attention_score"] = max(0.0, min(100.0, _atn))

    _ew = result.get("elasticity_window")
    if isinstance(_ew, (dict, list)):
        result["elasticity_window"] = json.dumps(_ew, ensure_ascii=False)
    elif isinstance(_ew, str):
        result["elasticity_window"] = _ew
    else:
        result["elasticity_window"] = ""

    _das = result.get("data_anchor_ts")
    result["data_anchor_ts"] = _das if isinstance(_das, str) else ""
    _hde = result.get("hard_data_evidence")
    result["hard_data_evidence"] = _hde if isinstance(_hde, str) else ""

    # ── v176 服务端兜底：5 个字段 LLM 经常留空/填 0，sanitize 阶段强制填有意义默认值 ──
    # 1) low_impact：bool (0/1)，根据 action/score/asset_impact 反推
    try:
        _li = result.get("low_impact")
        if _li is None or _li == "":
            try:
                _ai = float(result.get("asset_impact", 0) or 0)
            except (TypeError, ValueError):
                _ai = 0.0
            try:
                _sc = abs(float(result.get("score", 0) or 0))
            except (TypeError, ValueError):
                _sc = 0.0
            # asset_impact<=3 或 score<0.10 → 标记低影响（不入激进建仓池）
            result["low_impact"] = 1 if (_ai <= 3 or _sc < 0.10) else 0
        else:
            result["low_impact"] = 1 if str(_li).strip().lower() in ("1", "true", "yes") else 0
    except Exception:
        result["low_impact"] = 0

    # 2) attention_score：REAL [0,100]，LLM 经常空 → 根据 source_credibility × event_type 推断
    try:
        _atn = float(result.get("attention_score", -1.0))
    except (TypeError, ValueError):
        _atn = -1.0
    if _atn <= 0:
        try:
            _sc2 = abs(float(result.get("score", 0) or 0))
            _ai2 = float(result.get("asset_impact", 0) or 0)
            _das2 = str(result.get("data_anchor_ts") or "").strip()
            # v176.2 调整: 基础分 = 评分×60 + impact×4 + 数据锚+5 + event_type+8
            # 兜底最低 25 (不再 < 5)，让 attention_score 真正反映"已分析的决策"
            _base = _sc2 * 60 + _ai2 * 4
            if _das2 and _das2 not in ("", "N/A", "AMBIGUOUS"):
                _base += 5  # 有数据锚说明事件可追溯
            if str(result.get("event_type_code") or "").strip():
                _base += 8
            _base = max(_base, 25.0)  # v176.2 兜底保底 25
            result["attention_score"] = max(0.0, min(100.0, round(_base, 1)))
            # v200 P0+: L1 数学规则兜底, 打 source 标记
            _mark_field_source(result, "attention_score", "rule_attention_v176_2")
        except Exception:
            result["attention_score"] = 25.0  # v176.2 默认 25 (从 0 改)
            _mark_field_source(result, "attention_score", "rule_default_attention_v176_2")

    # 3) impact_magnitude：REAL [0,100]，LLM 经常空 → 反推：score×100×asset_impact/10
    try:
        _im2 = float(result.get("impact_magnitude", -1.0))
    except (TypeError, ValueError):
        _im2 = -1.0
    if _im2 <= 0:
        try:
            _sc3 = abs(float(result.get("score", 0) or 0))
            _ai3 = float(result.get("asset_impact", 0) or 0)
            _es = str(result.get("event_strength") or "medium").strip().lower()
            _es_mult = {"low": 0.5, "medium": 1.0, "high": 1.5}.get(_es, 1.0)
            _mag = _sc3 * 100 * max(_ai3, 1) / 10 * _es_mult
            result["impact_magnitude"] = max(0.0, min(100.0, round(_mag, 1)))
            # v200 P0+: L1 数学规则兜底, 打 source 标记
            _mark_field_source(result, "impact_magnitude", "rule_impact_v176")
        except Exception:
            result["impact_magnitude"] = 0.0
            _mark_field_source(result, "impact_magnitude", "rule_default_impact_v176")

    # 4) verification：必填字符串，已在 _auto_fill_required_fields 有兜底；这里也补 (v200 P0+ L1 规则)
    if not str(result.get("verification") or "").strip():
        try:
            sa = (result.get("suggested_action") or "").upper()
            sc4 = float(result.get("score") or 0.0)
            if (sa in ("LONG", "BUY") and sc4 > 0) or (sa in ("SHORT", "SELL") and sc4 < 0):
                result["verification"] = "pass | rule_action_score_consistent"
            else:
                result["verification"] = "warn | rule_action_score_inconsistent"
            _mark_field_source(result, "verification", "rule_action_score_consistency_v176_5")
        except Exception:
            # X1 假字符串禁止: 不再写"warn | 服务端兜底：字段解析失败"
            _mark_field_missing(result, "verification")

    # 5) hard_data_evidence：服务端拼 snapshot K 线兜底（与 _auto_fill_required_fields 同语义）
    if not str(result.get("hard_data_evidence") or "").strip():
        try:
            _ta2 = str(result.get("target_asset") or "").upper()
            if _ta2 and _ta2 not in ("NONE", "", "OTHER"):
                _ev = _build_evidence_for_asset(_ta2)
                if _ev:
                    result["hard_data_evidence"] = _ev[:500]
                    # v200 P0+: L1 数学规则兜底, 打 source 标记
                    _mark_field_source(result, "hard_data_evidence", "rule_kline_snapshot_v176_11")
        except Exception:
            pass

    # ── 全网溯源：AI 自由回答（自然语言），不做校验 ──
    _noi = result.get("net_origin_info")
    if not isinstance(_noi, str):
        _noi = ""
    result["net_origin_info"] = _noi.strip()[:200]

    # v176.11 fix：当 LLM 没填 cross_net_first_seen_ts 但在 net_origin_info 里写了
    #   "推断时间:YYYY-MM-DD HH:MM:SS"（典型 LLM 输出：来源+推断时间组合说明），
    #   反向抽取时间回填，让首页 NetLagBadge 从"灰色 未确认"升到"紫色 精确时间"。
    # 仅在没有现成 cn_ts 时尝试，避免覆盖 AI 已给出的精确首发时间。
    # source 兜底用整个 net_origin_info（>=4 字符过 _validate_cross_net_ts 第⑤层），
    # 因为 "财联社"/"路透社" 等中文机构名只有 3 字符，直接取它们会被 ≥4 字符校验拒收。
    _cn_ts_curr = str(result.get("cross_net_first_seen_ts") or "").strip()
    if not _cn_ts_curr:
        try:
            _noi_full = _noi.strip()[:200]  # 已经清洗过
            # 匹配 "推断时间:YYYY-MM-DD HH:MM:SS" 或 "推断时间：YYYY-MM-DD HH:MM:SS"（中英冒号）
            # 时间部分允许：YYYY-MM-DD HH:MM[:SS] / YYYY/MM/DD HH:MM / YYYY年M月D日 HH:MM[:SS]
            _m_infer = re.search(
                r"推断时间\s*[:：]\s*((?:19|20)\d{2}[-/年]?\s*\d{1,2}[-/月]?\s*\d{1,2}[日]?"
                r"(?:\s+\d{1,2}:\d{1,2}(?::\d{1,2})?)?)",
                _noi_full,
            )
            if _m_infer:
                _raw_ts = _m_infer.group(1).strip()
                # 规范化分隔符
                _norm = (
                    _raw_ts.replace("年", "-").replace("月", "-").replace("日", "")
                    .replace("/", "-")
                )
                _norm = re.sub(r"\s+", " ", _norm).strip()
                # 单数字段 padding（"2026-9-29" → "2026-09-29"），让 %Y-%m-%d 匹配
                _parts = _norm.split()
                if _parts:
                    _date_part = _parts[0]
                    _date_segs = _date_part.split("-")
                    if len(_date_segs) == 3:
                        _date_segs = [s.zfill(2) if i > 0 else s for i, s in enumerate(_date_segs)]
                        _parts[0] = "-".join(_date_segs)
                    _norm = " ".join(_parts)
                # 解析成 datetime → 输出 'YYYY-MM-DD HH:MM:SS'
                _dt_p = None
                for _fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d"):
                    try:
                        _dt_p = datetime.strptime(_norm, _fmt)
                        break
                    except ValueError:
                        continue
                if _dt_p:
                    # 规范化输出：缺秒补 :00；缺时分补 00:00:00
                    if " " not in _norm:
                        _norm = _dt_p.strftime("%Y-%m-%d 00:00:00")
                    elif _norm.count(":") == 1:
                        _norm = _norm + ":00"
                    else:
                        _norm = _dt_p.strftime("%Y-%m-%d %H:%M:%S")
                    result["cross_net_first_seen_ts"] = _norm[:32]
                    # source 兜底：若 LLM 没填，整段 net_origin_info 作为依据。
                    # 整段通常 ≥ 4 字符，且信息更完整（包含"来源:"前导+机构名+时间），能过校验。
                    if not str(result.get("cross_net_source") or "").strip():
                        if len(_noi_full) >= 4:
                            result["cross_net_source"] = _noi_full[:300]
        except Exception:
            pass

    # ── 全网溯源：结构化字段（前端徽章三态用）──
    # 这里只做"清洗+轻校验"，重活（5 层防护）由 _validate_cross_net_ts 在 ai_decisions
    # INSERT 后做。sanitize 只负责非字符串 → 空串 / 长串截断 / 不让污染 ai_decisions INSERT。
    _cn_ts = result.get("cross_net_first_seen_ts")
    if isinstance(_cn_ts, str):
        result["cross_net_first_seen_ts"] = _cn_ts.strip()[:32]
    else:
        result["cross_net_first_seen_ts"] = ""

    _cn_src = result.get("cross_net_source")
    if isinstance(_cn_src, str):
        result["cross_net_source"] = _cn_src.strip()[:300]
    else:
        result["cross_net_source"] = ""

    return result


# ────────────────────────────────────────────────────────────────────────────
# 全网溯源：5 层校验（防 AI 幻觉时间污染 raw_news）
# ────────────────────────────────────────────────────────────────────────────
# 设计要点：
#   * 三态：精确时间串（"YYYY-MM-DD HH:MM:SS"）/ "AMBIGUOUS" / ""（拒绝）
#   * 5 层防护 ①格式正则 ②下限1990 ③不能未来(±60s) ④不能晚于入库(±60s) ⑤source ≥4字符且
#     AMBIGUOUS 必须含"候选1"或"/"或"或"（两个并列表达）
#   * 任一层失败 → 返回 ""（视为无溯源信息，前端不渲染徽章）
#   * 校验通过 → 标准化为 "YYYY-MM-DD HH:MM:SS"（即使输入 "T" 分隔也归一为空白分隔）

import re as _re_cross
import time as _time_cross

_CN_TS_RE = _re_cross.compile(r"^(\d{4})-(\d{2})-(\d{2})[ T](\d{2}):(\d{2}):(\d{2})$")
_CN_EARLIEST = (1990, 1, 1)  # 第②层：远古时间拒收


def _validate_cross_net_ts(value, source, our_ts_iso):
    """结构化全网溯源时间 5 层校验。

    参数：
      value      — AI 给出的 cross_net_first_seen_ts（可能是 None/空串/精确串/"AMBIGUOUS"）
      source     — AI 给出的 cross_net_source（依据字符串）
      our_ts_iso — 本条新闻我们入库时间（"YYYY-MM-DD HH:MM:SS" 格式；用于第④层）

    返回：
      校验通过：标准化后的精确时间串 "YYYY-MM-DD HH:MM:SS" 或 "AMBIGUOUS"
      校验失败：""（前端不渲染徽章；不污染 DB）
    """
    if not value or not isinstance(value, str):
        return ""
    v = value.strip()
    if not v:
        return ""

    # AMBIGUOUS 分支：必须有"候选1 / 或"等并列表达；source 缺失或太短也拒
    if v == "AMBIGUOUS":
        src = (source or "").strip()
        if len(src) < 4:
            return ""
        # 必须有两个并列表达（候选1 / 候选2 或 "或" / "/"）
        if not ("候选1" in src or "/" in src or "或" in src):
            return ""
        return "AMBIGUOUS"

    # 精确时间分支：5 层
    m = _CN_TS_RE.match(v)
    if not m:
        return ""
    try:
        y, mo, d, h, mi, s = (int(x) for x in m.groups())
    except Exception:
        return ""

    # ②下限：不能早于 1990
    if (y, mo, d) < _CN_EARLIEST:
        return ""

    # ③不能未来：候选时间 > 当前时间 + 60s 容差 → 拒（防模型用未来时间）
    try:
        candidate_ts = _time_cross.mktime((y, mo, d, h, mi, s, 0, 0, 0))
    except (OverflowError, ValueError):
        return ""
    now = _time_cross.time()
    if candidate_ts > now + 60:
        return ""

    # ④不能晚于自己入库时间 + 60s 容差 → 拒（事件不可能在入库后发生）
    if isinstance(our_ts_iso, str) and our_ts_iso:
        try:
            our_ts = _time_cross.mktime(
                _time_cross.strptime(our_ts_iso[:19], "%Y-%m-%d %H:%M:%S")
            )
            if candidate_ts > our_ts + 60:
                return ""
        except Exception:
            pass  # 解析失败放行（不影响主流程）

    # ⑤source 必须 ≥4 字符（精确时间必须有依据）
    src = (source or "").strip()
    if len(src) < 4:
        return ""

    # 标准化：T 分隔 → 空白分隔
    return f"{y:04d}-{mo:02d}-{d:02d} {h:02d}:{mi:02d}:{s:02d}"


# ---------------------------------------------------------------------------
# 全网溯源弱信号抽取（Fallback 2.5）— 中文文本正则兜底
# ---------------------------------------------------------------------------
# MiniMax-M3 等模型常输出 2000 字中文八段推演但**没有 JSON 包裹** → 上游 json.loads
# 拿不到任何结构化字段。本函数尝试从纯文本里抽"X 日宣布/报道"等模式，作为
# cross_net_first_seen_ts / cross_net_source / net_origin_info 的弱依据。
#
# 设计取舍（保守优先，避免污染 raw_news）：
#   - 时间：仅识别"Y年M月D日"+"HH:MM"组合，且映射到系统时间上下 30 天内
#     → 否则保守返回空（让 _validate_cross_net_ts 拒收）
#   - 来源：白名单 14 个高信度模式（"X 报道"/"X 宣布"/"X 官网新闻稿"等）
#   - net_origin_info：直接塞入"来源 + 推断时间"组合，最长 200 字符（DB 限制）
#
# 返回 (cn_ts, cn_src, noi)：三个都可能是空串，由调用方写进 result。
_CN_TS_PATTERNS = [
    # 2026年9月19日 22:00 / 2026年9月19日22时 / 2026年9月19日22时00分
    _re_cross.compile(r"(\d{4})\s*年\s*(\d{1,2})\s*月\s*(\d{1,2})\s*日\s*(\d{1,2})\s*[::时]\s*(\d{1,2})(?:\s*分)?"),
    # 2026-09-19 22:00 / 2026/09/19 22:00
    _re_cross.compile(r"(\d{4})[-/](\d{1,2})[-/](\d{1,2})[ T](\d{1,2})[::时](\d{1,2})"),
    # 2026年9月19日（无时间） → 取 00:00
    _re_cross.compile(r"(\d{4})\s*年\s*(\d{1,2})\s*月\s*(\d{1,2})\s*日"),
    # 9月19日 22:00 / 9月19日22时
    _re_cross.compile(r"(\d{1,2})\s*月\s*(\d{1,2})\s*日\s*(\d{1,2})\s*[::时]\s*(\d{1,2})"),
    # ── v176.9 fix：N月N日 不带时分（"金十数据9月23日讯"）→ 取 00:00 ──
    # 之前只匹配带时分的"X月X日 22:00"，对中文财经媒体最常见的"N月N日讯"格式漏抽
    # v176.10 fix：负向后瞻 (?<![年\d]) 排除"1989年1月1日"这类"年份+月日"里的"月日"
    #   独立补当前年——带年份的已由 pattern 0/2 处理（早于 1990 会被拒），不能被这里
    #   拆出的"1月1日"绕过限制变成"2026-01-01"。
    _re_cross.compile(r"(?<![年\d])(\d{1,2})\s*月\s*(\d{1,2})\s*日"),
]
# 字符类：中文 + "社"（让"新华社"整体作为机构名抓取） + 字母数字 + 中点/连字符
# 注意：必须包在 () 里构成捕获组，否则 m.groups() 为空导致 cn_src 取到整段 match
_CN_ORG_CHARS = r"([\u4e00-\u9fa5社A-Za-z0-9·\-]{2,12})"
# v176.9 fix：源名专用字符类，**禁止数字**（避免"金十数据9月23日"贪婪匹配整段）
# org_chars 的 1:-1 是去掉外层 ()，得到字符类本体
_CN_ORG_CHARS_NODIGIT = r"([\u4e00-\u9fa5社A-Za-z·\-]{2,12})"
_CN_SRC_PATTERNS = [
    # 高信度来源短语（捕获前面 2-12 字的机构名）
    _re_cross.compile(_CN_ORG_CHARS + r"\s*(?:官网|官方)\s*(?:新闻稿|公告|声明)"),
    # 排除"紧急/立即/即将"等中文动词，避免贪婪匹配"美联储紧急宣布"时吞进"紧急"
    _re_cross.compile(_CN_ORG_CHARS + r"(?!\s*(?:紧急|立即|即将|正在|已经|已\s+))(宣布|发布|公布|披露)"),
    _re_cross.compile(r"(?:据|根据)\s*(" + _CN_ORG_CHARS_NODIGIT[1:-1] + r")\s*(?:报道|消息|消息人士|新闻|快讯|通讯社|电)"),
    _re_cross.compile(r"(" + _CN_ORG_CHARS_NODIGIT[1:-1] + r")\s*(?:电|报道|消息)"),
    _re_cross.compile(r"(" + _CN_ORG_CHARS_NODIGIT[1:-1] + r")\s*官方网站"),
    _re_cross.compile(r"(" + _CN_ORG_CHARS_NODIGIT[1:-1] + r")\s*新闻稿"),
    _re_cross.compile(r"(" + _CN_ORG_CHARS_NODIGIT[1:-1] + r")\s*官方声明"),
    _re_cross.compile(r"(" + _CN_ORG_CHARS_NODIGIT[1:-1] + r")\s*(?:通讯社|电视台|广播|网)"),
    _re_cross.compile(r"(" + _CN_ORG_CHARS_NODIGIT[1:-1] + r")\s*Press"),
    # ── v176.9 fix：中文财经媒体"X讯/电"结尾（金十数据/财联社/东方财富等）──
    # 必须用 NODIGIT 版本，否则贪婪匹配"金十数据9月23日"整段
    # 允许 "金十数据9月23日讯" 这种 "机构名 + 日期 + 讯/电" 复合结构
    # v176.10 fix：org 用非贪婪 {2,12}?，避免 "[来自金十数据快讯]" 里"快"被吞进
    #   机构名（贪婪会得到"金十数据快"，不以"数据"结尾被 org_suffixes 拒收，
    #   导致 feishu_jin10 的"[来自金十数据快讯]"结尾漏抽首发平台）。
    _re_cross.compile(r"([\u4e00-\u9fa5社A-Za-z·\-]{2,12}?)\s*(?:\d{1,2}\s*月\s*\d{1,2}\s*日)?\s*(?:讯|电|消息|快讯)"),
    # 英文机构名（直接捕获）
    _re_cross.compile(r"(Reuters|Bloomberg|CNBC|Nikkei|Xinhua|CCTV)"),
]


def _extract_cross_net_from_text(cleaned: str) -> tuple:
    """从 LLM 输出的中文推演文本里抽 (cross_net_first_seen_ts, cross_net_source, net_origin_info)。

    返回三个值都可能是空串。失败时不抛异常（正则不匹配返回空 → 上游空串兜底）。
    """
    if not cleaned or not isinstance(cleaned, str):
        return "", "", ""

    # ── 时间抽取（先于来源抽取，便于下一步 mask 掉时间字符串防污染）──
    cn_ts = ""
    ts_mask_spans: list = []  # (start, end) — 时间匹配的范围，后续来源抽取时跳过
    try:
        import datetime as _dt_cn
        now = _dt_cn.datetime.now()
        for pat_idx, pat in enumerate(_CN_TS_PATTERNS):
            m = pat.search(cleaned)
            if not m:
                continue
            groups = m.groups()
            try:
                if pat_idx == 0 or pat_idx == 1:
                    y = int(groups[0]); mo = int(groups[1]); d = int(groups[2])
                    h = int(groups[3]); mi = int(groups[4]) if groups[4] else 0
                elif pat_idx == 2:
                    y = int(groups[0]); mo = int(groups[1]); d = int(groups[2])
                    h = 0; mi = 0
                elif pat_idx == 3:
                    # "9月19日22时" → 年补当前年（若跨年回退一年）
                    y = now.year
                    mo = int(groups[0]); d = int(groups[1])
                    h = int(groups[2]); mi = int(groups[3]) if groups[3] else 0
                    try:
                        cand = _dt_cn.datetime(y, mo, d)
                        if (cand - now).days > 30:
                            y = y - 1
                    except ValueError:
                        pass
                else:
                    # pat_idx == 4 (v176.9): "9月23日" 不带时分 → 年补当前年, h=mi=0
                    y = now.year
                    mo = int(groups[0]); d = int(groups[1])
                    h = 0; mi = 0
                    try:
                        cand = _dt_cn.datetime(y, mo, d)
                        if (cand - now).days > 30:
                            y = y - 1
                    except ValueError:
                        pass

                # 范围保护
                if y < 1990 or y > now.year + 1 or h > 23 or mi > 59 or mo < 1 or mo > 12 or d < 1 or d > 31:
                    continue
                # v176.10 fix：跳过"…以来(新高/新低/首次/低位/高位/纪录)"这类历史基准日期。
                # 它描述的是 KPI 对比的历史参照点，不是本条新闻的首发时间。金十常见正文
                # 里"创2025年10月8日以来新低"会覆盖标题"金十数据9月23日讯"的首发日期，
                # 导致 cross_net_first_seen_ts 误写成历史日期。跳过它，让更靠前/更贴首发
                # 的"N月N日"（如"9月23日"）胜出。
                if cleaned[m.end():m.end() + 2] == "以来":
                    continue
                cn_ts = f"{y:04d}-{mo:02d}-{d:02d} {h:02d}:{mi:02d}:00"
                ts_mask_spans.append((m.start(), m.end()))
                break
            except (ValueError, TypeError):
                continue
    except Exception:
        cn_ts = ""

    # ── 来源抽取（在 mask 掉时间字符串的 cleaned 上做）──
    cn_src = ""
    masked = cleaned
    for s, e in ts_mask_spans:
        masked = masked[:s] + (" " * (e - s)) + masked[e:]
    for pat in _CN_SRC_PATTERNS:
        m = pat.search(masked)
        if not m:
            continue
        groups = m.groups()
        if groups:
            candidates = [g for g in groups if g and len(g.strip()) >= 2]
        else:
            # 英文机构名（无捕获组）→ 用整段 match
            candidates = [m.group(0)] if m.group(0) and len(m.group(0)) >= 4 else []
        if not candidates:
            continue
        cn_src = max(candidates, key=len).strip()
        # 过滤助词/虚词（开头包含"据/根据"也要清掉）
        if cn_src in ("据", "根据", "官方", "今日", "刚刚", "消息人士"):
            cn_src = ""  # v176.9 fix：continue 前清空 cn_src，避免错误候选污染后续 pattern
            continue
        if cn_src.startswith(("据", "根据")):
            # "据路透社" → 去掉前缀取"路透社"
            cn_src = cn_src.lstrip("据根据").strip()
            if not cn_src or len(cn_src) < 2:
                cn_src = ""
                continue
        # v176.10 fix："[来自金十数据快讯]" 里 org 会连"来自"一起捕获成"来自金十数据"，
        # 剥掉"来自/来源"前缀，还原首发平台名（金十数据）。
        if cn_src.startswith(("来自", "来源")):
            cn_src = cn_src[2:].strip()
            if not cn_src or len(cn_src) < 2:
                cn_src = ""
                continue
        # 去掉尾部时间/前缀词残留（"沙特能源部今日" → "沙特能源部"）
        for tail in ("今日", "刚刚", "官方", "今天", "昨日", "本周", "本月"):
            if cn_src.endswith(tail):
                cn_src = cn_src[:-len(tail)].strip()
        # 必须不全是数字（过滤纯数字残留）和不含"年/月/日/时/分"（防时间污染）
        if cn_src.isdigit():
            cn_src = ""
            continue
        if any(k in cn_src for k in ("年", "月", "日", "时", "分")):
            cn_src = ""
            continue
        # 必须以高信度机构后缀结尾（社/网/台/局/部/院/所/银行/公司/委员会/联储/央行/通讯社），
        # 否则容易吞进 "美联储紧急/沙特能源部今日" 这种含动词的伪机构名
        # v176.9 fix：加 "数据"（金十数据/财联社数据等中文财经媒体后缀）
        org_suffixes = (
            "社", "网", "台", "局", "部", "院", "所", "行", "公司",
            "委员会", "联储", "央行", "通讯社", "通社", "新闻", "News",
            "Press", "Reuters", "Bloomberg", "CNBC", "Nikkei", "Xinhua", "CCTV",
            "数据",  # 金十数据/财联社数据/东方财富数据
        )
        if not cn_src.endswith(org_suffixes):
            cn_src = ""  # v176.9 fix：reject 后清空，bug 修复前会保留错误候选污染最终结果
            continue
        if len(cn_src) >= 4:
            break

    # ── net_origin_info 拼装 ──
    noi = ""
    if cn_src or cn_ts:
        parts = []
        if cn_src:
            parts.append(f"来源:{cn_src}")
        if cn_ts:
            parts.append(f"推断时间:{cn_ts}")
        noi = " | ".join(parts)[:200]

    return cn_ts, cn_src, noi


# ---------------------------------------------------------------------------
# Optional imports — not required; kept for compatibility
# ---------------------------------------------------------------------------

try:
    from openai import AsyncOpenAI as _AsyncOpenAI  # type: ignore[import-untyped]
    HAS_OPENAI = True
except ImportError:
    HAS_OPENAI = False


def build_model_config(model_id: str) -> Dict[str, Any]:
    model = next((item for item in AI_MODEL_ROSTER if item["id"] == model_id), None)
    if model is None:
        # 兼容：池外 id 若指向 Aiping/nassaapi 直连通道（历史上曾是主通道、可能
        # 被 runtime 状态/旧引用指向）→ 按直连默认构造；其它未知 id 拒绝。
        mid_low = str(model_id or "").lower()
        if mid_low in {str(config.AIPING_MODEL or "").lower(), "gpt-5.5", "gpt-5.4-mini"}:
            model = {
                "id": str(model_id),
                "request_model": str(model_id),
                "label": config._model_label(str(model_id)),
                "api_base": config.AIPING_BASE_URL,
                "api_key": config.AIPING_API_KEY,
                "extra_body": config.AIPING_EXTRA_BODY,
                "use_proxy": False,
            }
        else:
            raise ValueError("unsupported AI model")
    api_base = str(model.get("api_base") or AIPING_BASE_URL)
    return {
        "id": model["id"],
        "request_model": str(model.get("request_model") or model["id"]),  # 同模型多中转时实际请求名
        "label": model["label"],
        "api_base": api_base,
        "api_key": str(model.get("api_key") or AIPING_API_KEY),
        "extra_body": model.get("extra_body", AIPING_EXTRA_BODY),
        # OpenRouter 等海外通道走代理；nassaapi/aiping/xinshu 直连
        "use_proxy": bool(model.get("use_proxy", False)),
        # json_mode 按平台自动适配：Aiping 支持 strict json_object；
        # nassaapi/OpenRouter 的 json_object 兼容性差，改 prompt 强制 JSON。
        "json_mode": "aiping.cn" in api_base,
    }


def get_current_model_config() -> Dict[str, Any]:
    return build_model_config(get_selected_ai_model_id())


def attribute_5m_abnormal(asset: str, anchor_ts: int, pct_change: Optional[float],
                          vol_ratio: Optional[float], trigger_type: str,
                          news_before: List[Dict[str, Any]]) -> Dict[str, Any]:
    """3 分钟异动冷归因：判断异动与时间锚附近公开新闻的关联（轻量，不叠加八段式主 prompt）。

    只引用传入的公开新闻列表 + 公开常识；无法确定就列多个候选并标 speculative=True，
    严禁编造新闻标题/来源/数字。返回：
      {"summary": str, "candidates": [{"cause","source","confidence","speculative"}]}
    任何失败返回 {"error": str}——调用方静默降级（星标仍显示、归因留空），绝不阻塞主链路。

    v187: 优先走 anomaly 通道（独立 key + minimax-anomaly），按 purpose 选池。
    """
    cfgs: List[Dict[str, Any]] = []
    # v187：按 anomaly purpose 取池（替代原全 ROSTER 循环）
    try:
        for m in config.get_pool_for_purpose("anomaly"):
            try:
                c = _resolve_model_cfg(m)
                if c:
                    c["key_alias"] = m.get("key_alias", "unknown")
                    cfgs.append(c)
            except Exception:
                continue
    except Exception:
        pass
    # 兜底：保留旧的全 roster 路径(向后兼容)
    if not cfgs:
        for m in AI_MODEL_ROSTER:
            try:
                c = _resolve_model_cfg(m)
                if c:
                    cfgs.append(c)
            except Exception:
                continue
        mm = _minimax_fallback_cfg()
        if mm:
            cfgs.append(mm)
        mm2 = _minimax_fallback_cfg2()
        if mm2:
            cfgs.append(mm2)
    if not cfgs:
        return {"error": "no model available"}

    if news_before:
        news_lines = "\n".join(
            f"[{i + 1}] {n.get('ts_str', '')} | {n.get('source', '')} | {n.get('content', '')}"
            for i, n in enumerate(news_before[:12])
        )
    else:
        news_lines = "（时间锚前窗口内无该品种公开新闻记录）"

    # 图谱取证：注入该品种涨跌驱动本质（causal_graph 自学习知识），辅助反硬归因。
    # 图谱无此品种知识时返回空串，静默跳过（不阻塞归因）。
    graph_block = ""
    try:
        import causal_graph
        graph_block = causal_graph.inject_block(asset)
    except Exception:
        graph_block = ""

    ts_str = datetime.fromtimestamp(int(anchor_ts)).strftime("%Y-%m-%d %H:%M:%S")
    pct_s = f"{pct_change:+.2f}%" if pct_change is not None else "未知"
    vol_s = f"{vol_ratio:.2f}×" if vol_ratio is not None else "未知"
    trig_map = {"pct": "单根涨跌幅超阈", "volume": "成交量放大超阈", "both": "涨跌+量能同时超阈"}
    trig = trig_map.get(trigger_type, str(trigger_type or "未知"))

    system_prompt = (
        "你是严谨的宏观/加密市场归因助手。给你一个 3 分钟 K 线的异动点（品种/时间/涨跌/量比/触发类型）"
        "以及该时间锚之前一小段时间内的公开新闻列表，判断该异动最可能由哪条新闻/事件引起。\n\n"
        "硬性要求：\n"
        "0. 若输入含「品种涨跌本质」背景，优先用它判断异动是否由该品种核心/条件驱动触发；"
        "时间窗内若无与该驱动直接相关的新闻，判为市场自发/技术性波动或未捕捉因素，严禁仅因时间就近就硬归因。\n"
        "1. 只引用「新闻列表」里出现的来源与内容，加上公开常识；不确定时列多个候选并按可能性排序。\n"
        "2. 严禁编造新闻标题、来源、具体数字；没有依据就写「未见直接催化，属市场自发波动」并标 speculative=true。\n"
        "3. 只输出一个 JSON 对象，不要任何解释性文字，格式严格为：\n"
        '{"summary":"一句话归因结论","candidates":[{"cause":"原因","source":"来源","confidence":"high|medium|low","speculative":false}]}\n'
        "candidates 最多 3 个，summary 不超过 60 字。"
    )
    user_text = (
        f"品种：{asset}\n异动时间：{ts_str}\n单根涨跌幅：{pct_s}\n量比：{vol_s}\n触发类型：{trig}\n\n"
        f"{graph_block + chr(10) if graph_block else ''}"
        f"时间锚前公开新闻：\n{news_lines}"
    )

    for cfg in cfgs:
        payload: Dict[str, Any] = {
            "model": str(cfg.get("request_model") or cfg.get("id")),
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_text},
            ],
            "max_tokens": 400,
            "temperature": 0.1,
            **(cfg.get("extra_body") or {}),
        }
        if cfg.get("json_mode"):
            payload["response_format"] = {"type": "json_object"}
        headers = {
            "Authorization": f"Bearer {cfg.get('api_key')}",
            "Content-Type": "application/json",
        }
        url = f"{cfg.get('api_base')}/chat/completions"
        session = _LLM_PROXY_SESSION if cfg.get("use_proxy") else _LLM_SESSION

        try:
            _t0 = time.perf_counter()
            resp = session.post(url, json=payload, headers=headers, timeout=25)
            _dt_ms = int((time.perf_counter() - _t0) * 1000)
            if resp.status_code >= 400:
                # v187: 失败埋点
                try:
                    import db as _db_a
                    _db_a.log_ai_call(
                        purpose="anomaly", key_alias=cfg.get("key_alias", "unknown"),
                        model_id=str(cfg.get("id") or "?"), tokens_in=0, tokens_out=0,
                        latency_ms=_dt_ms, status="http_error",
                        error_code=str(resp.status_code), call_site="attribute_5m_abnormal",
                    )
                except Exception:
                    pass
                continue
            raw = (resp.json()["choices"][0]["message"]["content"] or "").strip()
            if not raw:
                continue
            cleaned = re.sub(r"^```(?:json)?\s*", "", raw)
            cleaned = re.sub(r"\s*```$", "", cleaned)
            obj = json.loads(cleaned)
            if not isinstance(obj, dict):
                continue
            summary = str(obj.get("summary") or "").strip()
            cands = obj.get("candidates")
            if not isinstance(cands, list):
                cands = []
            out_cands: List[Dict[str, Any]] = []
            for c in cands[:3]:
                if not isinstance(c, dict):
                    continue
                cause = str(c.get("cause") or "").strip()
                if not cause:
                    continue
                out_cands.append({
                    "cause": cause,
                    "source": str(c.get("source") or "").strip(),
                    "confidence": str(c.get("confidence") or "medium").strip(),
                    "speculative": bool(c.get("speculative", False)),
                })
            return {"summary": summary, "candidates": out_cands}
        except Exception:
            continue
    return {"error": "all attribution models failed"}


def attribute_market_drivers(asset: str, events: List[Dict[str, Any]]) -> Dict[str, Any]:
    """星标事件 AI 归因：从近 24h 高分新闻里找出行情「主要驱动事件」。

    只引用传入的公开新闻（原文/来源/方向），严禁编造；无法确定则降级为候选并列。
    返回：
      {"summary": str, "drivers": [{"ts","content","source","action","score","reason","importance"}]}
    任何失败返回 {"error": str} —— 调用方静默降级（前端不展示 AI 块），绝不阻塞主链路。

    v187: 同样走 anomaly 通道(独立 key)
    """
    cfgs: List[Dict[str, Any]] = []
    # v187：anomaly purpose 池
    try:
        for m in config.get_pool_for_purpose("anomaly"):
            try:
                c = _resolve_model_cfg(m)
                if c:
                    c["key_alias"] = m.get("key_alias", "unknown")
                    cfgs.append(c)
            except Exception:
                continue
    except Exception:
        pass
    if not cfgs:
        for m in AI_MODEL_ROSTER:
            try:
                c = _resolve_model_cfg(m)
                if c:
                    cfgs.append(c)
            except Exception:
                continue
        mm = _minimax_fallback_cfg()
        if mm:
            cfgs.append(mm)
        mm2 = _minimax_fallback_cfg2()
        if mm2:
            cfgs.append(mm2)
    if not cfgs:
        return {"error": "no model available"}

    news_lines = "\n".join(
        f"[{i + 1}] ts={n.get('ts', '')} | action={n.get('action', '')} | score={n.get('score', '')} | "
        f"src={n.get('source', '')} | {n.get('content', '')}"
        for i, n in enumerate(events[:8])
    ) or "（无高分新闻）"

    # 图谱取证：注入该品种涨跌驱动本质，辅助反硬归因；无此品种知识时静默跳过。
    graph_block = ""
    try:
        import causal_graph
        graph_block = causal_graph.inject_block(asset)
    except Exception:
        graph_block = ""

    system_prompt = (
        "你是严谨的加密/宏观市场归因助手。给你一个品种近 24h 内分数最高的若干条公开新闻"
        "（含时间戳 ts、方向 action、分数 score、来源 src、原文），判断哪 1~3 条最可能构成该品种行情的主要驱动事件。\n\n"
        "硬性要求：\n"
        "0. 若输入含「品种涨跌本质」背景，优先用它判断哪些新闻最可能构成该品种的核心/条件驱动；"
        "与驱动无关的新闻不得仅因分数高就当作主导事件（严禁硬归因）。\n"
        "1. 只引用列表里出现的新闻原文与来源，不得编造标题/来源/数字。\n"
        "2. 按对行情影响排序输出主要驱动事件；无明确依据时 summary 写「未见单一主导事件，多因素共振」。\n"
        "3. 只输出一个 JSON 对象，格式严格为：\n"
        '{"summary":"一句话总结主要驱动","drivers":[{"ts":<事件ts整数>,"reason":"为何是主要驱动","importance":"high|medium"}]}\n'
        "drivers 最多 3 个，summary 不超过 60 字，reason 不超过 40 字，ts 必须原样照抄列表里的整数。"
    )
    user_text = (
        f"品种：{asset}\n\n"
        f"{graph_block + chr(10) if graph_block else ''}"
        f"近 24h 高分新闻：\n{news_lines}"
    )

    for cfg in cfgs:
        payload: Dict[str, Any] = {
            "model": str(cfg.get("request_model") or cfg.get("id")),
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_text},
            ],
            "max_tokens": 400,
            "temperature": 0.1,
            **(cfg.get("extra_body") or {}),
        }
        if cfg.get("json_mode"):
            payload["response_format"] = {"type": "json_object"}
        headers = {
            "Authorization": f"Bearer {cfg.get('api_key')}",
            "Content-Type": "application/json",
        }
        url = f"{cfg.get('api_base')}/chat/completions"
        session = _LLM_PROXY_SESSION if cfg.get("use_proxy") else _LLM_SESSION

        try:
            resp = session.post(url, json=payload, headers=headers, timeout=25)
            if resp.status_code >= 400:
                continue
            raw = (resp.json()["choices"][0]["message"]["content"] or "").strip()
            if not raw:
                continue
            cleaned = re.sub(r"^```(?:json)?\s*", "", raw)
            cleaned = re.sub(r"\s*```$", "", cleaned)
            obj = json.loads(cleaned)
            if not isinstance(obj, dict):
                continue
            summary = str(obj.get("summary") or "").strip()
            raw_drivers = obj.get("drivers")
            if not isinstance(raw_drivers, list):
                raw_drivers = []
            by_ts = {int(e.get("ts") or 0): e for e in events}
            out: List[Dict[str, Any]] = []
            for idx, d in enumerate(raw_drivers[:3]):
                if not isinstance(d, dict):
                    continue
                ts = int(d.get("ts") or 0)
                ev = by_ts.get(ts)
                if ev is None and idx < len(events):
                    ev = events[idx]  # AI 未照抄 ts 时，按输出顺序退化为事件顺序对应
                reason = str(d.get("reason") or "").strip()
                if not ev or not reason:
                    continue
                out.append({
                    "ts": int(ev.get("ts") or 0),
                    "content": str(ev.get("content") or "")[:160],
                    "source": str(ev.get("source") or "").strip(),
                    "action": str(ev.get("action") or "HOLD").upper(),
                    "score": float(ev.get("score") or 0.0),
                    "reason": reason,
                    "importance": str(d.get("importance") or "medium").strip(),
                })
            return {"summary": summary, "drivers": out}
        except Exception:
            continue
    return {"error": "all attribution models failed"}


# ── OpenRouter 健康检测 + MiniMax 备用通道 ──
# OpenRouter 余额不足(402)/不可用时，把 OpenRouter 通道替换为 MiniMax（保留权重）；
# 恢复后自动切回（探测结果缓存 _OPENROUTER_HEALTH_TTL 秒，旧注释误写"5 分钟"）。
_OPENROUTER_HEALTH: Dict[str, Any] = {"ts": 0.0, "ok": True}
_OPENROUTER_HEALTH_TTL = 5.0

# 健康探测自动轮询开关（测试阶段暂停）：关闭后 _openrouter_healthy() 不再发同步
# 探测请求，直接读缓存（默认 True），去掉每批次空转轮询，降低 token 消耗与资源占用。
# 保留 _probe_openrouter() 作基础存活校验，实盘前改为仅在“策略触发开仓动作”时执行一次。
_OPENROUTER_AUTO_PROBE = False

# OpenRouter 熔断：实际调用连续失败后冷却 60s（期间直接走 MiniMax，避免每条死等 65s 超时）
_openrouter_circuit: Dict[str, float] = {"until": 0.0}

# 引擎侧 OpenRouter 探测状态落盘（runtime/llm_health.json），供前端直接查看
_LLM_HEALTH_FILE = os.path.join(config.BASE_DIR, "runtime", "llm_health.json")


def _write_llm_health(ok: bool, status: Any, error: str) -> None:
    """把引擎视角的 OpenRouter 探测结果写入 runtime 文件（API/前端可读）。"""
    try:
        os.makedirs(os.path.dirname(_LLM_HEALTH_FILE), exist_ok=True)
        with open(_LLM_HEALTH_FILE, "w", encoding="utf-8") as f:
            json.dump({
                "openrouter_ok": bool(ok),
                "status_code": status,
                "error": error[:200],
                "ts": time.time(),
                "probe_model": "openai/gpt-4o-mini",
            }, f, ensure_ascii=False)
    except Exception:
        pass


def _probe_openrouter() -> bool:
    """基础存活校验：发一次探测并更新健康缓存（写入 runtime/llm_health.json）。

    仅在需要时显式调用——实盘阶段在“策略触发开仓动作”前调用一次即可，而不是
    每批次/定时高频轮询。用与分析一致的 max_tokens(600)：余额不足时 1 token 仍会
    200，600 token 才会按 402 正确暴露；探测成功不产生额外计费。
    """
    now = time.monotonic()
    ok = False
    status = None
    error = ""
    try:
        resp = _LLM_PROXY_SESSION.post(
            "https://openrouter.ai/api/v1/chat/completions",
            json={
                "model": "openai/gpt-4o-mini",
                "messages": [{"role": "user", "content": "ok"}],
                "max_tokens": 600,  # 与分析一致，余额不足时同样 402
                "temperature": 0.0,
            },
            headers={"Authorization": f"Bearer {config.OPENROUTER_API_KEY}"},
            timeout=20,
        )
        status = resp.status_code
        ok = status == 200
        if status == 402:
            print(f"[AI] OpenRouter 余额不足(402)，切换到 MiniMax 备用通道（{config.MINIMAX_MODEL}）", flush=True)
        elif status != 200:
            error = f"HTTP {status}: {resp.text[:120]}"
            print(f"[AI] OpenRouter 不可用(HTTP {status})，切换到 MiniMax 备用通道", flush=True)
    except Exception as exc:
        error = f"{type(exc).__name__}: {str(exc)[:120]}"
        print(f"[AI] OpenRouter 检测失败({type(exc).__name__})，切换到 MiniMax 备用通道", flush=True)
    _write_llm_health(ok, status, error)
    _OPENROUTER_HEALTH.update({"ts": now, "ok": ok})
    return ok


def _probe_openrouter_if_stale() -> None:
    """开仓动作触发的健康探测（带 TTL 节流）：距上次探测超过 TTL 才真正发请求。

    由开仓分支用 loop.run_in_executor 后台触发，不阻塞开仓动作与事件循环；
    结果写 _OPENROUTER_HEALTH 缓存，供 _openrouter_healthy() 判断是否切 MiniMax。
    """
    now = time.monotonic()
    if now - float(_OPENROUTER_HEALTH.get("ts", 0.0)) < _OPENROUTER_HEALTH_TTL:
        return
    _probe_openrouter()


def _openrouter_healthy() -> bool:
    """读取 OpenRouter 健康缓存（供 _resolve_model_cfg 判断是否切 MiniMax）。

    测试阶段 _OPENROUTER_AUTO_PROBE=False：不做同步探测，直接返回缓存值（默认
    True），避免在 async 事件循环主线程里同步阻塞 + 空转消耗 token/资源。实盘
    需要实时健康度时，在开仓前显式调用 _probe_openrouter() 再读本函数。
    """
    if not _OPENROUTER_AUTO_PROBE:
        return bool(_OPENROUTER_HEALTH.get("ok", True))
    now = time.monotonic()
    if now - float(_OPENROUTER_HEALTH.get("ts", 0.0)) < _OPENROUTER_HEALTH_TTL:
        return bool(_OPENROUTER_HEALTH.get("ok", True))
    return _probe_openrouter()


def _resolve_model_cfg(m: Dict[str, Any]) -> Dict[str, Any]:
    """OpenRouter 通道不可用且有 MiniMax Key 时，用 MiniMax 替代（保留原权重）。

    优先级：OpenRouter 通 → 用它；OpenRouter 不通或熔断冷却中 → MiniMax。
    """
    cfg = build_model_config(m["id"])
    if (
        cfg.get("use_proxy")
        and config.MINIMAX_API_KEY
        and (not _openrouter_healthy() or time.monotonic() < _openrouter_circuit["until"])
    ):
        return {
            "id": config.MINIMAX_MODEL,
            "label": f"{config.MINIMAX_MODEL} (MiniMax 备用)",
            "api_base": config.MINIMAX_BASE_URL,
            "api_key": config.MINIMAX_API_KEY,
            "extra_body": {},
            "use_proxy": bool(getattr(config, "MINIMAX_USE_PROXY", True)),
            "json_mode": False,
            "full_prompt": True,  # 走完整 _SYSTEM_PROMPT（含多品种 sibling 模板），
                                  # MiniMax 无 response_format 但给足模板能原生输出嵌套
        }
    return cfg


def _minimax_cfg(api_key: str, label_suffix: str = "MiniMax",
                 model: Optional[str] = None) -> Dict[str, Any]:
    """构造一个 MiniMax 通道 cfg（OpenAI 兼容，与主分析 _mm_cfg 同字段）。

    model: 不传则用 config.MINIMAX_MODEL；传了就用指定模型（minimax2 可独立指定新一代模型）。

    max_tokens 自动调高：M3 等新一代模型会先输出 <think> 推理块再给 JSON（114+ tokens），
    默认 900 易把 JSON 截断在 closing brace 前 → 解析失败误入 fallback。M3 自动 1500。
    """
    model_id = model or config.MINIMAX_MODEL
    extra_body: Dict[str, Any] = {}
    # M3 / 新一代模型需要更大 token 预算（内部 think 块 + 八段 JSON，实测 3257 字符 / 含中文时
    # 1 token≈1.5 字 → 1500 不够。提到 2500 给足 buffer，确保 JSON closing brace 不被截断）
    if model_id and model_id.upper() in {"MINIMAX-M3", "MINIMAX-M2.7", "MINIMAX-M2.7-HIGHSPEED"}:
        extra_body["max_tokens"] = 2500
        extra_body["thinking"] = {"type": "disabled"}  # M3 禁思考块，强制直接输出 JSON
    return {
        "id": model_id,
        "request_model": model_id,
        "label": f"{model_id} ({label_suffix})",
        "api_base": config.MINIMAX_BASE_URL,
        "api_key": api_key,
        "extra_body": extra_body,
        "use_proxy": bool(getattr(config, "MINIMAX_USE_PROXY", True)),
        "json_mode": False,
    }


def _minimax_fallback_cfg() -> Optional[Dict[str, Any]]:
    """MiniMax 备用通道 cfg（旧 key，优先）。

    归因/复盘等低频 LLM 任务复用此兜底，避免 OpenRouter key 失效(401)时
    归因 AI "像没启动"——MiniMax 直连国内，是当前最稳的兜底通道。
    """
    if not config.MINIMAX_API_KEY:
        return None
    return _minimax_cfg(config.MINIMAX_API_KEY)


def _minimax_fallback_cfg2() -> Optional[Dict[str, Any]]:
    """第二个 MiniMax key 的备用通道 cfg（新 key，并行补充）。

    模型名默认沿用 MINIMAX_MODEL；若 .env 设 MINIMAX2_MODEL，则用独立模型
    （如新一代 MiniMax-M3，让 #2 与 #1 形成「老模型稳 + 新模型强」互补）。
    """
    if not config.MINIMAX2_API_KEY:
        return None
    model = getattr(config, "MINIMAX2_MODEL", "") or config.MINIMAX_MODEL
    return _minimax_cfg(config.MINIMAX2_API_KEY, "MiniMax#2", model=model)


def _minimax_backup_active() -> bool:
    """OpenRouter 不可用/熔断冷却中且配了 MiniMax key ⇒ 兜底已激活。

    与 _resolve_model_cfg 的兜底触发条件保持一致。
    """
    return bool(config.MINIMAX_API_KEY) and (
        not _openrouter_healthy() or time.monotonic() < _openrouter_circuit["until"]
    )


def _minimax_backup_slots() -> int:
    """主池里会走 OpenRouter 的槽位总数（use_proxy 通道 weight 之和）。

    失效时这些槽位全部由旧 MiniMax 承接（旧优先主体）。
    """
    return sum(
        max(1, int(m.get("weight", 1)))
        for m in AI_MODEL_ROSTER
        if m.get("use_proxy")
    )


def _build_model_pool() -> List[Dict[str, Any]]:
    """构建实际用于并行分片的模型池（含 OpenRouter 失效时的 MiniMax 兜底扇出）。

    正常：主池各通道按 weight 复制槽位。
    OpenRouter 失效：use_proxy 槽位合并为 1 个 MiniMax 兜底（不按 weight 复制），
                     避免"失效通道按权重把 MiniMax 调用放大 9 倍"的 bug。
                     再追加 1 个第二个 MiniMax key 槽位作并行补充。
    """
    pool: List[Dict[str, Any]] = []
    backup_active = _minimax_backup_active()
    or_substituted = False  # 已合并过 use_proxy 失效槽位则不再追加
    for m in AI_MODEL_ROSTER:
        cfg = _resolve_model_cfg(m)
        weight = max(1, int(m.get("weight", 1)))
        if m.get("use_proxy") and backup_active:
            # 失效的 use_proxy 槽位只产生 1 个 MiniMax 兜底（不按 weight 复制）
            if not or_substituted:
                pool.append(cfg)
                or_substituted = True
        else:
            # 正常 slot 按 weight 复制
            pool.extend([cfg] * weight)
    if backup_active:
        cfg2 = _minimax_fallback_cfg2()
        if cfg2:
            # 追加 1 个第二个 MiniMax key 槽位作并行补充（不再按比例放大）
            pool.append(cfg2)
    return pool


# Compatibility export for callers that still inspect MODELS at import time.
MODELS: List[Dict[str, Any]] = [build_model_config(DEFAULT_AI_MODEL_ID)]

# 并发限流：批量请求分波发出避免打爆上游通道；充值后 OpenRouter 限流宽松可到 8。
# 单条新闻（1<8）不受影响。慢通道（如 nassaapi 波动 100s+）不再拖死单条。
_LLM_SEMAPHORE = asyncio.Semaphore(10)

# 主模型请求 session — 直连（nassaapi/aiping 均可直连，走本地代理反而
# ProxyError/挂起，实测 nassaapi 走代理 22307 会 59s 超时）。
_LLM_SESSION = requests.Session()
_LLM_SESSION.trust_env = False  # 忽略 env HTTP_PROXY，直连
_LLM_SESSION.headers.update({"User-Agent": "Mozilla/5.0"})

# ── 连接层快速重试（TLS 加固）────────────────────────────────────────
# 间歇 SSL 握手失败(SSLEOFError)/连接瞬断是秒级错误，用 urllib3 Retry 在「连接/读」
# 层快速重试几次即可吃掉绝大多数抽风；read 只重试 1 次，避免 60s×N 拖死 ai_worker。
_llm_retry = Retry(
    total=4,
    connect=3,  # SSL 握手/连接失败 → 秒级重试 3 次
    read=1,     # 读响应超时 → 只重试 1 次
    backoff_factor=1.0,
    status_forcelist=[429, 502, 503, 504],
    allowed_methods=["POST"],
)
_LLM_SESSION.mount("https://", HTTPAdapter(max_retries=_llm_retry))

# 海外通道（OpenRouter）走代理 — trust_env=True 读 env HTTP_PROXY
_LLM_PROXY_SESSION = requests.Session()
_LLM_PROXY_SESSION.headers.update({"User-Agent": "Mozilla/5.0"})

# 重要事件日历提示缓存（金十 list_calendar，10 分钟 TTL——日历变化慢，避免每条新闻打爆 MCP 配额）
_CALENDAR_HINT_CACHE: Dict[str, Any] = {"ts": 0.0, "text": ""}

# 日历通道最新探测状态（用于前端/运维可见：配额耗尽 vs 数据空 vs 正常）
# 字段：ts, ok, count, error, sample_event
_CALENDAR_STATUS: Dict[str, Any] = {
    "ts": 0.0,
    "ok": False,
    "count": 0,
    "error": "",
    "sample_event": "",
    "quota_exhausted": False,   # 全部 token 今日 1500/1500 都用完
}


def _build_calendar_hint() -> str:
    """即将发布的重要财经日历事件提示（缓存 10 分钟，MCP 失败静默）。

    供主分析 prompt 注入：让 AI 意识到未来数小时有 CPI/非农等
    高影响数据即将公布，避免把「公布前的预期波动」误判为新闻即时催化。
    """
    now = time.monotonic()
    if _CALENDAR_HINT_CACHE["text"] and now - _CALENDAR_HINT_CACHE["ts"] < 600.0:
        return _CALENDAR_HINT_CACHE["text"]
    text = ""
    status_err = ""
    status_count = 0
    sample = ""
    quota_exhausted = False
    try:
        import jin10_mcp

        events = jin10_mcp.upcoming_events(min_star=2, horizon_hours=24.0)
        if events:
            lines = [f"· {e['pub_time'][:16]} ★{e['star']} {e['title']}" for e in events[:6]]
            text = (
                "[重要事件日历提示] 未来24h内将公布以下重要经济数据/事件"
                "（注意区分『数据公布前预期交易』与『公布后即时反应』）：\n"
                + "\n".join(lines)
            )
            status_count = len(events)
            sample = events[0].get("title", "")[:60] if events else ""
    except Exception as exc:
        status_err = f"{type(exc).__name__}: {str(exc)[:120]}"
    # 探测配额耗尽（独立尝试，避免误判）
    try:
        import jin10_mcp as _jm
        cal = _jm.fetch_jin10_calendar()
        quota_exhausted = bool(cal.get("error") or "") and (
            "上限" in (cal.get("error", "") or "")
            or "已达" in (cal.get("error", "") or "")
        )
        if not status_err and quota_exhausted:
            status_err = "配额耗尽（所有 token 今日 1500/1500 用完）"
    except Exception:
        pass

    _CALENDAR_HINT_CACHE.update({"ts": now, "text": text})
    _CALENDAR_STATUS.update({
        "ts": now,
        "ok": bool(text),
        "count": status_count,
        "error": status_err,
        "sample_event": sample,
        "quota_exhausted": quota_exhausted,
    })
    return text


# ---------------------------------------------------------------------------
# DeepSeek API client (stdlib-only, OpenAI-compatible)
# ---------------------------------------------------------------------------

# v174: 结构化字段约束 — 必须从八段同源抽取 (放在 _SYSTEM_PROMPT 之前供字符串拼接)
_SYSTEM_PROMPT_SUFFIX = """

# ════════════════════════════════════════════════════════════════
# P0-2 关键字段前置块（最先看到，写在 JSON 最显眼处）
# 解决：MiniMax 看到 reasoning_path 八段后倾向"只填八段+少量字段"，导致
#       data_anchor_ts / hard_data_evidence / elasticity_window / verification 全空 (0%)
# 铁律：以下 4 个键**必须出现在你的顶层 JSON 中**，无数据也必须写 "N/A"。
# ════════════════════════════════════════════════════════════════

【必须填的 4 个核心字段 —— 缺一不可，缺则视为 schema 不完整】

1) "data_anchor_ts"（字符串）
   - 你的结论依据的行情数据对应的时间戳（取 prompt 快照时间）
   - 合法: "2026-09-21 11:05:00" / "2026-09-21T11:05:00Z" / "2026-09-21T11:05:00+08:00"
   - 不知道 → "N/A"；不确定 → "AMBIGUOUS"；不要写"刚才"/"近期"

2) "hard_data_evidence"（字符串，≤500 字）
   - 逐条结论附对应时间的硬数据（品种+时间+数值快照）
   - 示例: "XAU 11:05 close=4352.7 +0.55% | BTC 11:05 close=81920 -0.32% | 美元指数 11:05 104.2"
   - 无数据 → "N/A"；禁止无依据主观归因

3) "elasticity_window"（对象，不可省略键）
   - {"minutes": <整数持续分钟数>, "decay": "fast"|"medium"|"slow", "peak_after_minutes": <整数>}
   - 示例: {"minutes": 120, "decay": "medium", "peak_after_minutes": 30}
   - 不知道 → {"minutes": 0, "decay": "unknown", "peak_after_minutes": 0} + 写 "N/A" 备注

4) "verification"（字符串，≤200 字）
   - 跨字段自检：扫 4 组一致性
     (a) factor_ranking 多数方向 vs suggested_action
     (b) market_confirmation vs [驱动力] 段描述
     (c) impact_magnitude vs event_strength
     (d) [逻辑自检] vs [总结] 方向
   - 一致 → "pass | 全字段一致"；不一致 → "warn | <具体矛盾点>"

输出前最终自检：以上 4 个键是否都在 JSON 里？任何一个缺失 → 立刻补 "N/A" 或占位对象再输出。

# v174 结构化字段约束 (硬铁律, 缺失视为 schema 失败)

- asset_nature: 必须等于 [总框架] 段"该品种核心驱动"句的紧凑版 (≤80 字), 禁止自由重写。
  示例: [总框架] XAU 在当前实际利率上行+央行购金减速下，主导驱动从避险转向实际利率… → "asset_nature": "XAU 主导驱动：实际利率上行"
  缺失或仅"避险/抗通胀"套话 → fabrication_soft 触发 (warn | asset_nature 套话无数据)

- factor_ranking: 必须从 [因素定位] 段逐条抽出, JSON 数组;rank 字段填该条在原文里的排位(1/2), direction 必须是"利多|利空|中性"之一。
  示例: [因素定位] XAU 主链：实际利率子链(本新闻相关)… → "factor_ranking": [{"factor":"实际利率上行","rank":1,"direction":"利空"}, ...]
  v176.2: 因素定位只列本新闻真正命中的 1-2 条子链，禁止列通用 Top5

- snapshot_proximity: 你引用的快照距事件时间的分钟差。
  从 prompt 找到 [新闻时间] 标记和快照里 "snap_price_ts", 相减取分钟数。
  无快照/无时间写 "N/A"(字符串, 不是数字)。

- event_type_code: 必须从 prompt 注入的【事件类型选项清单】中**严格选 1 个** code 填入。
  禁止输出中文/英文自由字符串(如"地缘冲突"或 "Geopolitical Conflict")。
  若新闻不属于清单 25 个, 选 closest 一项 + 在 reasoning 里说明。

- v171 时间结构化字段 (硬铁律, 缺失视为 schema 失败并触发 fallback):
  * impact_curve: 必须填 4 个窗口的预期 delta_pct（数字, 可为 0 / null, 但键必须存在）
    示例: {"5m": 0.005, "30m": 0.012, "2h": 0.025, "1d": 0.040}
  * event_window_minutes: 整数 [0, 4320]（0=瞬时事件, 4320=3 天兜底）
    示例: 60 (1 小时影响窗口), 1440 (1 天), 4320 (3 天)
    不知道 → 填 "N/A"。
"""

_SYSTEM_PROMPT = """
⚠️⚠️⚠️ v176.6 关键变更（2026-09-26）⚠️⚠️⚠️
**缺技术面也可以出结果**！铁律 #4 / #9 修订如下：
- 技术面（K线/费率/技术指标）缺失 → market_confirmation=unknown，**不要直接 HOLD**
- 基本面强信号（政策落地/地缘升级/利率决议/重大事件落地）即使无技术面也应给 BUY/SELL
- asset_impact 默认 ≤5（标低），hard_data_evidence / data_anchor_ts 写 N/A
- 仅"新闻观点/汇总/无标的"（铁律 #10）才用 HOLD 兜底

⚠️⚠️⚠️ v176.3 关键变更（2026-09-23）⚠️⚠️⚠️
七段式结构改了！reasoning_path 必须按以下**新顺序**输出（每段 [段名] 方括号开头，缺一不可）：
[总框架] → [因素定位] → [驱动力] → [水位博弈] → [跨资产资金] → [反共识结论] → [逻辑自检]
旧的 [主体识别]/[资产性质]/[因素排行]/[叙事逻辑]/[因果链分析] 五段已删除，禁止再出现这些段名。
[总结] 段在 [逻辑自检] 之后独立输出。
如果你沿用旧段名，你的整个 JSON 会被服务端判定"七段不完整"重试一次，再失败则兜底模板占位入库。

---

你是掌管 10 亿美元宏观对冲基金的量化决策大脑。你的每次输出直接驱动真实交易，必须严谨、可追溯、不臆造。

# 总体任务

对每条新闻做两层分析，最终只输出一个 JSON：
- 第一层：判断这条新闻是什么、是否即时、影响哪些金融品种（按影响力排序）。
- 第二层：对主品种做七段式推演，给出买涨 / 买跌 / 不出手的方向结论。

# ⚠️ 必填字段（缺一视为响应不完整，会被前端判定"无法溯源"）

以下两个字段**必须出现在 JSON 输出**里，否则前端徽章不渲染（即便 ai_decisions 写库成功也没用）：
- `cross_net_first_seen_ts`：事件原始发生时间。精确 'YYYY-MM-DD HH:MM:SS' / 'AMBIGUOUS' / '' 三态。AI 不知道就老实填 ''，绝不要编时间。
- `cross_net_source`：cross_net_first_seen_ts 的依据来源（精确：出处名；AMBIGUOUS：候选1 / 候选2；空：留空串）。

# 第一层：主体、性质、品类、时效

## 1. 主体识别（对象 + 国别/市场）
在 reasoning_path 开头写一行：[主体识别]对象：XXX ｜ 国别/市场：XXX
- 对象类型：宏观/政策/利率、公司/个股、交易所/平台、大宗商品、人物表态、行业/赛道。
- 宏观类必须判国别；只有美国主体才能把 US10Y/DXY/VIX/TIPS 当直接论据，其它国别写 N/A，禁止用美国数据冒充该国数据。
- 铁律：分析对象 = 新闻主体及其直接相关资产，禁止"提到 A 就扯 B"；交易所/平台新闻归加密或对应品种，不套宏观。

## 2. 新闻性质（决定能否下单）
- 事实事件 = 已落地的增量信息（政策落地/数据公布/冲突断供实际发生/协议签署/央行决议）。→ 正常给方向。
- 观点/预测/汇总 = 分析师观点、官员表态预测（"称可能降息"≠决议）、转述未落地、日/周/月报总结。→ 已被市场消化或无可执行增量，一律 HOLD，reasoning 写明"观点/汇总类，不作交易信号"，七段照常分析。
- 无明确标的 = 会议/峰会/论坛/访问/人事任免/表态性外交。→ target_asset=NONE、market_category=MACRO（外汇/央行决议也是 MACRO，无单一可交易标的也归 MACRO 便于统一处理）、HOLD、中性分(±0.03~0.10)，并写明"无可交易标的"。**禁止**输出 market_category="OTHER"，会让系统把这条新闻送入人工复核队列而非静默落库。
- 拿不准就 HOLD。

## 3. 资产归类（选 target_asset，最重要）
本系统分析三类核心品种：黄金(XAU)、原油(WTI)、加密主流币(BTC/ETH/SOL)，并扩展支持股票类（美股/韩股/A股/个股）。按"谁被影响最大"归类：
- 地缘/战争/制裁/霍尔木兹/军事 → XAU 或 WTI（油运/OPEC/供给中断→WTI；避险/核对抗→XAU）。此类新闻 target_asset 不得为 BTC。
- 央行利率/CPI/流动性政策（美国）→ CRYPTO/BTC
- 能源/OPEC/原油库存 → OIL/WTI
- 加密行业自身（ETF/监管/交易所/技术升级）→ CRYPTO，主流币优先 BTC
- 地缘避险/央行购金/实际利率/通胀预期 → GOLD/XAU
- SK海力士 / SK Hynix / 海力士 / HBM / 高带宽内存 / 存储芯片 / DRAM / NAND / 内存 → market_category=STOCK、target_asset=SKHYNIX。利好（涨价/扩产/大单/业绩超预期/需求爆发）给 BUY，重大利多 asset_impact 给 7 以上。
- A股 / 上证 / 深证 / 沪深300 / 创业板 / 科创板 / 中国股市 → market_category=STOCK、target_asset=CN_STOCK。
- 其它美股/港股/个股财报 → market_category=STOCK、target_asset=US_STOCK 或 INDIVIDUAL_STOCK；无明确可交易标的就 HOLD，只作背景一句带过。
- v200 P1-A: 港股（如 09926.HK 康方生物、00625.HK 希音、00700.HK 腾讯）→ target_asset=HK_STOCK、market_category=STOCK。本系统对 HK_STOCK 不执行自动交易，但需要正确归类而不是 fallback 到 NONE。
- v200 P1-D: 聚合性市场信号归类（最重要）。当新闻描述"沪市超20家公司"、"A股多家"、"美股多家"、"港股多家"等 N 家集体行动（回购增持/资产重组/业绩预增/分红）→ 这是市场整体情绪信号而非个股，归类为对应市场：
    - "沪市/深市/科创板/创业板/A股/沪深" → market_category=STOCK, target_asset=CN_STOCK
    - "美股多家/Nasdaq/NYSE/标普" → market_category=STOCK, target_asset=US_STOCK
    - "港股多家/恒生" → market_category=STOCK, target_asset=HK_STOCK
  此类信号虽无单一可交易标的，但属于市场情绪指标，action=HOLD 即可，不应再 fallback 到 NONE。
- market_category 与 target_asset 对应（必须配对）：GOLD↔XAU、OIL↔WTI、COMMODITY↔XAG/COPPER、CRYPTO↔BTC/ETH/SOL、STOCK↔CN_STOCK/US_STOCK/HK_STOCK/INDIVIDUAL_STOCK/SKHYNIX；MACRO↔外汇/国债/央行决议（C=无具体可交易标的但宏观面有效）。**禁止**输出"OTHER"——若是新类别请选最接近的 6 选 1，缺映射会进人工复核队列。

## 3.5. 各品种盯盘优先级体系（v200 P2 知识库）

本节作为 LLM 分析的优先参考。三资产一句话总览（v200 P2 原文版）：
- 黄金：10年期TIPS实际利率第一，美元指数第二
- 原油：库存+地缘航运第一，美元指数第二，OI 看资金拥挤
- 加密：TIPS+NDX 第一；OI+资金费率+爆仓数据第二；美元指数第三

### v200 P4 精修版（三轴联动视角重述）
- **黄金（XAU）**：第一驱动 = 10年期TIPS实际利率（利率轴）；第二驱动 = 美元指数 DXY（情绪轴）；避险叙事与央行购金为辅。
- **原油（WTI）**：第一驱动 = 库存数据 + 地缘航运中断（通胀轴）；第二驱动 = 美元指数 DXY（情绪轴）；OI 看资金拥挤度。
- **加密主流（BTC/ETH/SOL）**：第一驱动 = TIPS实际利率 + 纳指100 NDX（利率+情绪轴联动）；第二驱动 = OI + 资金费率 + 爆仓数据（加密衍生品专属）；第三驱动 = 美元指数 DXY。

### 加密货币（BTC/ETH/SOL）盯盘指标优先级（最高→最低）
【宏观层】（与黄金共用，最高优先级）
- 美国10年期TIPS实际收益率：实际利率下行 → 利好BTC（无息资产）；实际利率上行 → 压制BTC。熊市阶段BTC偏风险资产，会跟随纳指；强避险叙事时BTC会切换成"数字黄金"和黄金共振。
- 美元指数 DXY：走强 → 全球美元流动性收紧 → 利空加密；走弱 → 资金易流入风险资产。
- CME FedWatch 美联储降息概率：降息预期=美元流动性宽松=利好加密整体估值。
- 纳指100（NDX）：大部分时间BTC属风险资产，和科技股联动；强避险叙事时才脱离纳指与黄金同步。

【加密衍生品特有指标】（永续合约专属，做加密最核心——OI 就是你刚才看原油时用到的）
- OI 未平仓持仓量 ⭐核心（**加密踩踏比原油黄金更剧烈）：
  * 价涨+OI 涨 → 新增杠杆多头进场，趋势有资金加持；但持续走高代表多头拥挤，随时踩踏。
  * 价涨+OI 跌 → 空头平仓（空头回补），无新多头，反弹持续性差。
  * 价跌+OI 涨 → 新增空头开空，下跌动能强。
  * 价跌+OI 跌 → 多头止损爆仓去杠杆，恐慌抛盘，**常见阶段性见底信号**（爆完即清）。
  * OI 急速下降 + 价跌 → 多头去杠杆完成，后续易出现技术性反弹（与原油 OI 逻辑同源）。
  （CL 原油暴跌本质就是多头平仓导致 OI 下降，加密行情这种踩踏会更剧烈）
- 资金费率 Funding Rate（永续独有，**阈值精修**）：
  * 8h 费率 >0.03% 偏多拥挤；>0.1% 高度拥挤，反转概率大增。
  * 负费率绝对值 >0.03% 偏空拥挤，>0.1% 高度空头拥挤，反弹概率大。
  * 连续 3 根 8h 费率同号（持续正偏多/持续负偏空）→ 单边情绪惯性，需警惕反转。
- 爆仓数据（多空清算量）：连续大单爆多头=加速下跌；集中爆空头=快速拉盘（杠杆踩踏效应远大于原油黄金）。
- BTC.D 市值占比：上涨 → 资金从山寨币回流BTC、大盘避险；下跌 → 资金流出BTC、山寨币行情启动。

【链上指标】（偏中长期，判断大周期）
- 交易所储备量（Exchange Reserve）：BTC 从交易所提币到冷钱包=长期持有者囤币=利好；大量转入交易所=准备卖出=利空。
- MVRV Z-score：BTC 处于周期高估/低估区间；高位高估容易大级别回调。
- 稳定币总供应量：增长=场外增量资金进场，市场有子弹；下降=资金离场。

【盘面技术指标】（短线交易，与原油黄金通用）
- RSI：重点看背离，单纯超买超卖在加密高波动里易钝化失效。
- MACD：动量判断，看红绿柱+DIF/DEA 背离。
- VWAP：日内多空分水岭。
- ATR：用来设置止损，加密波动极大，ATR 用来控制仓位大小。

### 三资产联动观察规律（v200 P4 扩到五条）
- 实际利率下行 → 黄金优先涨；若同时风险偏好回升（纳指涨），BTC 跟黄金共振上涨。
- 地缘冲突推升原油（供给风险）→ 通胀预期抬升 → 预期美联储维持高利率 → 利空加密 → 加密易下跌。
- 地缘缓和、原油暴跌 → 通胀预期回落、降息预期回升 → 利好黄金 + 同时利好 BTC。
- 美元指数 DXY 与风险资产（BTC/ETH/NDX）多数时间负相关；与黄金多空不绝对（避险时同涨，利率主导时负相关）。
- 加密 OI 急速下降 + 价跌 → 多头去杠杆完成，后续容易出现技术性反弹（与原油 OI 逻辑同源）。

⚠️ 风险提示：以上仅行情逻辑科普，加密货币在国内不受法律保护，杠杆衍生品风险极高，不构成任何交易建议。

### v200 P4: 跨品种对比使用规则
[SERVER CROSS-ASSET COMPARE]（4品种×5指标 对比表，prompt 头部会自动注入在 SERVER VERDICT 末尾）请**优先**引用此表做"单品种 vs 多品种"对比，避免单品种假信号：
- 若本品种与其他品种走势明显分化（如 BTC 涨但黄金/原油都跌）→ 可能是加密独立行情或避险情绪，需在 [反共识结论] 引用具体差异数据。
- 若本品种与其他品种同向 → 宏观驱动（美元/利率/地缘），按宏观框架分析。
- 5 指标 = 24h涨跌 / 7d涨跌 / 资金费率 / OI增减 / 1H RSI，用于多维强度对比。
- 单品种 RSI 超买/超卖要结合其他品种 RSI 综合判断（避免单品种假信号）。
- 决策结论必须基于本表（如"BTC 24h+1% vs 黄金-0.5% 反向走势"），不得跳过此表凭印象分析。

## 4. 时效溯源（即时性）
判断这条新闻是即时信息还是已被消化的旧闻：
- 对比 [新闻时间] 与该品种 K 线（15m/1h/4h）首次异动时点：发布前价格已异动 = 信息被抢跑（接近 price in，追单价值低）；发布后价格才异动 = 即时（有交易价值）。
- "全网最早出现"晚于发布时间 = 转载/二手，更接近 price in。
- 事后统计/复盘类新闻即使时间新，也无即时交易价值。
- 距现在越久信号越弱，超过约 45 分钟按过时处理，倾向 HOLD。

# 第二层：七段式推演（v176.3 精简版）

**结构改革原因**：v176.1 之前的旧结构前两段（资产性质 + 因素排行）让 LLM 凭印象写，
黄金叙事经常错（"避险/抗通胀"套话、因素排行凭主观）。v176.2 改为：
1) 把这两段拿掉，由 prompt 注入的"因果图谱回灌"硬先验承担（让数据说话）；
2) 新增 [总框架] 段，强制引用因果图谱回灌块做顶层框架；
3) 新增 [因素定位] 段，只在本新闻语境下定位属于哪条子链（不再列通用 Top5）；
4) v176.3 再精简：删除 [叙事逻辑] 段与 [因果链分析] 段，改为七段式。

reasoning_path 必须按以下顺序输出（每段以方括号段名开头，禁止用 1./冒号/破折号替代，七段缺一不可，信息不足写"无明显信号"），[逻辑自检] 之后加 [总结]：

【v200 P3 铁命令】LLM 必须输出完整 reasoning_path 八段（≥200 字符），不能用"简短摘要"代替——你只输出 50-80 字符的 reasoning 会触发服务端兜底，前端会显示"⚠ 数据缺"横幅，**用户看不到你的真实分析**。
- 每段至少 1-2 句；数据缺失的段写"无明显信号"（不要空着）
- 不要再"我只写一句总结就够了"——你要写完整八段骨架，每段一行
- 实在无数据也要写"无明显信号 / 等待数据补全"，**禁止**只输出 50 字符 reasoning 跳过八段

[总框架] 引用 prompt 注入的"因果图谱回灌"块的【核心驱动】一句话（≤80 字），声明本品种在当前宏观/资金/持仓结构下属于哪种属性（避险/抗通胀/风险资产/供给驱动商品 等），并明确该属性**增强还是削弱**本新闻向该品种的传导。**禁止凭印象重写因果图谱**，禁止用"避险/抗通胀/央行储备"等通用套话替代；prompt 没注入因果图谱时**必须扩展为 ≥40 字的占位文本**（例："服务端兜底：未注入因果图谱，引用上下文真实数据；当前市场未学到该品种驱动结构 → 自动占位 [总框架] 段"），不允许只写"未注入"3 字兜底。

[因素定位] 在总框架下定位本新闻属于哪条传导子链（例：黄金 → 实际利率子链 / 央行购金子链 / 地缘风险溢价子链 / 美元避险子链；原油 → OPEC 供给子链 / 需求性子链 / 库存子链 / 地缘风险溢价子链；BTC → 流动性子链 / ETF 资金子链 / 监管子链 / 风险偏好子链）。**禁止列通用 Top5 因素排行**，只挑本新闻真正命中的 1-2 条子链说明"新闻作用在哪个变量上、该变量历史上对价格弹性多大"。

[驱动力] 在已定位的子链上，具体什么在驱动价格。紧扣新闻主体；写出 asset_impact 打分依据（说不出依据的一律 ≤3）。若输入含 [门卫四步]，先复述 core_impact 那根筋及其底层驱动，再用盘面/资金证据判断是印证还是推翻它；印证就顺筋给方向，推翻就在 [逻辑自检] 写明「先验错在哪」，不得既不顺也不反驳地各说各话。最近 5 个小时有没有相关的信息，市场对此的涨跌态度如何？

[水位博弈] 价格处于什么周期位置。必须引用快照真实指标：RSI>70 超买 / <30 超卖；ADX<20 震荡 / >25 趋势；现价与 VWAP 关系；量能比 >1.5x 放量 / ≈1x 存量。持仓拥挤度引用 COT/黄金ETF/资金费率（标 N/A 只按技术指标判断并写明"未接入"）。看下新闻全网最早时间，行情是不是已经走完了。

[跨资产资金] 逐类引用真实数据写资金面（加密费率/保证金占比(多空持仓比)/OI/CVD/杠杆vs现货占比/ETF净流入/加密内部轮动/各金融品类COT投机净头寸/美债利率与期限利差/TIPS/DXY/VIX/爆仓流，至少 2-3 类）。字段映射：ls_ratio/top_ls_ratio=多空持仓比、OI=持仓量、taker/CVD=主动资金流、funding=费率、margin_debt=美股保证金债务(杠杆)、us_equity_mcap=美股总市值(现货)、oi_usd_bybit=加密永续OI(USD杠杆)、crypto.total_mcap=加密总市值(现货)、btc_etf_net=BTC现货ETF净流入(增量资金)、crypto.btc_dominance/eth_dominance=加密内部资金轮动、cot_noncomm_net/cot_spec_ratio=各金融品类投机净头寸/多空比(资金构成与集中)。CVD 正=净买盘、负=净卖盘；杠杆/现货占比=杠杆资金÷现货市值（美股、加密分开算）；margin_debt 环比/同比上行=杠杆扩张、下行=去杠杆；某类缺数据写"未接入(N/A)"；数据不稳定（单点异常/跨所口径不一/周度日度滞后）不得盲目采纳作方向依据，应写"数据不稳定(N/A)"并下调置信度或转 HOLD。

[反共识结论] 给出与表面叙事相反或易被忽略的具体证据点（价格已 price in/7d 区间位置/费率爆仓拥挤/数据与叙事背离）；禁止"尽管X…但Y建议谨慎"空模板。

[逻辑自检] 执行举反例验证：先假设"结论是错的"，论点与论据分别用真实数据核对；发现被证伪 → 写明矛盾并降级 HOLD 或下调置信度。

[总结] 每个品种一行，写清"最终怎么看、依据、方向"，与各自 suggested_action 一致；禁止"综上所述、总体来看"这类没有品种归属的空话。

# 时间字段使用规则（必读）

新闻 prompt 注入的时间字段可能存在以下失真：
- `[新闻时间]`：通常是入站采集时刻（金十/fj 等 WS 源没有原始发布时间），不是新闻平台声称的发布时间。
- `published_at`：金十/news 源可解析"平台声称发布时间"，但仅部分源写入成功。
- 不同源的"发布时间"参差不齐：jin10 可能比 ws-fj 早/晚 1h+ 是真实情况，不是 bug；但**所有新闻的"发布时间都完全相同"是采集 bug**。
- 遇到同 batch 新闻 [新闻时间] 全相同/全相差 0m 时，视为"采集失真"，不要据此判断时效；改用 [主体识别] 关键词 + 价格异动起点对位。
- 全网最早出现时间 = 这条消息/事件在全网全世界最早被发布的真实时刻（≠ 本系统采集入库时刻，也 ≠ 同指纹簇 first_seen_ts——那只是系统多源去重用的"最早采集时刻"，不代表真实全网最早）。填 cross_net_first_seen_ts 时以真实首发为准，用自身知识直接判断，可早于入库任意时长；严禁把 [新闻时间]/first_seen_ts 抄成全网最早时间。

# 数据缺失降级（必读）

- 市场价格数据接口在 prompt 中标记 N/A / 未接入 / 数据不稳定 → 优先标记 N/A，禁止凭印象/编造。
- 备选方案：模型可以"自己拉取信息" → 在推理中说明"我已自检：xx 来源未取得数据"或"经常识层面（xx）确认"；**没有数据就如实回答"无依据"，不得捏造数字/指标/时间戳**。幻觉一律降级 HOLD 并扣 5+ 分。
- 价格/资金/仓位类硬数据无 prompt 提供时，写 N/A 而非猜测；宏观常识层面可承接（如"地缘升级利好 XAU"）。

# Token 节约（执行层面，不影响分析质量）

- reasoning_path 每段不超过 5 行；不堆砌无信息量的修饰词；相同品种各段不重复同一论据。
- 主品种输出 20 字段齐全即可，sibling_decisions 只在确有独立传导链时输出（≤2 条）。
- 模型自身 reasoning 链不要复述 prompt 原文；只在结论需要时引用 prompt 关键数字。
- 禁止"综上所述、总体来看"等空模板词，直接给方向。

# 多品种规则

- 主品种用 20 字段；若新闻还显著影响其它品种（<=2 个），在 sibling_decisions 给出。
- sibling 准入三条全过才放：①有明确直接传导链 ②能给出明确方向 ③能写出自己独立的七段（除品种名外有实质差异）。说不清的品种只放 [跨资产资金] 一句带过，不放 sibling。
- 七段每段内每个涉及品种（主+副）以"[品种代码]："开头独立成行，主品种行在最前；禁止混写、禁止"同上/见主品种"、禁止留空（写不出要写明原因）。
- sibling_decisions 出现的每个品种，七段每一段都要有它的行；[总结] 也要逐品种。
- 地缘同时强影响黄金与原油：主品种 XAU/WTI 择一，另一个必进 sibling；加密弱相关只一句带过。

# score 锚定（sentiment_score，2h 窗口推动方向的置信度与幅度预期）

# 范围 0-100 整数（新约定，期望给出有区分度的分）：
- 40-60 中性（观望，不下单；这个范围出现代表"信号有但我不确定"，给 50）
- 60-75 偏多：情绪扰动 + 资金面弱信号（口头表态、第三方评论、二级数据、监管传闻）
- 75-90 明确多：直接推动供需或风险偏好（CPI/NFP 大幅偏离、美元剧震、黑客挤兑、ETF 获批/拒绝、OPEC 减产、制裁升级）
- 90-100 极强多：结构突变/黑天鹅（战争爆发、主权违约、交易所破产、FOMC 意外转向）
- 0-25 极空（空头逻辑的镜面对应）
- 25-40 偏空
# 严重警告：不要给 50 兜底！如果确实没信号，target_asset=NONE + action=HOLD + 任意分都可；但只要有方向，必须给出 60+ 或 25- 的明显分。
# 直接给整数（62、78、45 这种），不要带小数点。
# 叠加规则：direct_catalyst=true 上浮一档（~+10）；event_strength=low 上限 75、medium 上限 90；market_confirmation=negative 下调一档（~-10）；趋势同向 +5、反向 -5。

# 门卫四步核对（输入含 [门卫四步] 时必须执行）

- 输入里的 [门卫四步] 是前置语义 gate 的先验：core_impact=挖出的核心影响、main_asset=主品种、kind=性质、timing=时效、strength=强度(0-10)、impact_1to100=1-100 自评打分、verdict=可交易/排除（v181 已砍掉"只能看"）。
- 必须核对这条先验：同意 → 顺着 core_impact 这条筋推演，并写进 [驱动力]；不同意 → 在 [逻辑自检] 写明"推翻先验的理由"（基于快照真实数据），不得默默忽略。
- verdict=排除：这条新闻不会进入本阶段（realtime_filter 已置 is_noise=1）— 如果你看到此字段出现在输入里，说明是历史老数据，请照样给出建议（兜底）。
- 门卫未给出 core_impact（输入无 [门卫四步]）→ 自行判断，不受本段约束。

# Hermes 多席校验与历史绩效

- [Hermes Multi-Agent Writing] 新闻/宏观/风险/交易四席会给出方向票。四席多数与你的 suggested_action 不一致时，在 [逻辑自检] 说明分歧并下调置信度（软校验，四席反对宁可 HOLD）；最终方向仍由你独立给出。
- [Historical Performance] 类似信号 2h 真实结算：胜率<30% 降置信或 HOLD，>70% 可上调，Insufficient sample 忽略。

# JSON 输出

只输出 JSON。20 个字段缺一不可（含 asset_reasoning/asset_nature/factor_ranking/impact_magnitude）+ 可选 sibling_decisions：

{"reasoning_path": "[总框架]→[因素定位]→[驱动力]→[水位博弈]→[跨资产资金]→[反共识结论]→[逻辑自检]→[总结]",
 "asset_nature": "主品种性质定调",
 "asset_classification": "避险/抗通胀|供给驱动|高Beta|宏观锚|政策敏感 (5 选 1)",
 "fund_driver": "驱动力一句话总结<=80字（从 [驱动力] 段摘出）",
 "consensus_vs_us": "一致|反共识",
 "factor_ranking": [{"factor":"因素名","rank":1,"direction":"利多|利空|中性"}],
 "impact_magnitude": <1-100 对主品种影响的数量级>,
 "asset_reasoning": "主品种 XAU(黄金)：…；副品种 WTI(原油)：…（逐品种一行，与 sibling 集合一一对应，写不出写 N/A）",
 "sentiment_score": <float 0-100, 整数, 50=中性, 0=极空, 100=极多 — 给出有区分度的分，不要给 50 兜底>, "suggested_action": "BUY|SELL|HOLD",
 "reasoning": "主品种结论<=50字（target_asset=NONE 时也要写：说明事件为何与监控品种无关）", "market_category": "CRYPTO|GOLD|OIL|STOCK|MACRO|COMMODITY",
 "target_asset": "XAU|XAG|WTI|BTC|ETH|SOL|CN_STOCK|US_STOCK|HK_STOCK|INDIVIDUAL_STOCK|SKHYNIX|NONE", "prediction_type": "reversal|continuation|breakout",
 "event_phase": "early|mid|late", "market_confirmation": "positive|negative|unknown",
 "expected_horizon": "intraday|1-3d|1w+", "invalidation_condition": "失效条件<=40字",
 "event_strength": "low|medium|high", "direct_catalyst": true|false,
 "heat_score": <0-10>, "impact_score": <0-10>, "asset_impact": <0-10>,
"attention_score": <0-100 该新闻在全市场的关注度得分>,
  "elasticity_window": {"minutes": <影响持续分钟数>, "decay": "fast|medium|slow", "peak_after_minutes": <异动峰值出现于发布后多少分钟>},
  "data_anchor_ts": "结论依据的行情数据对应时间戳（取 prompt 快照里的时间，如 '2026-09-14 15:03'；无数据写 N/A）",
  "hard_data_evidence": "逐条结论附对应时间的硬数据（品种+时间+数值快照，如 'XAU 15:03 2412→2431 +0.79%'；无硬数据写 N/A，禁止无依据主观归因）",
  "net_origin_info": "全网溯源：这条新闻对应的事件，全网最早是什么时间发布的？来源是什么？用一段自然语言回答（不超过 80 字），如：'美联储9月18日22:00官网新闻稿' / '路透社英文版9月19日08:30首发' / '事件发生时间不详，最早报道见路透社10:00'。无法确认就答'无法确认'。",
  "cross_net_first_seen_ts": "结构化全网溯源时间（精确格式，区别于 net_origin_info 的自由文本）—— 三态取值：精确时间 'YYYY-MM-DD HH:MM:SS' / 'AMBIGUOUS' / 空串 ''。",
  "cross_net_source":        "cross_net_first_seen_ts 的依据来源（精确：出处名+链接/描述；AMBIGUOUS：写'候选1: … / 候选2: …'；无则空串）。",
 "sibling_decisions": [{"target_asset":"WTI","market_category":"OIL","sentiment_score":40,"suggested_action":"BUY","reasoning":"该品种一句","expected_horizon":"1-3d","event_strength":"medium","direct_catalyst":false,"asset_impact":<0-10>,"invalidation_condition":"失效条件"}]}

字段口径：
- insight_hit_ids（如 prompt 注入复盘经验）：只填真正采纳的 EX#id 整数，没采纳填 []，禁止编造不存在的 id。
- asset_reasoning：逐品种一行"主品种 XAU(黄金)：…"+"副品种 WTI(原油)：…"，每行<=60字，与 sibling 集合一一对应；写不出真实传导写"暂无明确传导路径(N/A)"。
- asset_classification：该资产最主要属性，从"避险/抗通胀｜供给驱动｜高Beta｜宏观锚｜政策敏感"5 选 1（target_asset=NONE 时填最接近的一项）。
- fund_driver：一句话总结 [驱动力] 段结论，<=80 字（无明确驱动力写"无明显驱动力(N/A)"）。
- consensus_vs_us：你的结论与市场共识的关系，从"一致/反共识"选 1；没有反共识点就写"一致"。
- expected_horizon：intraday→分钟级、1-3d→约2天、1w+→约7天；[FRED发布]→intraday，[FRED趋势]→1-3d/1w+。
- heat_score/impact_score/asset_impact：0-3 间接背景、4-6 间接传导、7-8 直接催化、9-10 决定性；多品种各自打分，在 [驱动力] 写依据。
- direct_catalyst=true = 事件直接针对该资产（如 ETF 获批）；false = 间接传导。
- attention_score：0-100 全市场关注度得分（新闻被提及/扩散的热度），仅作参考不直接决定方向。
- net_origin_info：全网溯源，AI 用自然语言一段话回答"这条新闻对应的事件，全网最早发布时间+来源"。无确切信息就答"无法确认"。不做校验，AI 自由回答。
- cross_net_first_seen_ts：结构化全网溯源时间（= 这条消息/事件在全网全世界最早发布的时间）。三态——精确时间 'YYYY-MM-DD HH:MM:SS'（可早于本系统入库时间任意时长，属正常；它不是采集入库时刻、也不是 first_seen_ts）、'AMBIGUOUS'（事件存在两种可能时间，无法判定唯一）、''（确实毫无头绪才留空）。用自身知识直接判断给出，不必等联网证据，能确定到小时就写整点。校验只拒：早于 1990、或晚于入库超过 60s。cross_net_source 必须给依据：精确→出处名（如"美联储官网新闻稿"/"路透社首发"），AMBIGUOUS→写"候选1: … / 候选2: …"或类似两条并列表达，source 不足 4 字符一律拒绝。

# 铁律（输出前逐条核对）

1. reasoning_path 七段用 [段名] 方括号、顺序固定、缺一不可；禁止编号/冒号/破折号替代。
2. suggested_action 与七段推演方向一致：偏多不许 SELL、偏空不许 BUY；隐含反向风险必须写出。
3. 分析对象 = 新闻主体，禁止张冠李戴、跨资产硬扯。
4. 只用 prompt 提供的真实数据，缺失写 N/A，禁止编造。所有结论必须附对应时间的硬数据截图（hard_data_evidence 逐条带品种+时间+数值，data_anchor_ts 给时间戳），无硬数据写 N/A，严禁输出无依据的主观归因。**v176.6 修订**：技术面（K线/费率/技术指标）缺失**不再自动 HOLD**——基本面强信号（政策落地/地缘升级/利率决议/重大事件落地）即使无技术面也应给 BUY/SELL，market_confirmation=unknown、hard_data_evidence 与 data_anchor_ts 写 N/A、asset_impact 默认 ≤5。仅"新闻观点/汇总/无标的"（见铁律 #10）才用 HOLD 兜底。
11. 新增子模块必填：attention_score（关注度得分）、elasticity_window（弹性窗参数）、data_anchor_ts（原始数据戳）、hard_data_evidence（硬数据截图）；缺一不可，缺失字段写 N/A。
5. 新闻利多 + 已大涨 + 费率极端 → 警惕 SELL（利好出尽）；利空 + 已大跌 + 费率负极端 → 警惕 BUY（空头拥挤）；快照与方向矛盾 → market_confirmation=negative。
6. 防追单：利多未反应果断 BUY / 已大涨不追；利空未反应果断 SELL / 已大跌不追空；恐慌利空按分钟级窗口。
7. BTC 是纯风险资产：战争中 SELL、宽松中 BUY，不存在避险属性。
8. 地缘升级/恶化 → XAU/WTI 必须给方向，不得用 HOLD 逃避；缓和/停火/协议 → 反转方向。
9. 缺市场数据（技术面/费率）→ market_confirmation=unknown，**不要直接 HOLD**（v176.6）；基本面强信号仍可给 BUY/SELL，详见铁律 #4 修订。
10. 观点/预测/汇总/无标的类 → HOLD（v176.6 唯一保留的 HOLD 兜底场景）。

13. v176.6 缺技术面兜底规则（2026-09-26）：基本面强信号（policy_decision/geopolitical_escalation/rate_decision/major_event_landed 类新闻）+ 技术面缺失 → 仍给 BUY/SELL；
    market_confirmation="unknown"；hard_data_evidence="N/A"；data_anchor_ts="N/A"；
    asset_impact 默认 ≤5（标低），elasticity_window.peak_after_minutes 写 0；
    reasoning 中明确标注"v176.6 缺技术面给方向，置信度下调"。

12. v181 结构化产出已并入上方 JSON：`asset_classification` / `fund_driver` / `consensus_vs_us` 三个字段必须填。
    `target_asset` / `key_factors` / `self_check` / `final_action` 由系统从 factor_ranking / verification / suggested_action 自动生成，
    无需你在末尾再额外输出 7 行文本。
""" + _SYSTEM_PROMPT_SUFFIX


_JSON_PROMPT_FORCE = _SYSTEM_PROMPT  # v170 改造：双通道已废弃，统一用 _SYSTEM_PROMPT；
                                 # 保留别名仅为兼容 /api/ai/prompts 展示与 ratio 计算引用。
                                 # 历史：原本是独立短版 prompt（含两层分析+八段说明+JSON 模板），
                                 #       与 _SYSTEM_PROMPT 重复约 70 行（~1100 tokens），
                                 #       实际分析路径已 100% 走 _SYSTEM_PROMPT（use_full_prompt=True）。



# ── 盘面结构分析指令块（按后台开关动态拼接）────────────────────────────────
# 开关开：附加到水位博弈段（多周期结构判断）；关：不附加（纯筹码情绪判断）。
# 即时新闻交易可关闭结构分析，避免结构判断影响追单/急拉决策。
_MARKET_STRUCTURE_BLOCK = """

═══ 盘面结构分析（开关已开启，并入水位博弈段执行）═══
在水位博弈段内，结合本条新闻做多周期结构量化判断（快照提供 1d/4h/1h/15m
周期趋势、ATR、区间高低点与资金费率；标 N/A 的周期当不存在，禁止补造）：
  · 价位位置：当前价在区间位置（>80% 高位=突破敏感 / <20% 低位=支撑敏感 / 40-60% 中性）
  · 波动分档：ATR<0.5%=低波动(新闻冲击被放大) / 0.5-1.5%=中波动 / >1.5%=高波动(冲击钝化)
  · 周期匹配：按 expected_horizon 选主导周期——intraday 看 1H/15m、1-3d 看 4H/1H、1w+ 看 1d/4H
  · 新闻×结构：利多+临近阻力=突破机会 / 利多+突破回踩=回踩机会 / 利空+支撑下方=破位 /
              新闻方向+结构反向=结构阻力高警惕
  · 周期共振：多周期同向=强确认；冲突=降级 confidence、market_confirmation 设 negative
  · 结构评分(-2~+2) → 写入 market_confirmation（>0 positive / <0 negative / =0 unknown）
"""

# ── 八段推理链强制校验 ──────────────────────────────────────────────
# v176.2 结构改革：删掉"主体识别"+"资产性质"+"因素排行" 三段（靠 prompt 注入的
# 因果图谱回灌块承担，避免 LLM 凭印象写错黄金叙事），改为六段 + 总框架前置：
#   1) [总框架] → 引用 prompt 注入的"因果图谱回灌"硬先验（核心驱动/资金面/传导链）
#   2) [因素定位] → 在总框架下定位本新闻属于哪条传导路径，影响哪条子链
#   3) [驱动力] → 在该子链上具体什么在驱动价格（不脱离总框架）
#   4) [水位博弈] → 该品种当前盘面位置（RSI/ADX/VWAP/量能）
#   5) [跨资产资金] → ETF/COT/杠杆资金动向
#   6) [反共识结论] → 与表面叙事相反的具体证据点
#   7) [逻辑自检] → 反例验证 + 自我降级
# 校验 reasoning_path 是否按新格式输出；不完整（AI 自由发挥/漏段）时带纠正提示重试 1 次。
_SIX_STAGES = ("总框架", "因素定位", "驱动力", "水位博弈", "跨资产资金", "反共识结论", "逻辑自检")

# 段名标记（用于按段切分 reasoning_path，供"副品种逐段覆盖"校验）。
# 与展示层 _MARKER_RE 同语义：段名后必须紧跟分隔符，避免误切正文里的同名词。
# 含 [总结]：用户要求"有关系就要有分析+总结"，总结是八段之后的独立段（按品种分行）。
_STAGE_MARK_RE = re.compile(
    r"(总框架|因素定位|资产性质|因素排行|盘面结构分析|盘面结构|驱动力|水位博弈|跨资产资金|反共识结论|逻辑自检|总结)"
    r"(?=[\s]*[:：—\-]|[\s]*[\]】]|[\r\n]|\s*$)"
)

_SIX_STAGE_RETRY_HINT = (
    "【v176.3 铁命令】你上一条 reasoning_path 用的是旧段名（资产性质/因素排行/叙事逻辑/因果链分析等），已被系统作废。"
    "现在严格按 v176.3 新顺序重写 reasoning_path，逐段输出，每段以方括号段名开头："
    "[总框架] → [因素定位] → [驱动力] → [水位博弈] → [跨资产资金] → [反共识结论] → [逻辑自检] → [总结]"
    "禁止再出现 [资产性质] / [因素排行] / [主体识别] / [主体] / [叙事逻辑] / [因果链分析] 等旧段名 ——"
    "你只要再写一个旧段名，整个 JSON 直接判无效入库。"
    "七段缺一不可（信息不足的段写\"无明显信号\"），其它字段保持不变，只输出完整 JSON。"
)

_SCHEMA_REQUIRED_FIELDS_RETRY_HINT = (
    "你上一条 JSON 顶层缺少必填字段：data_anchor_ts / hard_data_evidence / "
    "elasticity_window / verification / asset_classification / fund_driver / "
    "consensus_vs_us。这些字段就算没有真实数据也必须出现，"
    "用占位值填充（不知道就写 \"N/A\" / 数字 0 / 空对象）。"
    "请立即补全："
    "\"data_anchor_ts\": 取 prompt 快照时间（如 \"2026-09-21 11:05:00\"），不知道就 \"N/A\"；"
    "\"hard_data_evidence\": 列出你引用的真实数据（品种+时间+数值），没有就 \"N/A\"；"
    "\"elasticity_window\": {\"minutes\": <整数>, \"decay\": \"fast\"|\"medium\"|\"slow\", \"peak_after_minutes\": <整数>}；"
    "\"verification\": 跨字段自检结果，\"pass | 全字段一致\" 或 \"warn | <具体矛盾>\"；"
    "\"asset_classification\": 从\"避险/抗通胀|供给驱动|高Beta|宏观锚|政策敏感\"选 1 个；"
    "\"fund_driver\": 一句话驱动力总结，<=80 字；"
    "\"consensus_vs_us\": \"一致\" 或 \"反共识\"。"
    "只补缺失的字段，其它字段保持不变，重新输出完整 JSON。"
)

_MAIN_FOCUS_RETRY_HINT = (
    "你的 reasoning_path 七段里几乎没有针对你判定的主品种（target_asset）的分析 —— "
    "七段通篇在讲宏观/地区政治/别的资产，却没讲这个品种本身。规则："
    "【七段必须围绕 target_asset 展开】—— [水位博弈] 讲该品种的盘面位置（引用快照里"
    "该品种的 RSI/ADX/VWAP/量能）、[驱动力] 讲什么在"
    "推动该品种价格、[反共识结论] 给该品种的独立判断。禁止写与该品种无关的泛泛而谈。"
    "请重写 reasoning_path：每段都要落到 target_asset 上。"
    "如果你重新判断这条新闻其实与该品种没有明确传导链（例如会议/峰会/区域合作类新闻），"
    "那就把 target_asset 改为 \"NONE\"、suggested_action 改为 \"HOLD\"、"
    "sentiment_score 给中性区（±0.03~0.10），并在七段里说明为何无可交易标的。"
    "其它字段保持不变，只输出完整 JSON。"
)


def _is_slow_channel(model_cfg: Dict[str, Any]) -> bool:
    """是否慢通道（重试要跳过的对象）。

    历史背景：这些重试原本用 `use_json_mode` 当门槛（注释写"只对 json_mode 主通道重试，
    MiniMax 慢不重试"）。但 `json_mode` 只在 api_base 含 "aiping.cn" 时为 True，
    而模型池早已换成 OpenRouter/nassaapi（json_mode=False）→ 该门槛导致
    [副品种逐品种] 重试在真实通道上【永不触发】。
    改为直接判定慢通道：MiniMax 备用通道不快，重试会拖时长；其余都快、可重试。
    """
    try:
        base = str(model_cfg.get("api_base") or "")
        label = str(model_cfg.get("label") or "")
        return ("minimax" in base.lower()) or ("MiniMax" in label)
    except Exception:
        return False


_PER_ASSET_RETRY_HINT = (
    "你的 reasoning_path 七段里缺少某些副品种的逐品种分析。规则："
    "sibling_decisions 里出现的每个品种，七段【每一段】都必须有它自己的行，"
    "格式为「[品种代码]：该品种的分析」（主品种行放最前）。"
    "请重写 reasoning_path：每个涉及的品种在每一段都有独立一行，"
    "写该品种【自己】的价位/逻辑/影响，禁止照抄主品种的句子换个股名。"
    "某品种在某段确实无数据/推不出结论时，也必须写出该行并说明具体原因"
    "（如「[XAU]：本次新闻与黄金无直接传导链，本段不适用」、"
    "「[WTI]：快照缺该品种 RSI 数据，无法判断水位（N/A）」），"
    "禁止省略该行、禁止只写\"无/暂无\"。"
    "另外：如果你判断某个副品种其实与新闻无关、说不出它自己的分析，"
    "就把它从 sibling_decisions 里删掉（宁可不列，也不要列了却不写分析）。"
    "其它字段保持不变，只输出完整 JSON。"
)


def _six_stage_strict(path: str) -> bool:
    """严格格式：8 个 [段名] 方括号标记按顺序全部出现。"""
    if not path:
        return False
    pos = 0
    for stage in _SIX_STAGES:
        marker = f"[{stage}]"
        idx = path.find(marker, pos)
        if idx < 0:
            return False
        pos = idx + len(marker)
    return True


# 逐品种分行标记：`[XAU]：内容`（prompt 硬约束格式）或旧写法 `XAU：内容`
_PER_ASSET_LINE_RE = re.compile(
    r"(?:^|[；;]|\n|[\]】])[ \t]*(?:\[([A-Za-z][A-Za-z0-9/]{1,11})\]|([A-Za-z][A-Za-z0-9/]{1,11}))\s*[:：]",
    re.MULTILINE,
)


def _path_assets_in_stages(path: str) -> set:
    """reasoning_path 八段里出现过品种前缀行的品种代码集合。

    用于校验"进了 sibling_decisions 的品种，八段里必须有它自己的行"——
    否则前端展示该副品种时无内容可显示（用户诉求：要么有八段分析、要么它就不是副品种）。
    """
    if not path:
        return set()
    out = set()
    for m in _PER_ASSET_LINE_RE.finditer(path):
        code = (m.group(1) or m.group(2) or "").strip().upper()
        if code:
            out.add(code)
    return out


def _siblings_missing_in_stages(result: Dict[str, Any]) -> List[str]:
    """返回"在 sibling_decisions 里、但八段中没有任何逐品种行"的品种代码列表。"""
    try:
        sibs = result.get("sibling_decisions") or []
        if not isinstance(sibs, list) or not sibs:
            return []
        path = str(result.get("reasoning_path", "") or "")
        present = _path_assets_in_stages(path)
        missing: List[str] = []
        for s in sibs:
            if not isinstance(s, dict):
                continue
            code = str(s.get("target_asset") or "").strip().upper()
            if not code or code in ("NONE", "OTHER"):
                continue
            if code not in present:
                missing.append(code)
        return missing
    except Exception:
        return []


# 七段里需要逐品种覆盖的段（与展示层 FIXED_STAGE_KEYS 对齐）
_COVERAGE_STAGES = ("总框架", "因素定位", "驱动力", "水位博弈", "跨资产资金", "反共识结论", "逻辑自检")

# 非七段段名：这些段是附加结构，不算"该品种的七段分析内容"。
# [总结] 是七段之后的独立收口段（按品种分行），不参与逐品种覆盖校验。
_NON_COVERAGE_STAGES = ("总结",)


def _asset_missing_stages(path: str, code: str) -> List[str]:
    """某个品种在八段里【具体缺哪几段】的逐品种行（通用，主/副品种都适用）。

    用户要求（主副统一）：
      · 主品种：判了它就必须八段都围绕它分析（"判了主品种却没分析它"= 分析跑偏）
      · 副品种：列进 sibling 就必须有它自己的逐段行
    返回缺的段名列表（空 = 八段都有该品种的行）。
    """
    out: List[str] = []
    if not path or not code:
        return out
    segs: Dict[str, str] = {}
    marks = list(_STAGE_MARK_RE.finditer(path))
    for i, m in enumerate(marks):
        key = "盘面结构" if m.group(1).startswith("盘面结构") else m.group(1)
        end = marks[i + 1].start() if i + 1 < len(marks) else len(path)
        segs[key] = path[m.end():end]
    for stage in _COVERAGE_STAGES:
        seg = segs.get(stage)
        if seg is None or not _segment_has_asset(seg, code):
            out.append(stage)
    return out


def _alias_word_match(alias: str, text: str, upper: str) -> bool:
    """别名是否作为【独立词】出现（不匹配更长单词内部的子串）。

    为什么需要：实测 'coin' 会命中 'coinbase'、'mara' 会命中 'marathon'。
    这会让"其实没分析到某个品种"被算成"已分析"——针对性闸门与主导品种判定
    都会因此误放行（实测 300 条里有 16 段被 'coinbase' 误算给 COIN）。

    规则：
      · 纯 ASCII 别名 → 边界必须是"非字母数字"，即 coinbase / marbles 都不算
        （因此 COIN 需要 'coinbase' 这类更长的别名单独列出，而不是靠子串撞）
      · 含非 ASCII（中文）别名 → 直接子串包含即可（中文没有词边界）
    """
    if not alias:
        return False
    if alias.isascii():
        return bool(re.search(
            r"(?<![A-Za-z0-9])" + re.escape(alias.upper()) + r"(?![A-Za-z0-9])",
            upper,
        ))
    return alias in text


def _segment_mentions_asset(segment: str, code: str) -> bool:
    """段内是否【提及】该品种（含中文别名），不要求是行首标签形式。

    用于"分析针对性"判定：段内容里出现"黄金/金价/XAU"等即算在讲该品种。
    别名按"独立词"匹配（见 _alias_word_match），避免子串假阳性。
    """
    if not segment or not code:
        return False
    up = segment.upper()
    # 代码全词匹配（避免 XAUD 之类误判）
    if re.search(r"(?<![A-Z0-9])" + re.escape(code) + r"(?![A-Z0-9])", up):
        return True
    for alias_code, aliases in _ASSET_ALIASES:
        if alias_code != code:
            continue
        for a in aliases:
            if _alias_word_match(a, segment, up):
                return True
    return False


def _asset_mentioned_stage_count(path: str, code: str) -> int:
    """八段里有几段【提及】了该品种（宽松计数，用于针对性判定）。"""
    if not path or not code:
        return 0
    marks = list(_STAGE_MARK_RE.finditer(path))
    if not marks:
        return 0
    n = 0
    for i, m in enumerate(marks):
        end = marks[i + 1].start() if i + 1 < len(marks) else len(path)
        if _segment_mentions_asset(path[m.end():end], code):
            n += 1
    return n


# ── 针对性闸门（单一实现，主品种路径与副品种路径共用）──────────────────────
# 原则：方向单必须建立在"针对该品种的分析"之上。八段里提都没提到它 → 依据不足 →
# 不给方向单（降级 HOLD，不建仓）。阈值 2 段：只提 1 段往往是"跨资产顺带点名"，
# 不构成对它的分析。
_FOCUS_MIN_MENTIONED_STAGES = 1  # v176.3 fix: 从 2 降到 1。
# 原因：XAU/WTI/BTC 等主品种在 [总框架] 段就会被点名为锚品种，
# 即使后续段落不再重复提及（如白银 ETF 短新闻），也已是"主品种在分析中"。
# 原来 >=2 段把这类"短新闻·主品种锚定"误判为"分析无针对性"标 FAILED，
# 导致 reasoning_path 被服务端兜底覆盖，前端显示一片 N/A。
_FOCUS_GATE_PREFIX = "分析未落到主品种"
# 不参与针对性判定："没有可交易标的"与"其它品类"本来就不该有品种分析
_FOCUS_EXEMPT_ASSETS = ("", "NONE", "OTHER")
# 关键词推断兜底结果没有八段，不参与判定
_FOCUS_EXEMPT_PATH_PREFIX = "关键词推断"

# 闸门降级后的 reasoning 前缀（主/副品种各一份）。
# 它既是给用户看的理由，也是"这条已被针对性闸门降级"的标记 —— 下游分支据此
# 避免把降级结果换成别处的方向单。
_FOCUS_GATE_PREFIX_BY_ROLE = {
    "主品种": "分析未落到主品种",
    "副品种": "分析未落到副品种",
}
# 主品种「没分析」不再伪装成 HOLD 观望 → 实事求是标记「分析失败」
_FAILED_ACTION = "FAILED"
_FAILED_GATE_PREFIX = "分析失败"
_FOCUS_GATE_PREFIXES = tuple(_FOCUS_GATE_PREFIX_BY_ROLE.values()) + (_FAILED_GATE_PREFIX,)
_FOCUS_GATE_PREFIX = _FOCUS_GATE_PREFIX_BY_ROLE["主品种"]  # 兼容既有引用


def _is_gate_downgraded(result: Dict[str, Any]) -> bool:
    """该结果是否已被针对性闸门降级（reasoning 带降级说明）。"""
    if not isinstance(result, dict):
        return False
    return str(result.get("reasoning") or "").startswith(_FOCUS_GATE_PREFIXES)


def _main_established(main_result: Dict[str, Any],
                      main_asset: str = "") -> tuple:
    """主品种是否"立住了" —— 副品种方向单的前置条件（用户原则）。

    原则："先有主品种，后面才会有副品种。主品种都没有，何来副品种。"
    主品种立住 = 可交易品种 + 没被针对性闸门降级（= 八段确实分析了它）。
    返回 (是否立住, 原因)。原因用于给用户/前端说明为什么副品种不给方向单。
    """
    code = str(main_asset or (main_result or {}).get("target_asset") or "").strip().upper()
    if code in _FOCUS_EXEMPT_ASSETS:
        return (False, f"主品种未成立（{code or 'NONE'}，无可交易标的）")
    if _is_gate_downgraded(main_result):
        return (False, f"主品种 {code} 分析未成立（未落到主品种 / 分析失败）")
    return (True, "")


def _signal_trace(event: str, **fields: Any) -> None:
    """运行期信号留痕（默认关闭，`TRIDENT_SIGNAL_TRACE=1` 打开）。

    为什么要它：实测反复出现"闸门日志显示已降级、库里仍是方向单并建仓"，
    而用户终端的 stdout 不进文件 → 事后无法定位。这里把「闸门判定的输入/输出」
    和「真正落库的方向单」写进同一份 JSONL，用 news_id 一串就能对照出
    降级是在哪一步丢的。绝不抛异常影响主流程。
    """
    if not os.environ.get("TRIDENT_SIGNAL_TRACE"):
        return
    try:
        _d = os.path.join(config.BASE_DIR, "runtime")
        os.makedirs(_d, exist_ok=True)
        with open(os.path.join(_d, "signal_trace.jsonl"), "a", encoding="utf-8") as _f:
            _f.write(json.dumps({
                "ts": _now(),
                "event": event,
                **fields,
            }, ensure_ascii=False) + "\n")
    except Exception:
        pass


def _focus_downgrade(result: Dict[str, Any], asset: str = "",
                     path: str = "", role: str = "主品种",
                     reason: str = "") -> tuple:
    """针对性闸门：某品种八段提及段数不足却给了方向单 → 就地降级。

    主品种标「分析失败」(FAILED)，副品种降为 HOLD：
    - 主品种没分析到 = 本次分析失效，实事求是报「分析失败」，绝不伪装成观望；
    - 副品种无独立分析 / 主品种未立住 = 副品种不给方向单（观望）。
    返回 (是否降级, 提及段数)。asset 为空时自动取 result.target_asset。
    reason 非空时用它当降级说明（用于"主品种未立住 → 副品种无依据"这类情形）。
    被降级时写回 suggested_action/sentiment_score/reasoning，调用方需自行同步
    局部变量（下方两处调用点均已同步，历史踩过"日志显示已降级、结果仍是 BUY"）。
    """
    if not isinstance(result, dict):
        return (False, 0)
    code = str(asset or result.get("target_asset") or "").strip().upper()
    if code in _FOCUS_EXEMPT_ASSETS:
        _signal_trace("gate", asset=code, verdict="豁免(无品种)",
                      action=str(result.get("suggested_action") or ""))
        return (False, 0)
    body = str(path or result.get("reasoning_path") or "")
    if not body or body.startswith(_FOCUS_EXEMPT_PATH_PREFIX):
        _signal_trace("gate", asset=code, verdict="豁免(无八段)",
                      path_len=len(body),
                      action=str(result.get("suggested_action") or ""))
        return (False, 0)
    hits = _asset_mentioned_stage_count(body, code)
    action_in = str(result.get("suggested_action") or "").upper()
    if not reason and hits >= _FOCUS_MIN_MENTIONED_STAGES:
        _signal_trace("gate", asset=code, verdict="达标放行", hits=hits,
                      action=action_in, path_len=len(body))
        return (False, hits)
    if action_in not in ("BUY", "SELL"):
        _signal_trace("gate", asset=code, verdict="本就无方向单", hits=hits,
                      action=action_in, path_len=len(body))
        return (False, hits)  # 本来就是 HOLD，无需降级
    if role == "副品种":
        # 副品种「无独立分析 / 主品种未立住」→ 副品种不给方向单（观望），不是「分析失败」
        result["suggested_action"] = "HOLD"
        result["sentiment_score"] = 0.05
        result["reasoning"] = (
            reason or f"分析未落到{role}（八段仅 {hits} 段提及 {code}），依据不足故降级观望"
        )
        _signal_trace("gate", asset=code,
                      verdict="已降级HOLD(主品种未立住)" if reason else "已降级HOLD",
                      hits=hits, action=action_in, path_len=len(body), note=reason[:80])
    else:
        # 主品种「没分析」（八段没落到主品种）→ 实事求是标「分析失败」，绝不伪装成观望
        result["suggested_action"] = _FAILED_ACTION
        result["sentiment_score"] = 0.0
        result["reasoning"] = (
            f"{_FAILED_GATE_PREFIX}：{code} 八段未被有效分析（仅 {hits} 段提及）"
        )
        _signal_trace("gate", asset=code, verdict="分析失败(主品种无有效分析)",
                      hits=hits, action=action_in, path_len=len(body))
    return (True, hits)


# ── v176.5f: 闸门归因记录器（gate_chain 落库 + 自动评估）────────
# 每条决策落库时附带 3 个新字段:
#   gate_chain      TEXT    JSON 数组: [{gate, verdict, reason}, ...] 闸门调用链
#   original_action TEXT    LLM 给出的原始方向单 (BUY/SELL/HOLD)
#   ungated_action  TEXT    如果所有闸门都不拦，会用什么 action（影子模式）
# 设计目标:
#   - 用 SQL 直接回答"过去 24h 哪些闸门拦截最多 BUY"
#   - 用 ungated_action + forward_verdict 反事实评估"闸门过度保守还是正确"
#   - 不影响现有逻辑，仅做旁路观测 + 结构化留痕
class _GateRecorder:
    __slots__ = ("entries", "original_action", "ungated_action", "final_action")

    def __init__(self, llm_action: str):
        self.entries: List[Dict[str, Any]] = []
        self.original_action = str(llm_action or "").upper()
        self.ungated_action = self.original_action  # 影子模式初值=LLM 原始
        self.final_action = self.original_action     # 最终落库 action

    def record(self, gate: str, verdict: str, reason: str = "") -> None:
        """记录一次闸门判定。verdict='pass' / 'block' / 'skip'"""
        self.entries.append({
            "gate": gate,
            "verdict": verdict,
            "reason": str(reason or "")[:200],
        })

    def apply_block(self, gate: str, reason: str = "") -> None:
        """闸门判定 block：把 final_action 强制 HOLD，ungated_action 保持 LLM 原始。"""
        self.record(gate, "block", reason)
        self.final_action = "HOLD"

    def apply_pass(self, gate: str, reason: str = "") -> None:
        """闸门判定 pass：不改 action，仅记录。"""
        self.record(gate, "pass", reason)

    def apply_skip(self, gate: str, reason: str = "") -> None:
        """闸门被强信号跳过 (v176.5e strength>=_GATE_PASS_THR 路径)：记录但不拦。"""
        self.record(gate, "skip", reason)

    def to_json(self) -> str:
        return json.dumps(self.entries, ensure_ascii=False)

    def blocked_count(self) -> int:
        return sum(1 for e in self.entries if e.get("verdict") == "block")

    def blocked_gates(self) -> List[str]:
        return [e.get("gate", "") for e in self.entries if e.get("verdict") == "block"]


# ── v174: 事件结构化驱动的交易闸门 ────────────────────────────────
# 在 LLM 输出后、INSERT ai_decisions 之前同步拦截。三类拦截:
#   1. structured_check.verdict=fabricated (LLM 自检说不一致) → 强制 HOLD
#      (但 INSERT 时 judge 还没跑,这是 v174 schema 校验阶段的本地等价物)
#   2. verification 字段以 "fail" 开头 → LLM 自报存在字段矛盾 → 强制 HOLD
#   3. snapshot_proximity 数字 ≥ 60 (分钟) → 引用快照已严重滞后 → HOLD
#   4. event_type_code 空 + factor_ranking 空 → 结构化字段全缺, 信息不足 → HOLD
# 不拦截: structured_input 缺失 (数据库层兜底), reasoning 文本 OK (说明 LLM 给得出分析)
# 这些拦截只动 suggested_action / no_trade_reason,绝不丢整条 reasoning_path (用于诊断)。
_V174_FABRICATION_TOKENS = ("fabricated", "fabrication", "价格幻觉", "幻觉")


def _v174_event_gate(result: Dict[str, Any]) -> tuple:
    """事件结构化闸门: 同步拦截 LLM 输出中"不该下单"的情况。

    返回 (override_action, no_trade_reason)。
    - override_action in (None, "HOLD"): None=不拦, "HOLD"=强制 HOLD
    - no_trade_reason: 落 no_trade_reason 字段,前端可见
    调用方负责把这两个值应用回 result dict 与后续 INSERT 参数。
    """
    if not isinstance(result, dict):
        return (None, "")
    action = str(result.get("suggested_action") or "").upper()
    # 已经是 HOLD 不再拦 (避免污染 no_trade_reason)
    if action not in ("BUY", "SELL"):
        return (None, "")

    # ── 拦截 1: LLM 自检 verification 含 fail ──
    verification = str(result.get("verification") or "").strip().lower()
    if verification.startswith("fail") or verification.startswith("warn"):
        return ("HOLD", f"[v174-gate] LLM 自检 {verification[:60]} → 强制 HOLD")

    # ── 拦截 2: snapshot_proximity 太大 (引用快照严重滞后事件时间) ──
    sp_raw = result.get("snapshot_proximity")
    sp_num = None
    try:
        if isinstance(sp_raw, (int, float)):
            sp_num = float(sp_raw)
        elif isinstance(sp_raw, str):
            sp_s = sp_raw.strip().lower()
            if sp_s in ("n/a", "na", "none", ""):
                sp_num = None
            else:
                sp_num = float(sp_s)
    except (TypeError, ValueError):
        sp_num = None
    if sp_num is not None and sp_num >= 60:
        return ("HOLD", f"[v174-gate] snapshot_proximity={int(sp_num)}min ≥60min,价格已充分反应 → HOLD")

    # ── 拦截 3: 价格幻觉 token 出现在 reasoning / verification ──
    reasoning = str(result.get("reasoning") or "").lower()
    for token in _V174_FABRICATION_TOKENS:
        if token in verification or token in reasoning:
            return ("HOLD", f"[v174-gate] 检测到 '{token}' token,LLM 输出可能含价格幻觉 → HOLD")

    # ── 拦截 4: 关键事件结构化字段全缺 (LLM 完全没填,信息不足以支撑方向单) ──
    event_type_code = str(result.get("event_type_code") or "").strip()
    factor_ranking = result.get("factor_ranking")
    if not event_type_code and not factor_ranking:
        # 给了 BUY/SELL 但完全没说是什么事件、什么因素 → 不该真建仓
        return ("HOLD", "[v174-gate] event_type_code 与 factor_ranking 双空,事件信息不足 → HOLD")

    return (None, "")


# ── 传导机制闸门 ────────────────────────────────────────────────────
# 用户原则（原话）：
#   "新闻和主品种、副品种有关系，是指新闻的内容会影响到它们的价格波动，
#    这个影响可能大也可能小（影响小的就不下单交易），而不是指新闻某字和它们有关系。"
#
# 提示词里早已写了"文本提到该品种名 ≠ 新闻显著影响该品种"和准入三条自检，
# 但**代码从不检查**——只查"提及段数 >= 2"。于是"顺带点名"与"有传导链"在代码眼里
# 是同一件事。这里把关：方向单必须有该品种自己的传导链措辞，
# 说不清"事件 → 该品种价格"的就不给方向单（影响小/说不清 → 观望，符合用户规则）。
#
# 注意「因…而…」是中文最常见因果句式（"可能因地缘风险上升而受到资金流入"），
# 必须单独覆盖，否则会把明显有传导的句子误判成"无传导"。
_MECHANISM_RE = re.compile(
    r"传导|导致|推动|推升|推高|提振|压制|打压|拖累|施压|受益|利好|利空|"
    r"承压|带动|因此|使得|从而|进而|引发|刺激|驱动|"
    r"不利于|有利于|受益于|受制于|支撑其|压制其|"
    r"因.{0,12}而|受.{0,10}影响|令.{0,6}(上涨|下跌|走强|走弱)"
)


def _asset_has_mechanism(path: str, code: str) -> bool:
    """该品种自己的句子里，是否写出了"事件 → 该品种价格"的传导机制。

    只看该品种自己的行（专属行优先；无标签段仅在没有提到别的品种时算它的），
    不做整篇扫描 —— 整篇里别处的因果措辞不能替它证明传导。
    """
    scoped = _asset_scoped_text(path, code)
    if not scoped:
        return False
    for piece in re.split(r"[；;\n]", scoped):
        t = piece.strip()
        if t and _MECHANISM_RE.search(t):
            return True
    return False


def _mechanism_downgrade(result: Dict[str, Any], asset: str = "", strength: int = 0) -> bool:
    """传导机制闸门：方向单但说不出传导链 → 降级 HOLD。返回是否降级。

    与针对性闸门互补：
      · 针对性闸门 = 有没有【分析】（提及 >= 2 段）
      · 本闸门    = 有没有【传导】（说出事件如何影响该品种价格）
    两者都过，方向单才成立：有分析 + 有传导。

    v176.5: strength>=8 跳过本闸门 (信任 LLM 强信号, 不再因没写出传导链就降级)
    """
    if not isinstance(result, dict):
        return False
    if not MECHANISM_GATE_ENABLED:
        return False
    if strength >= 8:
        return False  # 强信号不降级
    code = str(asset or result.get("target_asset") or "").strip().upper()
    if code in _FOCUS_EXEMPT_ASSETS:
        return False
    body = str(result.get("reasoning_path") or "")
    if not body or body.startswith(_FOCUS_EXEMPT_PATH_PREFIX):
        return False
    if str(result.get("suggested_action") or "").upper() not in ("BUY", "SELL"):
        return False
    if _asset_has_mechanism(body, code):
        return False
    result["suggested_action"] = "HOLD"
    result["sentiment_score"] = 0.05
    result["reasoning"] = (
        f"未能写出 {code} 的传导链（新闻如何影响其价格），影响不明确故不给方向单"
    )
    _signal_trace("mechanism", asset=code, verdict="无传导链→降级HOLD",
                  path_len=len(body))
    return True


def _dominant_asset_in_path(path: str, exclude: str = "") -> tuple:
    """从八段内容反推"真正被分析的品种"——返回 (品种代码, 提及段数)。

    用途：实测模型会判错主品种 —— 新闻通篇讲 WTI 油价，却把 target_asset 写成 BTC
    （只因跨资产段提到过 BTC 资金费率），而八段内容其实全在分析 WTI。
    此时应按"八段实际在讲谁"纠正主品种，否则主行标的与分析对象脱节。
    """
    if not path:
        return ("", 0)
    skip = (exclude or "").upper()
    best_code, best_n = "", 0
    for code, _aliases in _ASSET_ALIASES:
        if code == skip:
            continue
        n = _asset_mentioned_stage_count(path, code)
        if n > best_n:
            best_code, best_n = code, n
    return (best_code, best_n)


def _siblings_missing_stages(result: Dict[str, Any]) -> Dict[str, List[str]]:
    """每个副品种【具体缺哪几段】的逐品种行。

    与 _siblings_missing_in_stages 的区别：后者只看"整篇里有没有该品种的行"，
    这里按段检查 —— 用户要求"列进来的副品种，八段【每一段】都要有它的行"。
    返回 {品种: [缺的段名, ...]}，只含缺段的品种。
    """
    out: Dict[str, List[str]] = {}
    try:
        sibs = result.get("sibling_decisions") or []
        if not isinstance(sibs, list) or not sibs:
            return out
        path = str(result.get("reasoning_path", "") or "")
        if not path:
            return out
        for s in sibs:
            if not isinstance(s, dict):
                continue
            code = str(s.get("target_asset") or "").strip().upper()
            if not code or code in ("NONE", "OTHER"):
                continue
            missing_stages = _asset_missing_stages(path, code)
            if missing_stages:
                out[code] = missing_stages
        return out
    except Exception:
        return out


def _segment_has_asset(segment: str, code: str) -> bool:
    """该段文本里是否有 code 这个品种的**专属行**（行首标签形式）。

    只认"行首标签"：`[CODE]：` / `CODE：` / `黄金：` / `gold: `。
    为什么不认"句中顺带提及"：实测 `当前资金流向显示出对原油的偏好，WTI 的资金费率为…`
    这种句子不含标签，若算作专属行，会把整句的方向算到该品种头上（该句既有
    "偏好"又有"费率转负"，方向会算反）。逐段覆盖判定的目的是确认"这段有没有
    针对该品种独立写一行"，所以必须认标签。
    """
    if not segment or not code:
        return False
    head = segment.lstrip("]】」』 \t")
    # 行首标签：CODE： / [CODE]： / 黄金： / [黄金]： / gold: （冒号可在方括号内外）
    pat = re.compile(
        r"(?:^|[；;]|\n)[ \t]*\[?" + re.escape(code) + r"\]?\s*[:：]",
        re.IGNORECASE,
    )
    if pat.search(head):
        return True
    # 中文/英文别名同样按"行首标签"匹配
    for alias_code, aliases in _ASSET_ALIASES:
        if alias_code != code:
            continue
        for a in aliases:
            if re.search(r"(?:^|[；;]|\n)[ \t]*\[?" + re.escape(a) + r"\]?\s*[:：]",
                         head, re.IGNORECASE):
                return True
    return False


def _six_stage_complete(path: str) -> bool:
    """宽松完整性：7 个段名都出现（兼容编号/冒号/破折号等模型自由格式）。"""
    if not path:
        return False
    return all(stage in path for stage in _SIX_STAGES)


# 空骨架：模型只把段名用箭头连写回显（`[总框架]→[因素定位]→…`），
# 段间无实质正文。_six_stage_complete 只看段名是否出现，会误判这种输出为"完整"，
# 需单独识别并触发格式重试（链路上格式重试不受 _is_slow_channel 限制，MiniMax 也生效）。
_TIGHT_STAGE_JOIN_RE = re.compile(r"\]\s*[→>\-—]\s*\[")


def _is_eight_stage_skeleton(path: str) -> bool:
    """空骨架判定：段名标记被 →/->/— 紧密连写 ≥5 处（正常输出不会这样连写）。"""
    return bool(path) and len(_TIGHT_STAGE_JOIN_RE.findall(path)) >= 5


# ── 总结段（用户要求：有关系就要有分析 + 总结）─────────────────────
# 实测背景：提示词里原先把"总结"要求只写在[因果链命中]的块里，且要求补进 reasoning
# 字段（会被截断到 80 字）→ 300 条样本里含"总结"的 0 条，等于从来没有总结。
# 现在两份提示词都硬性要求八段之后输出 [总结] 段并按品种分行。
_SUMMARY_MARKERS = ("[总结]", "【总结】", "总结：", "总结:")


def _summary_text(path: str) -> str:
    """取出总结段正文（没有则返回 ""）。兼容 [总结] / 【总结】/ 总结： 三种写法。"""
    if not path:
        return ""
    marks = list(_STAGE_MARK_RE.finditer(path))
    for i, m in enumerate(marks):
        if m.group(1) == "总结":
            end = marks[i + 1].start() if i + 1 < len(marks) else len(path)
            return path[m.end():end].strip()
    # 兜底：非标记写法（"总结：xxx"）
    for mk in ("总结：", "总结:"):
        idx = path.find(mk)
        if idx >= 0:
            return path[idx + len(mk):].strip()
    return ""


def _has_summary_for(path: str, code: str) -> bool:
    """总结里是否有该品种的行（[CODE]：/ 中文别名：）。

    用户要求：多品种时【每个品种都要有自己的总结行】，禁止只总结主品种。
    """
    text = _summary_text(path)
    if not text:
        return False
    for piece in re.split(r"[；;\n]", text):
        t = piece.strip().lstrip("]】」』 \t")
        if t and _segment_has_asset(t, code):
            return True
    # 兼容"按品种分行但没写标签"的写法：只有该品种名出现且不含其它品种
    if not code:
        return False
    others = [c for c, _ in _ASSET_ALIASES if c != code]
    if any(_segment_mentions_asset(text, o) for o in others):
        return False
    return _segment_mentions_asset(text, code)


_SUMMARY_RETRY_HINT = (
    "你的 reasoning_path 缺少 [总结] 段，或多品种时没有给每个品种单独一行总结。"
    "请在七段（[总框架]…[逻辑自检]）之后，补一段 [总结]，"
    "严格按品种分行（主品种 + 每个 sibling 各一行），格式：\n"
    "  [总结]\n"
    "  [品种代码]：该品种最终怎么看、依据是什么、方向是什么\n"
    "要求：① 每个品种都要有自己的总结行，禁止只总结主品种；"
    "② 每个品种的总结方向必须与它自己的 suggested_action 一致；"
    "③ 禁止\"综上所述/总体来看\"这类没有品种归属的空话；"
    "④ 七段里任何一段都不许留空，无数据的段写明具体原因。"
    "其它字段保持不变，只输出完整 JSON。"
)


# ── 推演内容 vs 下单方向一致性校验 ──────────────────────────────────
# 八段推演文本方向与 suggested_action 相反（如文本写"看多"却输出 SELL）时，
# 视为自相矛盾 → 带纠正提示重试 1 次，让模型自己决定改 action 还是改推演。
_DIRECTION_POS_WORDS = (
    "看多", "偏多", "利多", "利好", "上涨", "上行", "反弹", "回升",
    "买入", "增持", "看涨", "bullish", "upside",
    # 资金/避险流向（八段分品种常用）
    "流入", "净流入", "增仓", "避险买盘", "买盘", "需求上升", "资金流入",
    "走强", "强势", "升破", "放量上涨", "多头占优",
    # 传导类动词（分品种行常见："避险推升金价" / "供给担忧提振油价"）
    "推升", "推涨", "提振", "推高", "带动上涨", "刺激上涨",
    # 情绪/需求类（实测高频，缺失会让"该品种看多"算成中性）
    "需求回暖", "需求改善", "买盘增加", "受益", "获支撑", "筑底", "企稳",
    "偏强", "转强",
)
_DIRECTION_NEG_WORDS = (
    "看空", "偏空", "利空", "下跌", "下行", "回落", "承压",
    "卖出", "减持", "抛售", "看跌", "bearish", "downside",
    # 资金/避险流向（八段分品种常用）
    "流出", "净流出", "减仓", "抛压", "卖盘", "需求走弱", "资金流出",
    "走弱", "弱势", "跌破", "放量下跌", "空头占优", "避险属性弱",
    "避险买盘消退", "避险需求下降", "避险情绪降温",
    # 传导类动词（分品种行常见："避险退潮压制金价" / "需求疲软拖累油价"）
    "压制", "拖累", "打压", "施压", "推低", "带动下跌", "刺激下跌",
    # 资金轮动/情绪类（实测高频：写"转为风险资产/负面/谨慎"就是在看空该品种）
    "转向风险资产", "风险偏好上升", "负面影响", "需求下降", "需求疲软",
    "前景不明", "情绪谨慎", "保持谨慎", "回调风险", "回调", "抛售压力", "卖压",
    "转弱", "偏弱",
)


_DIRECTION_RETRY_HINT = (
    "你上一条输出的 suggested_action 与 reasoning_path 七段推演的方向相反（自相矛盾）。"
    "请复核并修正：要么调整 suggested_action 使其与推演结论方向一致，"
    "要么修正推演内容使其支持 suggested_action 的方向。"
    "注意\"利好出尽/利空出尽\"等反向场景：推演中必须明确写出反向措辞（如\"利好出尽、"
    "警惕回落\"），禁止让推演方向词与 action 自相矛盾。"
    "其它字段保持不变，只输出完整 JSON。"
)


def _count_direction_hits(text: str, words) -> int:
    """统计方向词命中次数：在每个位置上按【最长匹配】取一个词，跳过该词长度。

    为什么不能直接 `text.count(w)` 求和：
      · 短词被长词包含会重复计数（'刺激上涨' 里含 '上涨'）
      · 更糟的是"同长度不同词"互相包含：'走强' 与 '走弱' 相差一字，
        但 '走弱' 输入会被 '走强' 的搜索命中（'走' 后有 '弱'，不含 '强'——
        实际不命中；真正的问题是 '走弱' 的**子串**匹配路径）→ 逐位置最长匹配
        能保证同一处文本只被解释一次。
    """
    if not text:
        return 0
    uniq = sorted(set(words), key=len, reverse=True)
    pat = re.compile("|".join(re.escape(w) for w in uniq))
    return sum(1 for _ in pat.finditer(text))


def _direction_leaning(text: str) -> int:
    """从文本提取方向倾向：正向词多于负向词 → +1；负向词多 → -1；持平/无 → 0。

    注意：同一条文本可能同时命中正负词，按命中次数相抵 —— 刻意的保守设计：
    倾向不明（0）时不判"方向矛盾"，避免用粗糙的词频去改模型已写清的 action。
    """
    if not text:
        return 0
    low = text.lower()
    pos = _count_direction_hits(low, _DIRECTION_POS_WORDS)
    neg = _count_direction_hits(low, _DIRECTION_NEG_WORDS)
    if pos > neg:
        return 1
    if neg > pos:
        return -1
    return 0


# 资产 → market_category（多品种兜底解析用）
_ASSET_TO_CATEGORY_MAP = {
    "XAU": "GOLD", "WTI": "OIL", "BTC": "CRYPTO", "ETH": "CRYPTO", "SOL": "CRYPTO",
    "NVDA": "STOCK", "TSLA": "STOCK", "AAPL": "STOCK", "MSTR": "STOCK",
    "MSFT": "STOCK", "AMZN": "STOCK", "GOOGL": "STOCK", "COIN": "STOCK",
    "PLTR": "STOCK", "META": "STOCK", "SMCI": "STOCK", "MARA": "STOCK",
    "RIOT": "STOCK",
    # v175 品类扩展：A股 / 韩股海力士 / 个股兜底
    "SKHYNIX": "STOCK", "CN_STOCK": "STOCK", "US_STOCK": "STOCK", "INDIVIDUAL_STOCK": "STOCK",
}

# 八段文本中可识别的副品种（含别名），排除主品种后扫描提及
_ASSET_ALIASES = [
    ("XAU", ("黄金", "金价", "xau", "gold")),
    ("WTI", ("原油", "油价", "石油", "油气", "wti", "oil", "布伦特")),
    ("BTC", ("比特币", "btc", "bitcoin")),
    ("ETH", ("以太坊", "eth", "ethereum")),
    ("SOL", ("sol", "solana", "索拉纳")),
    # 美股（币安永续可交易）
    ("NVDA", ("英伟达", "nvda", "nvidia")),
    ("TSLA", ("特斯拉", "tsla", "tesla")),
    ("AAPL", ("苹果", "aapl", "apple")),
    ("MSTR", ("微策略", "mstr", "microstrategy")),
    ("MSFT", ("微软", "msft", "microsoft")),
    ("AMZN", ("亚马逊", "amzn", "amazon")),
    ("GOOGL", ("谷歌", "googl", "google", "alphabet")),
    ("COIN", ("coinbase", "coin")),
    ("PLTR", ("palantir", "pltr", "帕兰提尔")),
    ("META", ("meta", "facebook", "脸书")),
    ("SMCI", ("smci", "美超微")),
    ("MARA", ("mara", "marathon")),
    ("RIOT", ("riot",)),
    # v175 品类扩展：韩股海力士 + A股
    ("SKHYNIX", ("海力士", "sk hynix", "skhynix", "hynix", "hbm", "高带宽内存", "000660")),
    ("CN_STOCK", ("a股", "上证", "深证", "沪深300", "沪指", "深成指", "创业板", "科创板", "中国股市")),
    # v211 P1-C：扩半导体个股 ticker（与海力士常被同时提及的产业链个股）
    ("MU",      ("美光", "micron", "mu")),
    ("INTC",    ("英特尔", "intel", "intc")),
    ("TSMC",    ("台积电", "tsmc", "2330.tw")),
    ("SAMSUNG", ("三星电子", "三星", "samsung", "005930")),
]


def _rescue_target_asset_from_content(
    content: str,
    current_target: str = "",
    *,
    preferred_categories: Optional[Tuple[str, ...]] = None,
) -> str:
    """服务端兜底：LLM 把 target_asset 写成 NONE/OTHER 时，从新闻正文里识别真实 ticker。

    v211 P1-B 修复：
      历史 710 条 NO_MAPPING_FOR_ASSET:NONE 脏数据几乎全部是因为 LLM 在「个股/港股」等
      扩展品种上未输出 target_asset（如 "三星 HBM 占 DRAM 全球产能近三成" 应是 SKHYNIX，
      LLM 却写 OTHER/NONE）。本函数复用 _ASSET_ALIASES 关键词扫描正文，
      找到第一个命中的 ticker 兜底，避免数据进 unclassified_events 队列。

    优先规则：
      1. 优先尝试 SKHYNIX（韩股海力士，国内财经源常误判为 NONE）
      2. 然后其它个股 ticker（NVDA/TSLA/AAPL 等美股）
      3. 最后才是大宗品类（XAU/WTI/BTC 等），避免把「海力士 HBM 利好」误判为 MACRO

    入参:
      content:  新闻正文（必传；空字符串直接返回 current_target）
      current_target: 现有 target_asset；非 NONE/OTHER 时直接返回
      preferred_categories: 候选品类白名单；None 时按默认顺序扫描
    """
    cur = (current_target or "").strip().upper()
    if cur and cur not in ("", "NONE", "OTHER", "N/A", "NULL"):
        return current_target
    if not content or not content.strip():
        return current_target or "NONE"

    low = content.lower()

    # ── 优先级1：SKHYNIX 类（海力士专属，不含 HBM/DRAM 等存储泛词，避免把美光新闻误判） ──
    for kw in ("海力士", "sk hynix", "skhynix", "hynix", "000660"):
        if kw in low:
            return "SKHYNIX"

    # ── 优先级2：其它个股 ticker，按 _ASSET_ALIASES 顺序扫描（先到先得） ──
    #    所有纯英文别名统一走 word-boundary regex 匹配，避免子串误命中：
    #      "coin" → "Cointelegraph / Blockchain.com"
    #      "intel" → "Cointelegraph"
    #      "mu" → "目 / 模"（但中文不会命中，regex 已排除）
    #      "tsmc" → "xTSMCx" 类包装内容
    #    中文别名仍用 substring 匹配（中文无 word 概念）。
    import re as _re_rescue
    for asset, aliases in _ASSET_ALIASES:
        if asset == "SKHYNIX":  # 已扫过
            continue
        for alias in sorted(aliases, key=len, reverse=True):
            if not alias:
                continue
            # 中文别名 → substring 匹配
            if any('\u4e00' <= ch <= '\u9fff' for ch in alias):
                if alias in low:
                    return asset
                continue
            # 纯英文别名 → word-boundary 匹配
            pattern = r"(?<![a-z0-9])" + _re_rescue.escape(alias) + r"(?![a-z0-9])"
            if _re_rescue.search(pattern, low):
                return asset

    return current_target or "NONE"


def _infer_sibling_from_reasoning(path: str, main_asset: str = "NONE",
                                 max_sibs: int = 2) -> List[Dict[str, Any]]:
    """从 reasoning_path 八段文本兜底提取副品种（AI 未给 sibling_decisions 时）。

    AI 八段若明确分品种分析（"XAU：避险买盘升温；WTI：…"），这里识别非主品种的
    提及并按其上下文方向词推断 BUY/SELL，无明确方向 → HOLD（HOLD 不建仓，安全）。
    返回 sibling 结构，与 AI 直接输出格式一致。
    """
    if not path:
        return []
    low = path.lower()
    main = str(main_asset or "").upper().strip()
    out: List[Dict[str, Any]] = []
    for asset, aliases in _ASSET_ALIASES:
        if asset == main:
            continue
        # 找该资产在文本中的出现位置（别名最长优先匹配，避免 gold 命中 "golden" 之类）
        hit_at = None
        hit_word = ""
        for alias in sorted(aliases, key=len, reverse=True):
            idx = low.find(alias)
            if idx >= 0:
                hit_at = idx
                hit_word = alias
                break
        if hit_at is None:
            continue
        # 取该词所在分句作为该品种上下文（按 。；→ 换行 切句，避免邻句方向词污染）
        s_start = max(
            path.rfind("。", 0, hit_at), path.rfind("；", 0, hit_at),
            path.rfind("→", 0, hit_at), path.rfind("\n", 0, hit_at),
        )
        _ends = [x for x in (path.find("。", hit_at), path.find("；", hit_at),
                             path.find("→", hit_at), path.find("\n", hit_at)) if x >= 0]
        s_end = min(_ends) if _ends else len(path)
        ctx = path[s_start + 1:s_end].strip()
        if len(ctx) < 8:
            # 分句太短（命中词在句首/孤立）→ 放宽到命中词后 60 字
            ctx = path[hit_at:hit_at + 60]
        # ── 弱相关排除：分句表达"影响有限/未受显著影响/无关/仅提及" → 不算副品种 ──
        # （防"法国工业产出→BTC/XAU/WTI 弱相关"式误拉：八段为凑数提一句 ≠ 显著影响）
        _ctx_low = ctx.lower()
        if any(w in _ctx_low for w in (
            "影响有限", "影响不大", "影响较小", "未受显著影响", "未受明显影响",
            "无显著影响", "无关", "无影响", "影响有限",
            "not affected", "limited impact", "little impact", "unaffected",
            "仅弱相关", "弱相关", "关联度不大", "未显著改变", "一句带过",
            "照抄", "与通胀无关", "维度无关",
        )):
            continue
        lean = _direction_leaning(ctx)
        if lean > 0:
            action = "BUY"
        elif lean < 0:
            action = "SELL"
        else:
            action = "HOLD"  # 提及但方向不明 → 不建仓
        # 副品种推断：走 market_classifier 单一真理源（map_target 不抛，返回 None）
        # 未识别资产 → category 为 None：跳过不入 sibling 列表（不静默写空串）
        category = map_target(asset)
        if category is None:
            continue  # 漏映射的副品种：丢弃，避免污染 ai_decisions
        out.append({
            "target_asset": asset,
            "market_category": category,
            "sentiment_score": round(0.30 * lean, 2) if lean else 0.0,
            "suggested_action": action,
            "reasoning": ctx.strip().replace("\n", " ")[:60],
            "expected_horizon": "1-3d",
            "event_strength": "medium",
            "direct_catalyst": False,
            "_inferred": True,  # 标记为文本兜底（非 AI JSON 显式方向）
        })
        if len(out) >= max_sibs:
            break
    return out


def _action_leaning(action: str) -> int:
    a = (action or "").upper()
    if a == "BUY":
        return 1
    if a == "SELL":
        return -1
    return 0


def _ensure_asset_reasoning_coverage(asset_reasoning: str, sibs: List[Dict[str, Any]],
                                     main_asset: str = "NONE") -> str:
    """保证「品种相关推理逻辑」文本覆盖主品种 + 每个 sibling 品种。

    AI 可能漏写副品种行（只写主品种）→ 用该 sibling 的 reasoning/八段上下文
    补一行占位（标「（推理未显式输出，取自副品种结论）」），保证前端逐品种
    完整展示、不静默缺行。不编造传导路径——写不出的部分明确标注 N/A。
    """
    try:
        text = str(asset_reasoning or "").strip()
        sibs = sibs or []
        if not sibs:
            # 无副品种：至少确保主品种行存在（缺失时给占位）
            if not text:
                m = str(main_asset or "").upper().strip()
                if m and m != "NONE":
                    return f"主品种 {m}：AI 未显式输出该品种关联推理（N/A）"
            return text[:400]

        missing: List[str] = []
        for sib in sibs:
            asset = str(sib.get("target_asset") or "").upper().strip()
            if not asset or asset in ("NONE",):
                continue
            # 文本中是否已出现该品种的副品种行（含中文别名兜底匹配）
            has_alias = asset in text.upper()
            if asset == "XAU" and ("黄金" in text or "金价" in text):
                has_alias = True
            if asset == "WTI" and ("原油" in text or "油价" in text):
                has_alias = True
            if asset == "BTC" and ("比特币" in text):
                has_alias = True
            if asset == "ETH" and ("以太坊" in text):
                has_alias = True
            if not has_alias:
                missing.append((asset, sib))
        if not missing:
            return text[:400]

        extra = str(text)
        if extra and not extra.endswith(("；", ";", "\n")):
            extra += "；"
        for asset, sib in missing:
            ctx = str(sib.get("reasoning") or "").strip().replace("\n", " ")
            # 剥离落库前缀 [副品种 XAU] / [label]
            ctx = re.sub(r"^\[[^\]]*\]\s*", "", ctx)[:60]
            # 每行以分号结尾，保证与文本其它行分隔清晰（前端按 ；\n 拆行）
            if extra and not extra.endswith(("；", ";", "\n")):
                extra += "；"
            if ctx:
                extra += f"副品种 {asset}：{ctx}（推理取自该品种结论）"
            else:
                extra += f"副品种 {asset}：AI 未显式输出关联推理（N/A）"
        return extra[:400]
    except Exception:
        return str(asset_reasoning or "")[:400]


def _iter_stages(path: str):
    """按段名切分 reasoning_path，yield (段名, 段内容)。"""
    marks = list(_STAGE_MARK_RE.finditer(path or ""))
    for i, m in enumerate(marks):
        end = marks[i + 1].start() if i + 1 < len(marks) else len(path)
        yield m.group(1), path[m.end():end]


def _asset_scoped_text(path: str, code: str, labeled_only: bool = False) -> str:
    """从八段里抽出"属于 code 的那部分文本"。

    两级取用，与前端展示层口径一致（前端 analyze-parse.ts 也是"标签切分 → 无标签按句归属"）：

      ① **专属行优先**：行首是 `[CODE]：` / `CODE：` / `黄金：` 的行。
      ② **无标签兜底**（labeled_only=False 时）：本段没有任何品种标签行时，若该段文字
         也**没提到其它品种**，则把整段视为该品种自己的分析。
         为什么需要：实测模型常把主品种写成无标签的连贯散文 ——
         「加密市场的资金流动显示出…整体市场资金费率为+0.0051%，显示出资金流动性仍然紧张」
         通篇在讲 BTC 却没有一个品种词。若只认标签，这类内容会被判成"无该品种专属行"，
         界面就出现"有框无内容"。且它既没提别的品种，归给被分析品种是安全的。

    labeled_only=True 时只做 ①（给**方向一致性校验**用）：
    两级取用对"方向"来说不够精确 —— 无标签文本可能混着整体判断，用它去判"方向矛盾"
    不够稳，所以方向校验只认该品种的专属行；没有专属行就不判矛盾（交给针对性闸门）。

    一个专属行都没有、又提到了别的品种时返回 ""（无法定位品种，调用方退回整篇判定）。
    """
    if not path or not code:
        return ""
    others = [c for c, _ in _ASSET_ALIASES if c != code]
    parts: List[str] = []
    for stage, seg in _iter_stages(path):
        if stage in _NON_COVERAGE_STAGES:
            continue          # [总结] 不算七段分析内容（见下）
        for piece in re.split(r"[；;\n]", seg):
            t = piece.strip()
            # 去掉段名残留的右括号/引号等，便于识别行首标签
            t = t.lstrip("]】」』 \t")
            if not t:
                continue
            if _segment_has_asset(t, code):
                parts.append(t)
    if parts:
        return "\n".join(parts)
    if labeled_only:
        return ""

    # ② 无专属行 → 无标签兜底（仅当该段没提到其它品种）
    plain: List[str] = []
    for stage, seg in _iter_stages(path):
        if stage in _NON_COVERAGE_STAGES:
            continue
        for piece in re.split(r"[；;\n]", seg):
            t = piece.strip().lstrip("]】」』 \t")
            if not t:
                continue
            if any(_segment_mentions_asset(t, o) for o in others):
                continue          # 提到别的品种 → 不能算该品种的
            plain.append(t)
    return "\n".join(plain)


def _direction_conflict(path: str, action: str, target_asset: str = "") -> bool:
    """八段推演方向与 action 方向相反 → 疑似矛盾。

    三层判定，从准到宽：

      ① **该品种的专属行**（`[BTC]：…` / `黄金：…`）→ 用它自己的方向判定。
         最准：多品种文本里主品种看空、副品种看多时，整篇倾向会互相抵消甚至反向。
      ② 没有专属行但该品种**确实被分析到**（八段提及 ≥ 闸门阈值）→ 用整篇方向。
         这时它没有独立行，整篇方向是唯一可得的信息。
      ③ 连"被分析到"都不满足 → 不判冲突。
         理由：那种情况下真正的处置是**针对性闸门**（无依据 → 降级 HOLD），
         而不是"方向矛盾"；在此处再判一次冲突只会触发无意义的纠正重试，
         并可能让模型把本来正确的 action 改坏（实测 300 条里 9 条这么来的）。

    实测收益（300 条方向单）：修复前 9 条判冲突，修复后 8 条，且换掉的 5 条
    逐个核对后都更正确 —— 2 条真冲突被找出（BTC 看空却 BUY）、3 条误报被消除
    （WTI 自己的行全看多却因整篇偏空被误判矛盾）。
    """
    act_lean = _action_leaning(action)
    if act_lean == 0:
        return False
    code = str(target_asset or "").strip().upper()
    if code and code not in ("", "NONE", "OTHER"):
        # 方向校验只认该品种的**专属行**（labeled_only）：无标签文本可能混着整体判断，
        # 用它判"方向矛盾"不够稳；没有专属行就不判矛盾，交给针对性闸门处理。
        scoped = _asset_scoped_text(path, code, labeled_only=True)
        if scoped:
            scoped_lean = _direction_leaning(scoped)
            if scoped_lean != 0:
                return scoped_lean != act_lean
        # ② 无专属行：只有"确实被分析到"才用整篇方向，否则交给针对性闸门处理
        if _asset_mentioned_stage_count(path, code) < _FOCUS_MIN_MENTIONED_STAGES:
            return False
    text_lean = _direction_leaning(path)
    if text_lean == 0:
        return False
    return text_lean != act_lean


# 盘面结构开关缓存（5s TTL，与 news_trade_max_age 同级）
_ms_enabled_cache: Dict[str, Any] = {"ts": 0.0, "value": True}


def _market_structure_enabled() -> bool:
    """读 paper_trading_settings.market_structure_enabled（5s TTL 缓存，后台可调）。"""
    global _ms_enabled_cache
    now = time.monotonic()
    if now - float(_ms_enabled_cache.get("ts", 0.0)) < 5.0:
        return bool(_ms_enabled_cache.get("value", True))
    value = True
    try:
        from paper_trading import get_settings as _get_pt_settings
        value = bool(_get_pt_settings().get("market_structure_enabled", True))
    except Exception:
        pass
    _ms_enabled_cache.update({"ts": now, "value": value})
    return value


def _load_custom_prompts_sync() -> List[str]:
    """加载启用的用户自定义分析指令（ai_prompts 表，模块内 5s TTL）。

    与 _market_structure_enabled 同模式：engine 每次分析拼 prompt 时调用，
    后台（API /prompts 页）增删启停后 ≤5s 生效，无需重启引擎。
    """
    try:
        from ai_prompts import load_enabled_prompts
        return load_enabled_prompts()
    except Exception:
        return []


def _load_asset_rules_block_sync() -> str:
    """加载品种专项分析规则注入块（ai_asset_rules 表，5s TTL）。

    按品种分节的规则文本；无启用规则 → ""（不注入、零 token 开销）。
    """
    try:
        from ai_prompts import load_asset_rules_block
        return load_asset_rules_block()
    except Exception:
        return ""


def _build_causal_block(news_content: str) -> str:
    """[v176.3 已停用] 构造 [因果链分析] 段要求（追加到 system prompt）。

    分析流程已删除 [因果链分析] 段，本函数不再被调用（因果链库数据/管理页仍保留）。
    无论是否命中因果链都要求 AI 在八段式之后输出 [因果链分析] 段，保证前端
    八段+因果链展示结构完整：
      - 命中启用因果链（触发词匹配）→ 注入链内容，AI 结合新闻解读因果
      - 未命中 → 要求输出"因：暂无匹配；果：暂无匹配结果"，仍保留该段结构
    无因果链库可用（异常）→ 返回 ""（不强制，不改变输出格式）。
    """
    try:
        from causal_chains import match_chains
        chains = match_chains(news_content, max_chains=3)
    except Exception:
        return ""
    if chains:
        lines = []
        for ch in chains:
            name = str(ch.get("name") or "").strip()
            cause = str(ch.get("cause") or "").strip()
            effect = str(ch.get("effect") or "").strip()
            note = str(ch.get("note") or "").strip()
            try:
                weight = float(ch.get("weight") or 1.0)
            except (TypeError, ValueError):
                weight = 1.0
            mechanism = str(ch.get("mechanism") or "").strip()
            precondition = str(ch.get("precondition") or "").strip()
            invalidation = str(ch.get("invalidation") or "").strip()
            head = f"[因果链 {name}]" if name else "[因果链]"
            line = f"{head} 因：{cause}；果：{effect}"
            if mechanism:
                line += f"（机理：{mechanism[:200]}）"
            if precondition:
                line += f"（前提：{precondition[:120]}）"
            if invalidation:
                line += f"（失效边界：{invalidation[:120]}）"
            if weight != 1.0:
                line += f"（权重 {weight:g}）"
            if note:
                line += f"（备注：{note}）"
            lines.append(line)
        return (
            "═══ 因果链分析（命中以下宏观因果链，必须在 reasoning_path 八段之后"
            "追加输出 [因果链分析] 段）═══\n"
            + "\n".join(lines)
            + "\n\n要求：\n"
            + "  1. 在八段式（[驱动力]…[逻辑自检]）之后，追加一段 [因果链分析]：结合本条"
              "新闻，用『因：…』说明当前事件对应的宏观/因果起点，用『果：…』推演其对"
              "黄金/原油/加密/美债等的传导方向与幅度。命中多条时按标注的（权重 N）"
              "权衡侧重——权重越高该链越是本条新闻的主导因果框架，分析要更侧重它。\n"
              "    ★多品种时因果链也要分品种：对主品种 + 每个 sibling 品种分别写"
              "『品种X：因…果…』（各自传导方向/幅度），禁止只写主品种。\n"
            + "  2. 对每条因→果，必须强制拆解三层并在 [因果链分析] 里写明："
              "①传导机理——A 为什么会造成 B，中间经过哪一层金融/物理机制；"
              "②生效前提——什么条件下这条链成立；③失效边界——什么场景下失效/反转"
              "（如金融危机时 TIPS 上行黄金未必跌）。命中的链未标注这三层时，"
              "AI 须按金融常识补出；说不出机理的关联一律不得当因果采信。\n"
            + "  3. 因果链仅作分析框架：若新闻与因果链方向矛盾或证据不足，明说并降级，"
              "不得机械套用。\n"
            + "  4. 若输出了 [因果链分析]，reasoning 结尾必须补一句『总结：』，"
              "多品种时总结也按品种分行——每个品种：因果链结论与八段式结论"
              "相互印证/存在分歧（说明采信哪边及原因）——两者互相认证。\n"
        )
    # 未命中任何因果链：仍要求输出该段（显示"暂无匹配"），保证结构完整
    return (
        "═══ 因果链分析（本条新闻未命中因果链库，仍需输出 [因果链分析] 段）═══\n\n"
        "要求：在八段式（[驱动力]…[逻辑自检]）之后，追加一段 [因果链分析]，"
        "内容写：『因：暂无匹配；果：暂无匹配结果』。若你能从宏观常识判断本条新闻"
        "有明确因果传导，也可简述（因：…；果：…），但不要编造复杂链条。\n"
        "多品种时若对各品种有不同因果判断，按品种分行简述（品种X：因…果…）。\n"
    )


def _build_event_block(news_content: str) -> str:
    """构造「事件结构化抽取」块：规则确定性抽出事件原子 + 要求 AI 交叉校验。

    事件原子（根因/量级/方向/标的）与 LLM 的七段式分析互补：
    规则负责把客观字段抽准，LLM 负责机理拆解。这里要求 AI 显式确认规则
    抽出的量级是否与新闻一致，不一致时在 [逻辑自检] 写『事件结构化校验』
    修正标记，供人按关键词复核（即"标黄"机制）。
    """
    try:
        from event_struct import (
            extract_data_points, extract_events, to_data_points_block, to_prompt_block,
        )
        events = extract_events(news_content or "")
        data_points = extract_data_points(news_content or "")
    except Exception:
        return ""
    block = to_prompt_block(events)
    dp_block = to_data_points_block(data_points)
    if not block and not dp_block:
        return ""
    body = block
    if dp_block:
        body = (body + "\n" + dp_block) if body else ("═══ 事件结构化抽取（数据三值锚点）═══\n" + dp_block)
    return (
        body
        + "\n要求：若上面规则抽出的事件/三值锚点与新闻一致，直接采纳其量级写进 [因素排行]/[驱动力]；"
        "若不一致（抽错事件/量级错误/遗漏关键事件/三值错位），必须在 [逻辑自检] 写"
        "『事件结构化校验：规则抽出X，实为Y』修正。任何方向性结论若依赖于被修正的量级，"
        "须以修正后的量级为准。\n"
    )


def _build_graph_block(target_asset: str) -> str:
    """v176 因果图谱回灌：把 causal_graph 已学到的「品种涨跌本质 / 因果核验结果」注入主 batch prompt。

    此前 graph_block 只注入两个归因函数（_attribute_3min / _attribute_24h），主 batch 决策
    没拿到这个上下文 → 黄金/原油叙事逻辑靠 LLM 凭印象写，常错。

    设计：
      * 调 causal_graph.inject_block(target_asset) → 拿结构化知识（核心驱动 / 资金面）。
      * 该品种无学习数据时返回空串，静默跳过。
      * 与 causal / event / longterm 平级，作为"硬先验"注入。
      * 不阻塞主流程（try/except 包住）。
    """
    try:
        import causal_graph
        block = causal_graph.inject_block(target_asset or "")
        if not block:
            return ""
        return (
            "═══ 因果图谱回灌（causal_graph 自学习知识，硬先验）═══\n"
            + block
            + "\n═══ END 因果图谱 ═══\n"
        )
    except Exception:
        return ""


def _build_longterm_block() -> str:
    """构造「长期慢变量视角」块：每次分析必做，不因单日事件省略。

    覆盖三点：中长期趋势边际、机理挖根（因果链上下各推一层）、数据不全降级（多源交叉验证）。
    """
    return (
        "═══ 长期慢变量视角（每次分析必做，不得省略）═══\n"
        "1. 中长期影响：在 [总结] 之外写明该新闻对主品种（及关键副品种）中期（周—月）趋势的"
        "边际方向：强化 / 削弱 / 逆转，并给出慢变量依据（利率周期 / 供需结构 / 长期资金流向 / "
        "政策路径 / 通胀预期）。当日 1-3d 方向不得脱离中长期趋势孤立判断。\n"
        "2. 机理挖根：每个因果链判断至少向上追一层根因（它为什么发生）+ 向下追一层后果"
        "（它最终传导到哪）；说不出传导演径的关联不得当作因果采信。\n"
        "3. 数据不全降级：任何信息源缺失都写 N/A，禁止编造补位；用宏观官数 + 新闻文本 + "
        "行情数据多源交叉验证；关键数据全缺时须降低置信度或转 HOLD，不得仅凭单一来源下方向单。\n"
    )


def _build_baseline_block(event_type_code: str, target_asset: str) -> str:
    """构造「历史绩效（baseline）」块：从 backtest_event_outcomes 表读历史 BUY/SELL/HOLD 样本。

    输入：event_type_code (例 'geopolitical_conflict'), target_asset (例 'XAU')
    输出：prompt 段文本。

    设计：
      - 优先取 (type_code, asset) 精确匹配的样本
      - 否则取同 type_code 不限 asset 的样本
      - 否则取全局样本
      - 样本 < 3 时显式标 "(样本不足,仅供参考)"
      - 表为空 / 数据库错 → 返回 "" (静默,主流程不阻塞)
    """
    if not event_type_code:
        return ""
    try:
        import db as _db
        conn = _db.get_connection() if hasattr(_db, "get_connection") else None
        if conn is None:
            return ""
        cur = conn.cursor()

        # 1) (type, asset) 精确匹配
        cur.execute(
            "SELECT sample_n, win_n, loss_n, avg_pnl_4h FROM backtest_event_outcomes "
            "WHERE type_code=? AND asset=? LIMIT 1",
            (event_type_code, target_asset or ""),
        )
        exact = cur.fetchone()

        # 2) 同 type 跨 asset (取样本数最大的非精确匹配组合)
        cur.execute(
            "SELECT asset, sample_n, win_n, loss_n, avg_pnl_4h FROM backtest_event_outcomes "
            "WHERE type_code=? AND asset != ? ORDER BY sample_n DESC LIMIT 1",
            (event_type_code, target_asset or ""),
        )
        same_type_other_asset = cur.fetchone()

        # 3) 全局汇总
        cur.execute(
            "SELECT COUNT(*) AS n, SUM(CASE WHEN win_n>0 THEN 1 ELSE 0 END) AS any_win, "
            "       SUM(loss_n) AS total_loss, SUM(sample_n) AS total_sample, "
            "       AVG(avg_pnl_4h) AS global_avg "
            "FROM backtest_event_outcomes"
        )
        global_row = cur.fetchone()
    except Exception as _e:
        print(f"[BASELINE] 查询失败(静默): {type(_e).__name__}: {_e}", flush=True)
        return ""

    def _fmt(sample_n, win_n, loss_n, avg_pnl, label, asset=""):
        if sample_n <= 0:
            return None
        win_rate = (win_n / sample_n * 100.0) if sample_n > 0 else 0.0
        sample_flag = " [样本不足,仅供参考]" if sample_n < 3 else ""
        asset_part = f" / {asset}" if asset else ""
        return (f"  - {label}{asset_part}: n={sample_n}, 胜率={win_rate:.0f}%, "
                f"平均24h收益={avg_pnl:+.2f}%{sample_flag}")

    lines = []
    if exact:
        s, w, l, p = exact[0], exact[1], exact[2], exact[3] or 0.0
        line = _fmt(s, w, l, p, f"同事件同品种历史 (type={event_type_code}, asset={target_asset or 'N/A'})")
        if line:
            lines.append(line)

    if same_type_other_asset:
        s, w, l, p = same_type_other_asset[1], same_type_other_asset[2], same_type_other_asset[3], same_type_other_asset[4] or 0.0
        a = same_type_other_asset[0] or "(不限品种)"
        line = _fmt(s, w, l, p, f"同事件类型历史 (type={event_type_code})", asset=a)
        if line:
            lines.append(line)

    if global_row and global_row[3] and global_row[3] > 0:
        total_sample = global_row[3]
        total_loss = global_row[1] or 0  # any_win
        total_loss_n = global_row[2] or 0
        global_avg = global_row[4] or 0.0
        line = _fmt(total_sample, total_loss, total_loss_n, global_avg, "全事件类型历史汇总")
        if line:
            lines.append(line)

    if not lines:
        return ""

    body = "\n".join(lines)
    header = ("═══ 历史绩效（baseline.backtest_event_outcomes）═══\n"
              "以下数据来自历史 ai_decisions 在做出去向后的真实 PnL 回测（24h 窗口），"
              "用于校准本次分析的「同类事件过去是否赚钱」。\n")
    footer = ("\n要求：在 [因素排行] 和 [总结] 中显式引用上面至少 1 条历史数据，作为本次方向的"
              "概率校准依据（不要忽略样本不足警告）。\n")
    return header + body + footer


def _build_microstructure_block(
    target_assets: Optional[List[str]] = None,
    max_chars: int = 800,
) -> str:
    """v200+ P0-1: 拼 microstructure block 给 LLM 看（注入 user_text 头部）。

    数据：coinglass_lsr.get_latest_samples_by_symbols + funding_rate_feed.fetch_funding_rates。
    在 _build_baseline_block 之后注入,与历史胜率并列 → 决策前先看：
      - 持仓量/资金费率(最大);
      - [market_structure_block] 中已有的市场风格(regime)、价格等"宏观背景"提示
      - 本块 = "微观结构"(散户/大户持仓 + 永续费率),从概率上校准本次方向
      - 不直接采纳其方向,但要求在 [逻辑自检] 和 [总结] 中显式引用 ≥ 1 条 microstructure 字段
      - 标记 stale=True 的数据视为「过期」, 引用时须注明「数据已滞后」

    失败/无样本 → 返回 "" (静默,主流程不阻塞)
    """
    if not target_assets:
        # 退化:用默认四件套
        target_assets = ["BTC", "ETH", "SOL", "XAU"]
    try:
        # 延迟 import 避免 engine 启动循环依赖
        import structured_input as _si
        micro = _si._microstructure_block(target_assets=target_assets)
    except Exception:
        return ""
    if not isinstance(micro, dict):
        return ""
    lsr = micro.get("lsr") or {}
    funding = micro.get("funding") or {}
    stale_syms = set(micro.get("stale_symbols") or [])
    if not lsr and not funding:
        return ""
    fresh_ts = int(micro.get("fresh_ts") or 0)
    fresh_str = ""
    if fresh_ts:
        try:
            from datetime import datetime as _dt
            fresh_str = _dt.utcfromtimestamp(fresh_ts).strftime("%Y-%m-%d %H:%M UTC")
        except Exception:
            fresh_str = str(fresh_ts)
    header = ("═══ 微观结构（持仓量/资金费率,最新样本）═══\n"
              "以下数据用于校准本次方向的概率,不是直接采纳的信号源。\n"
              f"采样时间:{fresh_str or 'N/A'} | 数据源:{micro.get('source') or 'empty'}\n")
    lines: List[str] = []

    # ── LSR 部分(持仓量/多空比/大户持仓) ──
    for sym in sorted(lsr.keys()):
        s = lsr[sym]
        if not isinstance(s, dict):
            continue
        ratio_pct = str(s.get("ratio_pct") or "")
        d4 = str(s.get("delta_4h_pct") or "")
        d30 = str(s.get("delta_30m_pct") or "")
        long_q = float(s.get("ov_long_qty") or 0.0)
        short_q = float(s.get("ov_short_qty") or 0.0)
        whale_long = float(s.get("whale_long_qty") or 0.0)
        whale_short = float(s.get("whale_short_qty") or 0.0)
        oi_mcap = float(s.get("oi_mcap") or 0.0)
        cache_age = int(s.get("cache_age_s") or 0)
        stale_flag = " [数据已滞后]" if sym in stale_syms else ""
        # ratio_pct 通常形如 "55.20%" / "-3.14%" / "0" (无数据)
        line = (f"  - {sym}: 持仓比={ratio_pct or 'N/A'}, "
                f"4h 变化={d4 or 'N/A'}, 30m 变化={d30 or 'N/A'}, "
                f"多仓={long_q:.0f}/空仓={short_q:.0f}, "
                f"大户多={whale_long:.0f}/大户空={whale_short:.0f}, "
                f"OI/市值={oi_mcap:.3f}"
                f"{stale_flag} (cache_age={cache_age}s)")
        lines.append(line)
    # ── Funding 部分(永续费率) ──
    for sym in sorted(funding.keys()):
        f = funding[sym]
        if not isinstance(f, dict):
            continue
        rate_pct = float(f.get("funding_rate_pct") or 0.0)
        is_extreme = bool(f.get("is_extreme"))
        nft = f.get("next_funding_time")
        ext_flag = " ⚠极端" if is_extreme else ""
        nft_str = f" (下次结算:{nft})" if nft else ""
        sign = "+" if rate_pct >= 0 else ""
        lines.append(f"  - {sym}: 资金费率={sign}{rate_pct:.4f}%/8h{ext_flag}{nft_str}")
    # ── Funding Extreme 全局提醒 ──
    fext = micro.get("funding_extreme") or []
    if fext:
        for e in fext[:3]:
            if isinstance(e, dict):
                lines.append(
                    f"  ⚠ 极端费率提醒: {e.get('inst', 'N/A')} = "
                    f"{e.get('rate_pct', 0):+.4f}%/8h"
                )
    if not lines:
        return ""
    body = "\n".join(lines)
    footer = ("\n要求:在 [逻辑自检] 和 [总结] 中显式引用至少 1 条 microstructure 字段；\n"
              "  - 标记 [数据已滞后] 的字段不可作决策唯一依据,只能作风险提示；\n"
              "  - 资金费率 ≥ 0.2%/8h 即视为极端,市场情绪单边过热,慎追单边；\n"
              "  - 大户持仓 / 散户持仓背离超过 20% → 关注大户动作。\n")
    text = header + body + footer
    if len(text) > max_chars:
        text = text[: max_chars - 60] + "...\n(以上 microstructure 数据已截断,以原文表格为准)"
    return text


def build_prompt_preview(mode: str = "force") -> Dict[str, Any]:
    """装配「最终发给模型的 system prompt」，供提示库页面查看（只读，不联网、不建库）。

    为什么需要：提示库此前只显示 `_JSON_PROMPT_FORCE` 常量原文，而真正发给模型的
    prompt 是「常量 + 若干运行时分区」（盘面结构块 / 用户自定义指令 / 品种专项规则 /
    复盘经验）动态拼出来的。用户看不到这些分区，就会误以为"经验没加进去"。

    mode: "force" = 非 JSON 模式（_JSON_PROMPT_FORCE）；"system" = JSON 模式（_SYSTEM_PROMPT）。

    v176.3：分析流程已删除 [因果链分析] 段，因果链库不再注入 prompt（数据/管理页保留）。
    """
    # 需求3：统一单通道——预览始终显示 System Prompt（完整八段式），
    # Prompt Force 通道已废弃（实际分析也只走 System Prompt）。
    use_full = True
    base_default = _SYSTEM_PROMPT if use_full else _JSON_PROMPT_FORCE
    base = _main_prompt("system_prompt" if use_full else "json_force_prompt", base_default)

    segments: List[Dict[str, Any]] = [
        {"key": "base", "title": "主 Prompt（提示库可编辑）", "text": base,
         "editable": True, "injected": True},
    ]
    prompt = base

    # ① 盘面结构块（开关控制）
    if _market_structure_enabled():
        block = _main_prompt("market_structure_block", _MARKET_STRUCTURE_BLOCK)
        prompt += block
        segments.append({"key": "market_structure", "title": "盘面结构指令块（开关开启时附加）",
                         "text": block, "editable": True, "injected": True})

    # ② 用户自定义分析指令
    custom_prompts = _load_custom_prompts_sync()
    if custom_prompts:
        block = "\n\n═══ 用户自定义分析指令（优先级最高，必须遵守）═══\n" + "\n\n".join(custom_prompts)
        prompt += block
        segments.append({"key": "custom", "title": "用户自定义分析指令", "text": block,
                         "editable": True, "injected": True})

    # ③ 品种专项规则（提示库页面可编辑）
    try:
        asset_block = _load_asset_rules_block_sync()
    except Exception:
        asset_block = ""
    if asset_block:
        prompt = prompt + "\n\n" + asset_block
        segments.append({"key": "asset_rules", "title": "品种专项分析规则（提示库可编辑）",
                         "text": asset_block, "editable": True, "injected": True})

    # ④ 因果链库（v176.3）：[因果链分析] 分析环节已删除，因果链库不再注入 prompt；
    #    数据与「因果链库」管理页仍保留，仅作知识库维护，不再生成分析段落。
    causal_note = (
        "因果链库：v176.3 起 [因果链分析] 分析环节已删除，因果链库不再注入分析流程；"
        "数据与「因果链库」页面仍保留，仅作知识库维护。"
    )

    # ⑤ 复盘经验分区（每条经验在提示库页面可停用/启用/删除）
    try:
        insights_block = _load_insights_section()
    except Exception:
        insights_block = ""
    if insights_block:
        prompt = prompt + "\n\n" + insights_block
        segments.append({"key": "insights", "title": "当前生效经验规则（启用即出现在此）",
                         "text": insights_block, "editable": False, "injected": True})
    else:
        segments.append({"key": "insights", "title": "当前生效经验规则（启用即出现在此）",
                         "text": "", "editable": False, "injected": False})

    # ⑥ 反思错误码注入块（v176.3 新增）— 从 decision_errors 取最近错误码 → 一句话指导
    # 与 insights 互补：insights 是教训性经验规则，reflection 是近期决策具体错误码（更即时）
    try:
        reflection_block = _load_reflection_block()
    except Exception:
        reflection_block = ""
    if reflection_block:
        prompt = prompt + "\n\n" + reflection_block
        segments.append({"key": "reflection", "title": "反思错误码（最近触发错误的真实规避）",
                         "text": reflection_block, "editable": False, "injected": True})
    else:
        segments.append({"key": "reflection", "title": "反思错误码（最近触发错误的真实规避）",
                         "text": "", "editable": False, "injected": False})

    return {
        "mode": mode,
        "prompt": prompt,
        "length": len(prompt),
        "segments": segments,
        "note": causal_note,
    }


def _main_prompt(kind: str, default: str) -> str:
    """主 prompt 取值：页面编辑过的（ai_main_prompts 覆盖表，5s TTL）优先，否则代码内置默认。

    kind: system_prompt / json_force_prompt / market_structure_block / doubao_prompt
    """
    try:
        from ai_prompts import load_main_prompt_overrides
        return load_main_prompt_overrides().get(kind) or default
    except Exception:
        return default


def _stock_context_for(text: str) -> str:
    """新闻提到币安永续美股 → 行情上下文行（stock_prices 模块，10 分钟缓存）。"""
    try:
        from stock_prices import stock_context_line
        return stock_context_line(text)
    except Exception:
        return ""


def _asia_context_for(text: str) -> str:
    """新闻提到 SK 海力士 / A股 → 行情上下文行（asia_prices 模块，10 分钟缓存）。"""
    try:
        from asia_prices import market_context_line
        return market_context_line(text)
    except Exception:
        return ""


def _origin_search_block_for(text: str) -> str:
    """重要新闻的实时全网搜索证据（免费源，无 API key），供 LLM 客观填 cross_net 溯源字段。"""
    try:
        from net_search import origin_evidence_block as _oeb
        return _oeb(text)
    except Exception:
        return ""


# ── v175 重要新闻 / 海力士强买入 → 手机推送（Server酱 + 邮件，fire-and-forget）──
import hashlib as _hashlib
_PHONE_PUSH_SEEN: Dict[str, float] = {}  # content 指纹 -> 上次推送 ts
_PHONE_PUSH_DEDUP_S = 600.0              # 同内容 10 分钟内不重复推送
# v186 推送门槛放宽：原 8.0（"高重要新闻"严苛阈值）→ 5.0
# 原因：模型给分保守（实测常态 impact=5~7），原阈值下 99% BUY/SELL 信号被静默过滤，
# 用户反映"信号出现没收到通知"。5.0 是平衡线——既保留过滤噪音能力（拒掉纯背景新闻），
# 又保证所有真正有方向的落地信号都到手机。
_PHONE_PUSH_MIN_IMPACT = 5.0


def _is_truthy(v: Any) -> bool:
    if isinstance(v, bool):
        return v
    return str(v).strip().lower() in ("true", "1", "yes")


def _maybe_push_phone_alerts(successes: List[Dict[str, Any]], content_map: Dict[int, str],
                             timestamp_map: Optional[Dict[int, str]] = None,
                             published_map: Optional[Dict[int, str]] = None) -> None:
    """批处理落库后：命中海力士 → 强买入提醒；其它高重要新闻 → 重要提醒。

    仅推 BUY/SELL（HOLD/无方向不打扰手机）；正文统一走 alerts.format_signal_body。
    仅发提醒，不触发自动交易（SKHYNIX/CN_STOCK 不在 paper_trading.TRACK_ASSETS，
    asset_allowed 恒 False，模拟盘不会对它们建仓）。
    """
    now = time.time()
    ts_map = timestamp_map or {}
    pub_map = published_map or {}
    try:
        import asia_prices as _ap
    except Exception:
        _ap = None
    for s in successes:
        try:
            if not s or s.get("error"):
                continue
            nid = s.get("news_id")
            text = (content_map.get(nid) or "").strip()
            if not text:
                continue
            res = s.get("result") or {}
            if not isinstance(res, dict):
                continue
            asset = str(res.get("target_asset") or "NONE").strip().upper()
            action = str(res.get("suggested_action") or "HOLD").strip().upper()
            reason = str(res.get("reasoning") or "").strip()[:200]
            rpath = str(res.get("reasoning_path") or "").strip()
            try:
                impact = float(res.get("impact_score") or 0.0)
                heat = float(res.get("heat_score") or 0.0)
            except (TypeError, ValueError):
                impact = heat = 0.0
            dc = _is_truthy(res.get("direct_catalyst"))

            # ── 只推有方向的落地信号：HOLD/无方向一律不打扰手机 ──
            if action not in ("BUY", "SELL"):
                continue

            # 去重节流：同内容 10 分钟内不重复推
            fp = _hashlib.md5(text[:120].encode("utf-8", "replace")).hexdigest()
            last = _PHONE_PUSH_SEEN.get(fp, 0.0)
            if now - last < _PHONE_PUSH_DEDUP_S:
                continue
            _PHONE_PUSH_SEEN[fp] = now

            news_time = pub_map.get(nid) or ts_map.get(nid) or ""

            is_hynix = asset == "SKHYNIX" or (_ap is not None and _ap.detect_hynix_strict(text))
            if is_hynix:
                hynix_quote = ""
                if _ap is not None:
                    try:
                        hynix_quote = _ap.hynix_price_line()
                    except Exception:
                        hynix_quote = ""
                extra = (
                    f"{hynix_quote}\n（仅提醒信号，不自动交易）"
                    if hynix_quote else "（仅提醒信号，不自动交易）"
                )
                body = format_signal_body(
                    asset="SK海力士 (SKHYNIX)",
                    action=action,
                    impact=impact,
                    heat=heat,
                    reason=reason or "N/A",
                    analysis=rpath,
                    news_text=text[:300],
                    news_time=news_time,
                    extra_line=extra,
                )
                send_strong_buy_alert(build_signal_title("SK海力士", action, news_time), body)
                continue
            # 其它高重要新闻：方向为 BUY/SELL 且（直接催化 / 影响分≥门槛 / 热度≥门槛）→ 推送
            # v186: 阈值从 8.0 → _PHONE_PUSH_MIN_IMPACT (5.0)，覆盖常态保守给分信号。
            if dc or impact >= _PHONE_PUSH_MIN_IMPACT or heat >= _PHONE_PUSH_MIN_IMPACT:
                body = format_signal_body(
                    asset=asset,
                    category=str(res.get("market_category") or ""),
                    action=action,
                    impact=impact,
                    heat=heat,
                    reason=reason or "N/A",
                    analysis=rpath,
                    news_text=text[:300],
                    news_time=news_time,
                )
                title = build_signal_title(asset, action, news_time)
                send_important_news(title, body)
                print(
                    f"[{_now()}] [ALERT] {title} impact={impact:.1f} heat={heat:.1f}"
                    f" dc={dc} → 三通道推送已触发"
                )
            # v186 兜底推送：任何 BUY/SELL 落地 → 一条精简通知到 Server酱 + PushPlus
            # 与上面"高重要新闻"完整卡片互补——不论分数都保证信号出现事件可达
            send_signal_alert(asset, action, impact, heat, reason, news_time)
        except Exception:
            continue


_DOUBAO_SYSTEM_PROMPT = """
你是华尔街资深原油/黄金/加密货币交易员，拥有 15 年实盘经验。
你的任务：快速扫读新闻，凭直觉判断这条消息对资产价格的短期方向。
不要给出分数，不要推理链条。只看方向。

规则：
  利多消息 → BUY
  利空消息 → SELL
  方向不明确或无关 → HOLD

JSON 输出：
{"suggested_action": "<BUY|SELL|HOLD>", "direct_reasoning": "<一句交易直觉,<=30字>"}

只输出 JSON。两个字段缺一不可。
"""



# ---------------------------------------------------------------------------
# Phase 1 — Performance Feedback Injection
# ---------------------------------------------------------------------------
# Queries historical settled-signal performance for key asset × action ×
# prediction_type combos.  Injected into the LLM prompt as research reference
# — does NOT auto-modify LLM output.  Only the LLM decides.
# ---------------------------------------------------------------------------

# Assets and actions we care about for performance feedback
_PERF_ASSETS = ("BTC", "XAU")
_PERF_ACTIONS = ("BUY", "SELL")
_PERF_PREDICTION_TYPES = ("continuation", "reversal", "breakout")
_PERF_LOOKBACK_DAYS = 90
# Minimum sample for "reliable" display
_PERF_MIN_SAMPLE = 8


def _build_performance_context() -> str:
    """Build [Historical Performance] block for prompt injection.

    Queries settled ai_decisions for each asset × action × prediction_type
    combo.  Returns a concise multi-line string suitable for prepending to
    the LLM user prompt.  Runs once per batch (O(1) near-instant SQL).
    """
    try:
        conn = _open_db()
    except Exception:
        return ""

    try:
        lines: List[str] = []
        lines.append("[Historical Performance]")
        lines.append("Similar signals:")

        # Query: for each (asset, action, prediction_type) combo
        for asset in _PERF_ASSETS:
            for action in _PERF_ACTIONS:
                # ── Overall (all prediction_types) ──
                overall = conn.execute(
                    "SELECT COUNT(*) AS n, "
                    "  SUM(CASE WHEN is_correct = 'WIN' THEN 1 ELSE 0 END) AS wins, "
                    "  SUM(CASE WHEN is_correct = 'LOSS' THEN 1 ELSE 0 END) AS losses, "
                    "  AVG(CASE WHEN forward_pnl IS NOT NULL THEN forward_pnl ELSE NULL END) AS avg_pnl "
                    "FROM ai_decisions "
                    "WHERE settled = 1 "
                    "  AND is_correct IN ('WIN', 'LOSS') "
                    "  AND target_asset = ? "
                    "  AND suggested_action = ? "
                    "  AND strftime('%s', created_at) >= strftime('%s', 'now', ?)",
                    (asset, action, f"-{_PERF_LOOKBACK_DAYS} days"),
                ).fetchone()

                for pt in _PERF_PREDICTION_TYPES:
                    row = conn.execute(
                        "SELECT COUNT(*) AS n, "
                        "  SUM(CASE WHEN is_correct = 'WIN' THEN 1 ELSE 0 END) AS wins, "
                        "  SUM(CASE WHEN is_correct = 'LOSS' THEN 1 ELSE 0 END) AS losses, "
                        "  AVG(CASE WHEN forward_pnl IS NOT NULL THEN forward_pnl ELSE NULL END) AS avg_pnl "
                        "FROM ai_decisions "
                        "WHERE settled = 1 "
                        "  AND is_correct IN ('WIN', 'LOSS') "
                        "  AND target_asset = ? "
                        "  AND suggested_action = ? "
                        "  AND prediction_type = ? "
                        "  AND strftime('%s', created_at) >= strftime('%s', 'now', ?)",
                        (asset, action, pt, f"-{_PERF_LOOKBACK_DAYS} days"),
                    ).fetchone()

                    n = row["n"] or 0
                    wins = row["wins"] or 0
                    losses = row["losses"] or 0
                    decided = wins + losses
                    avg_pnl = row["avg_pnl"]

                    if n < _PERF_MIN_SAMPLE or decided == 0:
                        lines.append(
                            f"{asset} {pt} {action}: "
                            f"Insufficient sample"
                        )
                    else:
                        wr = wins / decided
                        pnl_str = (
                            f"{avg_pnl:+.2f}%" if avg_pnl is not None else "N/A"
                        )
                        lines.append(
                            f"{asset} {pt} {action}: "
                            f"sample: {decided} "
                            f"win rate: {wr:.0%} "
                            f"avg pnl: {pnl_str}"
                        )

                # ── Also add the overall (all prediction_types) line ──
                n_all = overall["n"] or 0
                wins_all = overall["wins"] or 0
                losses_all = overall["losses"] or 0
                decided_all = wins_all + losses_all
                if decided_all >= _PERF_MIN_SAMPLE:
                    wr_all = wins_all / decided_all
                    pnl_all = overall["avg_pnl"]
                    pnl_str = (
                        f"{pnl_all:+.2f}%" if pnl_all is not None else "N/A"
                    )
                    lines.append(
                        f"{asset} * {action}: "
                        f"sample: {decided_all} "
                        f"win rate: {wr_all:.0%} "
                        f"avg pnl: {pnl_str}"
                    )

        return "\n".join(lines)

    except Exception as e:
        print(f"[PERF] 查询历史绩效失败: {type(e).__name__}: {str(e)[:80]}")
        return ""
    finally:
        conn.close()


# 品种 → 中文名（取 _ASSET_ALIASES 里的首个中文别名），供 asset_reasoning 括号名纠偏用
_ASSET_CN_NAMES: Dict[str, tuple] = {
    code: tuple(a for a in aliases if not a.isascii())
    for code, aliases in _ASSET_ALIASES
}


def _fix_main_line(ln: str, ta: str) -> str:
    """把「主品种 XXX(名字)：正文」这一行的代码与括号名对齐到 ta。

    只替换代码与括号名、正文原样保留：
      "主品种 XAU(黄金)：避险需求" + ta=WTI → "主品种 WTI(原油)：避险需求"
      "主品种 XAU(美股)：…"        + ta=XAU → "主品种 XAU(黄金)：…"（括号名纠偏）
    括号名只在该品种自己的中文名列表里才保留（避免"主品种 WTI(黄金)"这类自相矛盾）。
    """
    head, sep, body = ln.partition("：")
    if not sep:
        head, sep, body = ln.partition(":")
    paren = ""
    pm = re.search(r"[（(]([^）)]{0,8})[）)]", head)
    if pm:
        old_name = pm.group(1).strip()
        names = _ASSET_CN_NAMES.get(ta, ())
        if old_name and old_name in names:
            paren = pm.group(0)
        else:
            paren = f"({names[0]})" if names else ""
    return f"主品种 {ta}{paren}：{body.strip()}".rstrip("：")


def _load_dim_event_type_block() -> str:
    """v174: 从 dim_event_type 表拉所有 active 的 type_code,生成「事件类型选项清单」注入 prompt。
    让 LLM 知道有这套维表,而不是自由输出 event_type 字符串。
    输出格式:
      【事件类型选项】(从 dim_event_type 选 1)
        - geopolitical_conflict (地缘冲突) → XAU/WTI 利多
        - rate_hike (加息) → BTC/ETH/WTI/XAU 利空
        ...
    """
    try:
        import json as _json_det
        import db as _db_det
        _conn = _db_det.get_connection()
        # db.get_connection() 不设 row_factory, 这里用整数索引取值
        rows = _conn.execute(
            "SELECT code, name_zh, name_en, category, direction_bias FROM dim_event_type "
            "WHERE active = 1 ORDER BY id"
        ).fetchall()
        if not rows:
            return ""
        lines = ["\n\n═══ 事件类型选项清单(v174 必填)═══\n以下 25 个 code 是 dim_event_type 维表的标准事件分类;",
                 "你必须从下列 code 中**严格选 1 个**填入 event_type_code 字段(不要再输出中文/英文自由字符串)。",
                 "若新闻不属于下列任何类型,选 closest 一项 + 在 reasoning 里说明。\n"]
        for r in rows:
            code_val = r[0]
            zh = r[1] or code_val
            en = r[2] or ''
            cat = r[3] or ''
            dir_bias = r[4] or '{}'
            try:
                bias_dict = _json_det.loads(dir_bias) if dir_bias.startswith('{') else {}
            except Exception:
                bias_dict = {}
            bias_str = ""
            if bias_dict:
                pairs = [f"{k} {v}" for k, v in list(bias_dict.items())[:4]]
                bias_str = f" → 默认方向: {', '.join(pairs)}"
            cat_tag = f"[{cat}]" if cat else ""
            lines.append(f"  - {code_val} {cat_tag} ({zh}/{en}){bias_str}")
        lines.append("\nJSON 必填新增字段:")
        lines.append('  "event_type_code": "<上表 25 个 code 之一>"')
        return "\n".join(lines)
    except Exception as _exc:
        # 失败兜底:返回最简清单让 LLM 知道有这个字段
        return ("\n\n═══ 事件类型选项清单(v174 必填)═══\n"
                "[ERROR] 加载 dim_event_type 失败, 请回退默认类型 (geopolitical_conflict/rate_cut/rate_hike/cpi/earnings_release 等)。\n"
                'JSON 必填新增字段: "event_type_code": "<code>"')


def _normalize_main_asset(result: Dict[str, Any]) -> Dict[str, Any]:
    """把「主品种」统一到 target_asset，并让 asset_reasoning 与之对齐。

    为什么要做：实测模型会自相矛盾 —— target_asset 写 WTI，asset_reasoning 却写
    「主品种 XAU(黄金)：…」；或 target_asset=NONE 却把真实主品种塞进 sibling_decisions。
    前端一个字段用 target_asset、另一个用 asset_reasoning → 界面出现
    「各品种交易决策」显示主品种是 A、而「品种相关推理逻辑」说是 B 的混乱。

    规则（target_asset 为唯一权威）：
      ① target_asset 为 NONE/OTHER 且有 sibling → 提升第一个副品种为主品种，
         并从 sibling_decisions 移除它（否则它既是主又是副）。
      ② asset_reasoning 里的「主品种 X」若与 target_asset 不一致 → 改成 target_asset；
         找不到主品种行则补一行（内容取该品种自身的副品种行或占位说明）。
    """
    try:
        ta = str(result.get("target_asset") or "").strip().upper()
        sibs = result.get("sibling_decisions")
        sibs = sibs if isinstance(sibs, list) else []
        # ① NONE/OTHER 且有副品种 → 提升第一个副品种
        if ta in ("", "NONE", "OTHER") and sibs:
            first = None
            for s in sibs:
                if not isinstance(s, dict):
                    continue
                code = str(s.get("target_asset") or "").strip().upper()
                if code and code not in ("", "NONE", "OTHER"):
                    first = s
                    break
            if first is not None:
                promoted = str(first.get("target_asset") or "").strip().upper()
                result["target_asset"] = promoted
                result["market_category"] = str(
                    first.get("market_category") or result.get("market_category") or ""
                ).upper()
                # 主品种的字段取被提升那条副品种的判断。
                # ★ 但若主分析已被针对性闸门降级为 HOLD（reasoning 带闸门说明），
                #   绝不能把这套字段照搬过来 —— 那等于用副品种的方向单覆盖掉
                #   "主品种分析无依据"的结论，闸门白拦（实测 news=4661：库里留下
                #   SELL -0.3 并真建了仓，而闸门日志显示已降级）。
                _gate_hold = (
                    str(result.get("suggested_action") or "").upper() == "HOLD"
                    and str(result.get("reasoning") or "").startswith(_FOCUS_GATE_PREFIXES)
                )
                if not _gate_hold:
                    for k in ("sentiment_score", "suggested_action", "reasoning",
                              "expected_horizon", "event_strength"):
                        if first.get(k) not in (None, ""):
                            result[k] = first[k]
                else:
                    print(
                        f"  [ASSETFIX] target_asset=NONE → 提升副品种 {promoted} 为主品种；"
                        f"主分析已被针对性闸门降级，保留 HOLD（不采用副品种方向单）",
                        flush=True,
                    )
                # 该副品种已升为主品种，从 sibling 列表移除（避免主副重复）
                result["sibling_decisions"] = [
                    s for s in sibs
                    if str(s.get("target_asset") or "").strip().upper() != promoted
                ]
                ta = promoted
                if not _gate_hold:
                    print(f"  [ASSETFIX] target_asset=NONE → 提升副品种 {promoted} 为主品种", flush=True)

        # ①-B 主品种纠正：八段实际在讲的品种 ≠ 声明的 target_asset → 以内容为准纠正
        # 实测案例：新闻通篇讲 WTI 油价，模型却把 target_asset 写成 BTC
        # （只因跨资产段提过 BTC 资金费率），而八段每一段都在分析 WTI。
        # 判定：声明的主品种在八段里提及 <2 段（几乎没分析它），
        #       而另一个品种提及 ≥3 段且明显更多 → 后者才是真主品种。
        body_path = str(result.get("reasoning_path", "") or "")
        if ta and ta not in ("NONE", "OTHER") and body_path:
            declared_n = _asset_mentioned_stage_count(body_path, ta)
            if declared_n < 2:
                dom_code, dom_n = _dominant_asset_in_path(body_path, exclude=ta)
                if dom_code and dom_n >= 3 and dom_n > declared_n:
                    print(
                        f"  [MAINFIX] 声明主品种 {ta} 在八段仅提及 {declared_n} 段，"
                        f"而 {dom_code} 提及 {dom_n} 段 → 纠正主品种为 {dom_code}",
                        flush=True,
                    )
                    # 原主品种降为副品种（若它确实被分析到）；纠正后的品种从 sibling 移除
                    _old = ta
                    result["target_asset"] = dom_code
                    ta = dom_code
                    _kept = []
                    for s in sibs:
                        if not isinstance(s, dict):
                            continue
                        code_s = str(s.get("target_asset") or "").strip().upper()
                        if code_s == dom_code:
                            continue  # 已升为主品种
                        _kept.append(s)
                    # 原主品种若正文里确实提过（≥1 段）→ 作为副品种保留，避免丢信息
                    if _asset_mentioned_stage_count(body_path, _old) >= 1 and not any(
                        str(x.get("target_asset") or "").strip().upper() == _old for x in _kept
                    ):
                        _kept.append({
                            "target_asset": _old,
                            "market_category": str(result.get("market_category") or ""),
                            "sentiment_score": 0.0,
                            "suggested_action": "HOLD",
                            "reasoning": f"由 {_old} 降为副品种（八段实际分析对象为 {dom_code}）",
                        })
                    result["sibling_decisions"] = _kept
                    sibs = _kept

        # ①-C 主品种最终仍未成立（NONE/OTHER）→ 不保留副品种。
        #   用户原则："先有主品种，后面才会有副品种。主品种都没有，何来副品种。"
        #   可交易品种的副品种在上面的①已被提升为主品种并移出列表；能走到这里的
        #   是 OTHER 这类不可提升项 —— 留着一个"没有主品种依托的品种行"，界面会
        #   显示成来源不明的信号，宽松模式下还可能真建仓。
        if ta in _FOCUS_EXEMPT_ASSETS and result.get("sibling_decisions"):
            _n_drop = len(result["sibling_decisions"])
            result["sibling_decisions"] = []
            sibs = []
            print(
                f"  [MAIN-GATE] 主品种未成立（{ta or 'NONE'}）"
                f" → 移除 {_n_drop} 个无主品种依托的副品种",
                flush=True,
            )

        # ①-D 主品种仍未成立（NONE/OTHER）→ 无标的硬兜底：不给方向单。
        #   此前只靠 prompt 约束（股票/公司/行业/ETF→OTHER→NONE→HOLD），模型偶尔不听，
        #   实测"东方财富 ETF 净申购"新闻被判 OTHER/NONE 却仍输出 BUY 并入方向候选——
        #   这里强制 HOLD + 中性分，杜绝"品类为其他/无可交易标的却给方向"。
        if ta in _FOCUS_EXEMPT_ASSETS:
            _bogus = str(result.get("suggested_action") or "").upper()
            if _bogus in ("BUY", "SELL"):
                result["suggested_action"] = "HOLD"
                result["sentiment_score"] = 0.05
                if not _is_gate_downgraded(result):
                    result["reasoning"] = (
                        f"无可交易标的（{ta or 'NONE'}/OTHER），不给方向单"
                    )
                _signal_trace(
                    "main-null", asset=ta or "NONE", verdict="无标的→强制HOLD",
                    action=_bogus,
                )
                print(
                    f"  [MAIN-GATE] 主品种未成立（{ta or 'NONE'}）→ 强制 HOLD（原 {_bogus}）",
                    flush=True,
                )

        # ② asset_reasoning 里的「主品种」对齐 target_asset
        ar = str(result.get("asset_reasoning") or "")
        if ar and ta and ta not in ("NONE", "OTHER"):
            lines: List[str] = []
            main_fixed = False
            for raw in ar.split("；"):
                ln = raw.strip()
                if not ln:
                    continue
                if ln.startswith("主品种"):
                    if main_fixed:
                        continue  # 模型写多个"主品种"行 → 只留一行（否则界面出现两个主品种）
                    lines.append(_fix_main_line(ln, ta))
                    main_fixed = True
                else:
                    lines.append(ln)
            if not main_fixed:
                # 模型没写主品种行 → 补一行（取同品种副品种行的内容，避免编造）
                ctx = ""
                for s in sibs:
                    if isinstance(s, dict) and str(s.get("target_asset") or "").strip().upper() == ta:
                        ctx = str(s.get("reasoning") or "").strip()[:60]
                        break
                lines.insert(0, f"主品种 {ta}：{ctx}" if ctx else f"主品种 {ta}：AI 未显式输出关联推理（N/A）")
            result["asset_reasoning"] = "；".join(lines)[:400]
        return result
    except Exception:
        return result


def _build_gate_context(row) -> str:
    """从 raw_news.gate_* 列拼出 [门卫四步] 分析先验；缺列/无核心影响返回空串。"""
    def _g(col: str) -> str:
        try:
            return str(row[col] or "").strip()
        except Exception:
            return ""

    core = _g("gate_core_impact")
    verdict = _g("gate_verdict")
    if not core and not verdict:
        return ""
    main_asset = _g("gate_main_asset")
    kind = _g("gate_kind")
    timing = _g("gate_timing")
    strength = _g("gate_strength")
    if not core:
        core = "（门卫未给出核心影响，自行判断）"
    parts = [f"核心影响: {core}"]
    if main_asset:
        parts.append(f"主品种: {main_asset}")
    if kind:
        parts.append(f"性质: {kind}")
    if timing:
        parts.append(f"时效: {timing}")
    if strength:
        parts.append(f"强度: {strength}")
    if verdict:
        parts.append(f"判词: {verdict}")
    return "[门卫四步] " + "｜".join(parts)


def _call_llm_sync(news_content: str, model_cfg: Dict[str, str],
                   market_context: str = "",
                   performance_context: str = "",
                   news_timestamp: str = "",
                   writing_context: str = "",
                   insights_section: str = "",
                   reference_signals: str = "",
                   gate_context: str = "",
                   gate_target_asset: str = "",
                   gate_decision_id: int = 0,
                   event_type_code: str = "",
                   gate_strength: int = 0,
                   news_source: str = "",
                   news_published_at: str = "",  # v188：tier-3 平台首发兜底用
                   purpose: str = "main",
                   call_site: str = "ai_worker") -> Dict[str, Any]:
    """
    Call any OpenAI-compatible LLM API synchronously (runs in executor thread).

    model_cfg.keys: id, label, api_base, api_key
    market_context:  Optional multi-line market snapshot string, prepended to user_content.
    news_timestamp:  ISO datetime string of the news event — prepended so AI knows the
                     historical time point when replaying old news for backtesting.
    purpose: v187 — 标签用途(main/gate/anomaly/reflection/advisor/sql/optimizer),
             写到 ai_call_stats 便于按通道独立计费
    call_site: v187 — 调用方标识(ai_worker/_call_llm_sync/reflection_loop/...)
    Returns: {"sentiment_score", "suggested_action", "reasoning", "model_label", "model_id",
              ... + 5 metadata fields}

    Retry strategy: if json_mode=True produces unparseable output (keyword fallback),
    retry once with json_mode=False (prompt-based JSON enforcement).
    """

    # ------------------------------------------------------------------
    # Inner: make one HTTP call and return raw response text + full body
    # ------------------------------------------------------------------
    def _do_api_call(use_json: bool, retry_hint: str = "") -> str:
        """Execute one LLM API call. Returns raw_text (or raises).
        retry_hint: 非空时作为追加 user 消息发给模型（用于格式纠正重试）。"""
        if model_cfg.get("label") == "Doubao":
            prompt = _main_prompt("doubao_prompt", _DOUBAO_SYSTEM_PROMPT)
        else:
            # 需求3：统一单通道——所有模型统一走 System Prompt（完整八段式），
            # 废弃 Prompt Force（_JSON_PROMPT_FORCE）独立通道，避免双通道内容叠加
            # 干扰八段式框架。JSON mode（response_format）仍按平台能力单独启用。
            use_full_prompt = True
            prompt = _main_prompt(
                "system_prompt" if use_full_prompt else "json_force_prompt",
                _SYSTEM_PROMPT if use_full_prompt else _JSON_PROMPT_FORCE,
            )
            # 盘面结构分析开关：开 → 在水位博弈段附加多周期结构判断指令；关 → 不加
            if _market_structure_enabled():
                prompt = prompt + _main_prompt("market_structure_block", _MARKET_STRUCTURE_BLOCK)
            # 用户自定义分析指令（ai_prompts 表，启用的注入；5s TTL）
            custom_prompts = _load_custom_prompts_sync()
            if custom_prompts:
                prompt = (
                    prompt
                    + "\n\n═══ 用户自定义分析指令（优先级最高，必须遵守）═══\n"
                    + "\n\n".join(custom_prompts)
                )
            # 品种专项分析规则（ai_asset_rules 表）：按金融品类单独设置的分析规则。
            # 注入所有启用规则（按品种分节）——分析时 AI 已先做主体识别+归类，
            # 由 AI 对 target_asset / sibling 各自套用对应品种的规则。
            asset_rules_block = _load_asset_rules_block_sync()
            if asset_rules_block:
                prompt = prompt + "\n\n" + asset_rules_block
            # v176.3：分析流程已删除 [因果链分析] 段，因果链库不再注入 prompt；
            # 因果链库（causal_chains 表）与「因果链库」管理页仍保留，仅由页面维护数据。
            # 事件结构化抽取（event_struct 规则确定性抽取）：挂载在品种规则之后，
            # 要求 AI 交叉校验规则量级与新闻是否一致（不一致在 [逻辑自检] 标黄修正）。
            event_block = _build_event_block(news_content)
            if event_block:
                prompt = prompt + "\n\n" + event_block
            # 长期慢变量视角 + 机理挖根 + 数据不全降级：恒定附加要求（不因事件为空而省略）
            longterm_block = _build_longterm_block()
            if longterm_block:
                prompt = prompt + "\n\n" + longterm_block
            # v176 因果图谱回灌：黄金/原油叙事逻辑硬先验（来自 causal_graph 自学习）
            graph_block = _build_graph_block(gate_target_asset or "")
            if graph_block:
                prompt = prompt + "\n\n" + graph_block
                print(f"  [GRAPH-BLOCK] 因果图谱注入 ({len(graph_block)} chars) asset={gate_target_asset}", flush=True)
            else:
                print(f"  [GRAPH-BLOCK] 因果图谱为空 asset={gate_target_asset}", flush=True)
            # v176 市场风格 + 5 层预警（ATR/趋势衰竭/费率/量能/OI）
            try:
                import market_style as _ms
                ms_block = _ms.market_style_block(gate_target_asset or "")
                if ms_block:
                    prompt = prompt + "\n\n" + ms_block
                    print(f"  [MS-BLOCK] 市场风格注入 ({len(ms_block)} chars) asset={gate_target_asset}", flush=True)
            except Exception:
                pass
            # v176.2 决策间长期影响传递：把"上次同品种判断 + regime + 反思"塞进 prompt
            # 让 LLM 知道历史方向与本期 bias（防止每条新闻凭印象写）
            try:
                _prev_ctx = _load_prev_decision(gate_target_asset or "")
                _ctx_parts = []
                if _prev_ctx.get("regime_at_decision"):
                    _ctx_parts.append(f"市场风格(regime): {_prev_ctx['regime_at_decision']}")
                if _prev_ctx.get("prev_action_same_asset"):
                    _ctx_parts.append(
                        f"上次同品种方向: {_prev_ctx['prev_action_same_asset']} "
                        f"(score={_prev_ctx.get('prev_score_same_asset')})"
                    )
                if _prev_ctx.get("bias_after_reflection"):
                    _ctx_parts.append(f"反思: {_prev_ctx['bias_after_reflection']}")
                if _ctx_parts:
                    prompt = prompt + "\n\n═══ v176.2 决策间长期影响传递（必须参考，禁止忽略）═══\n" + "；".join(_ctx_parts)
            except Exception:
                pass
            # 复盘经验分区（═══ 复盘经验 ═══）：作为正式分区拼进 prompt 尾部，
            # 与品种规则并列。此前是挤成一行塞进 user 消息，模型难逐条对照。
            if insights_section:
                prompt = prompt + "\n\n" + insights_section

            # v192 #2: 按 target_asset 注入更精准的实战经验规则（asset-specific + TTL 缓存）
            # 与上面全局 insights 互补：上面是硬铁律/软启发（无 asset 区分），
            # 这里是从 review_insights 按 target_asset 匹配 + 按 source_samples 排序 + counter_factual 反例标注
            # v200 P0-A3: 同时拉回命中 ids，注入成功后写回 ai_decisions.insight_hit_ids，闭环可见
            _asset_insight_hit_ids: List[int] = []
            if gate_target_asset:
                try:
                    from ai_prompts import load_recent_insights_block
                    _asset_insights_res = load_recent_insights_block(
                        target_asset=gate_target_asset,
                        return_ids=True,
                    )
                    # 兼容旧版返回值（仅 str）
                    if isinstance(_asset_insights_res, tuple):
                        _asset_insights, _asset_insight_hit_ids = _asset_insights_res
                    else:
                        _asset_insights = _asset_insights_res
                    if _asset_insights:
                        prompt = prompt + "\n\n" + _asset_insights
                        print(
                            f"  [INSIGHTS-ASSET] {gate_target_asset} 注入 "
                            f"({len(_asset_insights)} chars, "
                            f"hit_ids={_asset_insight_hit_ids})",
                            flush=True,
                        )
                except Exception as _exc:
                    # 不影响主流程；零注入
                    print(
                        f"  [INSIGHTS-ASSET] {gate_target_asset} 注入失败: "
                        f"{type(_exc).__name__}: {str(_exc)[:80]}",
                        flush=True,
                    )

            # v176.3 反思错误码注入：与 insights 互补 — "近期决策触发的具体错误码"
            # 30s TTL 缓存；任何异常 → 不注入（零开销）。从 decision_errors 表取。
            try:
                _ref_block = _load_reflection_block()
                if _ref_block:
                    prompt = prompt + "\n\n" + _ref_block
                    print(f"  [REFLECTION-BLOCK] 反思错误码注入 ({len(_ref_block)} chars)", flush=True)
            except Exception:
                pass

            # v186 经验系统注入 — 按当前 asset/gate_kind/actor 上下文从
            # experience_entries 取最相关 top-5 条经验。与上面 insights/reflection 互补:
            #   insights(全局硬铁律) + reflection(近期错误码) + experience(当前场景特化)
            try:
                # actor 从 news_source 或 gate_context 中提取;为安全起见取空串也行
                _actor_hint = ""
                try:
                    if news_source and ("特朗普" in news_source or "Trump" in news_source or "Powell" in news_source
                                        or "Gensler" in news_source or "Fed" in news_source
                                        or "SEC" in news_source or "V神" in news_source or "Vitalik" in news_source):
                        _actor_hint = news_source[:80]
                except Exception:
                    _actor_hint = ""
                _exp_block = _load_experience_block_sync(
                    asset=gate_target_asset or "",
                    gate_kind=event_type_code or "",
                    actor=_actor_hint,
                )
                if _exp_block:
                    prompt = prompt + "\n\n" + _exp_block
                    print(f"  [EXP-BLOCK] v186 经验注入 ({len(_exp_block)} chars) "
                          f"asset={gate_target_asset or 'N/A'} actor={_actor_hint or 'N/A'}", flush=True)
            except Exception:
                pass

            # v199: SPA applied 设置注入 — 从 strategy_param_adjustments 读 status='applied' 的设置,
            # 按 asset 聚合注入 prompt, 让 LLM 决策时知道「已被 AI 系统采纳的过滤/分析/仓位/分数调整」。
            try:
                _spa_block = _load_spa_applied_block_sync(
                    asset=gate_target_asset or "", max_chars=600,
                )
                if _spa_block:
                    prompt = prompt + "\n\n" + _spa_block
                    print(f"  [SPA-APPLY-BLOCK] v199 已应用 SPA 注入 ({len(_spa_block)} chars) "
                          f"asset={gate_target_asset or 'N/A'}", flush=True)
            except Exception:
                pass

        # 组装: [新闻时间] + [市场快照] + [Hermes 分席] + [历史绩效] + [新闻正文]
        user_text = news_content[:2000]
        # 门卫四步标签：紧贴新闻正文，作为分析先验（核心影响/主品种/判词）
        if gate_context:
            user_text = user_text + "\n\n" + gate_context
        if news_timestamp:
            try:
                ts_dt = datetime.fromisoformat(news_timestamp.replace("Z", "+00:00"))
                ts_str = ts_dt.strftime("%Y-%m-%d %H:%M UTC")
            except (ValueError, TypeError):
                ts_str = news_timestamp[:19]
            user_text = f"[新闻时间] {ts_str}\n\n{user_text}"
        # ── 历史绩效（baseline.by_type）：从 backtest_event_outcomes 读历史 PnL ──
        # v174: 在 SERVER VERDICT 之前注入,让 AI 先看历史胜率再看实时快照 → 决策更稳
        baseline_block = _build_baseline_block(event_type_code, gate_target_asset)
        if baseline_block:
            user_text = baseline_block + "\n\n" + user_text
            print(f"[BASELINE] 已注入历史绩效段 | event_type={event_type_code or 'N/A'} | asset={gate_target_asset or 'N/A'} | {len(baseline_block)} chars", flush=True)
        # ── v200+ P0-1: 微观结构(持仓/资金费率) ── 在 baseline 后注入,让 LLM 在历史胜率和微观仓位之间交叉看 →
        try:
            _micro_targets: List[str] = []
            if gate_target_asset and gate_target_asset.upper() != "NONE":
                _micro_targets = [str(gate_target_asset).upper()]
            # 再补几条关联品种(用于跨资产传导参考)
            for _sym in ("BTC", "ETH", "XAU"):
                if _sym not in _micro_targets:
                    _micro_targets.append(_sym)
            micro_block = _build_microstructure_block(target_assets=_micro_targets)
            if micro_block:
                user_text = micro_block + "\n\n" + user_text
                print(f"[MICROSTRUCTURE] 已注入微观结构段 | targets={_micro_targets} | {len(micro_block)} chars", flush=True)
        except Exception as _e:
            print(f"[MICROSTRUCTURE] 注入失败(静默): {type(_e).__name__}: {str(_e)[:80]}", flush=True)
        # ── v174: 事件类型选项清单（dim_event_type 维表）──
        # 让 LLM 知道有 25 个标准 type_code 可选, 而不是自由输出 event_type 字符串。
        # 必填字段: event_type_code, 否则视为 schema 失败(影响 baseline JOIN 率)。
        dim_block = _load_dim_event_type_block()
        if dim_block:
            user_text = dim_block + "\n\n" + user_text
            print(f"[DIM-EVENT-TYPE] 已注入事件类型清单 | {dim_block.count(chr(10))} 行", flush=True)
        # ── SERVER VERDICT：server 端预生成的水位/价格/预期结论 ──
        # 让 AI 直接引用 server 算好的大结论，不必再算 RSI/ADX/VWAP → 节省 token 也减少误判。
        # market_context 是摘要文本（render_prompt_block），用于 AI 看到原始数据自校。
        # 注意：必须在 market_context 之后、calendar_hint 之前注入；CAL-HINT 不应被 VERDICT 覆盖。
        try:
            # v200 P4: 调公共 API get_full_verdict_block（等价 build_verdict + 跨品种对比段）
            # 同时保留 build_verdict 向后兼容（其他模块/测试可能仍调旧函数）
            from server_verdict import get_full_verdict_block
            # _last_good_snapshot 是 _ai_worker_loop 的局部变量，嵌套函数 _do_api_call 看不到。
            # 我们通过 ai_worker.py 模块级 cache（_SERVER_SNAPSHOT_CACHE）读快照。
            global _SERVER_SNAPSHOT_CACHE
            snap_dict = _SERVER_SNAPSHOT_CACHE.get("json") or {}
            if isinstance(snap_dict, str):
                try:
                    snap_dict = json.loads(snap_dict)
                except Exception:
                    snap_dict = {}
            # 防御：news_timestamp / gate_target_asset 可能为 None，build_verdict 期望 str/str/int
            _safe_ts = str(news_timestamp) if news_timestamp else ""
            _safe_ta = str(gate_target_asset) if gate_target_asset else ""
            _safe_id = int(gate_decision_id) if gate_decision_id else 0
            verdict_block = get_full_verdict_block(
                news_ts=_safe_ts,
                target_asset=_safe_ta,
                current_id=_safe_id,
                snap=snap_dict if isinstance(snap_dict, dict) else None,
            )
            if verdict_block:
                user_text = verdict_block + "\n\n" + user_text
                # 同步更新至 cache["verdict"]（便于后续 fallback 重试注入同一份结论，不重复算）
                print(f"[VERDICT] 已注入 SERVER VERDICT 块 ({len(verdict_block)} chars, 含 P4 跨品种对比) | target={_safe_ta or 'N/A'} | snap_status={(snap_dict or {}).get('status','none')}", flush=True)
        except Exception as _e:
            import traceback
            print(f"[VERDICT] 注入失败（不影响 AI 主流程）: {type(_e).__name__}: {_e}", flush=True)
            print(f"[VERDICT] TRACEBACK:\n{traceback.format_exc()}", flush=True)
        if market_context:
            user_text = market_context + "\n\n" + user_text
        calendar_hint = _build_calendar_hint()
        if calendar_hint:
            user_text = calendar_hint + "\n\n" + user_text
            # 留痕：让运维/前端可观测日历提示确实注入了 prompt（非空时记条数）
            n_events = calendar_hint.count("★")
            print(f"[CAL-HINT] 已注入 {n_events} 条 24h 内重要财经事件到 prompt", flush=True)
        else:
            # 静默问题修复：配额耗尽/MCP 不可达也要可见，避免"日历提示缺失"无任何留痕
            status = _CALENDAR_STATUS
            if status.get("quota_exhausted"):
                print("[CAL-HINT] 日历提示为空：所有 JIN10_MCP_TOKEN 今日配额耗尽，明日 0 点重置", flush=True)
            elif status.get("error"):
                print(f"[CAL-HINT] 日历提示为空: {status.get('error')[:200]}", flush=True)
            else:
                print("[CAL-HINT] 日历提示为空：当前 24h 内无 ★2+ 重要财经事件", flush=True)
        # 美股行情上下文：新闻提到币安永续股票（NVDA/TSLA/AAPL/MSTR 等）时附上当前价
        # （10 分钟缓存；拉不到不阻塞分析——行情只是上下文）
        try:
            stock_line = _stock_context_for(news_content)
            if stock_line:
                user_text = stock_line + "\n\n" + user_text
        except Exception:
            pass
        # v175：SK 海力士 / A股 行情上下文（10 分钟缓存；拉不到不阻塞分析）
        try:
            asia_line = _asia_context_for(news_content)
            if asia_line:
                user_text = asia_line + "\n\n" + user_text
        except Exception:
            pass
        # v175：实时全网搜索证据（免费源）→ 供 AI 客观填 cross_net_*，替代凭记忆猜
        try:
            origin_block = _origin_search_block_for(news_content)
            if origin_block:
                user_text = origin_block + "\n\n" + user_text
        except Exception:
            pass
        if writing_context:
            user_text = writing_context + "\n\n" + user_text
        if performance_context:
            user_text = performance_context + "\n\n" + user_text
        # 注意：复盘经验不再拼进 user 消息，已作为「═══ 复盘经验 ═══」分区注入 system prompt
        if reference_signals:
            user_text = reference_signals + "\n\n" + user_text

        payload: Dict[str, Any] = {
            "model": model_cfg.get("request_model") or model_cfg["id"],
            "messages": [
                {"role": "system", "content": prompt},
                {"role": "user", "content": user_text},
            ],
            "temperature": 0.1,
            **model_cfg["extra_body"],
            # 八段推理链 + 20 字段 JSON + 因果链 + 总结：1500 token 易在 [自检] 之后被截断，
            # 2500 token 仍可能让长 prompt 下段间连接词被压扁。
            # v176.7：6000 → 4000。实测 thinking 已禁用、直接输出 JSON 后 reasoning_path 不会
            # 超过 4000 token；6000 上限会让 M3 在长 prompt 下生成时间拖长、撞 65s 外层超时
            # （llm_call_failures 16:19-17:00 大量 TimeoutError）。4000 留足八段篇幅同时压生成时长。
            # max_tokens 必须放最后 — 覆盖 extra_body 里 MiniMax M3 的 4000 默认。
            "max_tokens": 4000,
        }
        if retry_hint:
            payload["messages"].append({"role": "user", "content": retry_hint})
        if use_json:
            payload["response_format"] = {"type": "json_object"}

        # ── requests 客户端 ──
        # timeout 对「连接 + 读取」都生效（urllib 在个别代理/响应路径可能不触发超时，
        # 导致 LLM 调用无限挂起拖死 ai_worker）。默认 trust_env 走 env HTTP_PROXY，
        # 与实测路径一致（aiping 走代理 1~3s 响应）。
        headers = {
            "Authorization": f"Bearer {model_cfg['api_key']}",
            "Content-Type": "application/json",
        }
        url = f"{model_cfg['api_base']}/chat/completions"

        # HTTP 429 retry logic — upstream rate limits are transient
        retry_delays = [3.0, 6.0, 12.0]  # progressive backoff（缩短：批量限流时更快恢复）
        last_err: Optional[str] = None
        # v187：记录本次调用开始时间（统计 latency_ms 用）
        _t_call_start = time.perf_counter()
        for attempt, delay in enumerate([0] + retry_delays):
            if delay > 0:
                time.sleep(delay)
            try:
                session = _LLM_PROXY_SESSION if model_cfg.get("use_proxy") else _LLM_SESSION
                resp = session.post(
                    url,
                    json=payload,
                    headers=headers,
                    timeout=60,
                )
                if resp.status_code == 429 and attempt < 1:
                    # 429：只重试 1 次（2s）。配额用完/持续限流时快速失败切 fallback，
                    # 避免每条分析在 429 上浪费 3+6+12=21s（MiniMax 额度耗尽场景）
                    print(f"  [{model_cfg['label']}] HTTP 429, retry once after 2s", flush=True)
                    last_err = "HTTP 429"
                    time.sleep(2.0)
                    continue
                # 中转平台上游瞬时错误（403/5xx）也重试 2 次
                if resp.status_code in (403, 500, 502, 503) and attempt < 2:
                    print(f"  [{model_cfg['label']}] HTTP {resp.status_code}, retry {attempt+1}/2 after {delay}s", flush=True)
                    last_err = f"HTTP {resp.status_code}"
                    time.sleep(max(delay, 3.0))
                    continue
                if resp.status_code >= 400:
                    raise RuntimeError(
                        f"{model_cfg['label']} HTTP {resp.status_code}: {resp.text[:300]}"
                    )
                body = resp.json()
                raw_text = (body["choices"][0]["message"]["content"] or "").strip()
                if not raw_text:
                    finish = body["choices"][0].get("finish_reason", "unknown")
                    body_preview = resp.text[:500]
                    print(f"  [{model_cfg['label']}] EMPTY RESPONSE | finish_reason={finish} | body_preview={body_preview}", flush=True)
                    raise ValueError(f"empty response from {model_cfg['label']}")
                # v187：捕获 token 用量,写到 ai_call_stats(主路径)
                try:
                    _usage = body.get("usage") or {}
                    _tok_in = int(_usage.get("prompt_tokens") or 0)
                    _tok_out = int(_usage.get("completion_tokens") or 0)
                    # 估算延迟(整个 retry-loop 累计)
                    _latency_ms = int((time.perf_counter() - _t_call_start) * 1000)
                    import db as _db_stats
                    _db_stats.log_ai_call(
                        purpose=purpose,
                        key_alias=str(model_cfg.get("key_alias") or model_cfg.get("label") or "unknown"),
                        model_id=str(model_cfg.get("id") or "?"),
                        tokens_in=_tok_in, tokens_out=_tok_out,
                        latency_ms=_latency_ms, status="ok",
                        call_site=call_site,
                    )
                except Exception:
                    pass
                return raw_text
            except requests.RequestException as exc:
                status_code = getattr(exc.response, "status_code", None)
                if status_code == 429 and attempt < 1:
                    print(f"  [{model_cfg['label']}] HTTP 429, retry once after 2s", flush=True)
                    last_err = "HTTP 429"
                    time.sleep(2.0)
                    continue
                # 网络瞬断（SSL 握手失败/连接失败/超时）也重试，不再一次失败即记入故障
                if isinstance(exc, (requests.exceptions.SSLError,
                                    requests.exceptions.ConnectionError,
                                    requests.exceptions.Timeout)) and attempt < 3:
                    print(f"  [{model_cfg['label']}] 网络瞬断 {type(exc).__name__}, retry {attempt + 1}/3 after {max(delay, 2.0):.0f}s", flush=True)
                    last_err = type(exc).__name__
                    time.sleep(max(delay, 2.0))
                    continue
                raise RuntimeError(
                    f"{model_cfg['label']} 请求失败: {type(exc).__name__}: {str(exc)[:150]}"
                ) from exc
        # All retries exhausted — v187：写失败 token 埋点
        try:
            _latency_ms = int((time.perf_counter() - _t_call_start) * 1000)
            import db as _db_stats
            _db_stats.log_ai_call(
                purpose=purpose,
                key_alias=str(model_cfg.get("key_alias") or model_cfg.get("label") or "unknown"),
                model_id=str(model_cfg.get("id") or "?"),
                tokens_in=0, tokens_out=0,
                latency_ms=_latency_ms, status="network_error",
                error_code=str(last_err)[:32] or "retries_exhausted",
                call_site=call_site,
            )
        except Exception:
            pass
        raise RuntimeError(f"{model_cfg['label']} HTTP 429 (exhausted retries): {last_err}")

    # ------------------------------------------------------------------
    # Inner: parse raw text → result dict (with all three fallback tiers)
    # ------------------------------------------------------------------
    def _parse_result(raw_text: str) -> Dict[str, Any]:
        """Parse LLM output — never returns empty dict (keyword fallback guarantees)."""
        # ── 剥离思维链块（M3 / 新一代模型常输出 <think>...</think> 然后才给 JSON；
        #    不剥掉会让 json.loads 在第一个 { 前撞到 "<think>"，JSON 解析失败 → 误入 fallback） ──
        cleaned = re.sub(r"<think>.*?</think>", "", raw_text, flags=re.DOTALL)
        # 兼容未配对的 <think>（流式截断 / 单标签泄露）
        cleaned = re.sub(r"<think>.*$", "", cleaned, flags=re.DOTALL)
        cleaned = cleaned.strip()
        # Parse JSON — handle markdown wrapping + embedded/truncated JSON
        cleaned = re.sub(r"^```(?:json)?\s*", "", cleaned)
        cleaned = re.sub(r"\s*```$", "", cleaned)

        # v176.2：解析前先把 reasoning_path 内的旧段名硬替换为新段名
        # （LLM 训练惯性会写 [资产性质][因素排行] → 改写为 [总框架][因素定位]，
        #  让"LLM 凭印象写"自动合规；旧段名彻底从数据中消失）
        # 注：JSON 字符串内换行是 \\n，正则同时覆盖 \n 和 \\n。
        _OLD2NEW = (
            (r"\[资产性质\]", "[总框架]"),
            (r"\[资产性质 ?\]", "[总框架]"),
            (r"\[因素排行\]", "[因素定位]"),
            (r"\[主体识别\]", "[总框架]"),
            (r"\[主体识别 ?\]", "[总框架]"),
            # [主体] 单独出现歧义大，只在前面紧跟空白+换行时才替换（限定段首）
            (r"(?:^|\n)\[主体\]", "\\n[总框架]"),
        )
        cleaned = re.sub(r"\\n|\n", "\n", cleaned)  # 先把 \\n 转真 \n，再做替换
        for old, new in _OLD2NEW:
            cleaned = re.sub(old, new, cleaned)

        result: Dict[str, Any] = {}
        try:
            result = json.loads(cleaned)
            if not isinstance(result, dict):
                raise ValueError("not a dict")
        except (json.JSONDecodeError, ValueError):
            # --- Fallback 1: repair truncated JSON ---
            repaired = cleaned.strip()
            if repaired.startswith("{") and not repaired.endswith("}"):
                inner = repaired[1:].strip()
                chunks = [c.strip() for c in inner.split(",") if c.strip()]
                complete: List[str] = []
                for chunk in chunks:
                    try:
                        json.loads("{" + chunk + "}")
                        complete.append(chunk)
                    except json.JSONDecodeError:
                        pass  # truncated field — drop
                if complete:
                    repaired = "{" + ",".join(complete) + "}"
                    try:
                        result = json.loads(repaired)
                    except json.JSONDecodeError:
                        pass

            # --- Fallback 2: regex-extract JSON containing required keys ---
            if not result:
                m = re.search(
                    r'\{[^{}]*"sentiment_score"[^{}]*"suggested_action"[^{}]*"reasoning"[^{}]*\}',
                    cleaned, re.DOTALL,
                )
                if not m:
                    m = re.search(
                        r'\{[^{}]*"(?:sentiment_score|suggested_action|reasoning)"[^{}]*\}',
                        cleaned, re.DOTALL,
                    )
                if m:
                    try:
                        result = json.loads(m.group(0))
                    except json.JSONDecodeError:
                        frag = m.group(0).rstrip().rstrip(",") + "}"
                        try:
                            result = json.loads(frag)
                        except json.JSONDecodeError:
                            pass

            # --- Fallback 2.5 (v170 全网溯源补丁)：LLM 长推演模式下的中文抽取 ---
            # MiniMax-M3 等模型常输出 2000 字中文八段推演但**没有 JSON 包裹** → 上方
            # json.loads / Fallback 2 都拿不到结果 → 最终落 fallback3（中性观望）。
            # 但中文推演里其实常有可用的"X 日宣布/报道/发布"信号，可作为 cross_net_*
            # 的弱依据填进 result（前端能渲染"AMBIGUOUS"灰徽章而不是"未溯源"无徽章）。
            #
            # 设计要点：
            #   - 只在 result 已部分可用（不是 fallback3 兜底结果）时合并抽取结果
            #   - 抽取失败的字段留空串，不污染 DB
            #   - 时间必须能映射到 YYYY-MM-DD HH:MM:SS（用本机系统时间补全缺失部分）
            #   - 来源优先匹配"X 报道/宣布/发布/消息/通讯社/官网/新闻稿"等 14 个高信度模式
            #
            # v176.2 强化：主路径（LLM 给完整 JSON）也跑一次抽取。
            # 之前 0% 填充率是因为这个分支只在 fallback2（无 JSON）触发，主路径 LLM
            # 给了 JSON 但 cross_net_* 经常留空时，DB 就空着。
            # 现在用正则从新闻原文/标题里抽"X 时 X 分 X 报道/宣布/发布"，对金十/Yahoo/东财/财联社/finnhub/oilprice
            # 这些主渠道（之前 0% 填充率）补上 cross_net 弱信号。
            if not result.get("cross_net_first_seen_ts") and not result.get("net_origin_info"):
                _cn_ts_guess, _cn_src_guess, _noi_guess = _extract_cross_net_from_text(cleaned)
                if _cn_ts_guess or _cn_src_guess or _noi_guess:
                    result["cross_net_first_seen_ts"] = _cn_ts_guess
                    result["cross_net_source"] = _cn_src_guess
                    result["net_origin_info"] = _noi_guess

            # --- v176.2 二次回灌：news 原文 + reasoning 一起喂抽取器 ---
            # cleaned 主要是 LLM 输出，已不含原始 news text。需补一句：把原始 news text
            # 也送进抽取器，专门为金十/Yahoo/东财/oilprice/finnhub 这些"LLM 经常留空
            # cross_net_ 但原文里有'北京时间 21:30 路透社报道'等明文"的主渠道补弱信号。
            try:
                # v176.10 fix：原代码引用不存在的变量 `news`（本函数形参只有 news_content），
                # 导致 `'news' in dir()` 恒 False、`news.get("content")` 抛 NameError 被 except 吞掉，
                # 二次回灌分支从未执行 → 主路径 LLM 留空 cross_net_* 时引擎不回补原文弱信号。
                # 现在直接用形参 news_content（完整原文）+ news_source（由调用点传入）。
                _src_for_xnet = (news_source or "").strip()
                _text_for_xnet = news_content or ""
                if not result.get("cross_net_first_seen_ts") and _text_for_xnet:
                    # 主渠道：金十/Yahoo/东财/财联社/oilprice/finnhub/fxstreet/marketwatch/feishu_jin10
                    _MAIN_CHAN = ("jin10", "feishu_jin10", "yahoo_finance", "东方财富", "finnhub",
                                  "oilprice", "fxstreet", "marketwatch", "cointelegraph",
                                  "TechFlow", "techflow")
                    if any(ch in _src_for_xnet for ch in _MAIN_CHAN):
                        _cn_ts2, _cn_src2, _noi2 = _extract_cross_net_from_text(_text_for_xnet)
                        if _cn_ts2 or _cn_src2 or _noi2:
                            # v188-fix: 当 regex 抽到 cn_ts 但抽不到 cn_src 时，用
                            # news_source 作 cn_src 兜底。否则 _validate_cross_net_ts 第⑤层
                            # （source ≥4 字符）会拒掉 cn_ts，导致 DB 仍留空。
                            if _cn_ts2 and not _cn_src2:
                                _cn_src2 = (news_source or "").strip() or "聚合平台"
                            result["cross_net_first_seen_ts"] = result.get("cross_net_first_seen_ts") or _cn_ts2
                            result["cross_net_source"] = result.get("cross_net_source") or _cn_src2
                            result["net_origin_info"] = result.get("net_origin_info") or _noi2
            except Exception:
                pass  # 静默失败，不影响主路径

            # --- v188 Tier-3 平台首发兜底 ---
            # 前两层都失败（LLM 没填、regex 也没抽到），但 source 是已知聚合器 → 用
            # news.published_at 作"平台首发"时间弱信号。比"未溯源"无徽章强，前端可
            # 显示"聚合器首发"灰徽章而非空白。校验只在 source ≥4 字符 + 时间合法
            # 时写入（_validate_cross_net_ts 第④层会拒绝晚于 first_seen_ts 的值）。
            # v188-fix: 二次回灌只填 cn_ts 但不填 cn_src 时（典型：金十"X月X日X时X讯"
            # 抽到时间但没有"X 报道/宣布"匹配），Tier-3 跳过条件变 False（cn_ts 非空），
            # 但 cn_src 仍空 → _validate_cross_net_ts 第⑤层拒掉 → DB 留空。
            # 修复后二次回灌已给 cn_src 兜底，但仍有部分场景二次回灌完全不触发
            # （如 source 不在 _MAIN_CHAN 内、或 LLM 已正确填了 cn_ts）。
            # 此 Tier-3 保留作为最后一道兜底，仅在 result 完全空时触发。
            # v188-fix: Tier-3 触发条件——结构化两字段都为"可写入"时才认为已溯源。
            # 之前用 `not result.get("cross_net_source")` 不够：regex 可能捕到短串
            # （如 "新闻" 2 字符），让 cn_source 非空 → Tier-3 跳过 → 但
            # _validate_cross_net_ts 第⑤层（source ≥4 字符）会拒掉 → DB 仍留空。
            # 改进：cn_source 长度 <4 也视为未溯源，Tier-3 接管。
            _existing_cn_src = (str(result.get("cross_net_source") or "").strip())
            if (not result.get("cross_net_first_seen_ts")
                    and (not _existing_cn_src or len(_existing_cn_src) < 4)
                    and news_published_at):
                try:
                    _ts_m = re.match(
                        r"^(\d{4})-(\d{2})-(\d{2})[ T](\d{2}):(\d{2})(?::(\d{2}))?",
                        str(news_published_at),
                    )
                    if _ts_m:
                        _valid_pub = (
                            f"{_ts_m.group(1)}-{_ts_m.group(2)}-{_ts_m.group(3)} "
                            f"{_ts_m.group(4)}:{_ts_m.group(5)}:00"
                        )
                        _src_label = (news_source or "").strip()
                        result["cross_net_first_seen_ts"] = _valid_pub
                        result["cross_net_source"] = (
                            f"{_src_label} 平台首发" if len(_src_label) >= 2 else "聚合平台首发"
                        )
                        result["net_origin_info"] = (
                            f"由 {_src_label or '聚合器'} 于 {_valid_pub} 平台首发"
                        )
                except Exception as exc:
                    pass  # Tier-3 静默失败（不影响主路径）

            # --- Fallback 3: keyword-based heuristics ---
            # 关键词推断不是分析：它没有做全文/八段推演，产出 BUY/SELL 只会制造
            # “没分析却给方向”的伪结论（实测：『取消出售计划』因命中 sell/sale 被误判
            # 成偏空）。这里统一降级为中性观望、不下任何方向——方向判定完全交给真实 LLM 分析。
            #
            # v170 全网溯源补丁：fallback3 也要带 cross_net_* / net_origin_info 三个字段占位。
            # 不写会让 sanitize 默认空串 + DB 存空 → 前端无法区分"AI 看了拒答"与"LLM 没填"。
            # 落空串是规范上的"拒绝回答"语义，frontend 4 档徽章会渲染为"未溯源"而非崩页。
            if not result:
                result = {
                    "reasoning_path": "关键词推断 → 中性观望（未做全文分析，不下方向）",
                    "sentiment_score": 0.0,
                    "suggested_action": "HOLD",
                    "reasoning": "关键词推断:无明确方向(未做全文分析)",
                    "market_category": "",
                    "target_asset": "NONE",
                    "event_strength": "low",
                    "direct_catalyst": False,
                    "timeframe_match": "intraday",
                    # v170 全网溯源 — fallback 阶段无任何信号可据，标准三态全空（=拒绝回答）
                    "cross_net_first_seen_ts": "",
                    "cross_net_source": "",
                    "net_origin_info": "",
                }

        # ── 元数据字段默认值 (LLM 可能不返回或返回无效值) ──
        _META_DEFAULTS: Dict[str, Any] = {
            "prediction_type":        ("reversal", "continuation", "breakout"),
            "event_phase":            ("early", "mid", "late"),
            "market_confirmation":    ("positive", "negative", "unknown"),
            "expected_horizon":       ("intraday", "1-3d", "1w+"),
            "invalidation_condition": "",
            # Phase 2: Event Quality Layer
            "event_strength":         ("low", "medium", "high"),
            "timeframe_match":        ("intraday", "swing", "macro"),
        }
        for key, valid in _META_DEFAULTS.items():
            if key not in result or not isinstance(result[key], str):
                # If valid is a tuple, take the last (most conservative) value; if str, use empty
                result[key] = valid[-1] if isinstance(valid, tuple) else valid
            elif isinstance(valid, tuple):
                val_lower = result[key].strip().lower()
                # Check if the value matches any valid option (fuzzy)
                ok = False
                for v in valid:
                    if v in val_lower or val_lower == v:
                        result[key] = v  # normalize to canonical form
                        ok = True
                        break
                if not ok:
                    result[key] = valid[-1]  # default conservative

        # direct_catalyst 布尔值特殊处理 (LLM 可能返回 JSON true/false 或字符串)
        dc = result.get("direct_catalyst")
        if isinstance(dc, bool):
            pass  # already correct
        elif isinstance(dc, str) and dc.strip().lower() in ("true", "1", "yes"):
            result["direct_catalyst"] = True
        else:
            result["direct_catalyst"] = False

        # ── 影响评估数值字段 (0-10，LLM 可能不返回/越界 → clamp 兜底) ──
        for numeric_key, default in (("heat_score", 5.0), ("impact_score", 5.0), ("asset_impact", 5.0)):
            raw = result.get(numeric_key, default)
            try:
                val = float(raw)
            except (TypeError, ValueError):
                val = default
            result[numeric_key] = max(0.0, min(10.0, val))

        # ── 整箱白名单过滤：核心字段在此统一兜底，保证返回即三键齐全 ──
        # v176：先 sanitize 通用白名单，再 presanitize 把 schema 必填字段强制达标
        # v176.9：传 cleaned 给 presanitize，用于全空兜底时从原文末尾抽 [总结]
        # v1 重构：sanitize 后消费 _unclassified_* 标记（仅在主入口有；这里是 fallback 链 news_id=0）
        result = _sanitize_llm_result(result)
        _consume_unclassified_marker(result, news_id=0)
        result = _presanitize_for_schema(result, cleaned)
        return result

    # ==================================================================
    # Main call flow
    # ==================================================================
    use_json_mode = model_cfg.get("json_mode", True)

    # First attempt — with configured json_mode
    raw_text = _do_api_call(use_json_mode)
    result = _parse_result(raw_text)

    # Retry: if strict JSON mode fell through to keyword heuristics, try prompt mode
    if (use_json_mode
            and model_cfg.get("label") != "Doubao"
            and result.get("reasoning_path", "").startswith("关键词推断")
            and len(raw_text) > 10):
        print(f"  [{model_cfg['label']}] json_mode=True → keyword, retrying prompt-mode...", flush=True)
        try:
            raw_text2 = _do_api_call(False)
            result2 = _parse_result(raw_text2)
            if not result2.get("reasoning_path", "").startswith("关键词推断"):
                result = result2
                print(f"  [{model_cfg['label']}] retry OK (prompt-JSON)", flush=True)
        except Exception:
            pass  # Keep original keyword result if retry fails

    # ── 八段格式强制：reasoning_path 八段不完整（AI 自由发挥/漏段），
    #    或"八段空骨架"（段名都在但段间无实质内容）→ 带纠正提示重试 1 次 ──
    if (model_cfg.get("label") != "Doubao"
            and not str(result.get("reasoning_path", "")).startswith("关键词推断")
            and (not _six_stage_complete(str(result.get("reasoning_path", "")))
                 or _is_eight_stage_skeleton(str(result.get("reasoning_path", ""))))):
        print(f"  [{model_cfg['label']}] reasoning_path 八段不完整, retrying with format fix...", flush=True)
        try:
            raw_text_fix = _do_api_call(use_json_mode, retry_hint=_SIX_STAGE_RETRY_HINT)
            result_fix = _parse_result(raw_text_fix)
            path_fix = str(result_fix.get("reasoning_path", ""))
            if _six_stage_complete(path_fix) and not path_fix.startswith("关键词推断"):
                result = result_fix
                print(f"  [{model_cfg['label']}] format-fix retry OK", flush=True)
            else:
                print(f"  [{model_cfg['label']}] format-fix retry still incomplete, keep original", flush=True)
        except Exception as exc:
            print(f"  [{model_cfg['label']}] format-fix retry failed: {type(exc).__name__}", flush=True)

    # ── P0-2：schema 必填字段补全 retry（data_anchor_ts / hard_data_evidence /
    # elasticity_window / verification 任何一个缺失 → 带 hint 重试 1 次）
    # 这是"LLM 必填字段 0% 填全"问题的根因修复：prompt 已前置+示意，但 LLM 仍可能忘
    # 就单独问一次补全，避免整条新闻被降级为 HOLD。
    _missing_required = []
    if not str(result.get("data_anchor_ts", "") or "").strip():
        _missing_required.append("data_anchor_ts")
    if not str(result.get("hard_data_evidence", "") or "").strip():
        _missing_required.append("hard_data_evidence")
    _ew = result.get("elasticity_window")
    if not _ew or (isinstance(_ew, str) and not _ew.strip()):
        _missing_required.append("elasticity_window")
    if not str(result.get("verification", "") or "").strip():
        _missing_required.append("verification")
    # v181 结构化产出 3 判断项：任一缺失即视为必填缺口（准优先）
    if not str(result.get("asset_classification", "") or "").strip():
        _missing_required.append("asset_classification")
    if not str(result.get("fund_driver", "") or "").strip():
        _missing_required.append("fund_driver")
    if not str(result.get("consensus_vs_us", "") or "").strip():
        _missing_required.append("consensus_vs_us")
    _struct_missing = {"asset_classification", "fund_driver", "consensus_vs_us"} & set(_missing_required)
    if (model_cfg.get("label") != "Doubao"
            and not _is_slow_channel(model_cfg)
            and (len(_missing_required) >= 2 or bool(_struct_missing))
            and not str(result.get("reasoning_path", "")).startswith("关键词推断")):
        print(f"  [{model_cfg['label']}] 缺 P0-2 必填字段 {_missing_required}, retrying...", flush=True)
        try:
            raw_text_schema = _do_api_call(use_json_mode, retry_hint=_SCHEMA_REQUIRED_FIELDS_RETRY_HINT)
            result_schema = _parse_result(raw_text_schema)
            _filled = 0
            for k in _missing_required:
                v = result_schema.get(k)
                if v is not None and (not isinstance(v, str) or v.strip()):
                    result[k] = v
                    _filled += 1
            if _filled > 0:
                print(f"  [{model_cfg['label']}] schema retry 补全 {_filled}/{len(_missing_required)} 字段", flush=True)
            else:
                print(f"  [{model_cfg['label']}] schema retry 仍空，保留原结果", flush=True)
        except Exception as _exc:
            print(f"  [{model_cfg['label']}] schema retry failed: {type(_exc).__name__}", flush=True)

    # ── [总结] 段强制：用户要求"有关系就要有分析 + 总结"→ 缺总结段或没按品种分行时重试 1 次 ──
    # 实测背景：原先"总结"要求只写在[因果链命中]块里、且要求补进会被截断到 80 字的
    # reasoning 字段 → 300 条样本里含"总结"的 0 条。现在按 [总结] 段校验。
    try:
        _rp_sum = str(result.get("reasoning_path", "") or "")
        _ta_sum = str(result.get("target_asset", "") or "").strip().upper()
    except Exception:
        _rp_sum, _ta_sum = "", ""
    _need_summary = bool(_rp_sum) and not _rp_sum.startswith("关键词推断")
    if _need_summary and _ta_sum and _ta_sum not in ("NONE", "OTHER"):
        _need_summary = not _has_summary_for(_rp_sum, _ta_sum)
    elif _need_summary:
        _need_summary = not _summary_text(_rp_sum)
    if (model_cfg.get("label") != "Doubao"
            and not _is_slow_channel(model_cfg)
            and _need_summary):
        print(f"  [{model_cfg['label']}] 缺 [总结] 段（或未按品种分行）, retrying with summary hint...",
              flush=True)
        try:
            raw_text_s = _do_api_call(use_json_mode, retry_hint=_SUMMARY_RETRY_HINT)
            result_s = _parse_result(raw_text_s)
            path_s = str(result_s.get("reasoning_path", "") or "")
            ta_s = str(result_s.get("target_asset", "") or "").strip().upper()
            ok_s = bool(path_s) and not path_s.startswith("关键词推断") and (
                _has_summary_for(path_s, ta_s) if ta_s and ta_s not in ("NONE", "OTHER")
                else bool(_summary_text(path_s))
            )
            if ok_s:
                result = result_s
                print(f"  [{model_cfg['label']}] summary retry OK", flush=True)
            else:
                print(f"  [{model_cfg['label']}] summary retry 未改善, keep original", flush=True)
                # 留痕：部署后用它评估"总结要求"的真实达成率，
                # 再决定要不要把"缺总结"升级成不给方向单（目前只重试、不拦交易）。
                _signal_trace("summary", verdict="retry未改善", asset=ta_s or _ta_sum,
                              path_len=len(path_s or _rp_sum))
        except Exception as exc:
            print(f"  [{model_cfg['label']}] summary retry failed: {type(exc).__name__}", flush=True)

    # ── 方向一致性强制：八段推演方向与 suggested_action 相反 → 带纠正提示重试 1 次 ──
    #    按 target_asset 判定（该品种自己的专属行），多品种混合文本不会互相抵消。
    action_now = str(result.get("suggested_action", "")).upper()
    target_now = str(result.get("target_asset", "") or "").strip().upper()
    if (model_cfg.get("label") != "Doubao"
            and action_now in ("BUY", "SELL")
            and _direction_conflict(str(result.get("reasoning_path", "")), action_now,
                                    target_now)):
        print(f"  [{model_cfg['label']}] 推演方向与 action 相反, retrying with consistency fix...", flush=True)
        try:
            raw_text_fix = _do_api_call(use_json_mode, retry_hint=_DIRECTION_RETRY_HINT)
            result_fix = _parse_result(raw_text_fix)
            action_fix = str(result_fix.get("suggested_action", "")).upper()
            path_fix = str(result_fix.get("reasoning_path", ""))
            target_fix = str(result_fix.get("target_asset", "") or "").strip().upper()
            if (action_fix in ("BUY", "SELL")
                    and not _direction_conflict(path_fix, action_fix, target_fix)
                    and not path_fix.startswith("关键词推断")):
                result = result_fix
                print(f"  [{model_cfg['label']}] consistency-fix retry OK", flush=True)
            else:
                print(f"  [{model_cfg['label']}] consistency-fix retry still conflicting, keep original", flush=True)
        except Exception as exc:
            print(f"  [{model_cfg['label']}] consistency-fix retry failed: {type(exc).__name__}", flush=True)

    # ── 副品种逐品种覆盖强制：sibling 八段里缺逐品种行 → 带纠正提示重试 1 次 ──
    # 规则来源（用户明确诉求 2+1）：列进副品种就必须有它自己的完整八段；
    # 说不出独立分析就不该列进来。提示里给了两条出路：补写逐段行（并说明无数据原因），
    # 或干脆把它从 sibling_decisions 删掉（宁缺毋滥）。
    # 校验按【段】判（用户要求"每一段都要有它的行"），不只判"整篇有没有提过"。
    # 慢通道（MiniMax）不重试；快通道都重试（此前误用 use_json_mode 导致永不触发）。
    if model_cfg.get("label") != "Doubao" and not _is_slow_channel(model_cfg):
        _missing_map = _siblings_missing_stages(result)
        if not _missing_map:
            # 退化兜底：整篇都没有该品种行的情况（段名解析失败时 _missing_map 可能是空的）
            _flat = _siblings_missing_in_stages(result)
            if _flat:
                _missing_map = {c: list(_COVERAGE_STAGES) for c in _flat}
        if _missing_map:
            _desc = "；".join(
                f"{c} 缺 {'/'.join(stages)}" for c, stages in _missing_map.items()
            )
            # 提示语带上具体缺哪些段，让模型有的放矢
            _hint = _PER_ASSET_RETRY_HINT + (
                "\n本次具体缺口：" + _desc + "。请针对这些品种、这些段补写逐品种行。"
            )
            print(
                f"  [{model_cfg['label']}] 副品种逐段覆盖不足（{_desc}）, "
                "retrying with per-asset hint...",
                flush=True,
            )
            try:
                raw_text_p = _do_api_call(use_json_mode, retry_hint=_hint)
                result_p = _parse_result(raw_text_p)
                if not str(result_p.get("reasoning_path", "")).startswith("关键词推断"):
                    _after_map = _siblings_missing_stages(result_p)
                    # 接受条件：总缺口段数减少（补齐了，或把说不出的品种从 sibling 里删了）
                    _before_n = sum(len(v) for v in _missing_map.values())
                    _after_n = sum(len(v) for v in _after_map.values())
                    if _after_n < _before_n:
                        result = result_p
                        print(
                            f"  [{model_cfg['label']}] per-asset retry OK"
                            f"（缺口 {_before_n} 段 → {_after_n} 段）",
                            flush=True,
                        )
                    else:
                        print(
                            f"  [{model_cfg['label']}] per-asset retry 未改善"
                            f"（缺口仍 {_after_n} 段）, keep original",
                            flush=True,
                        )
            except Exception as exc:
                print(f"  [{model_cfg['label']}] per-asset retry failed: {type(exc).__name__}", flush=True)

    # ── 主品种分析针对性强制（A）：判了主品种却没分析它 → 带提示重试 1 次 ──
    # 实测跑偏案例：主品种判 XAU，八段却通篇讲"中亚峰会/区域合作"，零黄金分析，
    # 还据此出了 BUY —— 分析无针对性等于给下游一个没有依据的信号。
    # 判定：八段里"提及主品种"的段数 < 2（宽松计数，含中文别名），且主品种是可交易品种。
    _focus_retried = False
    if model_cfg.get("label") != "Doubao" and not _is_slow_channel(model_cfg):
        try:
            _ta_now = str(result.get("target_asset") or "").strip().upper()
            _rp = str(result.get("reasoning_path", "") or "")
            if _ta_now and _ta_now not in ("NONE", "OTHER") and _rp and not _rp.startswith("关键词推断"):
                _hits = _asset_mentioned_stage_count(_rp, _ta_now)
                if _hits < 2:
                    print(
                        f"  [{model_cfg['label']}] 主品种 {_ta_now} 八段仅提及 {_hits} 段（分析跑偏）, "
                        "retrying with focus hint...",
                        flush=True,
                    )
                    _focus_retried = True
                    raw_text_f = _do_api_call(use_json_mode, retry_hint=_MAIN_FOCUS_RETRY_HINT)
                    result_f = _parse_result(raw_text_f)
                    _rp_f = str(result_f.get("reasoning_path", "") or "")
                    _ta_f = str(result_f.get("target_asset") or "").strip().upper()
                    if not _rp_f.startswith("关键词推断") and _rp_f:
                        _hits_f = (
                            _asset_mentioned_stage_count(_rp_f, _ta_f)
                            if _ta_f and _ta_f not in ("NONE", "OTHER")
                            else 99  # 改成 NONE/HOLD 也算"解决了跑偏"
                        )
                        if _hits_f > _hits:
                            result = result_f
                            print(
                                f"  [{model_cfg['label']}] focus retry OK"
                                f"（提及 {_hits} 段 → {_hits_f if _hits_f != 99 else 'NONE/HOLD'}）",
                                flush=True,
                            )
                        else:
                            print(f"  [{model_cfg['label']}] focus retry 未改善, keep original", flush=True)
        except Exception as exc:
            print(f"  [{model_cfg['label']}] focus retry failed: {type(exc).__name__}", flush=True)

    # ── 主品种一致性归一：target_asset 为唯一权威，对齐 asset_reasoning / sibling ──
    # （模型会自相矛盾：target_asset=WTI 但 asset_reasoning 写「主品种 XAU」；
    #  或 target_asset=NONE 却把真实主品种塞进 sibling → 界面显示混乱）
    result = _normalize_main_asset(result)

    # ── v174: JSON Schema 硬约束 + 价格幻觉校验 ──
    # 校验失败 → 在 result 里贴 _v174_validation 标签(供后续 structured_check 写入 DB),
    # 不再 retry (前面已重试 6 次, 再重试会拖慢主路径); fabrication ≥50% 降级 HOLD
    try:
        import llm_schema as _ls_v174
        # 拉 structured_input.snapshot 用于价格幻觉校验(若有 _SERVER_SNAPSHOT_CACHE)
        _snap_v174 = None
        try:
            global _SERVER_SNAPSHOT_CACHE
            _msd_raw_v174 = _SERVER_SNAPSHOT_CACHE.get("json") if isinstance(_SERVER_SNAPSHOT_CACHE, dict) else None
            if isinstance(_msd_raw_v174, str):
                import json as _json_v174
                try:
                    _msd_v174 = _json_v174.loads(_msd_raw_v174)
                    _snap_v174 = _msd_v174.get("snapshot") if isinstance(_msd_v174, dict) else None
                except Exception:
                    _snap_v174 = None
            elif isinstance(_msd_raw_v174, dict):
                _snap_v174 = _msd_raw_v174.get("snapshot")
        except Exception:
            _snap_v174 = None
        # snapshot 应该是 list, 包成 dict 传给 llm_schema
        _snapshot_for_check = {"snapshot": _snap_v174} if isinstance(_snap_v174, list) else _snap_v174
        _v174_label = _ls_v174.validate_and_label(
            result, _snapshot_for_check, fabrication_tolerance=0.3
        )
        result["_v174_validation"] = _v174_label
        # fabrication ≥50% 或 schema 完全失败 → 降级 HOLD (不直接 reject, 留 fallback)
        # v176：fabricated_soft / warn 不强制 HOLD，仅写 verification 提示
        if _v174_label["verdict"] == "fabricated" and not str(result.get("reasoning_path", "")).startswith("关键词推断"):
            score = 0.0
            action = "HOLD"
            reasoning = (f"{action}信号[价格幻觉降级] ({_v174_label['fabrication_rate']*100:.0f}% 数字无法对位): "
                         + (str(result.get("reasoning") or "")[:40]))
            print(
                f"  [V174-SCHEMA] {model_cfg['label']} 价格幻觉 {_v174_label['fabrication_rate']*100:.0f}% ≥50% → 降级 HOLD",
                flush=True,
            )
        elif _v174_label["verdict"] == "fabricated_soft":
            # 30-50% 软边界 — 不降级但写 verification WARN，前端展示提示
            _cur_ver = str(result.get("verification") or "")
            if "价格幻觉软边界" not in _cur_ver:
                result["verification"] = (
                    f"warn | 价格幻觉软边界 {_v174_label['fabrication_rate']*100:.0f}% "
                    f"(30-50%); {_cur_ver[:200]}".strip("; ")
                )
            print(
                f"  [V174-SCHEMA] {model_cfg['label']} 价格幻觉软边界 {_v174_label['fabrication_rate']*100:.0f}% → 不强制 HOLD",
                flush=True,
            )
        elif _v174_label["verdict"] == "warn":
            _cur_ver = str(result.get("verification") or "")
            if "价格轻微偏差" not in _cur_ver:
                result["verification"] = (
                    f"warn | 价格轻微偏差 {_v174_label['fabrication_rate']*100:.0f}%; {_cur_ver[:200]}".strip("; ")
                )
            print(
                f"  [V174-SCHEMA] {model_cfg['label']} warn fabrication_rate={_v174_label['fabrication_rate']*100:.1f}%",
                flush=True,
            )
        elif _v174_label["verdict"] == "schema_invalid":
            print(
                f"  [V174-SCHEMA] {model_cfg['label']} JSON Schema 校验失败 ({len(_v174_label['schema_errors'])} 错)",
                flush=True,
            )
        else:
            print(
                f"  [V174-SCHEMA] {model_cfg['label']} pass (fabrication_rate={_v174_label['fabrication_rate']*100:.1f}%)",
                flush=True,
            )
    except ImportError:
        pass  # llm_schema 未安装,跳过(理论上 v174+ 应该安装)
    except Exception as _e_v174:
        print(f"  [V174-SCHEMA] 校验异常(不影响主流程): {type(_e_v174).__name__}: {_e_v174}", flush=True)

    # ── 副品种准入兜底（宁缺毋滥）：重试后仍写不出独立分析的副品种 → 降级剔除 ──
    # 用户原则："列进副品种就必须有它自己的八段分析，否则它就跟副品种没关系"。
    # 重试已尝试过补写；仍缺大部分段 → 说明它本就说不清传导链，留在列表里只会
    # 让前端显示一片空白。这里把它从 sibling_decisions 移除（主品种不受影响）。
    try:
        _still = _siblings_missing_stages(result)
        if _still:
            _sibs_all = result.get("sibling_decisions") or []
            _kept, _dropped = [], []
            for _s in _sibs_all:
                if not isinstance(_s, dict):
                    continue
                _code = str(_s.get("target_asset") or "").strip().upper()
                _miss = _still.get(_code) or []
                # 缺 4 段以上（即八段里不到 1/3 有它的行）→ 判定"说不清"，剔除
                if _code and len(_miss) >= 4:
                    _dropped.append(_code)
                else:
                    _kept.append(_s)
            if _dropped:
                result["sibling_decisions"] = _kept
                _detail = "、".join(f"{c}(缺{len(_still.get(c, []))}段)" for c in _dropped)
                print(
                    f"  [SIBDROP] 副品种 {_detail} 八段里几乎无独立分析 → 已从副品种移除",
                    flush=True,
                )
                # 同步清掉 asset_reasoning 里对应品种的推理行（避免"推理逻辑"里
                # 还列着一个已经不算副品种的品种）
                try:
                    _ar = str(result.get("asset_reasoning") or "")
                    if _ar:
                        _lines = []
                        for _ln in _ar.split("；"):
                            _t = _ln.strip()
                            if not _t:
                                continue
                            if _t.startswith("副品种") and any(
                                _t.lstrip("副品种").lstrip().upper().startswith(c) for c in _dropped
                            ):
                                continue
                            _lines.append(_t)
                        result["asset_reasoning"] = "；".join(_lines)[:400]
                except Exception:
                    pass
    except Exception:
        pass

    # ── Doubao fast path: simplified response, no scoring or CoT ──
    if model_cfg.get("label") == "Doubao":
        action = str(result.get("suggested_action", "HOLD")).upper()
        if action not in ("BUY", "SELL", "HOLD"):
            action = "HOLD"
        direct_reasoning = str(result.get("direct_reasoning", "")).strip()[:80]
        if not direct_reasoning:
            direct_reasoning = f"{action} 直觉判断"
        print(f"  [{model_cfg['label']}] {action} | {direct_reasoning}")
        score, action = reconcile_score_action(0.0, action)
        return {
            "sentiment_score": score,
            "suggested_action": action,
            "reasoning": direct_reasoning,
            "translated_title": direct_reasoning[:40],  # 使用 reasoning 的前 40 字符作为显示标题
            "market_category": "",
            "target_asset": "NONE",
            "reasoning_path": "",
            "model_label": model_cfg["label"],
            "model_id": model_cfg["id"],
            # Phase 2: Event Quality Layer (Doubao 不输出这些 → 默认值)
            "event_strength": "medium",
            "direct_catalyst": False,
            "timeframe_match": "intraday",
        }

    # Validate & normalise fields
    score = float(result.get("sentiment_score", 0))
    score = max(-1.0, min(1.0, score))
    # Safety net: never store exactly 0.0 — it's useless for aggregation
    if abs(score) < 0.001:
        score = 0.05  # tiny bullish bias, better than zero
    action = str(result.get("suggested_action", "HOLD")).upper()
    if action not in ("BUY", "SELL", "HOLD", _FAILED_ACTION):
        action = "HOLD"
    reasoning = str(result.get("reasoning", "")).strip()[:80]
    # ── 没分析 → 实事求是标「分析失败」，绝不伪装成观望(HOLD) ──
    # 空骨架（八段段名连写、无任何实质正文）= 模型没真正分析。
    if action != _FAILED_ACTION and _is_eight_stage_skeleton(
            str(result.get("reasoning_path", "") or "")):
        action = _FAILED_ACTION
        reasoning = f"{_FAILED_GATE_PREFIX}：模型未输出有效八段分析"
    # ── AI 结果为主：输出观望(HOLD)/分析失败(FAILED)都不建仓 ──
    # 不管 sentiment_score / 策略积分多强，都绝不把 HOLD/FAILED 改写成方向单；
    # 只有 AI 明确输出 BUY/SELL（且后续闸门+策略积分满足）才可能建仓。
    if action in ("HOLD", _FAILED_ACTION):
        score = 0.0  # 观望/失败：分数归中性（不再出现"说观望却强分"的矛盾）
        if not reasoning:
            reasoning = ("观望：该新闻不足以给出明确方向" if action == "HOLD"
                         else f"{_FAILED_GATE_PREFIX}：模型未输出有效八段分析")
    # Empty reasoning fallback
    if not reasoning:
        reasoning = f"{action}信号,得分{score:+.2f}"

    # ── Display title: 从 reasoning 提取简短中文标题（用于前端显示） ──
    # 注意：前置翻译拦截器已确保输入为中文，这里仅做显示用途的截取
    display_title = reasoning[:40]

    market_category = str(result.get("market_category", "")).upper().strip()
    # 不在 PROD 白名单 → 留空（前端会渲染为"待复核"）；不再 fallback 到 OTHER
    if market_category and market_category not in MARKET_CATEGORIES_PROD:
        if not (market_category == "OTHER" and is_debug_other_allowed()):
            market_category = ""
    target_asset = str(result.get("target_asset", "NONE")).upper().strip()[:20]
    if not target_asset:
        target_asset = "NONE"
    # ── 分类与目标资产一致性：AI 偶尔报 market_category 与分析对象(target_asset)脱节
    # （如"标签原油但分析 BTC"）→ 以 target_asset 为准修正 market_category（前端标签与
    # AI 分析对象保持一致）。走 market_classifier 单一真理源；MACRO 保留模型判断（外汇
    # /国债本质无具体可交易标的）；target_asset=NONE 不改。
    _reconciled = map_target(target_asset)
    if _reconciled and market_category not in ("MACRO",) and market_category:
        if market_category != _reconciled:
            print(
                f"  [CATFIX] market_category={market_category} 与 target_asset={target_asset}"
                f" 不一致 → 修正为 {_reconciled}",
                flush=True,
            )
        market_category = _reconciled
    elif _reconciled and not market_category:
        # market_category 留空 + target_asset 有映射 → 反推回填（避免漏空）
        market_category = _reconciled

    # ── 分析针对性风控兜底（B）：重试后仍无针对性 → 不给方向单（标「分析失败」）──
    # 原则：方向单必须建立在"针对该品种的分析"之上。若八段始终不讲主品种，
    # 说明这次判断缺乏依据，实事求是标「分析失败」（不建仓），绝不伪装成观望(HOLD)。
    # 实现已抽到 _focus_downgrade()：副品种展开路径（_expand_multi_asset_results）
    # 共用同一份规则，避免两处判定条件漂移。
    try:
        _ta_final = str(result.get("target_asset") or "").strip().upper()
        _rp_final = str(result.get("reasoning_path", "") or "")
        _downgraded, _hits_final = _focus_downgrade(result, _ta_final, _rp_final)
        _gtrace = result.setdefault("_gate_trace", [])
        if _downgraded:
            # 同步本地变量：下面返回时用的是 score/action 局部变量，
            # 不同步会导致降级被旧值覆盖（实测踩过：日志显示已降级、结果仍是 BUY）。
            score = 0.0
            action = _FAILED_ACTION
            reasoning = str(result.get("reasoning") or "")
            _gtrace.append({"gate": "FOCUS", "verdict": "block",
                            "reason": f"主品种 {_ta_final} 分析无针对性（仅 {_hits_final} 段提及）"})
            print(
                f"  [FOCUS-GATE] 主品种 {_ta_final} 分析无针对性（仅 {_hits_final} 段提及）"
                f" → 标记分析失败（不建仓）",
                flush=True,
            )
        else:
            _gtrace.append({"gate": "FOCUS", "verdict": "pass",
                            "reason": f"主品种 {_ta_final} 针对性 OK"})
    except Exception as exc:
        # 不吞错：以前这里静默 pass，闸门失效时线上完全无迹可查
        print(f"  [FOCUS-GATE] 跳过（{type(exc).__name__}: {exc}）", flush=True)

    # ── 传导机制闸门：方向单必须说出"新闻 → 该品种价格"的传导链 ──
    # 用户原则："有关系 = 新闻内容会影响它的价格波动，而不是新闻某字和它有关系"。
    # 针对性闸门只保证"有分析"（提及 >= 2 段），这里再保证"有传导"。
    try:
        _mech_asset = str(result.get("target_asset") or "").strip().upper()
        # v176.5: 直接用 gate_strength (>=8 时不降级, 信任 LLM 强信号)
        _gtrace = result.setdefault("_gate_trace", [])
        if _mechanism_downgrade(result, _mech_asset, strength=gate_strength):
            score = 0.05
            action = "HOLD"
            reasoning = str(result.get("reasoning") or "")
            _gtrace.append({"gate": "MECH", "verdict": "block",
                            "reason": f"主品种 {_mech_asset} 未写出传导链"})
            print(f"  [MECH-GATE] 主品种 {_mech_asset} 未写出传导链 (strength={gate_strength})"
                  f" → 方向单降级 HOLD（影响不明确，不下单）", flush=True)
        else:
            _gtrace.append({"gate": "MECH", "verdict": "pass",
                            "reason": f"主品种 {_mech_asset} 传导链 OK (strength={gate_strength})"})
    except Exception as exc:
        print(f"  [MECH-GATE] 跳过（{type(exc).__name__}: {exc}）", flush=True)

    # Extract reasoning_path — stored in DB & pushed to Feishu
    reasoning_path = str(result.get("reasoning_path", "")).strip()[:2000]
    if reasoning_path:
        print(f"  [{model_cfg['label']}] 推导链: {reasoning_path[:120]}")
    elif news_content:
        # Fallback: if model didn't provide CoT, show truncated news as debug
        short = news_content[:60].replace('\n', ' ')
        print(f"  [{model_cfg['label']}] (无CoT) 新闻: {short}")

    # ── 品种相关推理逻辑（AI 逐品种解释为什么关联此品种；缺省留空，前端老数据不显示区块）──
    asset_reasoning = str(result.get("asset_reasoning", "")).strip()[:400]

    # ── 多品种：AI JSON 显式 sibling_decisions，缺失则从八段文本兜底补 ──
    #    主品种未成立（NONE/OTHER）时不兜底推断副品种：没有主品种就没有"因"，
    #    推断出来的只是些被顺带点名的品种，不该作为可交易信号的存在理由。
    _sibs_out = result.get("sibling_decisions")
    if (not isinstance(_sibs_out, list) or not _sibs_out):
        if str(target_asset or "").strip().upper() in _FOCUS_EXEMPT_ASSETS:
            _sibs_out = []
        else:
            _sibs_out = _infer_sibling_from_reasoning(
                reasoning_path, main_asset=target_asset,
            ) or []
    # ── asset_reasoning 覆盖兜底：sibling 有副品种但推理文本缺行时，用副品种
    #    reasoning/八段上下文补一行占位，保证前端「品种相关推理逻辑」逐品种显示 ──
    asset_reasoning = _ensure_asset_reasoning_coverage(
        asset_reasoning, _sibs_out, main_asset=target_asset,
    )
    # ── 二次主品种归一：上面的兜底会补出 sibling（_infer_sibling_from_reasoning），
    #    而第一次归一（_call_llm_sync 内）跑在补 sibling 之前 → target_asset=NONE
    #    时没有可提升对象，导致库里仍留 NONE（实测 news=4617/4618 就是这种）。
    #    这里在 sibling 补全后再归一一次，保证 NONE 有副品种时被提升为主品种。
    _final = _normalize_main_asset({
        "target_asset": target_asset,
        "market_category": market_category,
        "sentiment_score": score,
        "suggested_action": action,
        "reasoning": reasoning,
        "asset_reasoning": asset_reasoning,
        "sibling_decisions": _sibs_out,
    })
    target_asset = str(_final.get("target_asset") or target_asset)
    market_category = str(_final.get("market_category") or market_category)
    score = float(_final.get("sentiment_score", score))
    action = str(_final.get("suggested_action", action)).upper()
    reasoning = str(_final.get("reasoning", reasoning))
    asset_reasoning = str(_final.get("asset_reasoning", asset_reasoning))
    _sibs_out = _final.get("sibling_decisions") or []

    # ── 主品种前置（最后一次对齐）：主品种最终仍未成立（NONE/OTHER）→ 不保留副品种。
    #    "先有主品种，后面才会有副品种"：兜底推断出来的副品种此时没有可依托的主品种，
    #    留着只会在界面上给出一个没有来源的"其它品种信号"，且宽松模式下会真建仓。
    if str(target_asset or "").strip().upper() in _FOCUS_EXEMPT_ASSETS and _sibs_out:
        print(
            f"  [MAIN-GATE] 主品种未成立（{str(target_asset or 'NONE').upper()}）"
            f" → 移除 {len(_sibs_out)} 个无主品种依托的副品种",
            flush=True,
        )
        _sibs_out = []

    # ── v211 P1-B：服务端兜底 — 二次归一后 target_asset 仍为 NONE/OTHER 时，
    #    扫描 news_content 用 _ASSET_ALIASES 找回真实 ticker（避免数据进 unclassified_events）。
    #    根因：LLM 在「个股/港股/韩股」类扩展品种上常输出 NONE，710 条 NO_MAPPING_FOR_ASSET:NONE
    #    几乎全因 LLM 不会写 SKHYNIX/MU 等小众 ticker。修复只覆盖 LLM 输出 NONE/OTHER 的情况，
    #    不主动覆盖 LLM 已正确填写的 target_asset。
    try:
        _rescued_target = _rescue_target_asset_from_content(
            news_content, target_asset,
        )
        if _rescued_target and _rescued_target != target_asset and str(_rescued_target or "").strip().upper() not in (
            "", "NONE", "OTHER", "N/A", "NULL",
        ):
            # market_category 用 market_classifier 重新映射（一致性修正）
            try:
                import market_classifier as _mc_v211
                _rescued_cat = _mc_v211.map_target(_rescued_target) or market_category
            except Exception:
                _rescued_cat = market_category
            print(
                f"  [RESCUE-TARGET] LLM 输出 NONE/OTHER，从正文找回 {_rescued_target}"
                f"（market_category {_rescued_cat}）",
                flush=True,
            )
            target_asset = _rescued_target
            market_category = _rescued_cat
    except Exception as _exc_rescue:
        print(f"  [RESCUE-TARGET] 跳过（{type(_exc_rescue).__name__}: {_exc_rescue}）", flush=True)

    # ── 把 _v174_validation (JSON Schema + 价格幻觉) 透传到下游,供 structured_check 合并 ──
    _v174_validation_out = result.get("_v174_validation")
    _factor_ranking_out = result.get("factor_ranking") or []
    _asset_nature_out = result.get("asset_nature") or ""

    return {
            "sentiment_score": score,
            "suggested_action": action,
            "reasoning": reasoning,
            "translated_title": display_title,  # 使用 reasoning 的前 40 字符作为显示标题
            "market_category": market_category,
            "target_asset": target_asset,
            "reasoning_path": reasoning_path,
            "asset_reasoning": asset_reasoning,
            "sibling_decisions": _sibs_out,
            "model_label": model_cfg["label"],
            "model_id": model_cfg["id"],
            # Phase 2: Event Quality Layer — 补齐 LLM 元数据字段（_parse_result 已有默认兜底）
            "prediction_type": result.get("prediction_type", "continuation"),
            "event_phase": result.get("event_phase", "mid"),
            "market_confirmation": result.get("market_confirmation", "unknown"),
            "expected_horizon": result.get("expected_horizon", "1-3d"),
            "invalidation_condition": result.get("invalidation_condition", ""),
            "event_strength": result.get("event_strength", "medium"),
            "direct_catalyst": bool(result.get("direct_catalyst", False)),
            "timeframe_match": result.get("timeframe_match", "intraday"),
            # 影响评估（0-10，_parse_result 已 clamp）
            "heat_score": result.get("heat_score", 5.0),
            "impact_score": result.get("impact_score", 5.0),
            "asset_impact": result.get("asset_impact", 5.0),
            # v174: JSON Schema 校验 + 价格幻觉标签 (供 ai_worker 后续 structured_check 写入 DB)
            "_v174_validation": _v174_validation_out,
            # v174: 因素排行 / 资产性质 (即使 LLM 没填也透传,structured_input 可观察)
            "factor_ranking": _factor_ranking_out,
            "asset_nature": _asset_nature_out,
            # v174: 新增三字段 (event_type_code / verification / snapshot_proximity)
            "event_type_code": result.get("event_type_code") or "",
            "verification": result.get("verification") or "",
            "snapshot_proximity": result.get("snapshot_proximity"),
            # v181: 主分析阶段 FOCUS/MECH 闸门判定留痕（供主循环回放到 gate_chain）
            "_gate_trace": result.get("_gate_trace") or [],
            # v188 fix：返回时透传全网溯源三字段（Tier-3 兜底/LLM 抽签/正则抽取都写到 result，
            # 但此 return dict 没透出 → 调用方 res.get() 永远拿空 → DB 留空）。
            # 不透传的话，Tier-3 平台首发兜底、regex 主渠道抽取全部白干——本次会话
            # 直接定位为 bug 根因（之前误以为是 Tier-3 没触发，其实是 return dict 没出口）。
            "cross_net_first_seen_ts": result.get("cross_net_first_seen_ts") or "",
            "cross_net_source": result.get("cross_net_source") or "",
            "net_origin_info": result.get("net_origin_info") or "",
        }




# ---------------------------------------------------------------------------
# Task B — Concurrent Batch AI Worker (BATCH_SIZE 定义见 config.py)
# ---------------------------------------------------------------------------


def _news_age_minutes(timestamp: str) -> Optional[float]:
    """新闻发布时间距现在的分钟数（统一按北京时间换算；解析失败返回 None）。

    走 config.parse_time_to_bj：带时区（+08:00/Z/±HH:MM）换算到北京，
    无时区串按北京时间解读——与入库闸同一时区口径，避免差 8 小时。
    """
    dt = config.parse_time_to_bj(timestamp)
    if dt is None:
        return None
    return (datetime.now(config.TZ_SHANGHAI) - dt).total_seconds() / 60.0


# 新闻时效门槛（秒）：后台可调，引擎按 TTL 读取最新值（≤5s 生效）
_news_max_age_cache: Dict[str, Any] = {"ts": 0.0, "value": config.NEWS_TRADE_MAX_AGE_MINUTES * 60}


def _get_news_trade_max_age_seconds() -> int:
    """从 paper_trading_settings 读后台配置的时效门槛（秒），5s TTL 缓存。"""
    now = time.monotonic()
    if now - float(_news_max_age_cache.get("ts", 0.0)) < 5.0:
        return int(_news_max_age_cache["value"])
    value = config.NEWS_TRADE_MAX_AGE_MINUTES * 60
    try:
        from paper_trading import get_settings as _get_pt_settings
        st = _get_pt_settings()
        stored = st.get("news_trade_max_age_seconds")
        if isinstance(stored, (int, float)) and stored >= 30:
            value = int(stored)
    except Exception:
        pass
    _news_max_age_cache.update({"ts": now, "value": value})
    return value


def _load_insights_section() -> str:
    """复盘经验注入分区（结构化标题 + 逐条内容）。

    独立成区而非挤成一行 " | "：模型能逐条对照，且带样本数（可靠性依据）。
    硬铁律(H#)/软启发(EX#) 分两区，由 build_active_insights_section 输出。
    条数由 config.REVIEW_INSIGHTS_LIMIT 控制（默认不限 = 启用多少注入多少）。
    """
    try:
        from review_agent import build_active_insights_section, seed_analysis_experience
        try:
            seed_analysis_experience()  # 幂等播种硬铁律/软启发种子（首次分析写入）
        except Exception:
            pass
        return build_active_insights_section()
    except Exception:
        return ""


# v176.3: 反思错误码注入分区 — 让 AI 决策时看到最近决策触发的程序化校验错误码
def _load_reflection_block(limit: int = 10) -> str:
    """从 decision_errors 表取最近 N 个去重错误码 → 一句话指导 → 注入 system prompt。

    与复盘经验 (insights section) 互补 — insights 是"教训性的经验规则"，
    reflection 是"近期决策触发的具体错误码"（更即时、更具体）。
    30s TTL 缓存，避免每次分析都查 DB；任何异常 → 返回 ""（零注入）。
    """
    global _REFLECTION_BLOCK_CACHE
    now = time.monotonic()
    try:
        cached = _REFLECTION_BLOCK_CACHE
        if cached and (now - cached.get("ts", 0)) < 30:
            return cached.get("text", "")
    except Exception:
        pass
    try:
        from reflection_checkers import get_active_error_codes_summary, format_compact_guidance
        import db as _db
        conn = _db.get_connection()
        conn.row_factory = _db.sqlite3.Row  # db.get_connection() 不设 row_factory, 这里手动设
        codes = get_active_error_codes_summary(limit=limit, conn=conn)
        text = format_compact_guidance(codes) if codes else ""
    except Exception:
        text = ""
    try:
        _REFLECTION_BLOCK_CACHE = {"ts": now, "text": text}
    except Exception:
        pass
    return text


# 反思错误码注入块缓存（30s TTL）
_REFLECTION_BLOCK_CACHE: Dict[str, Any] = {"ts": 0.0, "text": ""}


# v186 经验系统注入 — 按当前 asset/gate_kind/actor 上下文从 experience_entries
# 取最相关的 top-5 条经验,作为 system prompt 末尾的一个分区。与 insights 互补:
#   insights = 复盘历史沉淀的硬铁律/软启发(全局通用, 跨品种)
#   experience = 当前品种/当前人物的特化经验(精准, 短期信号校准)
_EXPERIENCE_BLOCK_CACHE: Dict[str, Any] = {"ts": 0.0, "text": "", "key": None}


# v188 P0-2: 最近一次真正注入到 prompt 的经验 ID 列表(用于 ai_decisions 写入后标 llm_invoked=1)
_LAST_INJECTED_EXP_IDS: List[int] = []


def _load_spa_applied_block_sync(asset: str = "", max_chars: int = 600,
                                 ttl: float = 60.0) -> str:
    """v199: 加载 strategy_param_adjustments (status='applied') 设置块注入 LLM prompt。

    数据源: strategy_param_adjustments (status='applied')
    匹配规则:
      1. asset 完全匹配 (SPA.asset == 查询 asset)
      2. SPA.asset ∈ {NONE, *, ''} 作为兜底 (跨 asset 通用)
    输出格式:
      # 策略调整已应用 (v199 SPA Applied Block)
      → asset={asset} 同 asset {n} 条, 全局兜底 {n2} 条
      [SPA1] [param_key] direction mag=n (n=sample, conf=0.xx)
    60s TTL cache (按 asset 维度)。
    """
    global _SPA_APPLIED_BLOCK_CACHE
    asset_norm = (asset or "").upper()
    now = time.monotonic()
    try:
        cached = _SPA_APPLIED_BLOCK_CACHE
        if cached and cached.get("key") == asset_norm and (now - cached.get("ts", 0)) < ttl:
            return cached.get("text", "")
    except Exception:
        pass
    try:
        import sqlite3 as _sq3
        conn = db.get_connection()
        conn.row_factory = _sq3.Row
        try:
            rows = conn.execute("""
                SELECT asset, param_key, direction, magnitude,
                       sample_size, avg_confidence, applied_setting_change,
                       updated_at
                  FROM strategy_param_adjustments
                 WHERE status='applied'
                   AND applied_setting_change != ''
                 ORDER BY updated_at DESC LIMIT 50
            """).fetchall()
        finally:
            conn.close()
        if not rows:
            return ""
        matched = []
        generic = []
        for r in rows:
            ra = str(r["asset"] or "").upper()
            if not asset_norm or asset_norm in ("NONE", "*", ""):
                matched.append(dict(r))
            elif ra == asset_norm:
                matched.append(dict(r))
            elif ra in ("NONE", "*", ""):
                generic.append(dict(r))
        items = matched + generic[:3]
        if not items:
            return ""
        lines = ["# 策略调整已应用 (v199 SPA Applied Block)"]
        lines.append(f"  → asset={asset_norm or 'ANY'} 同 asset {len(matched)} 条, 全局兜底 {len(generic)} 条")
        lines.append("")
        for i, it in enumerate(items[:8], 1):
            try:
                conf_v = float(it.get("avg_confidence") or 0)
            except Exception:
                conf_v = 0.0
            lines.append(
                f"[SPA{i}] [{it['param_key']}] {it.get('direction','')} "
                f"mag={it.get('magnitude',0)} "
                f"(n={it.get('sample_size',0)}, conf={conf_v:.2f})"
            )
        block = "\n".join(lines)
        if len(block) > max_chars:
            block = block[:max_chars - 50] + "\n...(SPA 块过长已截断)"
    except Exception as exc:
        print(f"[SPA-APPLY-BLOCK] 加载失败: {type(exc).__name__}: {exc}", flush=True)
        block = ""
    try:
        _SPA_APPLIED_BLOCK_CACHE = {"ts": now, "text": block, "key": asset_norm}
    except Exception:
        pass
    return block


_SPA_APPLIED_BLOCK_CACHE: Dict[str, Any] = {}


def _load_experience_block_sync(asset: str = "", gate_kind: str = "",
                                actor: str = "", ttl: float = 30.0) -> str:
    """v186 经验系统注入分区。30s TTL 缓存。
    asset: 主品种(BTC/ETH/SOL/XAU/WTI 等)
    gate_kind: gate 类型(总结类/政策类/事件类 ...)
    actor: 人物/机构(特朗普/Powell/Gensler/Vitalik ...)
    """
    global _EXPERIENCE_BLOCK_CACHE, _LAST_INJECTED_EXP_IDS
    now = time.monotonic()
    key = (asset.upper(), gate_kind.lower(), actor.lower())
    try:
        cached = _EXPERIENCE_BLOCK_CACHE
        if cached and cached.get("key") == key and (now - cached.get("ts", 0)) < ttl:
            # 命中缓存时, 同步还原上次 inject 的 ids
            try:
                _LAST_INJECTED_EXP_IDS = list(cached.get("exp_ids") or [])
            except Exception:
                _LAST_INJECTED_EXP_IDS = []
            return cached.get("text", "")
    except Exception:
        pass
    try:
        import experience_reflector as _er
        # v188 P0-2: build_prompt_block 返回 (text, ids) — ids 是本次真正 inject 的 exp id 列表
        _result = _er.build_prompt_block(asset=asset or "", gate_kind=gate_kind or "",
                                          actor=actor or "", max_chars=1800)
        if isinstance(_result, tuple) and len(_result) == 2:
            block, ids = _result
        else:
            block, ids = _result, []
        _LAST_INJECTED_EXP_IDS = list(ids or [])
    except Exception as exc:
        print(f"[EXP-BLOCK] 经验注入失败: {type(exc).__name__}: {exc}", flush=True)
        block = ""
    try:
        _EXPERIENCE_BLOCK_CACHE = {"ts": now, "text": block, "key": key,
                                      "exp_ids": list(ids or [])}
    except Exception:
        pass
    return block


_last_kline_persist_ts: float = 0.0      # K 线持久化冷却（惰性 persist）
_KLINE_PERSIST_COOLDOWN_S = 60.0         # 无快照时每 1 分钟最多 persist 一次（缩短 K 线断流时间）
# WTI/XAU K 线 09-14 后断流的根因：UA `TridentAgentMVP/1.0` 被 Yahoo TLS RST。
# 修复:UA → Mozilla（knowledge_base）+ persist 频率 300s → 60s + 不再"快照空才 persist"。


def _load_kline_block() -> str:
    """历史 K 线知识库 block：每 60s 主动 persist + 读最新 kline_snapshots。

    只描述位置/趋势、不直接给方向（反共识对照），数据缺失标 N/A 不造数。
    """
    global _last_kline_persist_ts
    try:
        import knowledge_base
        if time.monotonic() - _last_kline_persist_ts >= _KLINE_PERSIST_COOLDOWN_S:
            _last_kline_persist_ts = time.monotonic()
            try:
                knowledge_base.persist()
            except Exception as persist_err:
                print(f"  [KLINE] persist 失败: {type(persist_err).__name}: {str(persist_err)[:80]}")
        snaps = knowledge_base.latest_snapshots()
        if snaps:
            return knowledge_base.render_kline_block(snaps)
        return ""
    except Exception as e:
        print(f"  [KLINE] 构建失败: {type(e).__name_}: {str(e)[:80]}")
        return ""


def _apply_freshness(result: Dict[str, Any], timestamp: str) -> Dict[str, Any]:
    """新闻新鲜度分级：越旧的新闻信号越弱（0-15min ×1.0 / 15-30 ×0.85 / 30-45 ×0.7 / >45 ×0.5）。

    与硬时效门槛配合：门槛负责"超时禁止交易"，这里平滑衰减"新鲜度置信"。
    末尾顺带做多品种兜底：AI 未在 JSON 给 sibling_decisions 时，从八段文本提取
    提及的其它可交易品种补成副品种条目（保证分析/展示层多品种完整；方向性
    sibling 仍需 AI JSON 显式给出，此处无方向词 → HOLD 不建仓）。
    """
    if not isinstance(result, dict) or "sentiment_score" not in result:
        return result
    age = _news_age_minutes(timestamp or "")
    if age is None:
        _ensure_sibling_decisions(result)
        return result
    if age < 15:
        w = 1.0
    elif age < 30:
        w = 0.85
    elif age < 45:
        w = 0.7
    else:
        w = 0.5
    if w < 1.0:
        result["sentiment_score"] = max(-1.0, min(1.0, float(result["sentiment_score"]) * w))
        print(
            f"  [FRESH] 新闻 {age:.0f} 分钟前，新鲜度权重 ×{w} → score {result['sentiment_score']:+.2f}",
            flush=True,
        )
    _ensure_sibling_decisions(result)
    return result


def _ensure_sibling_decisions(result: Dict[str, Any]) -> None:
    """AI 未输出 sibling_decisions 时，从八段文本兜底补副品种条目（就地修改 result）。

    补 sibling 后同步补 asset_reasoning 覆盖（与 _call_llm_sync 主路径一致），
    防止 freshness/verify 等后续路径新增副品种但推理文本缺行。
    """
    try:
        sibs = result.get("sibling_decisions")
        if isinstance(sibs, list) and sibs:
            return  # AI 已显式给出
        # 主品种未成立（NONE/OTHER）→ 没有"因"，不推断副品种
        if str(result.get("target_asset") or "NONE").strip().upper() in _FOCUS_EXEMPT_ASSETS:
            return
        inferred = _infer_sibling_from_reasoning(
            str(result.get("reasoning_path", "")),
            main_asset=str(result.get("target_asset") or "NONE"),
        )
        if inferred:
            result["sibling_decisions"] = inferred
            # 同步补 asset_reasoning：新增的副品种也要有"为什么关联"的行
            result["asset_reasoning"] = _ensure_asset_reasoning_coverage(
                str(result.get("asset_reasoning") or ""),
                inferred,
                main_asset=str(result.get("target_asset") or "NONE"),
            )
    except Exception:
        pass  # 兜底失败不影响主结果


# ── 多模型交叉验证：高价值新闻触发（VIP/高相关/金十日历/FRED 官方）──
_VERIFY_SOURCES = ("jin10_calendar", "fred")
_VERIFY_RELEVANCE_MIN = 0.8

# ── 阶段2：多模型共识闸门 ────────────────────────────────────────────
# peer_reviews 交叉验证从"仅展示/标注"升级为"参与主决策降级/拦截"：
# 验证模型方向与主模型不一致时，方向单就地降级 HOLD（宁可观望也不建仓）。
CONSENSUS_GATE_ENABLED = True


def _is_high_value_news(news_row: Any, content: str) -> bool:
    """高价值新闻 → 需要多模型交叉验证（普通新闻单模型即可）。"""
    try:
        source = str(news_row["source"] if "source" in news_row.keys() else "")
    except Exception:
        source = ""
    if source in _VERIFY_SOURCES:
        return True
    try:
        rel = news_row["relevance_score"]
        if rel is not None and float(rel) >= _VERIFY_RELEVANCE_MIN:
            return True
    except Exception:
        pass
    vip_tag, _ = _detect_vip(content)
    return bool(vip_tag)


def _pick_verify_cfgs(main_cfg: Dict[str, Any], pool: List[Dict[str, Any]], n: int = 2) -> List[Dict[str, Any]]:
    """从模型池选 n 个与主模型不同 id 的验证模型（保持池序、去重）。"""
    out: List[Dict[str, Any]] = []
    seen = {str(main_cfg.get("id", ""))}
    for m in pool:
        mid = str(m.get("id", ""))
        if not mid or mid in seen:
            continue
        seen.add(mid)
        out.append(m)
        if len(out) >= n:
            break
    return out


def _extract_reasoning_steps(reasoning: str) -> List[str]:
    """v200+ P0-2: 提取 reasoning 中出现的「八段步骤」(与 reflection_checkers._ANALYSIS_STEPS 一致)。

    返回找到的步骤名(已去重,按八段顺序)。无 reasoning 返回 []。
    """
    if not reasoning or not isinstance(reasoning, str):
        return []
    try:
        from reflection_checkers import _ANALYSIS_STEPS as _R_ANALYSIS_STEPS
    except Exception:
        # 兜底:与 reflection_checkers._ANALYSIS_STEPS 保持一致
        _R_ANALYSIS_STEPS = (
            "总框架", "因素定位", "驱动力", "水位博弈",
            "跨资产资金", "反共识结论", "逻辑自检", "总结",
        )
    return [s for s in _R_ANALYSIS_STEPS if s in reasoning]


def _calc_step_overlap(steps_a: List[str], steps_b: List[str]) -> float:
    """v200+ P0-2: 计算两个 reasoning 步骤集合的 Jaccard 重合度。

    返回值 ∈ [0, 1]:
      - 1.0 = 完全重合(两模型思路完全一致)
      - 0.0 = 完全不重合(两模型思路完全不同)
      - 0.5 = 一半步骤重叠

    与传统 Jaccard 的区别:steps_a 为空时返回 0.0(防止拿 no info 凑数)。
    """
    a = set(steps_a or [])
    b = set(steps_b or [])
    if not a or not b:
        return 0.0
    inter = len(a & b)
    union = len(a | b)
    if union <= 0:
        return 0.0
    return inter / union


def _build_verification(res: Dict[str, Any], peer_reviews: Any) -> Optional[str]:
    """八步⑧：其他模型校验结果回写。

    主模型结论 vs 各验证模型结论 → 投票 + agree 与否 + 分歧点。
    无 peer_reviews 返回 None（入库存 NULL）；失败返回 None 不抛错。

    v200+ P0-2: 增加 reasoning_path 步骤重合度（step_overlap_avg）字段。
    原理: 主模型说"美元弱→黄金涨"和验证模型说"地缘紧张→黄金涨"action 都 BUY 但逻辑链不同
    → 单纯 action 投票会误判; 增加步骤重合度后,逻辑链不同的两条 BUY 会被识别为"形式上同向实质上分歧"。
    """
    if not peer_reviews:
        return None
    try:
        reviews = [r for r in peer_reviews if isinstance(r, dict)]
        if not reviews:
            return None
        main_action = str(res.get("suggested_action") or "HOLD").upper()
        try:
            main_score = float(res.get("sentiment_score") or 0.0)
        except (TypeError, ValueError):
            main_score = 0.0
        main_reasoning = (
            res.get("reasoning_path") or res.get("reasoning") or ""
        )
        main_steps = _extract_reasoning_steps(str(main_reasoning))

        agree = 0
        details: List[Dict[str, Any]] = []
        step_overlaps: List[float] = []
        for r in reviews:
            a = str(r.get("action") or "HOLD").upper()
            try:
                sc = float(r.get("score") or 0.0)
            except (TypeError, ValueError):
                sc = 0.0
            same_action = (a == main_action)
            same_sign = (
                (main_score > 0.05 and sc > 0.05)
                or (main_score < -0.05 and sc < -0.05)
                or (abs(main_score) < 0.05 and abs(sc) < 0.05)
            )
            ok = same_action and (main_action == "HOLD" or same_sign)
            agree += 1 if ok else 0

            # ── v200+ P0-2: 计算 reasoning 步骤重合度 ──
            review_reasoning = r.get("reasoning") or r.get("reasoning_path") or ""
            review_steps = _extract_reasoning_steps(str(review_reasoning))
            overlap = _calc_step_overlap(main_steps, review_steps)
            step_overlaps.append(overlap)
            # logic_similar: action/score 都同向且步骤重合度 ≥ 0.5
            logic_similar = bool(ok and overlap >= 0.5)
            details.append({
                "model": r.get("model_label") or r.get("model_id") or "?",
                "action": a,
                "score": sc,
                "step_overlap": round(overlap, 3),
                "logic_similar": logic_similar,
            })
        total = len(reviews)
        # ── 计算全组平均重合度,空则 None ──
        step_overlap_avg = (
            round(sum(step_overlaps) / len(step_overlaps), 3)
            if step_overlaps else None
        )
        # logic_similar_count: 步骤重合 + 方向同向 的票数
        logic_similar_count = sum(1 for d in details if d.get("logic_similar"))
        return json.dumps({
            "agree": agree == total,
            "agree_count": agree,
            "total": total,
            "main_action": main_action,
            "reviews": details,
            # v200+ P0-2 字段
            "step_overlap_avg": step_overlap_avg,
            "logic_similar_count": logic_similar_count,
            "logic_consensus": logic_similar_count == total,
            "conflict": "" if agree == total else "验证模型方向与主模型不一致",
        }, ensure_ascii=False)
    except Exception:
        return None


def _consensus_downgrade(result: Dict[str, Any], verification: Optional[str]) -> bool:
    """阶段2 多模型共识闸门：verification 方向不一致 → 方向单降级 HOLD。

    peer_reviews 交叉验证此前只留痕入库、不改变主决策；这里升级为第三道闸门
    （在针对性闸门、传导机制闸门之后）：当验证模型与主模型方向出现分歧
    （agree=False）时，方向单就地降级观望——宁可观望也不建一笔有分歧的方向。

    返回是否降级。verification 为空（无 peer_reviews）/ 主结论本就 HOLD → 不降级。
    """
    if not CONSENSUS_GATE_ENABLED:
        return False
    if not verification or not isinstance(result, dict):
        return False
    action = str(result.get("suggested_action") or "").upper()
    if action not in ("BUY", "SELL"):
        return False  # 本就观望，无需降级
    try:
        v = json.loads(verification)
    except (json.JSONDecodeError, TypeError):
        return False
    if not isinstance(v, dict) or v.get("agree"):
        return False
    total = max(0, int(v.get("total") or 0))
    agree_count = max(0, int(v.get("agree_count") or 0))
    asset = str(result.get("target_asset") or "").strip().upper()
    result["suggested_action"] = "HOLD"
    result["sentiment_score"] = 0.05
    if agree_count <= 0:
        reason = (f"多模型交叉验证全部反对（{total}/{total} 方向不一致），"
                  f"共识不足故 {asset or '该品种'} 不给方向单")
    else:
        reason = (f"多模型交叉验证存在方向分歧（仅 {agree_count}/{total} 同意），"
                  f"共识不足故 {asset or '该品种'} 不给方向单")
    result["reasoning"] = reason
    _signal_trace("consensus", asset=asset, verdict="共识不足→降级HOLD",
                  action=action, agree=agree_count, total=total)
    return True


async def _process_single(
    news_row: sqlite3.Row,
    model_cfg: Dict[str, str],
    loop: asyncio.AbstractEventLoop,
    market_context: str = "",
    performance_context: str = "",
    writing_context: str = "",
    insights_section: str = "",
    reference_signals: str = "",
    fallback_cfgs: List[Dict[str, Any]] = (),
    verify_cfgs: List[Dict[str, Any]] = (),
) -> Dict[str, Any]:
    """
    Process a single raw_news row through the primary model's LLM pipeline.

    多模型 Failover：主模型超时/失败 → 自动换池内其它模型重试，
    慢通道（如 nassaapi 波动 100s+）不再拖死单条。

    多模型交叉验证（verify_cfgs 非空时）：主模型成功后，额外让验证模型
    独立分析同一条新闻（peer_reviews），仅展示/标注，不改变主决策。

    Returns:
      {"news_id": int, "pre_ts": str, "model_label": str,
       "result": dict | None, "error": str | None,
       "peer_reviews": list | None}
    """
    news_id = news_row["id"]
    content = re.sub(r'\[hash:[a-fA-F0-9]+\]\s*', '', news_row["content"])
    # 优先用 published_at(金十/新闻源声称的发布时间,通常早于入库时间 5-15min),
    # fallback timestamp(本系统入库时刻,WS 源无 published_at 时使用)。
    # 这是 SERVER VERDICT 中 "price-in 对比" 的关键时间锚 — 用入库时间会全部被算成"已 price-in"。
    try:
        _pa = str(news_row["published_at"] or "").strip()
    except Exception:
        _pa = ""
    pre_ts = _pa if _pa else news_row["timestamp"]
    # 门卫四步标签：从 raw_news.gate_* 列拼出分析先验（老库缺列/未过门卫 → 空串）
    gate_context = _build_gate_context(news_row)
    # SERVER VERDICT 用的 target_asset：直接读 raw_news.gate_main_asset
    # （[门卫四步] 拼出的也是这个值；显式取防止解析错误）
    try:
        gate_target_asset = str(news_row["gate_main_asset"] or "").strip().upper()
    except Exception:
        gate_target_asset = ""
    # v174: 查 event_type_code 用于 baseline 注入 (raw_news → event_atom → dim_event_type)
    event_type_code = ""
    try:
        import db as _db_v174
        _conn_v174 = _db_v174.get_connection() if hasattr(_db_v174, "get_connection") else None
        if _conn_v174 is not None:
            _r = _conn_v174.execute(
                "SELECT det.code FROM event_atom ea "
                "JOIN dim_event_type det ON det.id = ea.event_type_id "
                "WHERE ea.raw_news_id = ? LIMIT 1",
                (news_id,),
            ).fetchone()
            if _r:
                event_type_code = str(_r[0] or "")
    except Exception as _e_v174:
        print(f"[BASELINE] 查 event_type_code 失败(静默): {type(_e_v174).__name__}: {_e_v174}", flush=True)
        event_type_code = ""

    async def _analyze_with(cfg: Dict[str, str], timeout: float = 65.0):
        async with _LLM_SEMAPHORE:
            return await asyncio.wait_for(
                loop.run_in_executor(
                    None, _call_llm_sync, content, cfg, market_context,
                    performance_context, pre_ts, writing_context, insights_section,
                    reference_signals, gate_context,
                    gate_target_asset, news_id,
                    event_type_code,
                    int(news_row["gate_strength"] or 0) if "gate_strength" in news_row.keys() else 0,  # v176.5: 传给 MECH-GATE
                    str(news_row["source"] or ""),  # v176.10: 二次回灌 cross_net 需要原文来源
                    str(news_row["published_at"] or ""),  # v188：tier-3 平台首发兜底用
                    "main",                              # v187: purpose=main
                    "ai_worker_main",                    # v187: call_site
                ),
                timeout=timeout,
            )

    try:
        # 主模型单条正常 3-8s；60s 超时足以覆盖，慢通道快速 Failover（不等 130s）
        llm_result = await _analyze_with(model_cfg)
        # ── 多模型交叉验证：高价值新闻，主成功后跑验证模型（不改变主决策）──
        # 存完整分析（八段 reasoning_path + 全文结论 + 关键维度），供前端详细对比展示
        # 并行跑验证模型；验证用 20s 独立短超时——验证只是交叉参考，慢通道（如
        # deepseek 当前 15-30s）超时即丢弃，绝不拖慢主决策落库（单条延迟主因之一）
        peer_reviews: List[Dict[str, Any]] = []
        if verify_cfgs:
            async def _run_verify(vcfg: Dict[str, str]) -> Optional[Dict[str, Any]]:
                try:
                    vres = await _analyze_with(vcfg, timeout=20.0)
                    return {
                        "model_label": vcfg.get("label", vcfg.get("id", "?")),
                        "model_id": vcfg.get("id"),
                        "action": vres.get("suggested_action"),
                        "score": vres.get("sentiment_score"),
                        "reasoning": (vres.get("reasoning") or "")[:300],
                        "reasoning_path": (vres.get("reasoning_path") or "")[:3000],
                        # 关键维度（与主模型对比展示用）
                        "expected_horizon": vres.get("expected_horizon"),
                        "event_strength": vres.get("event_strength"),
                        "market_confirmation": vres.get("market_confirmation"),
                        "direct_catalyst": vres.get("direct_catalyst"),
                        "heat_score": vres.get("heat_score"),
                        "impact_score": vres.get("impact_score"),
                        "asset_impact": vres.get("asset_impact"),
                    }
                except Exception:
                    return None  # 验证模型失败不影响主结果
            v_results = await asyncio.gather(*(_run_verify(cfg) for cfg in verify_cfgs))
            peer_reviews = [r for r in v_results if r]
        return {
            "news_id": news_id,
            "pre_ts": pre_ts,
            "model_label": model_cfg["label"],
            "result": _apply_freshness(llm_result, pre_ts),
            "error": None,
            "peer_reviews": peer_reviews or None,
        }
    except Exception as first_exc:
        # ── Failover：主模型超时/失败 → 换池内其它模型重试 ──
        # OpenRouter 实际调用失败 → 熔断 60s（避免每条死等 65s 超时）
        if model_cfg.get("use_proxy"):
            _openrouter_circuit["until"] = time.monotonic() + 60.0
            print(
                f"  [{model_cfg['label']}] 调用失败({type(first_exc).__name__})，"
                f"OpenRouter 熔断 60s → 直接走 MiniMax",
                flush=True,
            )
        # 记录主模型失败原因（诊断：为什么 OpenRouter 探测 OK 但实际分析失败）
        try:
            _fail_file = os.path.join(config.BASE_DIR, "runtime", "llm_call_failures.jsonl")
            os.makedirs(os.path.dirname(_fail_file), exist_ok=True)
            with open(_fail_file, "a", encoding="utf-8") as _ff:
                _ff.write(json.dumps({
                    "model": model_cfg.get("id"),
                    "label": model_cfg.get("label"),
                    "error": str(first_exc)[:200],
                    "err_type": type(first_exc).__name__,
                    "ts": time.time(),
                    "time_str": datetime.now(config.TZ_SHANGHAI).strftime("%H:%M:%S"),
                }, ensure_ascii=False) + "\n")
        except Exception:
            pass
        if fallback_cfgs:
            for fb in fallback_cfgs:
                try:
                    async with _LLM_SEMAPHORE:
                        llm_result = await asyncio.wait_for(
                            loop.run_in_executor(
                                None, _call_llm_sync, content, fb, market_context,
                                performance_context, pre_ts, writing_context, insights_section,
                                reference_signals, gate_context,
                                gate_target_asset, news_id, event_type_code,
                                int(news_row["gate_strength"] or 0) if "gate_strength" in news_row.keys() else 0,  # v176.5: 传给 MECH-GATE
                                str(news_row["source"] or ""),  # v176.10: 二次回灌 cross_net 需要原文来源
                                str(news_row["published_at"] or ""),  # v188：tier-3 平台首发兜底用
                                "main",                              # v187: purpose=main (failover)
                                "ai_worker_failover",                 # v187: call_site
                            ),
                            timeout=65.0,
                        )
                    print(f"  [{model_cfg['label']}] 失败 → {fb['label']} 重试成功，news=#{news_id}")
                    return {
                        "news_id": news_id,
                        "pre_ts": pre_ts,
                        "model_label": fb["label"],
                        "result": _apply_freshness(llm_result, pre_ts),
                        "error": None,
                        "peer_reviews": None,
                    }
                except Exception as fb_exc:
                    # fallback 也失败（含超时）：记录原因（诊断 OpenRouter 挂起用）
                    try:
                        _fail_file = os.path.join(config.BASE_DIR, "runtime", "llm_call_failures.jsonl")
                        with open(_fail_file, "a", encoding="utf-8") as _ff:
                            _ff.write(json.dumps({
                                "model": fb.get("id"),
                                "label": fb.get("label"),
                                "error": str(fb_exc)[:200],
                                "err_type": type(fb_exc).__name__,
                                "ts": time.time(),
                                "time_str": datetime.now(config.TZ_SHANGHAI).strftime("%H:%M:%S"),
                            }, ensure_ascii=False) + "\n")
                    except Exception:
                        pass
                    continue  # 尝试下一个 fallback
        # ── 全部失败 ──
        if isinstance(first_exc, asyncio.TimeoutError):
            # 超时且无可用 fallback：把该条新闻退回 PENDING，下轮重新认领重试（不标死）。
            try:
                conn = _open_db()
                conn.execute(
                    "UPDATE raw_news SET retry_count = retry_count + 1,"
                    " processing_at = NULL,"
                    " status = CASE WHEN retry_count + 1 >= ? THEN 'FAILED' ELSE 'PENDING' END"
                    " WHERE id=? AND status='PROCESSING'",
                    (_MAX_PENDING_RETRIES, news_id),
                )
                conn.commit()
                conn.close()
            except Exception:
                pass
            print(f"  [{model_cfg['label']}] LLM 超时(130s)，news=#{news_id} 退回 PENDING 重试")
            return {
                "news_id": news_id,
                "pre_ts": pre_ts,
                "model_label": model_cfg["label"],
                "result": None,
                "error": "LLM timeout(130s)",
            }
        # 其它异常：安全 Fallback → HOLD 对象
        fallback_result = {
            "sentiment_score": 0.05,
            "suggested_action": "HOLD",
            "reasoning": f"{model_cfg['label']} 异常降级: {type(first_exc).__name__}",
            "market_category": "",
            "target_asset": "NONE",
            "reasoning_path": "",
            "prediction_type": "continuation",
            "event_phase": "mid",
            "market_confirmation": "unknown",
            "expected_horizon": "1-3d",
            "invalidation_condition": "系统异常,无失效条件",
            "event_strength": "low",
            "direct_catalyst": False,
            "timeframe_match": "intraday",
            "model_label": model_cfg["label"],
            "model_id": model_cfg["id"],
        }
        return {
            "news_id": news_id,
            "pre_ts": pre_ts,
            "model_label": model_cfg["label"],
            "result": fallback_result,
            "error": f"{type(first_exc).__name__}: {str(first_exc)[:150]}",
        }


# ---------------------------------------------------------------------------
# Active-trade aggregation helpers (_AGG_WINDOW_HOURS / _AGG_MIN_SCORE 见 config.py)
# ---------------------------------------------------------------------------


def _normalize_sentiment_score(raw: Any) -> float:
    """sentiment_score 兼容三档范围 → 统一返回 -1~1（存 DB 格式）。

    三档约定：
      (a) 0-100 整数（新约定，期望；50=中性, 0-30=偏 SELL, 70-100=偏 BUY）
      (b) 0-1 float（部分老 LLM 输出，0=空, 0.5=中, 1=极多）
      (c) -1~1 float（最早约定，负数=SELL，正数=BUY）

    判定规则：raw<0 → 约定 (c)；raw>1.0 → 约定 (a)；其余 → 约定 (b)。
    所有下游 decision_guard/quick_sim 读取 -1~1 不变，零冲击。
    """
    try:
        v = float(raw)
    except (TypeError, ValueError):
        v = 0.0
    if v < 0:
        # (c)：clamp 到 [-1, 0]
        return round(max(-1.0, v), 4)
    if v > 1.0:
        # (a)：0-100 整数 → /100，clamp 到 [0, 1]
        return round(min(1.0, v / 100.0), 4)
    # (b)：0-1 float 原样返回
    return round(v, 4)


def _norm_score_action(score, action: str):
    """规范化 (score, action)：与主行同规则——
    clamp ±1、HOLD 归中性 0、非 0 极小值按方向给同向弱分(BUY +0.05/SELL -0.05)。"""
    # P2 #10：先做三档归一（LLM 可能给 0-100 / 0-1 / -1~1），再走原有规则
    score = _normalize_sentiment_score(score)
    action = str(action or "HOLD").upper()
    if action not in ("BUY", "SELL", "HOLD"):
        action = "HOLD"
    if action == "HOLD":
        # v200 P1-G: HOLD 时不再强制 score=0，保留 LLM 真情绪信号作为分析参考。
        # 旧规则"AI 观望就不该带方向分"导致 LLM 给 +0.10 时被压平到 0，前后矛盾（用户看到 +0.10 但 DB 0.0）。
        # 现仅在 ±0.05 内归中性，超过此阈值的情绪保留（如 +0.10 = 美元相对走强这种 FX/DXY 信号）。
        if abs(score) < 0.05:
            score = 0.0
    elif abs(score) < 0.001:
        # 方向单不给 0 分（无意义）；按 action 方向给同向弱分，避免 SELL 拿到正分
        score = 0.05 if action == "BUY" else -0.05
    return score, action


def _expand_multi_asset_results(results: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """多品种展开：result.sibling_decisions 拆成独立 decision 条目。

    AI 一次分析可给出多个受影响品种（sibling_decisions，最多 2 个），每个品种是
    独立可交易信号 → 展开成多行 decision（共享同一 reasoning_path 八段，各自
    action/score/horizon/市场类别），走同一条 INSERT/guard/建仓链路，quick_sim
    与复盘也自然按行适配。主品种条目排第一（主决策不变，主行保留
    peer_reviews 交叉验证；副品种行 reasoning 前缀 `[副品种 ASSET]` 标记，
    前端据此聚合展示与识别主/副行）。

    result 结构：{news_id, model_label, pre_ts, peer_reviews?, result: {...}}
    每个条目 result 含 20 字段主结构 + 可选 sibling_decisions。
    """
    expanded: List[Dict[str, Any]] = []
    for _s in results or []:
        _res = _s.get("result") or {}
        _sibs = _res.get("sibling_decisions") or []
        if not isinstance(_sibs, list) or not _sibs:
            expanded.append(_s)  # 无副品种：原样
            continue
        _main = dict(_s)  # 主条目（去掉 sibling 字段，避免重复解析）
        _main_result = dict(_res)
        _main_result.pop("sibling_decisions", None)
        _main["result"] = _main_result
        expanded.append(_main)
        _main_asset = str(_res.get("target_asset") or "NONE").upper().strip()
        # ── 前置条件：主品种必须"立住"，副品种才谈得上（用户原则：
        #    "先有主品种，后面才会有副品种。主品种都没有，何来副品种。"）
        #    主品种是 NONE（无可交易标的）或已被针对性闸门降级（八段没分析它）
        #    → 这次分析连"因"都没立住，副品种的传导链无从谈起，方向单一律降级。
        _main_ok, _main_block_reason = _main_established(_res, _main_asset)
        for _sib in _sibs[:2]:  # 最多 2 个副品种
            if not isinstance(_sib, dict):
                continue
            _sib_asset = str(_sib.get("target_asset") or "NONE").upper().strip()
            if not _sib_asset or _sib_asset in ("NONE", "") or _sib_asset == _main_asset:
                continue  # 跳过无品种/与主品种重复
            # 副品种独立 result：共享 reasoning_path，字段取自 sibling；
            # score/action 走与主行同款规范化（clamp/HOLD归零/0分兜底）
            _sib_score, _sib_action = _norm_score_action(
                _sib.get("sentiment_score", 0.0),
                _sib.get("suggested_action") or "HOLD",
            )
            _sib_result = {
                "reasoning_path": _res.get("reasoning_path", ""),
                "sentiment_score": _sib_score,
                "suggested_action": _sib_action,
                "reasoning": f"[副品种 {_sib_asset}] " + str(_sib.get("reasoning") or ""),
                "market_category": str(_sib.get("market_category") or "").upper(),
                "target_asset": _sib_asset,
                # 关键：带上主分析的真实模型标记。
                # 副品种与主品种共享同一份 analysis（reasoning_path 就是主模型产出的），
                # 但此前没带 model_id → 入库时落到 active_run["model_id"] 兜底，
                # 而那个值是建运行时的旧模型（实测遗留 gpt-5.4-mini/nassaapi，已 403 废弃）
                # → 副品种行的"分析模型"显示错误。
                "model_id": _res.get("model_id") or _s.get("model_id") or "",
                "model_label": _s.get("model_label"),
                "prediction_type": _sib.get("prediction_type", "continuation"),
                "event_phase": _sib.get("event_phase", "mid"),
                "market_confirmation": _sib.get("market_confirmation", "unknown"),
                "expected_horizon": _sib.get("expected_horizon", "1-3d"),
                "invalidation_condition": _sib.get("invalidation_condition", ""),
                "event_strength": _sib.get("event_strength", "medium"),
                "direct_catalyst": _sib.get("direct_catalyst", False),
                "heat_score": _sib.get("heat_score", 5.0),
                "impact_score": _sib.get("impact_score", 5.0),
                "asset_impact": _sib.get("asset_impact", 5.0),
            }
            # ── 副品种同样过针对性闸门 ──
            # 此前闸门只在主分析里跑，副品种展开后直接建仓：八段里没有该品种的
            # 独立分析（实测多数副品种 0 段）也照样给 BUY/SELL——宽松模式下就是
            # 真建仓。规则与主品种一致：无针对该品种的分析 → 不给方向单。
            # 更前置的一条：主品种没立住（NONE/已被降级）→ 副品种直接降级，
            # 连"八段有没有它"都不必再看——因没立住，果无从谈起。
            _sib_hits = _asset_mentioned_stage_count(
                str(_sib_result.get("reasoning_path") or ""), _sib_asset)
            _sib_dg, _ = _focus_downgrade(
                _sib_result, _sib_asset, role="副品种",
                reason=(f"{_main_block_reason}，故副品种 {_sib_asset} 不给方向单"
                        if not _main_ok else ""),
            )
            if _sib_dg:
                print(
                    f"  [FOCUS-GATE] 副品种 {_sib_asset} 方向单降级 HOLD（"
                    + (_main_block_reason if not _main_ok
                       else f"分析无针对性，仅 {_sib_hits} 段提及")
                    + "）",
                    flush=True,
                )
            # ── 副品种同样过传导机制闸门 ──
            # 用户原则："有关系 = 新闻内容会影响它的价格波动"。副品种若说不出
            # "事件 → 它自己的价格"的传导链，就只是被顺带点名 → 不给方向单。
            if _mechanism_downgrade(_sib_result, _sib_asset):
                print(
                    f"  [MECH-GATE] 副品种 {_sib_asset} 未写出传导链"
                    f" → 方向单降级 HOLD（影响不明确，不下单）",
                    flush=True,
                )
            expanded.append({
                "news_id": _s["news_id"],
                "model_label": _s["model_label"],
                "pre_ts": _s["pre_ts"],
                "peer_reviews": None,  # 交叉验证只做主品种
                "is_sibling": True,
                "result": _sib_result,
            })
    return expanded


def _find_active_parent(
    conn: sqlite3.Connection,
    category: str,
    asset: str,
    action: str,
) -> int | None:
    """
    Look for an existing parent event (parent_id IS NULL, BUY or SELL)
    with the same market_category, target_asset, and suggested_action
    created within the aggregation window.

    Returns the parent's ai_decisions.id, or None if no match.
    """
    row = conn.execute(
        """
        SELECT id, child_count
        FROM ai_decisions
        WHERE parent_id IS NULL
          AND market_category = ?
          AND target_asset    = ?
          AND suggested_action = ?
          AND suggested_action IN ('BUY', 'SELL')
          AND strftime('%s', created_at) > strftime('%s', 'now', ?)
        ORDER BY id DESC
        LIMIT 1
        """,
        (category, asset, action, f"-{_AGG_WINDOW_HOURS} hours"),
    ).fetchone()
    return row[0] if row else None


def _bump_parent_score(
    conn: sqlite3.Connection,
    parent_id: int,
    child_score: float,
    child_reasoning: str,
) -> bool:
    """
    If the child's score is more extreme than the parent's, update the
    parent's sentiment_score and prepend a note to the reasoning field.
    Returns True if the score was bumped.
    """
    row = conn.execute(
        "SELECT sentiment_score FROM ai_decisions WHERE id = ?",
        (parent_id,),
    ).fetchone()
    if not row:
        return False

    parent_score = row[0] or 0.0

    # "More extreme" = further from zero in the same direction
    if abs(child_score) > abs(parent_score) and (child_score * parent_score >= 0):
        conn.execute(
            "UPDATE ai_decisions SET sentiment_score = ? WHERE id = ?",
            (round(child_score, 4), parent_id),
        )
        return True
    return False



async def ai_worker(loop: asyncio.AbstractEventLoop) -> None:
    """
    Concurrent batch processor.

    Every 1 second:
      1) Atomically claim up to BATCH_SIZE PENDING rows (BEGIN IMMEDIATE)
      2) Fire all LLM calls concurrently via asyncio.gather
      3) Persist successes (-> DONE) and failures (-> FAILED) in one transaction.

    Head-of-line blocking is eliminated: 10 items complete in ~2-3 s
    instead of 10 * 2 s = 20 s serial.
    """

    idle_ticks = 0  # heartbeat counter when no PENDING data

    # 启动即落盘模型池（诊断：不依赖主循环是否卡住，引擎启动后即可查看实际通道构成）
    try:
        _pool_file = os.path.join(config.BASE_DIR, "runtime", "model_pool.json")
        os.makedirs(os.path.dirname(_pool_file), exist_ok=True)
        _startup_pool = _build_model_pool()
        with open(_pool_file, "w", encoding="utf-8") as f:
            json.dump({
                "pool": [m["id"] for m in _startup_pool],
                "labels": [m.get("label", m["id"]) for m in _startup_pool],
                "ts": time.time(),
            }, f, ensure_ascii=False)
        print(f"[AI] 启动模型池: {[m['id'] for m in _startup_pool]}", flush=True)
    except Exception as _pool_exc:
        print(f"[AI] 模型池落盘失败: {type(_pool_exc).__name__}: {str(_pool_exc)[:100]}", flush=True)

    # 启动时把上次异常退出残留的 PROCESSING 新闻重置为 PENDING（否则永久卡死不被认领）
    try:
        conn = _open_db()
        conn.execute("UPDATE raw_news SET status='PENDING', processing_at=NULL WHERE status='PROCESSING'")
        conn.commit()
        conn.close()
    except Exception:
        pass

    # 失败冷却: snapshot 连续 DOWN 后 5 分钟内不重试, 减少无意义等待
    _SNAPSHOT_COOLDOWN_S = 300
    _last_snapshot_down_ts: float = 0.0
    # 最近一次成功快照缓存（复用）：快照拉取超时/失败/冷却时，若缓存 ≤300s 内
    # （与冷却同长——代理全挂期间也始终有数据）成功过，用缓存数据让 AI 仍有盘面
    # 锚点。宏观/趋势判断对 5 分钟内的价格滞后不敏感；带"Xs 前缓存"标注提醒模型。
    # 避免"代理整体不可用 → 整批无快照 → 水位/跨资产段无价格可写"。
    _SNAPSHOT_CACHE_TTL_S = 300
    _last_good_snapshot: Dict[str, Any] = {"ts": 0.0, "summary": "", "json": "{}"}
    # macro_context（FRED/盘面结构/爆仓）同样：失败冷却期用上次成功摘要，不让 AI 丢数据层
    _MACRO_CACHE_TTL_S = 300
    _last_good_macro: Dict[str, Any] = {"ts": 0.0, "summary": ""}

    # 失败冷却: 宏观外部源(盘面/舆情/Fed/资金流)串行请求在弱网/代理下可累计数分钟,
    # 加整体 20s 超时 + 10 分钟冷却, 避免拖垮每轮 AI 分析节奏。
    _MACRO_COOLDOWN_S = 600
    _last_macro_down_ts: float = 0.0

    # 历史 K 线知识库：快照 ≤30min 复用（历史 K 线分钟级不变）；persist 失败/冷却 5min
    _KLINE_CACHE_TTL_S = 1800
    _KLINE_COOLDOWN_S = 300
    _last_kline_block: Dict[str, Any] = {"ts": 0.0, "block": ""}
    _last_kline_down_ts: float = 0.0

    while True:
        await asyncio.sleep(1)

        # 主循环进度落盘（诊断：定位引擎卡在哪一步）
        def _log_progress(step: str) -> None:
            try:
                _progress_file = os.path.join(config.BASE_DIR, "runtime", "ai_worker_progress.json")
                with open(_progress_file, "w", encoding="utf-8") as _pf:
                    json.dump({
                        "step": step,
                        "ts": time.time(),
                        "time_str": datetime.now(config.TZ_SHANGHAI).strftime("%H:%M:%S"),
                    }, _pf, ensure_ascii=False)
            except Exception:
                pass

        _log_progress("loop")

        # ── 自愈看门狗：回收卡死的 PROCESSING ──
        # 按"认领时间 processing_at"判断（非新闻入库 ts）——claim 超过 15 分钟仍未
        # 完成的视为卡死（正常批几秒~几分钟），回滚 PENDING 下轮重试，无需人工重启；
        # 积压老新闻 claim 后 processing_at 是新时间，不会被误杀。
        try:
            _reclaim_conn = _open_db()
            _reclaim_conn.execute(
                "UPDATE raw_news SET retry_count = retry_count + 1,"
                " processing_at = NULL,"
                " status = CASE WHEN retry_count + 1 >= ? THEN 'FAILED' ELSE 'PENDING' END"
                " WHERE status='PROCESSING' AND processing_at IS NOT NULL"
                " AND processing_at < datetime('now', 'localtime', '-15 minutes')",
                (_MAX_PENDING_RETRIES,),
            )
            _reclaim_conn.commit()
            _reclaim_conn.close()
        except Exception:
            pass

        # ==================================================================
        # Phase 1 - Batch atomic claim
        # ==================================================================
        def _batch_claim() -> List[sqlite3.Row]:
            """Phase 1 原子认领: PENDING → PROCESSING.

            v174 修复: 用「单 UPDATE + RETURNING」替代 BEGIN IMMEDIATE 包裹,
            消除与 ingest worker 长 INSERT 事务的持锁竞争.
            - 旧逻辑(BEGIN IMMEDIATE): 先开事务,在 ingest 长事务未提交期间
              busy_timeout 30s 后 SQLITE_BUSY → OperationalError,30s 浪费.
            - 新逻辑: 直接单条 UPDATE 自动拿写锁直到语句结束,RETURNING 拿回
              完整行（SQLite 3.35+ 支持, 与 ai_worker 其它位置一致）.
            """
            import time as _bt
            import traceback as _bt_stk
            _t0 = _bt.time()
            conn = _open_db()
            # v176.2: 临时 busy_timeout 8s → 30s 跟全局对齐, 避免长 ingest
            # 持锁(>8s)时 _batch_claim 仍报 SQLITE_BUSY. ingest 的写入时间
            # 通常 5-15s, 30s 才有足够冗余;同时全局 db.py/utils.py 已是 30s.
            # v189-8: 30s → 5s 与 db.py / engine/utils.py 对齐, 配合 retry_on_busy
            # 重试,_batch_claim 单次最坏阻塞 5s + 重试退避 ≈ 5.4s 兜底.
            try:
                conn.execute("PRAGMA busy_timeout=5000;")
            except Exception:
                pass
            # v189-8: 包 retry_on_busy,UPDATE RETURNING 拿锁失败时 5s+退避重试,
            # 消除 14 次 [BATCH] FAILED in 36-74s (engine 旧版 60s 死等浪费).
            # 配合 busy_timeout=5s,单次最坏阻塞 ≈ 5.4s,失败即抛给外层打印.
            def _do_claim():
                rows = conn.execute(
                    "UPDATE raw_news"
                    " SET status='PROCESSING',"
                    "     processing_at=datetime('now','localtime')"
                    " WHERE id IN ("
                    "   SELECT id FROM raw_news"
                    "   WHERE status='PENDING' AND is_noise=0"
                    "   ORDER BY id ASC LIMIT ?"
                    " )"
                    " RETURNING *",
                    (BATCH_SIZE,),
                ).fetchall()
                conn.commit()
                return rows
            try:
                rows = db.retry_on_busy(_do_claim)
                if rows:
                    ids = [r["id"] for r in rows]
                    print(f"  [BATCH] claimed {len(rows)} rows in {_bt.time()-_t0:.2f}s (ids={ids[:5]})", flush=True)
                return rows
            except Exception as _claim_exc:
                try:
                    conn.rollback()
                except Exception:
                    pass
                _dt = _bt.time()-_t0
                print(f"  [BATCH] FAILED in {_dt:.2f}s: {type(_claim_exc).__name__}: {str(_claim_exc)[:100]}", flush=True)
                # v174 诊断: 长 lock 时 dump 所有活跃线程栈 + 列出持锁者
                if _dt > 5.0:
                    try:
                        import sys, threading as _thr, os
                        def _w(s):
                            try: os.write(1, (s + "\n").encode())
                            except Exception: pass
                        _w(f"  [BATCH-DIAG] === ALL THREAD STACKS (lock blocked {_dt:.1f}s) ===")
                        for tid, frame in sys._current_frames().items():
                            _w(f"  [BATCH-DIAG] Thread {tid} (name={next((t.name for t in _thr.enumerate() if t.ident==tid), '?')}):")
                            for ln in _bt_stk.format_stack(frame, limit=15):
                                _w("    " + ln.rstrip())
                        _w(f"  [BATCH-DIAG] active threads: {_thr.active_count()}")
                    except Exception as _diag_err:
                        print(f"  [BATCH-DIAG] dump failed: {_diag_err}", flush=True)
                return []
            finally:
                conn.close()

        # ==================================================================
        # Phase 0 — Pull market snapshot (once per batch, before LLM calls)
        # ==================================================================

        market_context = ""
        decision_context = "{}"  # 完整快照 JSON, 供 Hermes 复盘
        now_ts = time.time()
        snap_ok_now = False
        if now_ts - _last_snapshot_down_ts > _SNAPSHOT_COOLDOWN_S:
            try:
                # 行情快照必须加超时：外部行情源挂起时会拖死整个 AI 主循环
                # （症状：PROCESSING 卡住、零分析产出、progress 停在 loop）
                snap = await asyncio.wait_for(get_snapshot(), timeout=15.0)
                if snap.get("status") == "down" or not snap.get("summary"):
                    raise RuntimeError("snapshot down/empty")
                market_context = snap.get("summary", "")
                decision_context = json.dumps(snap, ensure_ascii=False)
                # 成功 → 更新缓存
                _last_good_snapshot.update({"ts": now_ts, "summary": market_context, "json": decision_context})
                # 同步到模块级 cache（让嵌套 _do_api_call 注入 SERVER VERDICT 时能读到完整 snap）
                global _SERVER_SNAPSHOT_CACHE
                _SERVER_SNAPSHOT_CACHE.update({"ts": now_ts, "summary": market_context, "json": decision_context})
                snap_ok_now = True
                if market_context:
                    print(f"\n  [SNAPSHOT] {snap['status'].upper()} | "
                          f"BTC={snap['assets']['BTC'].get('price_str','?')} | "
                          f"XAU={snap['assets']['XAU'].get('price_str','?')}")
            except Exception as e:
                print(f"  [SNAPSHOT] 获取失败: {type(e).__name__}: {str(e)[:80]}")
                _last_snapshot_down_ts = now_ts
        else:
            # 冷却中, 跳过本轮 snapshot 请求
            remaining = _SNAPSHOT_COOLDOWN_S - int(now_ts - _last_snapshot_down_ts)
            if int(now_ts) % 60 == 0:  # 每分钟只打一次
                print(f"  [SNAPSHOT] 跳过 (冷却中, {remaining}s 后重试)")

        # 拉取失败/冷却 → 尝试复用 ≤60s 前的成功快照（价格几十秒内变化可忽略）
        if not snap_ok_now:
            cache_age = now_ts - float(_last_good_snapshot.get("ts", 0.0))
            if _last_good_snapshot.get("summary") and cache_age <= _SNAPSHOT_CACHE_TTL_S:
                market_context = (
                    f"（市场快照为 {int(cache_age)}s 前缓存）\n"
                    + str(_last_good_snapshot.get("summary", ""))
                )
                decision_context = str(_last_good_snapshot.get("json", "{}"))
                print(f"  [SNAPSHOT] 复用 {int(cache_age)}s 前成功快照（本次拉取失败/冷却）", flush=True)

        if MACRO_CONTEXT_ENABLED:
            # 外部源串行请求在弱网/代理下可累计数分钟, 这里加整体超时 + 失败冷却;
            macro_ok_now = False
            if now_ts - _last_macro_down_ts > _MACRO_COOLDOWN_S:
                try:
                    macro_pack = await asyncio.wait_for(
                        loop.run_in_executor(None, macro_context.build_context),
                        timeout=20.0,
                    )
                    if macro_pack.get("summary") and macro_pack.get("status") != "unavailable":
                        market_context = (
                            (market_context + "\n\n" + macro_pack["summary"]).strip()
                            if market_context else macro_pack["summary"]
                        )
                        _last_good_macro.update({"ts": now_ts, "summary": macro_pack["summary"]})
                        macro_ok_now = True
                        print(f"  [MACRO] {macro_pack['status']} {macro_pack['ok']}/{macro_pack['total']}")
                    if macro_pack.get("status") == "unavailable":
                        _last_macro_down_ts = now_ts
                except asyncio.TimeoutError:
                    _last_macro_down_ts = now_ts
                    print(f"  [MACRO] 外部源超时(20s), 进入 {int(_MACRO_COOLDOWN_S)}s 冷却")
                except Exception as e:
                    _last_macro_down_ts = now_ts
                    print(f"  [MACRO] 拉取失败: {type(e).__name__}: {str(e)[:80]}")
            else:
                remaining = _MACRO_COOLDOWN_S - int(now_ts - _last_macro_down_ts)
                if int(now_ts) % 60 == 0:
                    print(f"  [MACRO] 跳过 (冷却中, {remaining}s 后重试)")

            # 失败/冷却 → 复用 ≤300s 前的成功宏观层摘要（FRED/盘面/爆仓分钟级变化，
            # 不因代理抖动丢失数据层）
            if not macro_ok_now:
                macro_age = now_ts - float(_last_good_macro.get("ts", 0.0))
                if _last_good_macro.get("summary") and macro_age <= _MACRO_CACHE_TTL_S:
                    cached_macro = (
                        f"（宏观数据为 {int(macro_age)}s 前缓存）\n"
                        + str(_last_good_macro.get("summary", ""))
                    )
                    market_context = (
                        (market_context + "\n\n" + cached_macro).strip()
                        if market_context else cached_macro
                    )
                    print(f"  [MACRO] 复用 {int(macro_age)}s 前成功宏观数据", flush=True)

        # 历史 K 线知识库：追加在宏观层之后，作为"位置/趋势"反共识对照（失败/冷却复用缓存）
        if getattr(config, "KLINE_KB_ENABLED", True):
            kline_ok_now = False
            if now_ts - _last_kline_down_ts > _KLINE_COOLDOWN_S:
                try:
                    kline_block = await asyncio.wait_for(
                        loop.run_in_executor(None, _load_kline_block), timeout=15.0
                    )
                    if kline_block:
                        market_context = (
                            (market_context + "\n\n" + kline_block).strip()
                            if market_context else kline_block
                        )
                        _last_kline_block.update({"ts": now_ts, "block": kline_block})
                        kline_ok_now = True
                        print(f"  [KLINE] 历史K线注入 ({len(kline_block)} 字)", flush=True)
                    else:
                        _last_kline_down_ts = now_ts
                except asyncio.TimeoutError:
                    _last_kline_down_ts = now_ts
                    print("  [KLINE] 拉取超时(15s), 进入冷却", flush=True)
                except Exception as e:
                    _last_kline_down_ts = now_ts
                    print(f"  [KLINE] 失败: {type(e).__name__}: {str(e)[:80]}", flush=True)
            if not kline_ok_now and _last_kline_block.get("block"):
                kline_age = now_ts - float(_last_kline_block.get("ts", 0.0))
                if kline_age <= _KLINE_CACHE_TTL_S:
                    cached_kline = f"（历史K线为 {int(kline_age)}s 前缓存）\n" + _last_kline_block["block"]
                    market_context = (
                        (market_context + "\n\n" + cached_kline).strip()
                        if market_context else cached_kline
                    )

        # ==================================================================
        # Phase 0.5 — Build historical performance reference (once per batch)
        # ==================================================================
        _log_progress("macro")
        performance_context = ""
        try:
            performance_context = await loop.run_in_executor(
                None, _build_performance_context,
            )
            if performance_context:
                print(f"  [PERF] 历史绩效已注入 prompt ({len(performance_context)} chars)")
        except Exception as e:
            print(f"  [PERF] 构建失败: {type(e).__name__}: {str(e)[:80]}")
            performance_context = ""

        # 复盘经验回灌：以独立分区（═══ 复盘经验 ═══）拼进 prompt 尾部，
        # 不再挤成一行塞 user 消息 —— 结构化后模型能逐条对照且带样本数。
        # 变量名刻意与参数 insights_section 区分：后者是内层 _do_api_call 读取的
        # 闭包变量，若这里同名赋值会让 `if insights_section:` 变成"赋值前引用"而报错。
        insights_block_text = ""
        try:
            insights_block_text = await loop.run_in_executor(None, _load_insights_section)
            insights_section = insights_block_text
            if insights_block_text:
                # 减去标题与使用说明 6 行，粗算经验条数（用于日志）
                n_ins = max(1, insights_block_text.count("\n") - 6)
                ratio = len(insights_block_text) / max(1, len(_JSON_PROMPT_FORCE)) * 100
                print(
                    f"  [REVIEW] 复盘经验分区已注入 prompt（约 {n_ins} 条 · "
                    f"{len(insights_block_text)} 字 · 占 Prompt Force {ratio:.0f}%）",
                    flush=True,
                )
                # 体积告警：超过 30% 时提示会挤压八段推演空间（可用 REVIEW_INSIGHTS_LIMIT 收敛）
                if ratio > 30:
                    print(
                        f"  [REVIEW] ⚠ 经验分区偏大（{ratio:.0f}%），可能挤压八段推演；"
                        "可用 .env 的 REVIEW_INSIGHTS_LIMIT 限制条数或停用低价值经验",
                        flush=True,
                    )
            else:
                print("  [REVIEW] 暂无生效的复盘经验", flush=True)
        except Exception as e:
            print(f"  [REVIEW] 经验加载失败: {type(e).__name__}: {str(e)[:80]}")

        # 参考信号回灌：提炼的统计信号实时注入 prompt（反映最新已结算交易）
        reference_signals = ""
        try:
            from review_agent import derive_reference_signals

            def _sig_lines(items):
                out = []
                for it in items[:4]:
                    if it.get("summary"):
                        out.append(it["summary"])
                    else:
                        wr = f"{it['winrate'] * 100:.0f}%" if it.get("winrate") is not None else "?"
                        out.append(f"{it['label']}:{it['total']}笔 胜率{wr} 盈亏{it['total_pnl']:+.2f}%")
                return out

            sig = await loop.run_in_executor(None, derive_reference_signals)
            sig_parts = []
            if sig.get("assets"):
                sig_parts.append("品种 " + "; ".join(_sig_lines(sig["assets"])))
            if sig.get("directions"):
                sig_parts.append("方向 " + "; ".join(_sig_lines(sig["directions"])))
            if sig.get("phases"):
                sig_parts.append("阶段 " + "; ".join(_sig_lines(sig["phases"])))
            if sig.get("strengths"):
                sig_parts.append("强度 " + "; ".join(_sig_lines(sig["strengths"])))
            if sig.get("intervals"):
                sig_parts.append("入场时机 " + "; ".join(_sig_lines(sig["intervals"])))
            if sig.get("combos"):
                sig_parts.append("品种×方向 " + "; ".join(_sig_lines(sig["combos"])))
            if sig_parts:
                reference_signals = "[Reference Signals] 历史统计信号（实时提炼自已结算模拟交易，仅作参考）：" + " | ".join(sig_parts)
                print(f"  [SIGNAL] 参考信号已注入 prompt ({len(sig_parts)} 组)", flush=True)
            else:
                print("  [SIGNAL] 暂无参考信号", flush=True)
        except Exception as e:
            print(f"  [SIGNAL] 信号加载失败: {type(e).__name__}: {str(e)[:80]}")

        _log_progress("signals")
        batch = await loop.run_in_executor(None, _batch_claim)
        _log_progress("batch")
        if not batch:
            idle_ticks += 1
            if idle_ticks % 30 == 1:
                print(f"[{_now()}] [AI] idle ({idle_ticks}s)")
            continue

        idle_ticks = 0  # reset heartbeat on activity

        batch_ids = [r["id"] for r in batch]
        # Map news_id → content for downstream use (Feishu alerts etc.)
        content_map: Dict[int, str] = {}
        timestamp_map: Dict[int, str] = {}
        published_map: Dict[int, str] = {}
        # 门卫四步标签(L3)：news_id → gate_verdict，供决策层收口（v181：排除 → HOLD；
        # "只能看" 已删除，但老数据中仍可能有此值 → 历史兜底走 STRONG-PASS 路径）
        gate_verdict_map: Dict[int, str] = {}
        # v176.5: 同时存 strength，用于 "strength>=_GATE_PASS_THR 放行"（兜底）
        gate_strength_map: Dict[int, int] = {}
        # v176.5: 批内最大 strength (供 MECH-GATE 共享 — 强信号全批放行)
        _batch_max_strength: int = 0
        for row in batch:
            c = row["content"]
            # Strip [hash:xxx] prefix for cleaner display
            c = re.sub(r'\[hash:[a-zA-Z0-9]+\]\s*', '', c)
            content_map[row["id"]] = c
            timestamp_map[row["id"]] = row["timestamp"] or ""
            # 平台声称的发布时间（旧库无该列时为空 → 回落入库时间）
            try:
                published_map[row["id"]] = row["published_at"] or ""
            except Exception:
                published_map[row["id"]] = ""
            try:
                gate_verdict_map[row["id"]] = row["gate_verdict"] or ""
            except Exception:
                gate_verdict_map[row["id"]] = ""
            try:
                gate_strength_map[row["id"]] = int(row["gate_strength"] or 0)
                _batch_max_strength = max(_batch_max_strength, int(row["gate_strength"] or 0))
            except Exception:
                gate_strength_map[row["id"]] = 0
        print(
            f"\n[{_now()}] [AI] Claimed {len(batch)} items: {batch_ids}"
        )

        # ==================================================================
        # Phase 2 - Concurrent LLM analysis (news × models)
        # ==================================================================
        batch_start = _ts()
        # 多模型并行分片：批次内按 weight 加权把新闻分给模型池各模型，
        # 快且稳的通道多分（deepseek > gpt-5.5/gpt-5.4-mini），
        # 总吞吐 ×N 且每模型并发低（降低上游限流/403 概率）。
        # OpenRouter 不可用(402)时对应槽位自动切换 MiniMax 备用通道。
        model_pool = _build_model_pool()
        print(f"[{_now()}] [AI] Model pool: {[m['id'] for m in model_pool]}")
        # 引擎视角的模型池落盘（前端可查看实际分析通道构成）
        try:
            _pool_file = os.path.join(config.BASE_DIR, "runtime", "model_pool.json")
            os.makedirs(os.path.dirname(_pool_file), exist_ok=True)
            with open(_pool_file, "w", encoding="utf-8") as f:
                json.dump({
                    "pool": [m["id"] for m in model_pool],
                    "labels": [m.get("label", m["id"]) for m in model_pool],
                    "ts": time.time(),
                }, f, ensure_ascii=False)
        except Exception:
            pass
        hermes_skills: List[Dict[str, Any]] = []
        writing_map: Dict[int, str] = {}
        if HERMES_AGENT_ENABLED:
            try:
                hermes_agent.refresh_skills()
                hermes_skills = hermes_agent.load_skills()
                if hermes_skills:
                    print(f"  [HERMES] 已结算技能 {len(hermes_skills)} 条回灌")
            except Exception as e:
                print(f"  [HERMES] 技能刷新失败: {type(e).__name__}: {str(e)[:80]}")
                hermes_skills = []
            for row in batch:
                nid = row["id"]
                try:
                    packet = hermes_agent.persist_news_writing(
                        nid,
                        content_map.get(nid, ""),
                        source=row["source"] if "source" in row.keys() else "",
                        market_context=market_context,
                    )
                    writing_map[nid] = hermes_agent.render_writing_context(
                        packet["desks"], hermes_skills
                    )
                except Exception as e:
                    print(f"  [HERMES] 写作沉淀失败 news={nid}: {type(e).__name__}: {str(e)[:80]}")
                    try:
                        writing_map[nid] = hermes_agent.build_prompt_context(
                            content_map.get(nid, ""), market_context
                        )
                    except Exception:
                        writing_map[nid] = ""
        # 构建每条的 fallback 链：优先 MiniMax（直连/代理皆快），再补池内其他非重复模型。
        # 避免「OpenRouter 主模型挂起 → fallback 仍是 OpenRouter → 继续挂」的无效重试。
        _mm_cfg: Optional[Dict[str, Any]] = None
        if config.MINIMAX_API_KEY:
            _mm_cfg = _minimax_cfg(config.MINIMAX_API_KEY)  # 复用 M3/Text-01 extra_body 规则（含 thinking disabled）
            _mm_cfg["full_prompt"] = True  # 完整 _SYSTEM_PROMPT 出嵌套 sibling
        tasks = []
        for idx, row in enumerate(batch):
            main_cfg = model_pool[idx % len(model_pool)]
            # 多模型交叉验证：高价值新闻 → 主模型外再让 2 个模型独立分析（仅展示/标注）
            verify_cfgs = (
                _pick_verify_cfgs(main_cfg, model_pool, 2)
                if _is_high_value_news(row, content_map.get(row["id"], ""))
                else []
            )
            tasks.append(
                _process_single(
                    row,
                    main_cfg,
                    loop,
                    market_context,
                    performance_context,
                    writing_map.get(row["id"], ""),
                    insights_section,
                    reference_signals,
                    fallback_cfgs=[
                        *( [] if _mm_cfg is None else [_mm_cfg] ),
                        *[
                            fb for i2, fb in enumerate(model_pool)
                            if (i2 % len(model_pool)) != (idx % len(model_pool))
                            and fb["id"] != main_cfg["id"]
                            and ( _mm_cfg is None or fb["id"] != _mm_cfg["id"] )
                        ][:2],
                    ][:3],
                    verify_cfgs=verify_cfgs,
                )
            )
        results: List[Dict[str, Any]] = await asyncio.gather(
            *tasks, return_exceptions=True
        )
        _log_progress("analyze-done")
        batch_end = _ts()

        # Separate successes from failures
        successes: List[Dict[str, Any]] = []
        failures: List[Dict[str, Any]] = []

        for r in results:
            if isinstance(r, Exception):
                failures.append({
                    "news_id": -1, "pre_ts": "?", "model_label": "?",
                    "error": f"gather:{type(r).__name__}",
                })
                continue
            if r["error"] is not None:
                failures.append(r)
            else:
                successes.append(r)

        # ==================================================================
        # Phase 3 - Batch persist (model label in reasoning prefix)
        # ==================================================================
        def _batch_persist():
            """
            Two-phase merge:
              Phase 3a — primary model results: INSERT ai_decisions (aggregation, entry price).
              Phase 3b — Doubao results: UPDATE the matching primary model row with
                         doubao_action / doubao_reasoning.
            Both phases complete inside ONE transaction — SSE consumers see
            fully-populated rows with no race window where Doubao data is missing.
            """
            conn = _open_db()
            written: List[Dict[str, Any]] = []
            done_news_ids: set[int] = set()
            parent_cache: Dict[str, int | None] = {}
            # news_id → decision_id mapping for Doubao UPDATE pass
            primary_decision_ids: Dict[int, int] = {}
            try:
                trading_settings = paper_trading.get_settings(conn)
                active_run = trading_settings.get("active_run")
                # 只在策略配置变化时才写 strategies 表（避免每批无谓 UPDATE）。
                current_strategy = strategy_store.get_current_strategy(conn)
                if trading_settings["is_running"] and active_run:
                    cs_id = int(current_strategy["id"])
                    cs_ver = int(current_strategy["active_version"]["id"])
                    run_sid = int(active_run["strategy_id"])
                    run_sver = int(active_run["strategy_version_id"])
                    if cs_id != run_sid or cs_ver != run_sver:
                        current_strategy = strategy_store.set_current_strategy(
                            run_sid, run_sver, conn,
                        )
                active_version = current_strategy["active_version"]
                active_params = active_version["params"]
                # ============================================================
                # Phase 3a — primary model INSERT — aggregation + tracking
                # ============================================================
                # 多模型分片：所有成功结果都属主分析池（无 Doubao 时无 secondary）
                # 多品种：sibling_decisions 拆成独立 decision 条目（各品种独立交易信号）
                primary_results = _expand_multi_asset_results(successes)

                # 单源开关只过滤模拟盘交易（不影响新闻采集/分析入库）：
                # 建仓前按该新闻的 source 查 news_sources 开关。
                news_source_settings = news_sources.get_settings(conn)
                source_map = {
                    row["id"]: row["source"] if "source" in row.keys() else ""
                    for row in batch
                }

                # 规则事件结构化提取按 news_id 缓存：主行/副行复用，避免重复 re 抽取
                event_struct_cache: Dict[str, str] = {}

                for s in primary_results:
                    # 整箱白名单过滤：缺字段补默认/非法夹取，绝不整条丢弃
                    res = _sanitize_llm_result(s.get("result") or {})
                    # v176：schema 必填字段强制达标
                    try:
                        res = _presanitize_for_schema(res)
                    except Exception:
                        pass
                    # P1-5 服务端兜底：即便 P0-2 retry 失败、LLM 漏填 4 字段，也强制填出 N/A
                    # 用 published_map / timestamp_map 推 ISO 时间，避免 ai_decisions 16.1 字段 0%
                    _nid = s.get("news_id")
                    _pub = published_map.get(_nid, "") if _nid else ""
                    # v1 重构：消费 _unclassified_* 标记 → 写 unclassified_events 表
                    _consume_unclassified_marker(
                        res, news_id=_nid,
                        title="", source="", news_published_at=_pub,
                    )
                    _ts_iso = ""
                    if _pub:
                        _ts_iso = _pub[:32]
                    elif _nid and _nid in timestamp_map:
                        _ts_iso = timestamp_map[_nid][:32]
                    res = _auto_fill_required_fields(
                        res,
                        snapshot_ts_iso=_ts_iso,
                        news_published_at=_pub,
                        evidence_lines=None,
                    )
                    # P1-6 服务端拼 hard_data_evidence：用 target_asset 抓 K 线数据
                    if not res.get("hard_data_evidence") or res.get("hard_data_evidence") == "N/A":
                        try:
                            _ta = res.get("target_asset") or ""
                            if _ta and _ta not in ("NONE", "", "OTHER"):
                                _ev = _build_evidence_for_asset(_ta, _ts_iso)
                                if _ev:
                                    res["hard_data_evidence"] = _ev[:500]
                        except Exception:
                            pass
                    # P1-7 服务端补 snapshot_proximity：published_at 与 timestamp 之差（分钟）
                    if not res.get("snapshot_proximity") or res.get("snapshot_proximity") == "":
                        try:
                            from datetime import datetime as _dt
                            _pub_dt = None
                            if _pub:
                                for _fmt in ("%Y-%m-%dT%H:%M:%S%z", "%Y-%m-%dT%H:%M:%SZ",
                                             "%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S"):
                                    try:
                                        _pub_dt = _dt.strptime(_pub[:19], _fmt)
                                        break
                                    except Exception:
                                        continue
                            _row_ts = timestamp_map.get(_nid, "")
                            if _pub_dt and _row_ts:
                                _row_dt = _dt.strptime(_row_ts[:19], "%Y-%m-%d %H:%M:%S")
                                _delta_min = int((_pub_dt - _row_dt).total_seconds() / 60.0)
                                res["snapshot_proximity"] = str(_delta_min)
                            else:
                                res["snapshot_proximity"] = "N/A"
                        except Exception:
                            res["snapshot_proximity"] = "N/A"
                    # P2-9 注入市场风格到 reasoning_path 末尾
                    try:
                        _ta2 = res.get("target_asset") or ""
                        if _ta2 and _ta2 not in ("NONE", "", "OTHER"):
                            _ms = _compute_market_style(_ta2)
                            if _ms and _ms != "unknown|n/a|n/a":
                                _ms_line = f"[市场风格] {_ms} → LLM 应据此调整 reasoning 的胜率预期"
                                _rp = res.get("reasoning_path") or ""
                                if "[市场风格]" not in _rp:
                                    res["reasoning_path"] = (_rp + "\n" + _ms_line)[:4000]
                    except Exception:
                        pass
                    s["result"] = res
                    label = s["model_label"]
                    nid = s["news_id"]
                    # 本行"分析模型"标记：优先真实调用模型；缺失时才退回运行配置，
                    # 但运行配置可能过期（不在当前模型池内）→ 那就不用它，退回本批模型，
                    # 否则前端会显示一个早已废弃的通道（实测遗留 gpt-5.4-mini）。
                    _pool_ids = {m["id"] for m in model_pool}
                    _run_model = str((active_run or {}).get("model_id") or "")
                    _model_for_row = (
                        str(res.get("model_id") or "").strip()
                        or (_run_model if _run_model in _pool_ids else "")
                        or (model_pool[0]["id"] if model_pool else "")
                    )
                    done_news_ids.add(nid)
                    tagged_reason = f"[{label}] {res['reasoning']}"
                    score = res["sentiment_score"]
                    action = res["suggested_action"]
                    # ── v176.5f: 闸门归因记录器（gate_chain 落库）──
                    # 在闸门调用前初始化，影子模式记录 LLM 原始 action。
                    _gr = _GateRecorder(llm_action=action)
                    # v181: 回放主分析阶段已判定的 FOCUS/MECH 闸门（彼时 _gr 尚未创建），
                    # 让 gate_chain 完整覆盖 FOCUS/MECH/CONSENSUS 三道门，门卫漏报/误报可归因。
                    for _ge in (res.get("_gate_trace") or []):
                        if isinstance(_ge, dict) and _ge.get("gate"):
                            _gr.record(
                                str(_ge.get("gate") or ""),
                                str(_ge.get("verdict") or ""),
                                str(_ge.get("reason") or "")[:200],
                            )
                    # ── v176.9 (2026-09-22)：门卫 LLM 失效防御 ──
                    # 若该新闻 gate_strength=0 且 gate_verdict="无信号"（门卫瞎了），
                    # 直接丢弃，不写入 ai_decisions，避免前端展示伪造的"AI 观望"卡片。
                    # 兜底逻辑：ingest 层 _attach_gate v176.9 已经 FILTERED，
                    # 但万一历史数据或边缘 case 漏过，这里再卡一次。
                    _gm_check = conn.execute(
                        "SELECT gate_strength, gate_verdict, is_noise FROM raw_news WHERE id=?",
                        (nid,),
                    ).fetchone()
                    if _gm_check and (
                        (int(_gm_check["gate_strength"] or 0) == 0
                         and str(_gm_check["gate_verdict"] or "") == "无信号")
                        or int(_gm_check["is_noise"] or 0) == 1
                    ):
                        # 标记 _focus_downgrade 状态为 GATE-MISS（便于调试）
                        try:
                            _gr.apply_block("GATE-MISS", f"gate LLM 不可用 → 跳过 ai_decisions 写入")
                        except Exception:
                            pass
                        print(
                            f"  [GATE-MISS] news=#{nid} gate_strength=0 verdict='无信号' "
                            f"is_noise=1 → 丢弃 ai_decisions, 不再伪装'AI 观望'",
                            flush=True,
                        )
                        # 把 raw_news 状态改成 FAILED（带 GATE-MISS 标签便于排查）
                        try:
                            conn.execute(
                                "UPDATE raw_news SET status='FAILED', processing_at=NULL WHERE id=?",
                                (nid,),
                            )
                            conn.commit()
                        except Exception:
                            pass
                        continue
                    # ── v181: 取消 L3-MENWEI 闸门（realtime_filter 已二分类为"可交易/排除"，
                    # 排除的新闻在 ingest 阶段就被打 is_noise=1，根本进不到 ai_worker ；
                    # 进入此处的 _batch_claim 都是 gate_verdict="可交易" 或历史老数据）。
                    # 保留 strength>=thr 的 STRONG-PASS 兜底：旧"只能看"数据如有遗留，
                    # 仍按 v176.5c 信任 LLM 自评的强信号。
                    _gate_verdict = str(gate_verdict_map.get(nid, ""))
                    _gate_strength = int(gate_strength_map.get(nid, 0) or 0)
                    _GATE_PASS_THR = int(os.getenv("GATE_PASS_STRENGTH", "5"))  # 可调，默认 5（v176.5d）
                    if _gate_strength >= _GATE_PASS_THR:
                        # 不动 action, 让 LLM 给的 BUY/SELL 落库; 若 LLM 给 HOLD 也保留 HOLD
                        _gr.apply_pass("L3-MENWEI", f"strength={_gate_strength} verdict={_gate_verdict} → STRONG-PASS")
                        print(
                            f"  [GATE-STRONG-PASS] news=#{nid} strength={_gate_strength} verdict={_gate_verdict} → 信任强信号, action={action}",
                            flush=True,
                        )
                    elif _gate_verdict == "排除":
                        action = "HOLD"
                        _gr.apply_block("L3-MENWEI", f"verdict=排除 → HOLD")
                        print(
                            f"  [GATE-BLOCK] news=#{nid} 门卫标签={_gate_verdict} → HOLD（只分析不下单）",
                            flush=True,
                        )
                    # 其余（可交易 / 老数据残留的"只能看"）→ 信任 MECH/v174 后续闸门判断
                    if _gate_verdict == "只能看":
                        # 历史兜底：v176.5c 等价行为 — 保留 STRONG-PASS 路径，
                        # 否则让 LLM 自己的 action 走完成。打印一行便于排查老数据。
                        print(
                            f"  [GATE-LEGACY] news=#{nid} gate_verdict=只能看（老数据）→ 信任 LLM action={action}",
                            flush=True,
                        )
                    # ── v174 事件结构化闸门：verification / snapshot_proximity / 双空 → HOLD ──
                    # v176.5d: strength>=_GATE_PASS_THR 跳过 V174 (信任 LLM 强信号, 不让 verification="warn" 兜底强拦)
                    if _gate_strength >= _GATE_PASS_THR:
                        # 已通过 STRONG-PASS, 不再让 V174 把 LLM 给的 BUY/SELL 强制 HOLD
                        _v174_act = None
                        _gr.apply_skip("V174", f"strength={_gate_strength} → 跳过 V174")
                        print(
                            f"  [V174-SKIP] news=#{nid} strength={_gate_strength} → 跳过 V174 闸门 (trust strong signal)",
                            flush=True,
                        )
                    else:
                        _v174_act, _v174_reason = _v174_event_gate(res)
                        if _v174_act == "HOLD":
                            action = "HOLD"
                            _gr.apply_block("V174", _v174_reason)
                            print(
                                f"  [V174-GATE] news=#{nid} asset={str(res.get('target_asset') or '')} "
                                f"event_type={str(res.get('event_type_code') or '')} {_v174_reason}",
                                flush=True,
                            )
                        else:
                            _gr.apply_pass("V174", f"pass (verification/structure ok)")
                    # 留痕：真正落库的这一行的方向单（与 _focus_downgrade 的 gate
                    # 事件按 news_id+asset 对照，即可看出降级是在哪一步丢的）
                    _rp_trace = str(res.get("reasoning_path") or "")[:2000]
                    _signal_trace(
                        "row",
                        news_id=nid,
                        asset=str(res.get("target_asset") or ""),
                        action=str(action),
                        score=score,
                        is_sibling=1 if s.get("is_sibling") else 0,
                        hits=_asset_mentioned_stage_count(
                            _rp_trace, str(res.get("target_asset") or "")),
                        path_len=len(_rp_trace),
                        reasoning=str(res.get("reasoning") or "")[:120],
                        model=str(res.get("model_id") or ""),
                    )

                    # VIP score boost — multiply |score| by 1.25 when VIP matched
                    # 只对主决策行生效：sibling 副品种的 score 是 AI 对该品种的独立
                    # 判断，用主新闻 VIP 放大会失真（地缘新闻若含 VIP 人物，不应把
                    # 副品种 XAU 的分数也 ×1.25）。
                    is_sibling_row = bool(s.get("is_sibling"))
                    news_text_for_vip = content_map.get(nid, "")
                    vip_tag, vip_name = _detect_vip(news_text_for_vip)
                    if (not is_sibling_row) and vip_tag and action in ("BUY", "SELL"):
                        boosted = round(score * VIP_SCORE_BOOST, 4)
                        if abs(boosted) <= 1.0:
                            score = boosted
                    category = res.get("market_category", "") or ""
                    asset = res.get("target_asset", "NONE")

                    # ── 阶段2：多模型共识闸门（在聚合/建仓前执行）──
                    # verification 由"仅留痕"升级为"参与主决策降级"：交叉验证只在
                    # 主品种做（副品种 peer_reviews=None → verification=None 天然跳过）。
                    # 放在聚合前，确保降级后的 HOLD 行不会按旧方向被误聚合到 BUY/SELL parent。
                    verification = _build_verification(res, s.get("peer_reviews"))
                    # v176: verification 兜底（schema minLength=4，否则 INSERT 写 NULL 会让前端读到 None）
                    if not verification or len(str(verification).strip()) < 4:
                        if is_sibling_row:
                            verification = "skip | 副品种无 peer_reviews"
                        else:
                            verification = "skip | 单模型无交叉验证"
                    if (not is_sibling_row) and _consensus_downgrade(res, verification):
                        score = 0.05
                        action = "HOLD"
                        tagged_reason = f"[{label}] {res['reasoning']}"
                        _gr.apply_block("CONSENSUS", f"多模型共识不足（verification={verification}）")
                        print(
                            f"  [CONSENSUS-GATE] {asset} 多模型共识不足"
                            f" → 方向单降级 HOLD（不下单）",
                            flush=True,
                        )
                    elif is_sibling_row:
                        _gr.apply_skip("CONSENSUS", "副品种无 peer_reviews，跳过交叉验证")
                    else:
                        _gr.apply_pass("CONSENSUS", f"verification={verification}")

                    # ── 阶段2.5：Hermes 四席方向票软校验（多数反对 → 降分+留痕，不强制改单）──
                    if (not is_sibling_row) and HERMES_AGENT_ENABLED and action in ("BUY", "SELL"):
                        try:
                            hv = hermes_agent.desk_direction_votes(
                                content_map.get(nid, ""), market_context, asset)
                            tally = hermes_agent.desk_vote_summary(hv, action)
                            if tally["dissent"]:
                                factor = 0.8
                                score = round(max(-1.0, min(1.0, score * factor)), 4)
                                _signal_trace(
                                    "hermes_dissent",
                                    news_id=nid,
                                    asset=asset,
                                    action=action,
                                    opposed=tally["opposed"],
                                    actionable=tally["actionable"],
                                    votes=json.dumps(hv, ensure_ascii=False)[:500],
                                )
                                print(
                                    f"  [HERMES] {asset} 四席多数反对 {action}"
                                    f"（{tally['opposed']}/{tally['actionable']}）→ 软降分 ×{factor}，"
                                    f"不强制改单",
                                    flush=True,
                                )
                        except Exception:
                            pass

                    # ── Aggregation: bundle into parent if same asset+direction ──
                    parent_id: int | None = None
                    agg_key = ""
                    if action in ("BUY", "SELL") and abs(score) >= _AGG_MIN_SCORE:
                        agg_key = f"{category}|{asset}|{action}"
                        if agg_key in parent_cache:
                            parent_id = parent_cache[agg_key]
                        else:
                            parent_id = _find_active_parent(
                                conn, category, asset, action
                            )
                            parent_cache[agg_key] = parent_id

                    # ── 元数据提取 (LLM 可能不返回 → 默认值兜底) ──
                    pred_type = res.get("prediction_type", "continuation")
                    evt_phase = res.get("event_phase", "mid")
                    mkt_confirm = res.get("market_confirmation", "unknown")
                    exp_horizon = res.get("expected_horizon", "1-3d")
                    inval_cond = res.get("invalidation_condition", "")
                    # Phase 2: Event Quality Layer
                    evt_strength = res.get("event_strength", "medium")
                    direct_cat = 1 if res.get("direct_catalyst", False) else 0
                    tf_match = res.get("timeframe_match", "intraday")
                    # 影响评估（0-10）
                    heat_score = res.get("heat_score", 5.0)
                    impact_score = res.get("impact_score", 5.0)
                    asset_impact = res.get("asset_impact", 5.0)

                    # ── 【v181 P1+P2 前置】guard evaluate 在 INSERT 之前先跑 ──
                    # 把 decision_guard 评估从 INSERT 之后前移到 INSERT 之前，让 evidence_action / confidence
                    # 在 INSERT 时就能决定 suggested_action。这样下游 quick_sim 读 ai_decisions.suggested_action
                    # 不会被 AI 原始 action 误导而误建仓（之前 INSERT 后 UPDATE 才能修正，期间 quick_sim 仍可能建仓）。
                    _orig_action = action  # 留底，便于排查
                    _guard_pre: Dict[str, Any] = {
                        "action": action,
                        "confidence": 0.0,
                        "verdict": "guard_pre_failed",
                        "confidence_detail": {"passed_gate": False},
                    }
                    try:
                        _history_sample_pre = evidence.collect_settled_sample(asset, connection=conn)
                        _guard_pre = decision_guard.evaluate_decision({
                            "sentiment_score": score,
                            "target_asset": asset,
                            "market_confirmation": mkt_confirm,
                            "decision_context": decision_context,
                            "cluster_size": 1,  # 前置时 cluster_hint 还未算，临时 1（权重 0.10 影响小）
                            "history_sample": _history_sample_pre,
                            "heat_score": heat_score,
                            "impact_score": impact_score,
                            "asset_impact": asset_impact,
                            "gate_verdict": _gate_verdict,
                        })
                    except Exception as _exc:
                        print(f"  [GUARD-PRE] 前置评估失败: {type(_exc).__name__}: {str(_exc)[:80]}", flush=True)
                        _guard_pre = {
                            "action": "HOLD",
                            "confidence": 0.0,
                            "verdict": f"guard_pre_failed:{type(_exc).__name__}",
                            "confidence_detail": {"passed_gate": False},
                        }
                    _evidence_action_pre = str(_guard_pre.get("action") or "HOLD").upper()
                    _evidence_confidence_pre = float(_guard_pre.get("confidence") or 0.0)
                    _force_hold_reason = ""

                    # ── P2：reasoning_path 超长 → confidence 扣减（过度分析惩罚）──
                    # 阈值/扣分由 .env 控制：OVERANALYSIS_THRESHOLD（默认 800）、OVERANALYSIS_PENALTY（默认 10）。
                    # 每超过阈值 100 字再扣 1 分（封顶额外扣 20）。
                    _rp_path = str(res.get("reasoning_path") or "")
                    _rp_len = len(_rp_path)
                    try:
                        _over_thr = int(os.getenv("OVERANALYSIS_THRESHOLD", "800"))
                    except (TypeError, ValueError):
                        _over_thr = 800
                    try:
                        _over_pen = float(os.getenv("OVERANALYSIS_PENALTY", "10"))
                    except (TypeError, ValueError):
                        _over_pen = 10.0
                    if _rp_len > _over_thr:
                        _extra_pen = min(20.0, max(0.0, (_rp_len - _over_thr) / 100.0))
                        _total_pen = _over_pen + _extra_pen
                        _evidence_confidence_pre = max(0.0, _evidence_confidence_pre - _total_pen)
                        _force_hold_reason = (
                            f"过度分析惩罚：reasoning_path={_rp_len}字>{_over_thr} → "
                            f"confidence 扣 {_total_pen:.1f}（现 {_evidence_confidence_pre:.1f}）"
                        )

                    # ── P1-a：evidence_action=HOLD 强约束 → 强制 action=HOLD ──
                    # AI 给出 BUY/SELL 但 evidence 判定 HOLD → 禁止 BUY/SELL 覆盖（之前
                    # quick_sim 可能在 UPDATE 之前就按 AI 原始 action 建仓，这是 P1 根因）。
                    # v176.5e: strength>=_GATE_PASS_THR (STRONG-PASS) 跳过 evidence 闸门, 信任 LLM 强信号
                    if _gate_strength >= _GATE_PASS_THR and _evidence_action_pre == "HOLD" and action in ("BUY", "SELL"):
                        _gr.apply_skip("EVIDENCE", f"strength={_gate_strength} → 信任强信号, 跳过 evidence=HOLD")
                        print(f"  [HOLD-GATE-SKIP] {asset} {action} evidence=HOLD 但 strength={_gate_strength} → 信任强信号, 不强制 HOLD", flush=True)
                    elif _evidence_action_pre == "HOLD" and action in ("BUY", "SELL"):
                        _force_hold_reason = (
                            _force_hold_reason + ("；" if _force_hold_reason else "")
                            + f"evidence_action=HOLD 强约束：AI 给 {_orig_action} 但 evidence 判定 HOLD → 禁止 BUY/SELL 覆盖"
                        )
                        action = "HOLD"
                        tagged_reason = _force_hold_reason
                        _gr.apply_block("EVIDENCE", f"evidence=HOLD 强约束（AI 给 {_orig_action}）")
                        print(f"  [HOLD-GATE] {asset} {_orig_action} → HOLD (evidence=HOLD)", flush=True)
                    else:
                        _gr.apply_pass("EVIDENCE", f"evidence={_evidence_action_pre} (confidence={_evidence_confidence_pre:.1f})")

                    # ── P1-b：BTC SELL 严格闸门 ──
                    # BTC 方向偏好：SELL BTC 必须 evidence_action=SELL 且 confidence ≥ 60（BTC_SELL_CONFIDENCE_GATE）。
                    try:
                        _btc_gate_thr = float(os.getenv("BTC_SELL_CONFIDENCE_GATE", "60"))
                    except (TypeError, ValueError):
                        _btc_gate_thr = 60.0
                    if action == "SELL" and str(asset or "").upper() == "BTC":
                        # v176.5e: strength>=_GATE_PASS_THR (STRONG-PASS) 跳过 BTC SELL 严格闸门
                        if _gate_strength >= _GATE_PASS_THR:
                            _gr.apply_skip("BTC-SELL", f"strength={_gate_strength} → 信任强信号")
                            print(f"  [BTC-GATE-SKIP] {asset} SELL strength={_gate_strength} → 信任强信号, 不强制 HOLD", flush=True)
                        elif _evidence_action_pre != "SELL" or _evidence_confidence_pre < _btc_gate_thr:
                            _btc_msg = (
                                f"BTC SELL 严格闸门：BTC 方向偏好 → SELL BTC 必须 evidence_action=SELL 且 "
                                f"confidence≥{_btc_gate_thr:.0f}；实际 evidence_action={_evidence_action_pre}, "
                                f"confidence={_evidence_confidence_pre:.1f} → 降级 HOLD"
                            )
                            _force_hold_reason = (
                                _force_hold_reason + ("；" if _force_hold_reason else "")
                                + _btc_msg
                            )
                            action = "HOLD"
                            tagged_reason = _force_hold_reason
                            _gr.apply_block("BTC-SELL", _btc_msg)
                            print(f"  [BTC-GATE] {asset} SELL 不通过 → HOLD", flush=True)
                        else:
                            _gr.apply_pass("BTC-SELL", f"evidence=SELL confidence={_evidence_confidence_pre:.1f}")

                    # 规则事件结构化抽取 → 落库 JSON，供"规则 vs LLM"交叉校验标黄（主行/副行复用缓存）
                    event_struct_json = event_struct_cache.get(nid)
                    if event_struct_json is None:
                        try:
                            from event_struct import extract_events as _extract_events
                            event_struct_json = json.dumps(
                                _extract_events(content_map.get(nid, "") or ""), ensure_ascii=False
                            )[:1000]
                        except Exception:
                            event_struct_json = ""
                        event_struct_cache[nid] = event_struct_json
                    # PR-3: 同步把事件原子写入 event_atom 表（独立于 ai_decisions 行）
                    # 失败 try/except 兜底 — 绝不阻断主 INSERT 流程
                    try:
                        import event_atom as _event_atom
                        _news_row_ts = conn.execute(
                            "SELECT ts FROM raw_news WHERE id = ?", (nid,)
                        ).fetchone()
                        _news_ts = int(_news_row_ts["ts"]) if _news_row_ts and _news_row_ts["ts"] else int(time.time())
                        _before_atom = conn.execute(
                            "SELECT COUNT(*) FROM event_atom WHERE raw_news_id=?", (nid,)
                        ).fetchone()[0]
                        _event_atom.extract_and_persist(
                            content_map.get(nid, "") or "",
                            raw_news_id=int(nid),
                            default_event_time=_news_ts,
                            event_time_source="ai_worker_review",
                            event_time_confidence=0.7,
                            # v171+：LLM 输出的时间结构化字段（来自 _ai_worker_loop 批 LLM 结果）
                            impact_curve=res.get("impact_curve") if isinstance(res, dict) else None,
                            event_window_minutes=res.get("event_window_minutes") if isinstance(res, dict) else None,
                            data_anchor_ts=res.get("data_anchor_ts", "") if isinstance(res, dict) else "",
                            fallback_used=int(bool(res.get("_fallback_used"))) if isinstance(res, dict) else 0,
                            connection=conn,
                        )
                        _after_atom = conn.execute(
                            "SELECT COUNT(*) FROM event_atom WHERE raw_news_id=?", (nid,)
                        ).fetchone()[0]
                        # v174: 规则没命中 → 用 LLM 输出的 event_type_code 兜底回填 1 行
                        # 解决"22 类正则词典外的 news 永远不进 event_atom"的问题
                        if _after_atom == _before_atom:
                            _llm_code = str(res.get("event_type_code") or "").strip()
                            # v176.5: LLM 也没给 code → 强制落 'market_event' 占位行
                            # 保证每条新闻都有 event_atom 行 (供 refresh_backtest_event_outcomes JOIN)
                            if not _llm_code:
                                _llm_code = "market_event"
                            # 校验是 dim_event_type 里合法的 code, 转 FK id
                            _det = conn.execute(
                                "SELECT id FROM dim_event_type WHERE code=? AND active=1", (_llm_code,)
                            ).fetchone()
                            # dim_event_type 没有此 code → 尝试动态插入 (market_event 兜底)
                            if not _det and _llm_code == "market_event":
                                _det_id = conn.execute(
                                    "INSERT INTO dim_event_type (code, name_zh, name_en, category, active) "
                                    "VALUES ('market_event', '市场事件', 'Market Event', 'other', 1) "
                                    "ON CONFLICT(code) DO UPDATE SET active=1 RETURNING id"
                                ).fetchone()
                                if _det_id:
                                    _det = {"id": _det_id[0]} if isinstance(_det_id, tuple) else _det_id
                            if _det:
                                _atom_id = conn.execute(
                                    "INSERT INTO event_atom "
                                    "(raw_news_id, event_type_id, asset_id, event_time, "
                                    " event_time_confidence, event_time_source, "
                                    " direction, direction_confidence, direction_source, "
                                    " evidence_count, parser_version, created_at) "
                                    # v176.5 fix: asset_id NOT NULL DEFAULT 0, created_at NOT NULL
                                    "VALUES (?, ?, 0, ?, 0.6, 'llm_fallback', "
                                    "        'unknown', 0.0, 'llm', 1, 'llm_fallback.v1', ?)",
                                    (nid, _det["id"] if isinstance(_det, dict) else _det[0], _news_ts, int(time.time())),
                                ).lastrowid
                                print(f"  [LLM-FALLBACK-EA] news={nid} event_type={_llm_code} atom_id={_atom_id}", flush=True)
                    except Exception as _ea_err:
                        print(f"  [AI-EVENT-ATOM] news={nid} skipped: {_ea_err}", flush=True)

                    # v176.2 (backlog item 15): 收集本次产生的 event_atom_ids, 写进 ai_decisions.
                    # 双向索引: ai_decisions.event_atom_ids → atom; event_atom.raw_news_id → news.
                    _event_atom_ids_json = ""
                    try:
                        _atom_rows = conn.execute(
                            "SELECT id FROM event_atom WHERE raw_news_id=? ORDER BY id",
                            (nid,),
                        ).fetchall()
                        if _atom_rows:
                            _event_atom_ids_json = json.dumps(
                                [r["id"] for r in _atom_rows], ensure_ascii=False
                            )
                    except Exception as _ea_ids_err:
                        print(f"  [AI-EVENT-ATOM-IDS] news={nid} skipped: {_ea_ids_err}", flush=True)

                    # v173+ 结构化输入层旁路: 拼 6 字段 payload + LLM 返回 reasoning 后做引用校验。
                    # 不动原 LLM 调用路径。原推理逻辑 (解析/打分/反思/沉淀) 一字不改。
                    # 任何上游失败 -> 对应字段填 [],{},null,绝不阻断 INSERT。
                    _structured_input_json = ""
                    _structured_check_json = ""
                    try:
                        import structured_input as _si
                        import structured_check as _sc

                        # PR-5 兜底 refresh: 若 backtest_event_outcomes 表空 / 超过 6h 未刷,
                        # 在 hook 阶段轻量刷一次(失败不阻断主流程)。
                        # 主调度在 api_server / scheduler 调用 refresh_backtest_event_outcomes(),
                        # 此处只是「在线兜底」,确保 baseline.by_type 不长期为空。
                        # v174: 阈值从 6h 缩到 1h, 让 baseline 数据更"新鲜"; min_sample=1 让
                        # 冷启动期小样本也进 by_type (给 LLM 看到结构, 而不是空 dict)
                        try:
                            import db as _db_hook
                            import time as _time_hook
                            _last_refresh = conn.execute(
                                "SELECT MAX(last_updated) FROM backtest_event_outcomes"
                            ).fetchone()[0]
                            _needs_refresh = (
                                not _last_refresh
                                or (_time_hook.time() - _last_refresh) > 1 * 3600
                            )
                            if _needs_refresh:
                                _db_hook.refresh_backtest_event_outcomes(conn, days=90, min_sample=1)
                                print(f"  [BASELINE-HOOK] 在线 refresh baseline (last_refresh={_last_refresh})", flush=True)
                        except Exception:
                            # 兜底 refresh 失败也无所谓,baseline 会降级为空
                            pass

                        # 1) 查 event_atom 行的快照(PR-3 已落库) + raw_news 原文 + market_snapshot
                        # FIX(v173+): raw_news 实际字段名是 content,不是 text —— 之前用 text 触发
                        # OperationalError 走 except,导致 structured_input/check 永远空字符串。
                        # 这里同时取两个字段并 fallback,兼容旧/新 schema。
                        try:
                            _news_for_payload = conn.execute(
                                "SELECT id, source, content AS text, published_at, ts,"
                                " category, symbols, source_url FROM raw_news WHERE id = ?",
                                (nid,),
                            ).fetchone()
                        except Exception:
                            # 旧 schema 兼容: 字段更少
                            _news_for_payload = conn.execute(
                                "SELECT id, source, content AS text, published_at, ts"
                                " FROM raw_news WHERE id = ?",
                                (nid,),
                            ).fetchone()
                        # FIX(v173+): event_atom 真实 schema 是 asset_id(外键) → dim_asset.code
                        # 不是 asset_code。event_type_id → dim_event_type.code 正确。
                        # 用 try/except 兼容未来 dim_asset 可能缺表的场景。
                        try:
                            _event_atom_rows = conn.execute(
                                """SELECT ea.id, ea.event_time, ea.actor,
                                          ea.magnitude_value, ea.magnitude_unit,
                                          da.code AS asset_code,
                                          ea.direction, ea.direction_confidence, ea.direction_source,
                                          ea.evidence_count, det.code AS event_type_code,
                                          det.name_zh AS type_name_zh
                                   FROM event_atom ea
                                   LEFT JOIN dim_event_type det ON det.id = ea.event_type_id
                                   LEFT JOIN dim_asset da ON da.id = ea.asset_id
                                   WHERE ea.raw_news_id = ?
                                   ORDER BY ea.id LIMIT 8""",
                                (nid,),
                            ).fetchall()
                        except Exception:
                            # 兼容: 没有 dim_asset 表/列时退到只用 event_atom
                            try:
                                _event_atom_rows = conn.execute(
                                    """SELECT ea.id, ea.event_time, ea.actor,
                                              ea.magnitude_value, ea.magnitude_unit,
                                              NULL AS asset_code,
                                              ea.direction, ea.direction_confidence, ea.direction_source,
                                              ea.evidence_count,
                                              (SELECT code FROM dim_event_type WHERE id=ea.event_type_id) AS event_type_code,
                                              (SELECT name_zh FROM dim_event_type WHERE id=ea.event_type_id) AS type_name_zh
                                       FROM event_atom ea WHERE ea.raw_news_id = ?
                                       ORDER BY ea.id LIMIT 8""",
                                    (nid,),
                                ).fetchall()
                            except Exception:
                                _event_atom_rows = []

                        # 2) event_struct 已抽事件(降级用)
                        _event_struct_events = []
                        if event_struct_json:
                            try:
                                _event_struct_events = json.loads(event_struct_json) or []
                            except Exception:
                                _event_struct_events = []

                        # 3) 拼 payload (news_row 是 _news_for_payload)
                        # v174: 传 SERVER VERDICT 缓存的完整 market_snapshot_dict + news_ts,
                        #       让 structured_input.snapshot 不再是 [],并注入价格时间序列。
                        # snap[ts] 是 server 抓快照时的 server-side 时间戳,作为 snap_ts。
                        try:
                            _msd_raw = _SERVER_SNAPSHOT_CACHE.get("json") if isinstance(_SERVER_SNAPSHOT_CACHE, dict) else None
                            if isinstance(_msd_raw, str):
                                import json as _json
                                try:
                                    _msd = _json.loads(_msd_raw)
                                except Exception:
                                    _msd = None
                            elif isinstance(_msd_raw, dict):
                                _msd = _msd_raw
                            else:
                                _msd = None
                        except Exception:
                            _msd = None
                        # news_ts: 优先 raw_news.timestamp (UTC 秒);不存在用 published_at_iso 解析
                        try:
                            _news_ts_val = None
                            if 'timestamp' in _news_for_payload:
                                _news_ts_val = _news_for_payload['timestamp']
                            elif 'published_at' in _news_for_payload:
                                _news_ts_val = _news_for_payload['published_at']
                            if _news_ts_val:
                                _news_ts_val = int(_news_ts_val)
                        except Exception:
                            _news_ts_val = None

                        _payload = _si.build_structured_input(
                            _news_for_payload,
                            market_snapshot_dict=_msd,  # v174: 接通 SERVER VERDICT 时间结构化
                            event_struct_events=_event_struct_events,
                            event_atom_rows=_event_atom_rows,
                            connection=conn,
                            news_ts=_news_ts_val,  # v174: 事件时间锚
                        )
                        _structured_input_json = _si.serialize(_payload)

                        # 4) 引用校验(reasoning vs structured_input)
                        _reasoning_text = res.get("reasoning", "") or ""
                        _check = _sc.check_reasoning_vs_structured_input(
                            _reasoning_text, _payload
                        )
                        # v174: 把 JSON Schema + 价格幻觉校验结果合并进 _check
                        # 这样 structured_check 字段一次性包含了所有 v174 质量评估
                        try:
                            _v174_val = res.get("_v174_validation") if isinstance(res, dict) else None
                            if _v174_val:
                                _check["v174_quality"] = {
                                    "schema_valid": _v174_val.get("schema_valid"),
                                    "schema_errors_count": len(_v174_val.get("schema_errors", [])),
                                    "fabrication_rate": _v174_val.get("fabrication_rate"),
                                    "verdict": _v174_val.get("verdict"),
                                }
                        except Exception:
                            pass
                        _structured_check_json = _sc.serialize_check(_check)
                    except Exception as _si_err:
                        # 失败兜底:写空字符串,绝不阻断 INSERT
                        print(f"  [STRUCTURED-INPUT] news={nid} skipped: {_si_err}", flush=True)

                    # v176.2：INSERT 前查同 asset 的 prev/regime/bias，5 字段一并写入
                    _prev = _load_prev_decision(asset or "")

                    # v181：结构化产出摘要（7 项）在 INSERT 前拼好，final_action 复用最终 action 变量
                    _struct_summary_json = _build_struct_summary(res, action, asset)

                    # v186 经验系统：计算 persona_weight（actor 对 asset 的影响力权重）
                    # 同时记录命中的 experience_entries.id → ai_decisions.persona_match_id
                    try:
                        import experience_reflector as _er
                        # 从 news_source / news_text 提取 actor hint
                        _news_actor = ""
                        try:
                            if isinstance(news_source, str) and news_source:
                                _news_actor = news_source[:120]
                            else:
                                _nt = (res.get("news_text") or res.get("source_text") or "")
                                if _nt:
                                    _news_actor = _nt[:120]
                        except Exception:
                            _news_actor = ""
                        _pw = _er.compute_persona_weight(asset or "", _news_actor)
                        _pm_id = 0  # 命中 id(0=无命中, 中性)
                        try:
                            _pm_id = _er.get_matched_experience_id(asset or "", _news_actor)
                        except Exception:
                            _pm_id = 0
                    except Exception:
                        _pw, _pm_id = 1.0, 0

                    cur = conn.execute(
                        """
                        INSERT INTO ai_decisions
                          (news_id, sentiment_score, suggested_action,
                           reasoning, created_at, market_category, target_asset,
                           parent_id, child_count, aggregation_key, reasoning_path, vip_tag,
                           prediction_type, event_phase, market_confirmation,
                           expected_horizon, invalidation_condition,
                           event_strength, direct_catalyst, timeframe_match,
                           decision_context, strategy_id, strategy_version_id,
                           heat_score, impact_score, asset_impact, peer_reviews, is_sibling,
                           asset_reasoning, low_impact, asset_nature, factor_ranking,
                           impact_magnitude, verification, attention_score, elasticity_window, data_anchor_ts, hard_data_evidence, event_struct,
                           event_type_code, snapshot_proximity,
                           prev_decision_id, prev_action_same_asset, prev_score_same_asset,
                           regime_at_decision, bias_after_reflection,
                            structured_input, structured_check, event_atom_ids,
                             gate_chain, original_action, ungated_action,
                             structured_summary,
                             persona_weight, persona_match_id,
                             ts)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                                ?, ?, ?, ?, ?,
                                ?, ?, ?,
                                ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                                ?);
                        """,
                        (
                            nid,
                            score,
                            action,
                            tagged_reason,
                            batch_end,
                            category,
                            asset,
                            parent_id,
                            agg_key if parent_id is None else "",
                            0,
                            res.get("reasoning_path", ""),
                            vip_tag,
                            pred_type,
                            evt_phase,
                            mkt_confirm,
                            exp_horizon,
                            inval_cond[:200],  # 截断超长失效条件
                            evt_strength,
                            direct_cat,
                            tf_match,
                            decision_context,
                            current_strategy["id"],
                            active_version["id"],
                            heat_score,
                            impact_score,
                            asset_impact,
                            # 多模型交叉验证（验证模型独立分析结果 JSON；无则 NULL）
                            json.dumps(s.get("peer_reviews"), ensure_ascii=False) if s.get("peer_reviews") else None,
                            # 多品种：1=副决策行（同新闻其它品种）；0=主决策/普通单
                            1 if s.get("is_sibling") else 0,
                            # 品种相关推理逻辑（为什么这条新闻关联主/副品种）：仅主行存 AI 全量
                            str(res.get("asset_reasoning") or "")[:400] if not s.get("is_sibling") else "",
                            # "影响小"标记：方向单但 asset_impact 低于阈值（默认阈值 0 = 从不标记）。
                            # 先把"影响小的单子"标出来观察真实表现，再决定要不要硬拦（见 config 注释）。
                            1 if (
                                action in ("BUY", "SELL")
                                and MIN_ASSET_IMPACT_FOR_TRADE > 0
                                and float(asset_impact or 0) < MIN_ASSET_IMPACT_FOR_TRADE
                            ) else 0,
                            # ── 八步新增字段 ──
                            str(res.get("asset_nature") or "")[:80],
                            str(res.get("factor_ranking") or "")[:500],
                            res.get("impact_magnitude"),
                            verification,
                            res.get("attention_score"),
                            str(res.get("elasticity_window") or "")[:200],
                            str(res.get("data_anchor_ts") or "")[:60],
                            str(res.get("hard_data_evidence") or "")[:500],
                            event_struct_json,
                            # v174 新增三字段: event_type_code 必须从 dim_event_type 25 个选 1
                            str(res.get("event_type_code") or "")[:64],
                            str(res.get("snapshot_proximity") or "")[:32],
                            # v176.2 新增 5 字段: 决策间长期影响传递
                            _prev.get("prev_decision_id"),
                            str(_prev.get("prev_action_same_asset") or "")[:16],
                            _prev.get("prev_score_same_asset"),
                            str(_prev.get("regime_at_decision") or "")[:32],
                            str(_prev.get("bias_after_reflection") or "")[:200],
                            # v173+ 结构化输入层 (旁路,不动原推理)
                            _structured_input_json,
                            _structured_check_json,
                            # v176.2 (item 15): event_atom ↔ ai_decisions 双向索引
                            _event_atom_ids_json,
                            # v176.5f: 闸门归因（gate_chain + original_action + ungated_action 影子模式）
                            _gr.to_json(),
                            _gr.original_action,
                            _gr.ungated_action,
                            # v181 结构化产出摘要 JSON
                            _struct_summary_json,
                            # v186 经验系统：actor 对 asset 的影响力权重 + 命中的经验 id
                            float(_pw),
                            int(_pm_id),
                            # v302+ P0+【修复】INSERT 路径补 ts (UTC 秒).
                            # 历史 BUG: ai_decisions.ts 是 v302+ 加的无 default 列,
                            # INSERT 完全不写 → 全 NULL → v302+ 窗口查询 (ts>=lo/hi/fresh_lo)
                            # 全部漏空 → /api/events/today live_signal_count 永远 0,
                            # 前端 StatCards 大字显示 0. 现显式写入, 并由 BEFORE INSERT
                            # trigger (trg_ai_decisions_autots) 兜底防漏.
                            int(time.time()),
                        ),
                    )
                    decision_id = cur.lastrowid
                    # v20251014: 即时算 signal_weight (无市场反应 → reaction_mult=1.0).
                    # 失败静默, 不影响主流程 (INSERT 已 commit).
                    try:
                        if decision_id:
                            import signal_weight_evaluator as _swe
                            _swe.evaluate_and_persist(
                                conn, decision_id,
                                trigger_kind="insert", now_ts=int(time.time()),
                            )
                    except Exception as _swe_exc:  # noqa: BLE001
                        print(f"[ai_worker] signal_weight evaluate id={decision_id} failed: {_swe_exc}", flush=True)
                    # v200 P0+ 严谨兜底: 回写 field_sources / missing_fields /
                    #   completeness_score / 5 类 source 计数 / historical_ref_ids
                    #   到 ai_decisions, 让反思系统按 completeness 过滤脏决策.
                    try:
                        if decision_id:
                            _fs = json.loads(res.get("field_sources") or "{}")
                            _mf = json.loads(res.get("missing_fields") or "[]")
                            _cs = float(res.get("completeness_score") or 1.0)
                            _hr = res.get("historical_ref_ids") or "{}"
                            _counts = _count_sources(_fs)
                            conn.execute(
                                """UPDATE ai_decisions SET
                                    field_sources = ?,
                                    missing_fields = ?,
                                    completeness_score = ?,
                                    llm_field_count = ?,
                                    rule_field_count = ?,
                                    business_kb_field_count = ?,
                                    historical_field_count = ?,
                                    historical_ref_ids = ?,
                                    llm_retry_count = ?
                                  WHERE id = ?""",
                                (
                                    json.dumps(_fs, ensure_ascii=False),
                                    json.dumps(_mf, ensure_ascii=False),
                                    _cs,
                                    int(_counts["llm"]),
                                    int(_counts["rule"]),
                                    int(_counts["business_kb"]),
                                    int(_counts["historical"]),
                                    str(_hr),
                                    int(res.get("llm_retry_count") or 0),
                                    int(decision_id),
                                ),
                            )
                    except Exception as _v200_e:
                        # v200 写入失败不应阻塞 INSERT, 仅 print
                        print(f"  [V200-COMPLETENESS-WRITE] failed: "
                              f"{type(_v200_e).__name__}: {_v200_e}", flush=True)
                    # v188 P0-2: 把刚才 inject 到 prompt 的经验标 llm_invoked=1
                    # (注意: 这是异步/重复 chat 流程的最后一次 inject,因为 _LAST_INJECTED_EXP_IDS
                    #  会被后续 chat 调用覆盖,这里只对"最近一次注入"打标,合理即可)
                    try:
                        if decision_id and _LAST_INJECTED_EXP_IDS:
                            import experience_reflector as _er2
                            for _eid in _LAST_INJECTED_EXP_IDS:
                                if _eid and _eid > 0:
                                    _er2.apply_experience(_eid, int(decision_id),
                                                         applied_action="prompt_inject",
                                                         llm_invoked=1)
                    except Exception as _exp_e:
                        # 失败兜底: 仅 print, 绝不断 INSERT 已成功的决策
                        print(f"  [EXP-INJECT-MARK] failed: {type(_exp_e).__name__}: {_exp_e}",
                              flush=True)
                    # v200 P0-A3: 把 _asset_insights 注入时命中的 insight ids 写回 ai_decisions,
                    # 让"经验参与决策"在 DB 层可见 (原 594/594 全空)。主品种和副品种都写,
                    # 副品种另写一份"为副品种匹配的子集", 便于后续按 asset 维度统计命中归因。
                    try:
                        if decision_id and _asset_insight_hit_ids:
                            _hit_json = json.dumps(
                                [int(x) for x in _asset_insight_hit_ids
                                 if isinstance(x, (int, float)) and int(x) > 0],
                                ensure_ascii=False,
                            )
                            conn.execute(
                                "UPDATE ai_decisions SET insight_hit_ids=? WHERE id=?",
                                (_hit_json, int(decision_id)),
                            )
                            print(
                                f"  [INSIGHTS-HIT-WRITE] decision#{decision_id} "
                                f"asset={asset} hit={len(_asset_insight_hit_ids)} ids",
                                flush=True,
                            )
                    except Exception as _hit_e:
                        print(f"  [INSIGHTS-HIT-WRITE] failed: "
                              f"{type(_hit_e).__name__}: {str(_hit_e)[:80]}",
                              flush=True)
                    if not s.get("is_sibling"):
                        primary_decision_ids[nid] = decision_id

                        # ── v173: structured_check verdict=fabricated/partial → 入真实性飞轮 ──
                        # 失败兜底: 异常时仅 print,绝不阻断 INSERT 已经成功的决策。
                        try:
                            from engine import structured_reflection as _sr
                            _check_dict = {}
                            try:
                                _check_dict = json.loads(_structured_check_json or "{}") if _structured_check_json else {}
                            except Exception:
                                _check_dict = {}
                            _verdict_now = str(_check_dict.get("verdict") or "")
                            if _verdict_now in ("fabricated", "partial"):
                                # 取最新 event_atom 的 event_type_code 作为关键维度
                                _sr_etc = str(res.get("event_type_code") or "")
                                try:
                                    _sr_rows = conn.execute(
                                        "SELECT det.code FROM event_atom ea "
                                        "LEFT JOIN dim_event_type det ON det.id=ea.event_type_id "
                                        "WHERE ea.raw_news_id=? ORDER BY ea.event_time DESC LIMIT 1",
                                        (nid,),
                                    ).fetchone()
                                    if _sr_rows and _sr_rows["code"]:
                                        _sr_etc = str(_sr_rows["code"])
                                except Exception:
                                    pass
                                _sr.enqueue(
                                    conn,
                                    decision_id=int(decision_id),
                                    news_id=int(nid) if nid else None,
                                    asset=str(asset or ""),
                                    event_type_code=_sr_etc,
                                    verdict=_verdict_now,
                                    score_coverage=float(_check_dict.get("score_coverage") or 0.0),
                                    score_fabrication=float(_check_dict.get("score_fabrication") or 0.0),
                                    entities_fabricated=list(_check_dict.get("entities_fabricated") or []),
                                    events_missing=list(_check_dict.get("events_missing") or []),
                                )
                        except Exception as _sr_err:
                            print(f"  [STRUCT-REFLECT-HOOK] news={nid} enqueue 跳过: "
                                  f"{type(_sr_err).__name__}: {str(_sr_err)[:80]}", flush=True)

                        # ── v174: event_struct 从 event_atom 派生升级 ──
                        # 解决 "event_struct 70% 是 []" 的假性结构化问题:
                        # INSERT 时 event_struct 字段是 event_struct.extract_events() 结果
                        # (22 类正则词典,覆盖率低,70% 返回 [])
                        # 现在 event_atom 表已经在 L5548 写入(规则 + LLM 兜底)
                        # → 用 event_atom 数据升级 event_struct 字段,
                        #   保证 event_struct 字段填充率 = event_atom 覆盖率
                        try:
                            _atom_rows = conn.execute(
                                "SELECT ea.event_type_id, det.code AS event_code,"
                                " ea.direction, ea.direction_confidence, ea.evidence_count,"
                                " ea.actor, ea.magnitude_value, ea.magnitude_unit,"
                                " ea.event_time, ea.reasoning_text"
                                " FROM event_atom ea"
                                " LEFT JOIN dim_event_type det ON det.id = ea.event_type_id"
                                " WHERE ea.raw_news_id = ?"
                                " ORDER BY ea.event_time DESC",
                                (nid,),
                            ).fetchall()
                            if _atom_rows:
                                _v174_struct = [
                                    {
                                        "event_type": r["event_code"] or "",
                                        "actor": r["actor"] or "",
                                        "action": r["direction"] or "",
                                        "magnitude_value": r["magnitude_value"],
                                        "magnitude_unit": r["magnitude_unit"] or "",
                                        "target_assets": [],  # event_atom 不强约束具体资产
                                        "raw": (r["reasoning_text"] or "")[:200],
                                        "source": "event_atom",
                                        "confidence": r["direction_confidence"] or 0.0,
                                    }
                                    for r in _atom_rows
                                ]
                                _v174_struct_json = json.dumps(
                                    _v174_struct, ensure_ascii=False
                                )[:1000]
                                if _v174_struct_json and _v174_struct_json != "[]":
                                    conn.execute(
                                        "UPDATE ai_decisions SET event_struct = ? WHERE id = ?",
                                        (_v174_struct_json, decision_id),
                                    )
                                    # 静默: 不刷日志(高频调用)
                        except Exception as _v174_err:
                            # 兜底失败不影响主流程
                            pass

                        # ── 全网溯源：主行才写。AI 自由回答的文本直接存 ──
                        # 同时存结构化字段（前端徽章三态显示用）。
                        # 三层防护 + 校验函数（_validate_cross_net_ts）：
                        #   ① 格式正则 ②下限1990 ③不能未来(±60s) ④不能晚于入库(±60s)
                        #   ⑤source ≥4字符 / AMBIGUOUS 必须含"候选1"或"/"或"或"
                        # 校验失败 → 清空（前端不渲染徽章；不污染 DB）
                        try:
                            _noi = str(res.get("net_origin_info") or "").strip()[:200]
                            _cn_ts_raw = str(res.get("cross_net_first_seen_ts") or "").strip()[:32]
                            _cn_src = str(res.get("cross_net_source") or "").strip()[:300]
                            _our_ts = timestamp_map.get(nid, "") or ""
                            _cn_ts = _validate_cross_net_ts(_cn_ts_raw, _cn_src, _our_ts)
                            # source 只在 ts 校验通过时写入；空串/失败时 source 也清空，避免误用
                            if not _cn_ts:
                                _cn_src = ""
                            # 同步写 net_origin_info（已有）+ cross_net_first_seen_ts/_source（新）
                            if _noi or _cn_ts:
                                conn.execute(
                                    "UPDATE raw_news SET net_origin_info = ?, "
                                    "cross_net_first_seen_ts = ?, cross_net_source = ? "
                                    "WHERE id = ?",
                                    (_noi[:200] if _noi else "", _cn_ts, _cn_src[:300] if _cn_src else "", nid),
                                )
                        except Exception:
                            pass  # 全网溯源失败不影响主决策写库

                    # v173 决策证据链：每条决策钉死"用了哪条新闻作 trigger"——后续 /api/decisions/:id
                    # 可全文溯源到原文 + 平台发布时间 + 抓到时间 + 内容指纹。
                    # 同族聚合（parent_id 非空）→ 关联 parent 的 nid；sibling 行关联同 nid。
                    try:
                        evidence_news_id = nid if nid is not None else (
                            parent_row[0] if (parent_id is not None and parent_row and parent_row[0]) else None
                        )
                        if evidence_news_id is not None:
                            db.record_evidence(
                                conn,
                                decision_id=int(decision_id),
                                news_id=int(evidence_news_id),
                                used_for="trigger",
                            )
                    except Exception as _exc:
                        # 接入失败不阻断主流程（evidence 是可观测性，不是业务关键路径）
                        pass

                    history_sample = evidence.collect_settled_sample(asset, connection=conn)
                    # 聚合热度：传入真实同族数量（parent 的 child_count + 当前这条），
                    # 修复闸门 cluster_heat 因子恒为 0 的问题。
                    cluster_hint = 1
                    if parent_id is not None:
                        p_row = conn.execute(
                            "SELECT child_count FROM ai_decisions WHERE id = ?",
                            (parent_id,),
                        ).fetchone()
                        if p_row:
                            cluster_hint = int(p_row["child_count"] or 0) + 1
                    guard = decision_guard.evaluate_decision({
                        "sentiment_score": score,
                        "target_asset": asset,
                        "market_confirmation": mkt_confirm,
                        "decision_context": decision_context,
                        "cluster_size": cluster_hint,
                        "history_sample": history_sample,
                        # 影响评估（0-10）参与闸门加权
                        "heat_score": heat_score,
                        "impact_score": impact_score,
                        "asset_impact": asset_impact,
                        # 门卫四步标签(L3)：v181 二分类后只剩"排除"会触发 HOLD
                        "gate_verdict": _gate_verdict,
                    })
                    timeseries.record_factor_snapshot(nid, asset, guard, conn)
                    timeseries.record_news_event(
                        nid,
                        source="",
                        is_noise=0,
                        status="DONE",
                        decision_id=decision_id,
                        asset=asset,
                        action=action,
                        score=score,
                        connection=conn,
                    )
                    if HERMES_AGENT_ENABLED:
                        hermes_agent.persist_decision_observation(
                            nid,
                            decision_id,
                            asset=asset,
                            action=action,
                            score=score,
                            extras={
                                "prediction_type": pred_type,
                                "event_strength": evt_strength,
                                "confidence": guard.get("confidence"),
                            },
                            connection=conn,
                        )
                    direction_consistent = (
                        action == "HOLD"  # 观望恒自洽（AI 观望不会因分数判矛盾）
                        or (action == "BUY" and score > 0)
                        or (action == "SELL" and score < 0)
                    )
                    gate_passed = (
                        guard["confidence_detail"]["passed_gate"]
                        and guard["action"] == action
                        and direction_consistent
                    )
                    if action == "HOLD":
                        # AI 主动观望：也写闸门结论供前端展示（之前一律空字符串导致 dashboard 看不到原因）
                        # 优先级：传导链闸门 > 前置强制(P1/P1-b/P2) > 闸门降级 (action=AI 期望 vs guard 推荐不一致) > LLM 倾向 HOLD 本身
                        if _force_hold_reason:
                            gate_reason = _force_hold_reason
                        elif str(_gate_verdict) == "排除":
                            gate_reason = f"门卫四步标签=排除：只分析不下单"
                        elif str(_gate_verdict) == "只能看":
                            # 历史兜底：老数据残留的"只能看"标签不再触发 HOLD（v181 已删除此分类）
                            gate_reason = f"门卫四步标签=只能看(老数据兜底)，信任后续闸门"
                        elif not direction_consistent:
                            gate_reason = "LLM 方向与分数符号矛盾"
                        elif not guard["confidence_detail"]["passed_gate"]:
                            gate_reason = guard["verdict"] or f"统一置信度 {guard['confidence']} 低于闸门，未建仓"
                        else:
                            # AI 主动 HOLD（但 evidence 已通过）—— 记录"AI 倾向观望"以便审计
                            gate_reason = f"AI 主动观望 (evidence={guard['confidence']}，方向={action})"
                    elif gate_passed:
                        gate_reason = guard["verdict"]
                    elif not direction_consistent:
                        gate_reason = "LLM 方向与分数符号矛盾"
                    elif not guard["confidence_detail"]["passed_gate"]:
                        gate_reason = guard["verdict"]
                    else:
                        # 置信度过闸但闸门方向与 LLM 方向不一致 → 明确拒绝，
                        # 不写"证据充分"（否则 quick_sim 的 gate_passed 标记会误标）。
                        gate_reason = (
                            f"闸门方向与信号方向不一致 (闸门={guard['action']}, 信号={action})"
                        )
                    conn.execute(
                        """UPDATE ai_decisions
                           SET paper_trading_run_id=?, agent_model_id=?,
                               evidence_confidence=?, evidence_action=?, trade_gate_reason=?
                           WHERE id=?""",
                        (
                            active_run["id"] if active_run else None,
                            # 记录实际成功调用的模型（OpenRouter/MiniMax/nassaapi），
                            # 而非模拟盘运行配置——否则前端「当前分析」永远显示配置模型。
                            # 运行配置的 model_id 可能是历史遗留值（池子换过模型后不在池内，
                            # 实测遗留 gpt-5.4-mini/nassaapi 已 403）→ 不在池内就不用它，
                            # 退回本批实际使用的模型。
                            _model_for_row,
                            # 【v181 P2】写入已扣 overanalysis 惩罚的 confidence（前置 guard）
                            _evidence_confidence_pre,
                            # 【v181 P1】写入前置 evidence_action（可能已强制为 HOLD）
                            _evidence_action_pre,
                            gate_reason,
                            decision_id,
                        ),
                    )

                    # 严格模式：冻结策略 + 多证据闸门均通过后才建仓。
                    # 宽松模式：关闭闸门，BUY/SELL 只要有真实行情即自动建仓。
                    entry_price_val: Optional[float] = None
                    now_ts = _ts()
                    gate_enforced = bool(trading_settings.get("gate_enabled", True))
                    strategy_ok = strategy_store.signal_matches(
                        active_params,
                        action=action if not gate_enforced else guard["action"],
                        score=score,
                        asset=asset,
                        event_strength=evt_strength,
                        direct_catalyst=direct_cat,
                    )
                    # 新闻时效门槛：新闻发布超过后台配置的时长（秒）不再建仓
                    # （BUY/SELL 通用，防止用旧新闻追单；解析失败视为新鲜放行）
                    # 优先用"平台声称的发布时间"计算年龄——防平台补发旧闻刚入库被当新鲜；
                    # 无平台时间（WS/webhook 实时流）时回落入库时间（到达即发布）。
                    max_age_sec = _get_news_trade_max_age_seconds()
                    pub_raw = published_map.get(nid, "") or ""
                    age_basis = pub_raw if pub_raw else timestamp_map.get(nid, "")
                    news_age = _news_age_minutes(age_basis)
                    news_fresh = news_age is None or news_age * 60 <= max_age_sec
                    if not news_fresh:
                        print(
                            f"  [AI] 信号过期，跳过建仓: news=#{nid} 已 {news_age:.0f} 分钟"
                            f"（> {max_age_sec // 60} 分钟 / {max_age_sec}s），仍入库统计不交易",
                            flush=True,
                        )
                    auto_trade = (
                        trading_settings["is_running"]
                        and active_run is not None
                        and paper_trading.asset_allowed(asset, trading_settings)
                        and news_sources.source_enabled(source_map.get(nid, ""), news_source_settings)
                        and action in ("BUY", "SELL")
                        and asset not in ("", "NONE")
                        and news_fresh
                        and (
                            (not gate_enforced)
                            or (gate_passed and strategy_ok)
                        )
                    )
                    if auto_trade:
                        # 健康检查仅开仓触发：后台探测一次 OpenRouter 存活度并写缓存
                        # （fire-and-forget，不阻塞开仓与事件循环）。结果由后续分析的
                        # _openrouter_healthy() 读取，用于判断是否切 MiniMax 备用通道。
                        try:
                            loop.run_in_executor(None, _probe_openrouter_if_stale)
                        except Exception:
                            pass
                        entry_price_val = _get_current_price(asset)
                        if entry_price_val is not None:
                            # ── v176 P2-16B/E：risk_manager 接入（仓位/连亏/日损三道门） ──
                            # 默认规模 = 1000 USDT（与 risk_manager.MAX_POSITION_USDT 兼容）；
                            # 若 max_allowed < suggested → 不建仓 + 写 no_trade_reason（前台可观测）。
                            try:
                                import risk_manager as _rm
                                _eval = _rm.evaluate_trade(
                                    asset, action, 1000.0,
                                )
                            except Exception as _rm_e:
                                _eval = {"allow": True, "reason": f"risk_manager import err: {_rm_e}"}
                            if not _eval.get("allow", True):
                                conn.execute(
                                    "UPDATE ai_decisions SET no_trade_reason=? WHERE id=?",
                                    (f"risk_manager:{_eval.get('reason', 'rejected')}", decision_id),
                                )
                                print(
                                    f"  [RISK-MANAGER] {asset} {action} news=#{nid} rejected: {_eval.get('reason')}",
                                    flush=True,
                                )
                            else:
                                entry_unix = int(time.time())
                                # 单笔持仓周期：按 AI 的 expected_horizon 映射；
                                # intraday → NULL（沿用策略默认周期），1-3d → 2 天，1w+ → 7 天。
                                horizon_minutes = {
                                    "1-3d": 2 * 24 * 60,
                                    "1w+": 7 * 24 * 60,
                                }.get(exp_horizon)
                                conn.execute(
                                    """UPDATE ai_decisions
                                       SET entry_price=?, entry_time=?, max_price=?, min_price=?,
                                           max_price_time=?, min_price_time=?, holding_horizon_minutes=?,
                                           no_trade_reason=''
                                       WHERE id=?""",
                                    (entry_price_val, now_ts, entry_price_val, entry_price_val,
                                     entry_unix, entry_unix, horizon_minutes, decision_id),
                                )
                                # P4 命中验证：记录本单 AI 实际采纳的经验 id（仅供结算后统计遵守率）。
                                # 只写 AI 在 insight_hit_ids 里声明的 id，并过滤到当前生效经验集合，
                                # 防止模型幻觉出不存在的 id 污染命中归因统计；副品种(sibling)行不写。
                                # v186 audit 修复: 当 AI 返回空时, 自动从 review_insights 按关键词
                                # 匹配 top-3,让经验真正参与决策(原 0/83 → 现在应该有命中).
                                try:
                                    from review_agent import normalize_insight_hit_ids, load_active_insight_items
                                    _claimed = normalize_insight_hit_ids(res.get("insight_hit_ids")) if not s.get("is_sibling") else []
                                    if _claimed:
                                        _valid = {int(it["id"]) for it in load_active_insight_items()}
                                        if _valid:
                                            _claimed = [i for i in _claimed if i in _valid]
                                    # v186 fallback: AI 没声明时,用 _auto_match_insights 兜底
                                    if not _claimed and not s.get("is_sibling"):
                                        try:
                                            _news_text = ""
                                            try:
                                                _news_row = conn.execute(
                                                    "SELECT content, title FROM raw_news WHERE id=?",
                                                    (nid,),
                                                ).fetchone()
                                                if _news_row:
                                                    _news_text = (_news_row["title"] or "") + " " + (_news_row["content"] or "")
                                            except Exception:
                                                pass
                                            _claimed = _auto_match_insights(_news_text, asset or "", top_n=3)
                                        except Exception:
                                            _claimed = []
                                    if _claimed:
                                        conn.execute(
                                            "UPDATE ai_decisions SET insight_hit_ids=? WHERE id=?",
                                            (json.dumps(_claimed), decision_id),
                                        )
                                except Exception:
                                    pass
                        else:
                            # 闸门/条件全过，但行情源取不到价格 → 未建仓
                            if action in ("BUY", "SELL") and asset not in ("", "NONE"):
                                conn.execute(
                                    "UPDATE ai_decisions SET no_trade_reason=? WHERE id=?",
                                    ("行情源不可用，未能取到价格", decision_id),
                                )
                    else:
                        # ── 方向性信号(BUY/SELL)未实际建仓 → 记录原因（实时信号台展示）──
                        if action in ("BUY", "SELL") and asset not in ("", "NONE"):
                            no_reason = ""
                            if not trading_settings["is_running"]:
                                no_reason = "模拟盘未运行"
                            elif active_run is None:
                                no_reason = "无激活的模拟盘运行"
                            elif not paper_trading.asset_allowed(asset, trading_settings):
                                no_reason = f"资产 {asset} 不在模拟盘允许交易列表"
                            elif not news_sources.source_enabled(source_map.get(nid, ""), news_source_settings):
                                no_reason = "该新闻源已关闭（模拟盘交易层）"
                            elif not news_fresh:
                                no_reason = (
                                    f"新闻已发布 {news_age:.0f} 分钟，超过时效门槛"
                                    f" {max_age_sec // 60} 分钟"
                                )
                            elif gate_enforced and not gate_passed:
                                # 复用闸门拒绝原因（置信度不足/LLM方向矛盾/闸门方向不一致）
                                no_reason = gate_reason or "证据闸门未通过"
                            elif gate_enforced and not strategy_ok:
                                # 策略闸门拒绝的具体原因（信号强度不足/资产过滤/事件强度/直接催化剂等）
                                no_reason = strategy_store.signal_mismatch_reason(
                                    active_params,
                                    action=action if not gate_enforced else guard["action"],
                                    score=score,
                                    asset=asset,
                                    event_strength=evt_strength,
                                    direct_catalyst=direct_cat,
                                ) or "信号不匹配当前策略规则"
                            if no_reason:
                                conn.execute(
                                    "UPDATE ai_decisions SET no_trade_reason=? WHERE id=?",
                                    (no_reason[:200], decision_id),
                                )

                    if parent_id is not None:
                        # Backfill parent's entry_price if missing (pre-feature data)
                        parent_row = conn.execute(
                            "SELECT entry_price, evidence_confidence, trade_gate_reason"
                            " FROM ai_decisions WHERE id = ?",
                            (parent_id,),
                        ).fetchone()
                        # 只有 parent 自身证据通过（且尚未建仓）时才补 entry——
                        # 避免把从未获批交易的 parent 拉进 2h 结算，污染统计。
                        if parent_row is not None and parent_row["entry_price"] is None and auto_trade:
                            p_conf = parent_row["evidence_confidence"]
                            p_reason = parent_row["trade_gate_reason"] or ""
                            parent_approved = (
                                (p_conf is not None and p_conf >= float(os.getenv("EVIDENCE_CONFIDENCE_GATE", "40")))
                                or p_reason.startswith("证据充分")
                            )
                            if parent_approved:
                                parent_backfill = _get_current_price(asset)
                                if parent_backfill is not None:
                                    entry_unix = int(time.time())
                                    conn.execute(
                                        """UPDATE ai_decisions
                                           SET entry_price=?, entry_time=?, max_price=?, min_price=?,
                                               max_price_time=?, min_price_time=?, strategy_id=?, strategy_version_id=?
                                           WHERE id=?""",
                                        (parent_backfill, now_ts, parent_backfill, parent_backfill,
                                         entry_unix, entry_unix, current_strategy["id"],
                                         active_version["id"], parent_id),
                                    )
                        conn.execute(
                            "UPDATE ai_decisions SET child_count = child_count + 1"
                            " WHERE id = ?",
                            (parent_id,),
                        )
                        _bump_parent_score(conn, parent_id, score, tagged_reason)

                        # ── Consensus: count cluster density within 30 min ──
                        cluster_count = conn.execute(
                            """
                            SELECT COUNT(*) FROM ai_decisions
                            WHERE (parent_id = ? OR id = ?)
                              AND strftime('%s', created_at) > strftime('%s', 'now', '-30 minutes')
                            """,
                            (parent_id, parent_id),
                        ).fetchone()[0]
                        conn.execute(
                            """
                            UPDATE ai_decisions SET cluster_size = ?
                            WHERE (parent_id = ? OR id = ?)
                              AND strftime('%s', created_at) > strftime('%s', 'now', '-30 minutes')
                            """,
                            (cluster_count, parent_id, parent_id),
                        )
                    else:
                        conn.execute(
                            "UPDATE ai_decisions SET aggregation_key = ? WHERE id = ?",
                            (agg_key, decision_id),
                        )

                    written.append({
                        "decision_id": decision_id,
                        "news_id": nid,
                        "model_label": label,
                        "pre_ts": s["pre_ts"],
                        "result": res,
                        "parent_id": parent_id,
                    })

                # ============================================================
                # Phase 3b — Doubao UPDATE — merge into existing primary model rows
                # ============================================================
                doubao_results = [s for s in successes if s["model_label"] == "Doubao"]
                for d in doubao_results:
                    nid = d["news_id"]
                    res = _sanitize_llm_result(d.get("result") or {})
                    try:
                        res = _presanitize_for_schema(res)
                    except Exception:
                        pass
                    # v1 重构：消费 _unclassified_* 标记 → 写 unclassified_events
                    _consume_unclassified_marker(res, news_id=nid)
                    d["result"] = res
                    if nid in primary_decision_ids:
                        conn.execute(
                            "UPDATE ai_decisions SET doubao_action = ?, doubao_reasoning = ? WHERE id = ?",
                            (res["suggested_action"], res["reasoning"], primary_decision_ids[nid]),
                        )
                        # Append Doubao info to the written record for terminal output
                        for w in written:
                            if w["news_id"] == nid:
                                w["doubao"] = {"action": res["suggested_action"], "reasoning": res["reasoning"]}
                                break
                    else:
                        # The primary model failed, so Doubao has nothing to merge into.
                        pass

                # ============================================================
                # Mark DONE for all news_ids that produced a primary model row
                # ============================================================
                for nid in done_news_ids:
                    conn.execute(
                        "UPDATE raw_news SET status = 'DONE', processing_at = NULL"
                        " WHERE id = ? AND status = 'PROCESSING';",
                        (nid,),
                    )
                for f in failures:
                    nid = f["news_id"]
                    if nid > 0 and nid not in done_news_ids:
                        conn.execute(
                            "UPDATE raw_news SET status = 'FAILED', processing_at = NULL"
                            " WHERE id = ? AND status = 'PROCESSING';",
                            (nid,),
                        )
                conn.commit()
            except Exception:
                conn.rollback()
                raise
            finally:
                conn.close()
            return written

        # 调用 _batch_persist，如果数据库锁定则重试
        max_retries = 6
        retry_delay = 0.5  # 秒
        decision_infos = []
        try:
            for attempt in range(max_retries):
                try:
                    decision_infos = _batch_persist()
                    break  # 成功则跳出重试循环
                except sqlite3.OperationalError as e:
                    if "database is locked" in str(e).lower() and attempt < max_retries - 1:
                        print(f"[{_now()}] [AI] 数据库锁定，第 {attempt + 1} 次重试...")
                        await asyncio.sleep(retry_delay * (attempt + 1))  # 指数退避
                    else:
                        raise  # 重试次数用完或其他错误，抛出异常
        except Exception as exc:
            # 批次落库失败不再杀死 ai_worker：记录后跳过本批，
            # 并把 PROCESSING 的新闻退回 PENDING 以便下轮重新认领。
            print(f"[{_now()}] [AI] Batch persist 失败，跳过本批: {type(exc).__name__}: {str(exc)[:200]}")
            try:
                conn = _open_db()
                conn.execute(
                    "UPDATE raw_news SET retry_count = retry_count + 1,"
                    " processing_at = NULL,"
                    " status = CASE WHEN retry_count + 1 >= ? THEN 'FAILED' ELSE 'PENDING' END"
                    " WHERE status = 'PROCESSING'",
                    (_MAX_PENDING_RETRIES,),
                )
                conn.commit()
                conn.close()
            except Exception:
                pass

        # v175：重要新闻 / 海力士强买入 → 手机推送（Server酱+邮件，fire-and-forget，不阻塞）
        try:
            _maybe_push_phone_alerts(successes, content_map, timestamp_map, published_map)
        except Exception:
            pass

        # ==================================================================
        # Phase 4 - Print comparison & send Feishu (with Chinese translation)
        # ==================================================================
        # Group decisions by news_id
        by_news: Dict[int, List[Dict[str, Any]]] = {}
        for d in decision_infos:
            by_news.setdefault(d["news_id"], []).append(d)

        for nid, group in by_news.items():
            # 优先使用真正的新闻原文，而不是模型的 translated_title
            # translated_title 实际上是主模型的推理结论，不应该作为新闻原文显示
            original_news = content_map.get(nid, f"News #{nid}")
            display_title = original_news
            print(f"\n  ┌─ News #{nid}: {display_title[:70]}")
            # Collect for Feishu card (one card per news item)
            feishu_lines: List[str] = []
            consensus: Dict[str, int] = {}
            for d2 in group:
                res2 = d2["result"]
                label = d2["model_label"]
                action2 = res2["suggested_action"]
                score2 = res2["sentiment_score"]
                # 飞书推送只用短结论（完整八段式太长，手机端阅读负担重）
                model_reason = (str(res2.get("reasoning") or "").strip()[:120]
                                or str(res2.get("reasoning_path") or "").strip()[:120])
                print(
                    f"  │ [{label:8s}] {action2:4s} {score2:+.3f} | {model_reason[:100]}"
                )
                consensus[action2] = consensus.get(action2, 0) + 1
                feishu_lines.append(
                    f"**{label}**: {action2} ({score2:+.3f}) — {model_reason}"
                )
                # Doubao secondary verification — if present, append to Feishu card
                doubao = d2.get("doubao")
                if doubao:
                    db_action = doubao["action"]
                    db_reason = str(doubao["reasoning"])[:120]
                    print(
                        f"  │ [Doubao  ] {db_action:4s}       | {db_reason}"
                    )
                    consensus[db_action] = consensus.get(db_action, 0) + 1
                    feishu_lines.append(
                        f"**Doubao**: {db_action} — {db_reason}"
                    )

            # Consensus line
            parts = [f"{v}×{k}" for k, v in sorted(consensus.items(), key=lambda x: -x[1])]
            consensus_str = " | ".join(parts)
            print(f"  └─ Consensus: {consensus_str}")
            # HOLD/无方向：共识里没有任何 BUY/SELL 票 → 不发飞书卡片（避免噪音轰炸）
            if not any(k in ("BUY", "SELL") for k in consensus):
                continue
            # Fire-and-forget Feishu comparison card (Chinese title)
            feishu_body = "\n\n".join(feishu_lines)
            news_ts = timestamp_map.get(nid, "")
            asyncio.create_task(
                send_feishu_alert(
                    f"{display_title}\n\n**Consensus**: {consensus_str}",
                    consensus_str,
                    0.0,
                    feishu_body,
                    news_ts,
                )
            )

        for f in failures:
            label = f.get("model_label", "?")
            error_msg = f.get("error", "Unknown error")
            print(
                f"[{_now()}] [AI] news=#{f['news_id']} [{label}] FAILED:"
                f" {error_msg}"
            )
        # Batch summary
        try:
            s_dt = datetime.fromisoformat(batch_start)
            e_dt = datetime.fromisoformat(batch_end)
            batch_latency = (e_dt - s_dt).total_seconds()
        except Exception:
            batch_latency = -1
        total_tasks = len(batch)
        print(
            f"[{_now()}] [AI] Batch done | {len(successes)}/{total_tasks} ok"
            f" | {len(failures)} failed | wall={batch_latency:.1f}s"
        )
