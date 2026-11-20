#!/usr/bin/env python3
"""worker_helpers.py — ai_worker 里「自成闭环」的函数（2026-11-18 第二批外迁）。

挑选口径（脚本判定，不是肉眼挑）：一个函数能被搬走，必须满足
  1) 自由变量只落在 builtins / worker_config 常量 / 模块级 import 别名 / 本批一起搬的函数 里；
  2) 不写 `global`；
  3) 对本批内函数的调用传递闭包封闭（谁的依赖留在 ai_worker，谁就先剔除）。

结果：63 个函数、约 1512 行（校验/补字段/降级文案/模型池这类纯逻辑），搬完后
ai_worker 只剩"编排 + 调用"，这些函数不再跟着 14,000 行的文件一起被 diff。

ai_worker 用 `from engine.worker_helpers import *` 接回，函数名不变。
"""

from __future__ import annotations

from .utils import _detect_vip, _now, _open_db, _ts
from config import AI_MODEL_ROSTER, DEFAULT_AI_MODEL_ID, AIPING_API_KEY, AIPING_BASE_URL, AIPING_EXTRA_BODY, get_selected_ai_model_id, VIP_SCORE_BOOST, BATCH_SIZE, HERMES_AGENT_ENABLED, MACRO_CONTEXT_ENABLED, _AGG_WINDOW_HOURS, _AGG_MIN_SCORE, MECHANISM_GATE_ENABLED, MIN_ASSET_IMPACT_FOR_TRADE
from datetime import datetime, timedelta
from market.market_classifier import MARKET_CATEGORIES_PROD, TARGET_TO_MARKET, UnclassifiedError, infer_market_category, is_debug_other_allowed, map_target, row_is_unclassified, validate_pair
from typing import Any, Dict, List, Optional, Tuple
import causal_graph
import config
import factor_ontology
import json
import os
import re
import sqlite3
from common import time_utils
import time

# 常量（_ASSET_ALIASES / _FOCUS_EXEMPT_ASSETS / FIELD_WEIGHTS / CONSENSUS_GATE_ENABLED …）
from engine.worker_config import *

__all__ = [
    "_action_leaning", "_alias_word_match", "_apply_freshness", "_auto_fill_required_fields", "_autofill_soft_fields", "_build_graph_block", "_build_longterm_block", "_build_reclaim_sql", "_build_struct_summary", "_build_verification", "_bump_parent_score", "_calc_step_overlap", "_consensus_downgrade", "_count_direction_hits", "_count_sources", "_db_safe", "_derive_cross_net_from_noi", "_direction_leaning", "_ensure_asset_reasoning_coverage", "_ensure_sibling_decisions", "_expire_gate_down_sql", "_expire_sql", "_extract_reasoning_steps", "_fallback_business_knowledge_drivers", "_fallback_historical_reasoning", "_find_active_parent", "_fundamental_hint", "_has_fresh_tradeable", "_infer_sibling_from_reasoning", "_init_result_completeness", "_is_eight_stage_skeleton", "_is_resolved_asset", "_is_slow_channel", "_is_specific_asset_category", "_is_truthy", "_looks_placeholder", "_mark_field_missing", "_mark_field_source", "_main_prompt", "_minimax_backup_slots", "_minimax_cfg", "_minimax_fallback_cfg", "_minimax_fallback_cfg2", "_news_age_minutes", "_norm_score_action", "_normalize_sentiment_score", "_origin_search_block_for", "_parse_result_light", "_pick_main_model_cfg", "_safe_child_count", "_segment_has_asset", "_segment_mentions_asset", "_signal_trace", "_six_stage_complete", "_six_stage_strict", "_stock_context_for", "_asia_context_for", "_v174_event_gate", "asset_in_core_scope", "build_model_config", "classify_analysis_degraded", "declares_non_core_news", "degraded_reasoning_template", "direction_consistent", "get_current_model_config", "has_unclosed_think", "is_none_target", "is_skeleton_result", "merge_two_step", "news_mentions_pool_asset", "reconcile_score_action", "rerender_degrade_text_for_asset", "should_schema_retry", "strip_think_blocks", "is_empty_llm_output", "_path_assets_in_stages", "_siblings_missing_in_stages", "_asset_missing_stages", "_asset_mentioned_stage_count", "_is_gate_downgraded", "_main_established", "_asset_has_mechanism", "_dominant_asset_in_path", "_siblings_missing_stages", "_summary_text", "_has_summary_for", "_iter_stages", "_asset_scoped_text", "_direction_conflict", "_STAGE_MARK_RE", "_PER_ASSET_LINE_RE", "_MECHANISM_RE", "_FOCUS_GATE_PREFIXES", "_fallback_invalidation_condition", "_extract_factors_fallback", "_validate_cross_net_ts", "_extract_cross_net_from_text", "_rescue_target_asset_from_content", "_INVAL_COND_TEMPLATES", "_CN_TS_RE", "_CN_TS_PATTERNS", "_CN_SRC_PATTERNS", "rescue_budget_ok", "rescue_budget_state_for", "_RESCUE_LIMIT_PER_WINDOW", "_RESCUE_NONE_LIMIT_PER_WINDOW", "_build_extra_models_consensus", "_build_gate_context",
]



_THINK_PAIR_RE = re.compile(r"<think>.*?</think>", re.DOTALL)
_THINK_OPEN_RE = re.compile(r"<think>.*$", re.DOTALL)


def strip_think_blocks(raw_text: str) -> str:
    """剥掉思维链块，**但绝不把后面的 JSON 一起删掉**。

    v342 事故：老逻辑 `re.sub(r"<think>.*$")` 对未闭合 <think> 是从标签删到文末，
    实测把 1455 字（think + 完整 JSON）削成 0 字 → json.loads 空串 → 降级兜底。
    新逻辑：未闭合时，从标签之后找第一个 "{" / "["，保留那之后的内容。
    """
    t = str(raw_text or "")
    t = _THINK_PAIR_RE.sub("", t)
    if "<think>" in t:
        last = t.rfind("<think>")
        cands = [p for p in (t.find("{", last), t.find("[", last)) if p != -1]
        t = t[min(cands):] if cands else _THINK_OPEN_RE.sub("", t)
    return t.strip()


def is_empty_llm_output(raw_text: str) -> bool:
    """LLM 原始输出是否为空/几乎为空（去 think 块与空白后 < 10 字）。"""
    if raw_text is None:
        return True
    t = str(raw_text)
    if not t.strip():
        return True
    t = strip_think_blocks(t)
    t = re.sub(r"^```(?:json)?", "", t.strip(), flags=re.IGNORECASE)
    return len(t.strip()) < _EMPTY_RAW_MIN_CHARS

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
    # v461: `or 1.0` 会把 0.0 当"没设过"重置回 1.0 —— 扣到 0 之后再标一个缺失字段，
    # 分数反而从 1.0 重新起扣（0 → 0.85）。这里只把 None 当默认，0 是合法值。
    raw_cur = result.get("completeness_score")
    try:
        cur = 1.0 if raw_cur is None else float(raw_cur)
    except (TypeError, ValueError):
        cur = 1.0
    result["completeness_score"] = max(0.0, cur - weight)


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


def _fundamental_hint(res, text=""):
    """给基本面因子用的"新闻原文"提示（只用来从公司名反解代码，不做别的）。

    背景：target_asset 常是 CN_STOCK 这类类别标签，A股/港股通路因此触发率≈0。
    asset_kinds 里有一张同市场的公司名→代码表，拿到正文就能把「贵州茅台」认成
    600519.SH。取不到就返回空串 —— 解析不出代码时因子层仍报 placeholder，不猜。
    """
    try:
        return str((res or {}).get("news_ticker") or text
                   or (res or {}).get("news_text") or (res or {}).get("source_text")
                   or (res or {}).get("title") or "")[:400]
    except Exception:
        return ""


def _build_reclaim_sql(active_ids: Optional[List[int]] = None) -> Tuple[str, List[Any]]:
    """看门狗回收 SQL + 参数（带「在办批次」排除子句）。

    active_ids 为本进程正在处理的批次 id。必须排除它们：单批耗时可能远超
    15 分钟（行情源全超时时实测 42 分钟），若中途被回收，批事务末尾
    `UPDATE ... WHERE status='PROCESSING'` 会匹配 0 行 → 一条都进不了 DONE
    → 队头那批被无限重认领 → 「待分析」永不下降。
    """
    ids = sorted({int(i) for i in (active_ids or [])})
    if not ids:
        return _WATCHDOG_RECLAIM_SQL_BASE, [_BATCH_RETRY_COOLDOWN_SEC, _MAX_PENDING_RETRIES]
    sql = _WATCHDOG_RECLAIM_SQL_BASE + " AND id NOT IN (%s)" % ",".join("?" * len(ids))
    return sql, [_BATCH_RETRY_COOLDOWN_SEC, _MAX_PENDING_RETRIES, *ids]


def _has_fresh_tradeable(conn) -> bool:
    """建仓窗口内、门卫判「可交易」、还没分析过的行是否存在。

    v432（2026-11-17）：存在就**单条认领、插队直跑**，不再凑满一批。
    背景：实测可交易信号从新闻到决策平均 39–45 分钟，正好压在建仓窗口
    （2700s）边缘 —— 一批 4 条要 8–15 分钟，为了等齐同批的慢行，
    真正能交易的那条被拖过期，最后只落库、不下单（当日订单 0 笔）。
    单条直跑把「新鲜可交易」的等待从"整批"降到"一条"。
    """
    try:
        _win = int(os.getenv("TRIDENT_TRADE_WINDOW_SEC", "2700") or 2700)
    except Exception:
        _win = 2700
    # 门卫未判定的行也允许插队，但只限最近 15 分钟（更老的判了也赶不上窗口，
    # 让它们留在常规批次里排队，不抢插队名额）。
    _deg_fresh = 900
    _now = int(time.time())
    # v444：断档水位（0 = 不限制）。与认领 SQL 共用同一份口径。
    try:
        import news_cutoff as _nc  # noqa: WPS433
        _floor = int(_nc.get_floor())
    except Exception:
        _floor = 0
    try:
        return bool(
            conn.execute(
                "SELECT 1 FROM raw_news"
                " WHERE status='PENDING' AND COALESCE(is_noise, 0) = 0"
                "   AND COALESCE(gate_unavailable, 0) IN (0, 2)"
                "   AND ts IS NOT NULL"
                "   AND ("
                "     (COALESCE(gate_verdict, '') = '可交易' AND ts >= ?)"
                "     OR (COALESCE(gate_unavailable, 0) = 2 AND ts >= ?)"
                "   )"
                "   AND (processing_at IS NULL"
                "        OR processing_at <= datetime('now','localtime'))"
                # v444：断档水位之下的行不算「新鲜可交易」—— 与认领 SQL 同口径。
                # 否则会先判定"有可交易 → 只认领 1 条"，而认领 SQL 又把这条挡住
                # → 35s 空跑一轮，且主循环迟迟拿不到新新闻（实测 17:27 就这样）。
                "   AND (? <= 0 OR ts >= ?)"
                " LIMIT 1",
                (_now - _win, _now - _deg_fresh, _floor, _floor),
            ).fetchone()
        )
    except Exception:
        return False


def _expire_sql(keep_tradeable: bool, has_reason_col: bool,
                keep_gate_down: bool = False, reason: str = "stale_expired") -> str:
    """按开关/表结构拼归档 SQL（v408 / v419）。

    keep_tradeable=True 时，门卫判「可交易」的行不进归档 WHERE —— 见上方注释 B。
    keep_gate_down=True 时，门卫**未判定**（gate_unavailable=2）的行也不进这条 WHERE，
    改由 `_expire_gate_down_sql` 用更长的宽限单独处理（v419）——
    否则「放行」只是把标记从 1 改成 2，行照样超龄蒸发。
    has_reason_col=False（老库未 migrate）时去掉 filter_reason，避免整条 UPDATE 被拒。
    """
    set_clause = "UPDATE raw_news SET status='FILTERED', processing_at=NULL"
    if has_reason_col:
        set_clause += ", filter_reason='%s'" % reason
    where = ["status='PENDING'", "COALESCE(gate_unavailable, 0) IN (0, 2)"]
    if keep_tradeable:
        where.append("COALESCE(gate_verdict, '') != '可交易'")
    if keep_gate_down:
        where.append("COALESCE(gate_unavailable, 0) != 2")
    where.append("(? > 0 AND ts IS NOT NULL AND ts < ?)")
    return set_clause + " WHERE " + " AND ".join(where)


def _expire_gate_down_sql(has_reason_col: bool) -> str:
    """门卫未判定（=2）的行单独归档，留**不同**的 filter_reason（v419）。

    为什么要分两条 SQL 而不是一条带 OR：
      · 宽限不同（普通行 6h，这类 24h）—— 一条 SQL 只能有一个时间条件；
      · 留痕不同：`stale_expired` 读起来是「超龄」，`expired_gate_down` 才是
        「门卫没判过、也没人分析过」，事后要回放时能精确捞出来。
    """
    set_clause = "UPDATE raw_news SET status='FILTERED', processing_at=NULL"
    if has_reason_col:
        set_clause += ", filter_reason='expired_gate_down'"
    where = ["status='PENDING'", "COALESCE(gate_unavailable, 0) = 2",
             "(? > 0 AND ts IS NOT NULL AND ts < ?)"]
    return set_clause + " WHERE " + " AND ".join(where)


def _safe_child_count(value) -> int:
    """把 ai_decisions.child_count 读成整数，脏值一律当 0。

    v322 事故：child_count 列曾被写成 'OIL|WTI|BUY' 这类聚合键（INSERT 参数
    错位），下游 int() 抛 ValueError → 整批 16 条落库作废 → 队头反复占位 →
    前端「待分析」永不下降。单个脏字段绝不允许炸掉整批，这里统一兜底。

    EXEMPT-HONESTY: child_count 是**内部计数列**（不是用户可见的行情/涨跌字段）。
    扫描器的规则针对的是「把缺失的行情数据伪装成 0%」这种假数据；
    而计数列的 DB NULL → 0 是合法聚合语义（test_no_fabrication.py 顶部注释
    明确写着「不扫内部聚合 score/length/count — DB NULL → 0 是合法语义」）。
    """
    try:
        # EXEMPT-HONESTY: 内部计数列，DB NULL/脏值 → 0 是合法聚合语义（非用户可见行情）
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


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


def has_unclosed_think(raw_text: str) -> bool:
    """原文里有 <think> 却没有 </think>（M3/2.7 常见：think 不闭合，JSON 紧跟在后面）。"""
    t = str(raw_text or "")
    return "<think>" in t and "</think>" not in t


def is_skeleton_result(result: Dict[str, Any]) -> bool:
    """解析成功、但"正文"几乎是空的 —— 模型只交了一副骨架。

    实测（2026-11-16）：tokens_out=33 的调用能返回合法 JSON（target_asset + HOLD），
    四个正文键合计只有几个字。这种输出 `is_empty_llm_output` 判不出来（原文不短），
    于是走下坡路：sanitize 补 default → 骨架八段 → 记成"LLM 未返回可用分析"。

    判据：reasoning / reasoning_path / asset_nature / asset_reasoning 四键
    正文合计 < 30 字。正常 HOLD 的短 JSON 光 reasoning 就有 40+ 字，不会误伤。
    """
    if not isinstance(result, dict):
        return True
    total = 0
    for k in _SKELETON_FIELDS:
        v = result.get(k)
        if v is None:
            continue
        total += len(str(v).strip())
    return total < _SKELETON_MIN_CHARS


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
        # ── v322: 补齐这几个字段（之前丢失，导致两个隐蔽 bug）────────────
        # ① key_alias：熔断器按"账户"隔离（minimax1 / minimax2 / local ...）。
        #    丢了它 → _do_api_call 用 label 当别名、main_pool_ready 用 key_alias
        #    当别名 → **两个不同的熔断器实例** → 熔断了但体检看不到
        #    → 永远不"跳过认领" → 通道全挂时照样烧额度。
        # ② timeout：本地 Ollama 通道需要 180s（LOCAL_LLM_TIMEOUT），
        #    丢了它 → _do_api_call 回退 60s → 本地兜底必然 ReadTimeout，
        #    等于"本地兜底对主分析完全无效"。
        # ③ is_local / purpose：用途判定与池构建要用。
        "key_alias": model.get("key_alias", "unknown"),
        "timeout": float(model.get("timeout", 60.0)),
        "is_local": bool(model.get("is_local", False)),
        "purpose": model.get("purpose", []),
        "request_model_name": model.get("request_model") or model["id"],
    }


def get_current_model_config() -> Dict[str, Any]:
    return build_model_config(get_selected_ai_model_id())


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
        # v396 P3：2500 → 4000。实测八段 JSON（中文）常见 3000+ token，
        # 2500 会在 closing brace 前截断 → 解析失败 → 记成"LLM 未返回可用分析"。
        # 这是 llm_empty 73 条里"截断"那部分的主因。
        try:
            extra_body["max_tokens"] = int(os.getenv("MINIMAX_MAX_TOKENS", "4000"))
        except (TypeError, ValueError):
            extra_body["max_tokens"] = 4000
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
        # v322: 补齐 key_alias / timeout（熔断与超时都依赖这两个字段）
        "key_alias": label_suffix.lower().replace("#", "") or "minimax1",
        "timeout": float(getattr(config, "MINIMAX_TIMEOUT", 60.0) or 60.0),
        "is_local": False,
        "purpose": ["main"],
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


def _minimax_backup_slots() -> int:
    """主池里会走 OpenRouter 的槽位总数（use_proxy 通道 weight 之和）。

    失效时这些槽位全部由旧 MiniMax 承接（旧优先主体）。
    """
    return sum(
        max(1, int(m.get("weight", 1)))
        for m in AI_MODEL_ROSTER
        if m.get("use_proxy")
    )


def _autofill_soft_fields(result: Dict[str, Any]) -> List[str]:
    """v435 P0-2：结构性字段本地推导，不再为它们单独发一次 LLM 请求。

    依据（2026-11-17 全天统计，`engine.out.log` 里「缺 P0-2 必填字段」的字段分布）：
        fund_driver 464 次、consensus_vs_us 464 次、asset_classification 464 次、
        asset_nature 399 次、target_asset 398 次、reasoning 388 次、factor_ranking 388 次。
    其中前四个**不是模型的判断**，而是"描述/分类"字段：
      · asset_classification 由 market_category（服务端已按 target_asset 定）唯一映射；
      · asset_nature 是资产性质的短描述，可直接由 target_asset + market_category 拼；
      · fund_driver 用模型自己写的 reasoning 前 60 字即可（模型已判过方向）；
      · consensus_vs_us 是二值标签，用 sentiment_score 的中性区间判（|s|<=0.05 → 一致）。
    这些每次缺失都触发一次 `schema_fill_lean`（≈500 token / 3–6s），
    实测今天 152 次里大部分就是为了补这几个字段 —— 纯浪费。

    不本地造的是**真判断字段**：target_asset / reasoning / factor_ranking /
    elasticity_window（需要具体分钟数与衰减），它们缺失仍走原有抢救链。
    本地补过的字段会记进 `_schema_autofill`，落库后能统计"多少比例是补的"。

    返回本次补了哪些字段（空列表 = 什么都没补）。
    """
    filled: List[str] = []
    try:
        _ta = str(result.get("target_asset") or "").strip().upper()
        _mc = str(result.get("market_category") or "").strip().upper()

        # ① asset_classification：按 market_category 映射（服务端事实，不依赖模型）
        if not str(result.get("asset_classification") or "").strip():
            _cls = _SCHEMA_AUTOFILL_MAP_ASSET_CLASS.get(_mc)
            if _ta in ("XAU", "GOLD", "XAG", "GLD"):
                _cls = "避险/抗通胀"
            elif _ta in ("WTI", "BRENT", "OIL", "USO"):
                _cls = "供给驱动"
            if _cls:
                result["asset_classification"] = _cls
                filled.append("asset_classification")

        # ② asset_nature：描述性短句（标的为空/NONE 时不造，避免"无标的却有资产性质"）
        _an = str(result.get("asset_nature") or "").strip()
        if len(_an) < 4 and _ta and _ta not in ("NONE", "OTHER"):
            _cls = str(result.get("asset_classification") or "").strip() or "交易标的"
            result["asset_nature"] = f"{_ta}｜{_cls}"[:40]
            filled.append("asset_nature")

        # ③ fund_driver：用模型自己写的结论截断（不新增判断）
        if not str(result.get("fund_driver") or "").strip():
            _src = str(result.get("reasoning") or result.get("reasoning_path") or "").strip()
            _driver = re.sub(r"[\r\n]+", " ", _src)[:60].strip()
            if _driver:
                result["fund_driver"] = _driver
                filled.append("fund_driver")

        # ④ consensus_vs_us：中性区间判"一致"，其余"反共识"
        if not str(result.get("consensus_vs_us") or "").strip():
            try:
                _s = float(result.get("sentiment_score") or 0.0)
            except (TypeError, ValueError):
                _s = 0.0
            result["consensus_vs_us"] = "一致" if abs(_s) <= 0.05 else "反共识"
            filled.append("consensus_vs_us")

        # ⑤ verification：自检结论，如实标明是本地结构补全（不伪装成模型自检通过）
        if not str(result.get("verification") or "").strip():
            result["verification"] = (
                "pass | 结构字段本地补全：" + "、".join(filled)
                if filled else "pass | 结构字段本地补全"
            )
            filled.append("verification")
    except Exception:
        pass

    if filled:
        result["_schema_autofill"] = list(filled)
    return filled


def _db_safe(value: Any) -> Any:
    """v396.2：把模型可能返回的 list/dict 归一成标量，避免绑定参数炸掉整批落库。

    历史事故（2026-11-16 20:56）：`reasoning_path` 被模型返回成数组 →
    sqlite3.ProgrammingError: Error binding parameter 11: type 'list' is not supported →
    整批 8 条回滚、退 PENDING、再被重新认领，队列同一批反复打转。

    规则：list/dict → JSON 文本（内容不丢）；标量原样；其余 → str。
    """
    if value is None or isinstance(value, (str, int, float, bytes)):
        return value
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, (list, dict, tuple)):
        try:
            return json.dumps(value, ensure_ascii=False)
        except Exception:
            return str(value)
    return str(value)


def is_none_target(target_asset: Any) -> bool:
    """v398：标的是否为"没锁住"（空 / NONE / OTHER）。"""
    return str(target_asset or "").strip().upper() in ("", "NONE", "OTHER")


def news_mentions_pool_asset(news_text: str) -> bool:
    """v396.2：这条新闻里到底有没有出现交易池资产/宏观驱动关键词。

    用途 —— 判断"NONE 是模型偷懒，还是这条新闻天生没有可交易标的"。

    背景（实测）：池外宏观/个股评论类新闻占队列大头，模型如实判 NONE，
    但下游仍按"NONE = 没锁住标的"的假设，一路做 schema 补全 retry + 两步抢救
    + 落库前抢救（合计 3-5 次额外模型调用），最后**还是**判 NONE。
    单条因此多花 1-2 分钟，一批 8 条能拖 20 分钟。

    口径：全文（含标题）一次也不出现 POOL_KEYWORDS 里的任何词 → 判"天生无标的"。
    宁可多花 token（关键词命中就照常抢救），不可误杀真信号。
    """
    txt = str(news_text or "")
    if not txt.strip():
        return True  # 空文本 → 不敢断言，按"可能有"处理（照常抢救）
    # v420：改用**只含品种名**的表，理由见下方 _ASSET_ONLY_KEYWORDS 注释。
    _up_txt = txt.upper()
    try:
        return any(k.upper() in _up_txt for k in _ASSET_ONLY_KEYWORDS)
    except Exception:
        return True  # 表不可用 → 保守放行，不改变原行为


def declares_non_core_news(result: Dict[str, Any], news_text: str) -> bool:
    """v396.2：模型明确判 NONE + 分类不是具体品种 + 全文无池内关键词 → "天生非核心"。

    三者同时成立才认定：模型说了没标的、它自己也没给**品种级**分类、新闻本身也没提到
    任何监控品种 —— 这种情况再做抢救调用是纯浪费（抢救不可能凭空造出标的）。

    v420（2026-11-17）：原来要求 `market_category` **完全为空**，实测根本命中不了。
    今日 21 条 NONE 决策里 18 条（86%）被模型填了 `MACRO`（宏观），只有 3 条为空
    → 快路径日志命中 **0 次**，等于这条省调用的路从来没走过。
    但 `MACRO` 是"宏观"这个**合法且正确**的非品种分类，它恰恰说明模型不是在偷懒 ——
    它明确说了"这是宏观新闻，没有单一品种"。用它来否定快路径是逻辑反了。
    改判：`market_category` 只要不是**具体品种**（XAU/BTC/WTI/美股…）就放行；
    真给了品种级分类却又不填 target_asset，那才说明可能有标的没锁住，照常抢救。
    """
    if not is_none_target(result.get("target_asset")):
        return False
    _mc = str(result.get("market_category") or "").strip()
    if _mc and _is_specific_asset_category(_mc):
        return False
    # reasoning_path 是"真推演"的硬证据：没有推演的 NONE 仍要照常抢救
    if len(str(result.get("reasoning_path") or "").strip()) < 100:
        return False
    return not news_mentions_pool_asset(news_text)


def _is_specific_asset_category(market_category: str) -> bool:
    """v420：判断 `market_category` 是不是"具体品种级"分类。"""
    _up = str(market_category or "").upper()
    return any(k.upper() in _up for k in _SPECIFIC_ASSET_CATEGORY_HINTS)


def merge_two_step(target: Dict[str, Any], step_a: Dict[str, Any],
                   step_b: Dict[str, Any]) -> tuple:
    """两步抢救的合并规则（纯函数）。

    第一步只锁 target_asset + 方向；第二步只补八段与证据。
    **锁不住标的就整段放弃**（不拿第二步的半成品去覆盖，避免"有题材无标的"）。
    空串 / 空数组 / 少于 4 字的字段一律不覆盖原值。
    """
    out = dict(target or {})
    notes = []
    ta = str((step_a or {}).get("target_asset") or "").strip().upper()
    if not ta or ta in ("NONE", "OTHER"):
        return out, notes
    out["target_asset"] = ta
    notes.append(f"标的={ta}")
    for k in ("suggested_action", "sentiment_score", "market_category"):
        v = (step_a or {}).get(k)
        if v not in (None, "", []):
            out[k] = v
    for k in _STEP_B_KEYS:
        v = (step_b or {}).get(k)
        if v is None:
            continue
        if isinstance(v, str) and len(v.strip()) < 4:
            continue
        if isinstance(v, (list, dict)) and not v:
            continue
        out[k] = v
        notes.append(k)
    return out, notes


def _pick_main_model_cfg() -> Optional[Dict[str, Any]]:
    """挑一条 main 用途通道（落库前抢救用；不轮询、不记失败）。"""
    for _m in AI_MODEL_ROSTER:
        _purposes = _m.get("purpose") or []
        if "main" in _purposes:
            return _m
    return AI_MODEL_ROSTER[0] if AI_MODEL_ROSTER else None


def should_schema_retry(is_slow: bool, missing_required, allow_slow_retry: bool = True) -> bool:
    """慢通道要不要也做一次 schema 补全重试。

    背景（v342 实测）：`_is_slow_channel()` 把 MiniMax 整条判成慢通道 → schema 补全、
    format-fix、summary 三类重试**在生产通道上全部静默跳过**，
    于是"重试逻辑写了却从不执行"，缺失字段只能落降级。

    这里只放行"再不放行就必然是废数据"的情形：
      - target_asset 缺失（下游只能记 NONE），或
      - 必填缺口 >= 3 个
    其余仍按慢通道跳过，避免每条新闻都多烧一次调用。
    """
    miss = list(missing_required or [])
    if not miss:
        return False
    if not is_slow:
        return True
    if not allow_slow_retry:
        return False
    return ("target_asset" in miss) or (len(miss) >= _SCHEMA_RESCUE_MIN_GAP)


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


def asset_in_core_scope(asset: str) -> bool:
    """该品种是否在核心监控清单内（供兜底文案判定用）。"""
    return str(asset or "").strip().upper() in _CORE_SCOPE_ASSETS


def degraded_reasoning_template(*, asset: str, market_category: str, ticker_tag: str = "",
                                action: str = "HOLD", score_s: str = "未评估",
                                summary_line: str = "", in_scope: Optional[bool] = None,
                                ) -> str:
    """LLM 未返回可用分析时的八段兜底 —— 按"是否核心品种"**说实话**（v336）。

    旧实现（v200）无论标的什么品种，都写死"事件不直接命中本系统核心监控品种"。
    实测 6459 条走了这段兜底，其中 3692 条（57%）标的是 XAU/WTI/BTC/ETH ——
    核心品种被写成"不在监控范围"，于是 30.8% 的 HOLD 被误读成"模型判断无方向"，
    真实原因是 LLM 空输出/被截断。这里只改文案与归因口径，**不碰任何买卖判定**。
    """
    ta = str(asset or "NONE").strip().upper()
    if in_scope is None:
        in_scope = asset_in_core_scope(ta)
    mc = str(market_category or "").strip().upper()
    tick = f"；涉及标的 {ticker_tag}" if ticker_tag and "涉及标的" not in ticker_tag else (f"；{ticker_tag}" if ticker_tag else "")

    if in_scope:
        return (
            f"[总框架] 目标资产 {ta}（market_category={mc}）{tick}；"
            f"本次 **LLM 未返回可用分析**（空输出或解析前被截断），该品种在核心监控清单内，"
            f"此处按「分析降级」记录，**不代表该品种无影响**\n"
            f"[因素定位] 降级原因：LLM 输出为空/截断，未形成八段推演；"
            f"这不是模型判断「无方向」，需要重跑或人工复核后才能定性\n"
            f"[驱动力] 未获得有效传导路径描述 → 不做方向推断，"
            f"也不用兜底文字伪造理由（LLM 原始输出 {action}）\n"
            f"[水位博弈] 未注入/未评估，按中性处理，不抢跑\n"
            f"[跨资产资金] 未评估（无有效 LLM 输出，跨资产联动未知）\n"
            f"[反共识结论] sentiment={score_s}，action={action}（降级记录，非判断）\n"
            f"[逻辑自检] target_asset={ta} ∈ 核心监控清单 → 结论必须记为"
            f"「分析降级」，不得写成「品种不在监控范围」\n"
            f"[总结] {summary_line or f'{ta} 分析降级（LLM 空输出）'}"
        )

    return (
        f"[总框架] 目标资产 {ta}（market_category={mc}）{tick}；"
        f"该标的**不在**核心监控清单（BTC/XAU/WTI/ETH/SOL），按非交易信号处理\n"
        f"[因素定位] 该事件归类为个股/港股/汇总/宏观类等扩展品种，"
        f"本框架不执行自动交易；LLM 输出 {action} 仅作分析记录\n"
        f"[驱动力] 无跨品种资金传导路径到核心监控品种；"
        f"事件本身的资金/情绪影响停留在所属市场，未进入本系统套利监控框架\n"
        f"[水位博弈] 盘面水位无影响（事件与核心品种盘面不相关）\n"
        f"[跨资产资金] 无跨资产联动（事件与核心监控品种无关）\n"
        f"[反共识结论] sentiment={score_s}，action={action}（中性观望，不下单）；"
        f"非交易结论不代表事件无意义，仅表示本框架不执行\n"
        f"[逻辑自检] target_asset={ta} 不在核心监控清单 → 按 HOLD 兜底；"
        f"若用户关注此标的所属市场，请走人工/个股通道\n"
        f"[总结] {summary_line or f'{ta} 不在核心监控清单'}"
    )


def rerender_degrade_text_for_asset(result: Dict[str, Any], asset: str,
                                    *, market_category: Optional[str] = None,
                                    action: Optional[str] = None,
                                    score_s: Optional[str] = None) -> bool:
    """品种被纠正/提升后，把降级文案按**最终品种**重写。

    背景（实测）：168 条核心品种决策里有 93 条（55%）的 reasoning_path 写着
    「目标资产 NONE…该标的不在核心监控清单」，实际品种是 XAU/WTI/BTC。
    根因不是判定错，而是降级兜底先按 NONE 写了文案，随后 ASSETFIX 只改了
    target_asset、没重写文案 → 记录自相矛盾，反思据此会把"LLM 空输出"学成
    "品种不在监控范围"。

    本函数只改文案与归因口径，**不碰 suggested_action / sentiment_score**。
    返回是否改写。
    """
    if not isinstance(result, dict):
        return False
    ta = str(asset or "").strip().upper()
    if not ta or ta in ("NONE", "OTHER"):
        return False
    path = str(result.get("reasoning_path") or "")
    if not path or not any(m in path for m in _DEGRADE_STALE_MARKERS):
        return False

    in_scope = asset_in_core_scope(ta)
    mc = str(market_category if market_category is not None
             else result.get("market_category") or "").strip().upper()
    act = str(action or result.get("suggested_action") or "HOLD").upper()
    sc = score_s
    if sc is None:
        _raw = result.get("sentiment_score")
        sc = f"{float(_raw):+.2f}" if isinstance(_raw, (int, float)) else "未评估"

    result["reasoning_path"] = degraded_reasoning_template(
        asset=ta, market_category=mc, action=act, score_s=str(sc),
        summary_line=f"{ta} 分析降级（LLM 输出为空/截断）", in_scope=in_scope,
    )

    rsn = str(result.get("reasoning") or "")
    # ⚠️ 检测串不能是「不在核心监控清单」——原文里中间隔着 markdown 的 ** 标记
    # （"该标的**不在**核心监控清单"），连写的串永远匹配不上。
    if rsn and "**不在**核心监控清单" in rsn:
        _fix = ("该品种在核心监控清单内，本次按「分析降级」记录（LLM 空输出/截断）"
                if in_scope else "该标的确实不在核心监控清单（BTC/XAU/WTI/ETH/SOL）")
        rsn = rsn.replace(_DEGRADE_STALE_SENTENCE, _fix)
        rsn = rsn.replace("该标的**不在**核心监控清单", _fix)
        result["reasoning"] = rsn
    return True


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
        factor_ontology.log_injection(target_asset, block, mode="batch")
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


def _is_truthy(v: Any) -> bool:
    if isinstance(v, bool):
        return v
    return str(v).strip().lower() in ("true", "1", "yes")


def _news_age_minutes(timestamp: str) -> Optional[float]:
    """新闻发布时间距现在的分钟数（统一按北京时间换算；解析失败返回 None）。

    走 config.parse_time_to_bj：带时区（+08:00/Z/±HH:MM）换算到北京，
    无时区串按北京时间解读——与入库闸同一时区口径，避免差 8 小时。
    """
    dt = config.parse_time_to_bj(timestamp)
    if dt is None:
        return None
    return (datetime.now(config.TZ_SHANGHAI) - dt).total_seconds() / 60.0


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


def _extract_reasoning_steps(reasoning: str) -> List[str]:
    """v200+ P0-2: 提取 reasoning 中出现的「八段步骤」(与 reflection_checkers._ANALYSIS_STEPS 一致)。

    返回找到的步骤名(已去重,按八段顺序)。无 reasoning 返回 []。
    """
    if not reasoning or not isinstance(reasoning, str):
        return []
    try:
        from review.reflection_checkers import _ANALYSIS_STEPS as _R_ANALYSIS_STEPS
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
        return 0.0          # 非法输入 → 中性 0（不能先落 0.0 再走映射，否则会变成 -1）
    if v < 0:
        # (c)：-1~1，中心本来就是 0，原样 clamp
        return round(max(-1.0, v), 4)
    # v336 修复：下面两档的中点都是 0.5（(a) 50 / (b) 0.5），而存库口径是 -1~1、
    # 中点为 0 —— 必须**围绕中点线性拉伸**，否则：
    #   · (a) 40（偏空 40 分）→ 旧代码存成 +0.40（强多）
    #   · (b) 0.3（偏空）    → 旧代码存成 +0.30（偏多）
    # 实测佐证：suggested_action=SELL 的 146 条决策里有 90 条 sentiment 是正数，
    # 方向与动作自相矛盾；BUY 的 243 条则全部为正（因为正数永远不会变负）。
    # 两档的换算其实同一个式子：new = 2*s - 1（s 为旧存值），可用它修历史行。
    if v > 1.0:
        # (a) 0-100，50=中性 —— 必须围绕中点拉伸
        return round(max(-1.0, min(1.0, v / 50.0 - 1.0)), 4)
    # (b) 0-1：**保持原样**。这一档的 0.5 到底是不是"中性"无法验证，
    # 而且 0.0 在这里同时承担"没给分"的语义 —— 一旦按 (0.5 为中) 拉伸，
    # 0.0 会变成 -1.0（极端看空），把"缺省"错当成"最强空头"。
    # 所以只修 (a)：它的中点由 prompt 明确规定（50=中性），且 v>1 时才进来，
    # 不会与"缺省 0"混淆。
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


# ---------------------------------------------------------------------------
# 全网溯源 noi 反推（纯函数）— 历史回填脚本与引擎共用同一份口径
# ---------------------------------------------------------------------------
_CN_NOI_TS_RE = re.compile(
    r"推断时间\s*[:：]\s*((?:19|20)\d{2}[-/年]?\s*\d{1,2}[-/月]?\s*\d{1,2}[日]?"
    r"(?:\s+\d{1,2}:\d{1,2}(?::\d{1,2})?)?)"
)
_CN_NOI_SRC_RE = re.compile(r"来源\s*[:：]\s*([^|｜\n]{2,200})")


def _derive_cross_net_from_noi(noi: str) -> Tuple[str, str]:
    """从 net_origin_info 反推 (cross_net_first_seen_ts, cross_net_source)。

    net_origin_info 有两种来源形态：
      ① LLM 自己写的 "来源:路透社 | 推断时间:2026-10-11 08:07:00"
      ② _extract_cross_net_from_text() 兜底拼的 "推断时间:2026-10-11 08:07:00"（无来源）

    时间：抓 "推断时间:" 后的时间并归一为 'YYYY-MM-DD HH:MM:SS'。
    来源：优先取显式 "来源:XXX"（需 ≥4 字符才够 _validate_cross_net_ts 第⑤层门槛）；
          否则退回整段 net_origin_info —— v176.11 约定：中文机构名常只有 3 字符
          （"路透社"/"财联社"），直接取会被第⑤层拒收，导致时间一起被丢掉。

    两者都可能为空串。纯函数 —— engine 写库与历史回填脚本共用同一份口径，
    避免"脚本算一套、引擎算另一套"。
    """
    noi = (noi or "").strip()[:200]
    if not noi:
        return "", ""

    # ── 时间 ──
    ts = ""
    m = _CN_NOI_TS_RE.search(noi)
    if m:
        raw_ts = m.group(1).strip()
        norm = (
            raw_ts.replace("年", "-").replace("月", "-").replace("日", "").replace("/", "-")
        )
        norm = re.sub(r"\s+", " ", norm).strip()
        parts = norm.split()
        if parts:
            date_segs = parts[0].split("-")
            if len(date_segs) == 3:
                date_segs = [s.zfill(2) if i > 0 else s for i, s in enumerate(date_segs)]
                parts[0] = "-".join(date_segs)
            norm = " ".join(parts)
        dt = None
        for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d"):
            try:
                dt = datetime.strptime(norm, fmt)
                break
            except ValueError:
                continue
        if dt is not None:
            if " " not in norm:
                ts = dt.strftime("%Y-%m-%d 00:00:00")
            elif norm.count(":") == 1:
                ts = norm + ":00"
            else:
                ts = dt.strftime("%Y-%m-%d %H:%M:%S")

    # ── 来源 ──
    src = ""
    ms = _CN_NOI_SRC_RE.search(noi)
    if ms:
        named = ms.group(1).strip()
        if len(named) >= 4:
            src = named
    if not src and len(noi) >= 4:
        src = noi
    return ts[:32], src[:300]


def _is_resolved_asset(asset: str) -> bool:
    """标的是不是"真实可查"的（不是 NONE / 桶标签 / 空）。

    只用于降级原因码的区分，不做任何路由判断。
    """
    a = str(asset or "").strip().upper()
    if a in ("", "NONE", "OTHER", "NULL"):
        return False
    try:
        from asset_kinds import classify
        kind, _ = classify(a)
        return kind in ("us", "a_share", "hk", "non_equity", "other_market")
    except Exception:
        return False


def classify_analysis_degraded(reasoning_path: str, no_trade_reason: str = "",
                               asset: str = "") -> tuple:
    """把"没分析"与"判断为观望"分开（纯函数）。

    返回 (flag, reason)。flag=1 表示这条决策其实是降级兜底，不构成判断。
    只做记账分类，不改任何方向/动作。

    v409: 加 `asset`。实测 126 条 `reason='target_none'` 的行 target_asset 却是
    XAU / WTI / NVDA —— 模型当时确实把标的判成 NONE，但路由后来补上了代码。
    这类再叫 "target_none" 会让人以为是"这条新闻没有标的"，读错整张 S18/S16。
    单列 `asset_late_bound`：**降级仍成立**（分析是对着 NONE 做的），但原因说的是
    "模型没定标的、路由事后补"，不是"世界没有标的"。
    """
    p = str(reasoning_path or "")
    r = str(no_trade_reason or "")
    if "目标资产 NONE" in p:
        if _is_resolved_asset(asset):
            return 1, "asset_late_bound"
        return 1, "target_none"
    if "未返回可用分析" in p:
        return 1, "llm_empty"
    # 异常兜底（模型调用抛异常后拼出的 "xxx 异常降级: RuntimeError" 类文案）。
    # 与 llm_empty 同档：没有可用分析，置信度按占位处理，不构成判断。
    if "异常降级" in p or "异常降级" in r:
        return 1, "llm_empty"
    # 服务端模板的措辞会变：实测有"服务端兜底"/"服务端回灌"/"服务端仅回灌"三种写法，
    # 用连写串匹配会漏（"仅"字夹在中间）。改为两个词分开判。
    if "服务端" in p and ("回灌" in p or "兜底" in p):
        return 1, "server_fallback"
    # "只有 reasoning 摘要"（LLM 真东西摘要）不是八段推演，同样不算判断。
    if "LLM 真东西摘要" in p:
        return 1, "summary_only"
    if "LLM 源降级" in r:
        return 1, "source_degraded"
    if any(t in p for t in ("按分析降级", "未形成八段推演")):
        return 1, "degraded_text"
    return 0, None


def _parse_result_light(raw: str) -> Dict[str, Any]:
    """从模型原文里抠出第一个 JSON 对象（轻量版，不跑 sanitize）。

    只用于两步通道的中间结果：拿 target_asset 等少数键，不需要 20 字段兜底，
    也不能触发 presanitize 的降级文案（否则"补"和"判"互相污染）。
    """
    t = strip_think_blocks(str(raw or ""))
    t = re.sub(r"^```(?:json)?", "", t.strip(), flags=re.IGNORECASE)
    t = t.replace("```", "").strip()
    start = t.find("{")
    if start < 0:
        return {}
    depth = 0
    for i in range(start, len(t)):
        if t[i] == "{":
            depth += 1
        elif t[i] == "}":
            depth -= 1
            if depth == 0:
                try:
                    return json.loads(t[start:i + 1])
                except Exception:
                    return {}
    return {}


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


def _fallback_business_knowledge_drivers(target_asset: str,
                                         allow_generic: bool = False) -> Optional[List[str]]:
    """L2 业务知识兜底: 从 causal_graph 取该品种**已学到**的驱动列表.

    Returns: 驱动列表(如 ["美元 DXY 反向", "实际利率反向"]) 或 None.

    来源:
      - 已学习的: causal_graph.get_knowledge(asset) -> core_factors + condition_factors
      - 未学习的: **默认不再编造** (allow_generic=False)。v176.2 的硬编码通用宏观框架
        挂在 inject_block 兜底里, 它对该品种没有任何样本证据; 旧实现在 L2 直接把它
        当业务知识写进 factor_ranking, 属于"用套话冒充知识"。要显式传
        allow_generic=True 才会走这条(目前仅供人工排障/对照)。
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
    if not allow_generic:
        # v408: 没学到就是没学到 —— 不拿通用框架冒充该品种的知识
        return None
    # 兜底: 未学到时用通用驱动结构(inject_block 内部已含兜底)
    # v310: 但**只对白名单内的真实品种**用。inject_block 的兜底是硬编码的通用宏观
    # 框架 (美元DXY反向 / 实际利率反向 ...), 对一个根本不存在的品种 (如 'ZZZ_UNKNOWN')
    # 来说等于**编造因素** —— v200 的 X1"假数据禁止"要求这种情况留空 (L5)。
    try:
        from market.market_classifier import TARGET_TO_MARKET
        if target_asset not in TARGET_TO_MARKET:
            return None
    except Exception:
        pass
    try:
        block = causal_graph.inject_block(target_asset, top=4)
        factor_ontology.log_injection(target_asset, block, mode="l2")
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


_TIGHT_STAGE_JOIN_RE = re.compile(r"\]\s*[→>\-—]\s*\[")


def _is_eight_stage_skeleton(path: str) -> bool:
    """空骨架判定：段名标记被 →/->/— 紧密连写 ≥5 处（正常输出不会这样连写）。"""
    return bool(path) and len(_TIGHT_STAGE_JOIN_RE.findall(path)) >= 5


def _main_prompt(kind: str, default: str) -> str:
    """主 prompt 取值：页面编辑过的（ai_main_prompts 覆盖表，5s TTL）优先，否则代码内置默认。

    kind: system_prompt / json_force_prompt / market_structure_block / doubao_prompt
    """
    try:
        from ai_prompts import load_main_prompt_overrides
        return load_main_prompt_overrides().get(kind) or default
    except Exception:
        return default

# ═══════════════════════════════════════════════════════════════════════
# 八段/逐品种解析簇（2026-11-19 第三批外迁）：纯文本/正则，不触库、不持模块状态。
# stage 正则与 _FOCUS_GATE_PREFIXES 派生量随簇迁入；ai_worker import * 接回。
# ═══════════════════════════════════════════════════════════════════════
_STAGE_MARK_RE = re.compile(
    r"(总框架|因素定位|资产性质|因素排行|盘面结构分析|盘面结构|驱动力|水位博弈|跨资产资金|反共识结论|逻辑自检|总结)"
    r"(?=[\s]*[:：—\-]|[\s]*[\]】]|[\r\n]|\s*$)"
)

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

# 降级前缀元组（config 角色前缀 + 失败前缀派生），与主模块同口径。
_FOCUS_GATE_PREFIXES = tuple(_FOCUS_GATE_PREFIX_BY_ROLE.values()) + (_FAILED_GATE_PREFIX,)

# ═══════════════════════════════════════════════════════════════════════
# 中文溯源/因素/失效条件兜底簇（2026-11-19 第四批外迁）：纯解析，不触库、不持状态。
# ═══════════════════════════════════════════════════════════════════════
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
            # v342: 降级模板里的整句会被当成"因素"写进 factor_ranking
            # （实测出现 factor='该标的**不在**核心监控清单'）—— 这是口径噪声，不是因素。
            if any(t in cand for t in _DEGRADE_TEXT_MARKS):
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

    # ── L2 业务知识兜底: **只认已学到的因果图谱驱动** ──
    # v408: 旧实现允许退回到 inject_block 的"通用硬编码结构"(美元DXY反向/实际利率反向),
    # 于是任何品种都能拿到 4 条一模一样的"驱动" —— 那不是学到的知识, 是套话, 却被打上
    # business_knowledge 的标。宁可留空(L5), 也不把通用框架当该品种的驱动写进 factor_ranking。
    drivers = _fallback_business_knowledge_drivers(target, allow_generic=False)
    if drivers:
        return [{"factor": d[:200], "rank": i, "direction": direction}
                for i, d in enumerate(drivers[:5], 1)]

    # ── L5 留空: 不写假字符串, 让上游知道是缺失 ──
    return []

_CN_TS_RE = re.compile(r"^(\d{4})-(\d{2})-(\d{2})[ T](\d{2}):(\d{2}):(\d{2})$")

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
    #   v317 fix：改用 time_utils.parse_ts_to_bj（北京时间唯一入口）。
    #   旧写法用 time.mktime((y,mo,d,...))，它按**本机 localtime** 解释裸元组 ——
    #   本机 TZ 若不是 +08:00，判定就会整体偏移 8 小时（实测机器 TZ=CST 恰好正确，
    #   但换台机器/换 TZ 就静默错判）。裸串按北京解读是项目统一约定（time_utils 文件头）。
    candidate_dt = time_utils.parse_ts_to_bj(f"{y:04d}-{mo:02d}-{d:02d} {h:02d}:{mi:02d}:{s:02d}")
    if candidate_dt is None:
        return ""
    if candidate_dt > datetime.now(time_utils.BJ) + timedelta(seconds=60):
        return ""

    # ④不能晚于自己入库时间 + 60s 容差 → 拒（事件不可能在入库后发生）
    #   v317 fix：同样走 time_utils.parse_ts_to_bj。
    #   旧写法 strptime(our_ts_iso[:19], "%Y-%m-%d %H:%M:%S") 对真实落库格式
    #   '2026-10-11T16:05:15+08:00'（T 分隔 + 偏移）**必然解析失败** → except 分支
    #   直接放行 → 这道"时间不能晚于入库"的护栏实际上一直处于**失效**状态
    #   （实测 319 行 cn_ts 晚于入库数小时仍被当作有效）。
    if isinstance(our_ts_iso, str) and our_ts_iso:
        our_dt = time_utils.parse_ts_to_bj(our_ts_iso)
        if our_dt is not None and candidate_dt > our_dt + timedelta(seconds=60):
            return ""

    # ⑤source 必须 ≥4 字符（精确时间必须有依据）
    src = (source or "").strip()
    if len(src) < 4:
        return ""

    # 标准化：T 分隔 → 空白分隔
    return f"{y:04d}-{mo:02d}-{d:02d} {h:02d}:{mi:02d}:{s:02d}"

_CN_TS_PATTERNS = [
    # 2026年9月19日 22:00 / 2026年9月19日22时 / 2026年9月19日22时00分
    re.compile(r"(\d{4})\s*年\s*(\d{1,2})\s*月\s*(\d{1,2})\s*日\s*(\d{1,2})\s*[::时]\s*(\d{1,2})(?:\s*分)?"),
    # 2026-09-19 22:00 / 2026/09/19 22:00
    re.compile(r"(\d{4})[-/](\d{1,2})[-/](\d{1,2})[ T](\d{1,2})[::时](\d{1,2})"),
    # 2026年9月19日（无时间） → 取 00:00
    re.compile(r"(\d{4})\s*年\s*(\d{1,2})\s*月\s*(\d{1,2})\s*日"),
    # 9月19日 22:00 / 9月19日22时
    re.compile(r"(\d{1,2})\s*月\s*(\d{1,2})\s*日\s*(\d{1,2})\s*[::时]\s*(\d{1,2})"),
    # ── v176.9 fix：N月N日 不带时分（"金十数据9月23日讯"）→ 取 00:00 ──
    # 之前只匹配带时分的"X月X日 22:00"，对中文财经媒体最常见的"N月N日讯"格式漏抽
    # v176.10 fix：负向后瞻 (?<![年\d]) 排除"1989年1月1日"这类"年份+月日"里的"月日"
    #   独立补当前年——带年份的已由 pattern 0/2 处理（早于 1990 会被拒），不能被这里
    #   拆出的"1月1日"绕过限制变成"2026-01-01"。
    re.compile(r"(?<![年\d])(\d{1,2})\s*月\s*(\d{1,2})\s*日"),
]

_CN_SRC_PATTERNS = [
    # 高信度来源短语（捕获前面 2-12 字的机构名）
    re.compile(_CN_ORG_CHARS + r"\s*(?:官网|官方)\s*(?:新闻稿|公告|声明)"),
    # 排除"紧急/立即/即将"等中文动词，避免贪婪匹配"美联储紧急宣布"时吞进"紧急"
    re.compile(_CN_ORG_CHARS + r"(?!\s*(?:紧急|立即|即将|正在|已经|已\s+))(宣布|发布|公布|披露)"),
    re.compile(r"(?:据|根据)\s*(" + _CN_ORG_CHARS_NODIGIT[1:-1] + r")\s*(?:报道|消息|消息人士|新闻|快讯|通讯社|电)"),
    re.compile(r"(" + _CN_ORG_CHARS_NODIGIT[1:-1] + r")\s*(?:电|报道|消息)"),
    re.compile(r"(" + _CN_ORG_CHARS_NODIGIT[1:-1] + r")\s*官方网站"),
    re.compile(r"(" + _CN_ORG_CHARS_NODIGIT[1:-1] + r")\s*新闻稿"),
    re.compile(r"(" + _CN_ORG_CHARS_NODIGIT[1:-1] + r")\s*官方声明"),
    re.compile(r"(" + _CN_ORG_CHARS_NODIGIT[1:-1] + r")\s*(?:通讯社|电视台|广播|网)"),
    re.compile(r"(" + _CN_ORG_CHARS_NODIGIT[1:-1] + r")\s*Press"),
    # ── v176.9 fix：中文财经媒体"X讯/电"结尾（金十数据/财联社/东方财富等）──
    # 必须用 NODIGIT 版本，否则贪婪匹配"金十数据9月23日"整段
    # 允许 "金十数据9月23日讯" 这种 "机构名 + 日期 + 讯/电" 复合结构
    # v176.10 fix：org 用非贪婪 {2,12}?，避免 "[来自金十数据快讯]" 里"快"被吞进
    #   机构名（贪婪会得到"金十数据快"，不以"数据"结尾被 org_suffixes 拒收，
    #   导致 feishu_jin10 的"[来自金十数据快讯]"结尾漏抽首发平台）。
    re.compile(r"([\u4e00-\u9fa5社A-Za-z·\-]{2,12}?)\s*(?:\d{1,2}\s*月\s*\d{1,2}\s*日)?\s*(?:讯|电|消息|快讯)"),
    # 英文机构名（直接捕获）
    re.compile(r"(Reuters|Bloomberg|CNBC|Nikkei|Xinhua|CCTV)"),
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

# ═══════════════════════════════════════════════════════════════════════
# 两步抢救预算限流（2026-11-19 第五批外迁）：纯状态机/纯函数，不触库。
# ═══════════════════════════════════════════════════════════════════════
_RESCUE_LIMIT_PER_WINDOW = int(os.getenv("TRIDENT_TWO_STEP_RESCUE_LIMIT", "10") or 10)

_RESCUE_NONE_LIMIT_PER_WINDOW = int(
    os.getenv("TRIDENT_TWO_STEP_RESCUE_NONE_LIMIT", "3") or 3)

def rescue_budget_ok(now_ts: float, state: Dict[str, Any],
                     limit: int = None, window: float = None) -> bool:
    """两步抢救的滑动窗口限流（每 10 分钟最多 N 条）。

    为什么需要：每条抢救要多 2 次调用（约 +45~90s），不限流时一个 8 条的批次会被
    拖到十几分钟，后面的新闻全排队等。这里只限制"抢救"，正常分析不受影响。
    """
    lim = _RESCUE_LIMIT_PER_WINDOW if limit is None else int(limit)
    win = _RESCUE_WINDOW_SEC if window is None else float(window)
    start = float(state.get("window_start") or 0.0)
    if start <= 0 or (now_ts - start) >= win:
        state["window_start"] = now_ts
        state["used"] = 0
    if int(state.get("used") or 0) >= lim:
        return False
    state["used"] = int(state.get("used") or 0) + 1
    return True

def rescue_budget_state_for(target_asset: Any) -> tuple:
    """v398：按标的返回 (预算状态, 上限)。

    NONE 类走独立窗口（默认 3/10min），有标的的仍用原来的 10 条窗口 ——
    避免"一批 8 条 NONE"把真正可救的额度吃干（实测日志 窗口已用 6/10…9/10）。
    """
    if is_none_target(target_asset):
        return _RESCUE_NONE_STATE, _RESCUE_NONE_LIMIT_PER_WINDOW
    return _RESCUE_STATE, _RESCUE_LIMIT_PER_WINDOW

# ── 多模型共识投影（纯聚合，2026-11-19 第六批外迁）──
def _build_extra_models_consensus(peer_reviews: Any, main_action: str = "") -> str:
    """把同行评审投影成 ai_decisions.extra_models_consensus（legacy dict 形态）。

    形态: {"<模型标签>": {"agree": bool, "action": ..., "score": ...,
                          "reasoning": ..., "model_id": ...}}

    · agree = 该评审 action 与**主模型 action 一致**（主模型 action 未知 → 无法判断
      一致性 → 返回 "" 不写，让 _consensus_mult 走 fail-closed 1.0，绝不虚高）；
    · 同一模型族只保留一条（_consensus_mult 的 dict 形态不做族去重，
      这里先去重，避免"一个模型自己问自己两次"被当成两票）；
    · 返回 "" 表示没有可用的跨模型评审 → 调用方写 NULL。

    v314 P0：这个字段此前**全库为空**（生产只写 peer_reviews），
    验收 #4 要求 extra_models_consensus 有内容才算"多模型共识真的采了"。
    """
    if not peer_reviews:
        return ""
    try:
        from signals.signal_weight_evaluator import _model_family as _fam
    except Exception:
        def _fam(x):
            m = str(x or "").strip().lower()
            return m.rsplit("/", 1)[-1] if "/" in m else m
    main = str(main_action or "").strip().upper()
    if not main:
        return ""
    out: Dict[str, Any] = {}
    seen = set()
    for r in peer_reviews:
        if not isinstance(r, dict):
            continue
        action = str(r.get("action") or "").strip().upper()
        if not action:
            continue
        fam = _fam(r.get("model_id") or r.get("model_label"))
        if fam:
            if fam in seen:
                continue
            seen.add(fam)
        label = str(r.get("model_label") or r.get("model_id") or "reviewer")
        out[label] = {
            "agree": bool(action == main),
            "action": action,
            "score": r.get("score"),
            "reasoning": (r.get("reasoning") or "")[:300],
            "model_id": r.get("model_id") or label,
        }
    if not out:
        return ""
    try:
        return json.dumps(out, ensure_ascii=False)
    except Exception:
        return ""

# ── 门卫四步先验拼装（纯字符串，2026-11-19 第七批外迁）──
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
    if main_asset and main_asset.upper() in ("BTC", "XAU", "WTI", "ETH", "SOL"):
        # v451：门卫已锁定池内主品种时，主分析不得再判 NONE/OTHER。
        # 实测 21:30 一批 8 条里"美、布两油走低""沃勒称可能加息50bp"全被判
        # 非核心 → routing_miss，LLM 明明拿到了 [门卫四步] 却没照用。
        parts.append(f"硬约束: target_asset 必须填 {main_asset}，禁止 NONE/OTHER")
    return "[门卫四步] " + "｜".join(parts)
