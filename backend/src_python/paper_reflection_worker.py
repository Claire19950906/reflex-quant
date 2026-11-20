#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""模拟实盘反思 worker —— v186 第二层反思基础设施。

职责:
  - 每 5min 扫 quick_sim_trades 中 settled=1 + ai_decisions.paper_trading_run_id IS NOT NULL
    的记录 (P1 改造：以 quick_sim_trades 为结算权威 JOIN ai_decisions 取决策上下文)
  - 对每个未生成 trade_reflections_v2 的决策,生成三件套反思
  - 落库 trade_reflections_v2,触发经验自适应

执行:
  - main() 一次性扫并补漏
  - daemon_loop() → 每 N 秒轮询(N 由 env PAPER_REFLECT_INTERVAL_S, 默认 300s=5min)
"""
from __future__ import annotations

import json
import re
import time
import os
import sqlite3 # v309 P0-1: _resolve_dimension_for_decision 的类型标注
from typing import Any, Dict, List, Optional

import db

# v309 P1: 统一时间口径 (唯一入口, 详见 time_utils.py 文件头)
from common import time_utils

_DEDUPE_SELECT = """
    SELECT a.id AS decision_id, a.target_asset, a.suggested_action, a.sentiment_score,
           a.event_type_code, a.event_strength, a.impact_score, a.heat_score,
           a.attention_score, a.direct_catalyst, a.market_confirmation,
           qs.entry_price AS entry_price, qs.exit_price AS exit_price,
           qs.pnl_pct AS forward_pnl, qs.mfe_pct, qs.mae_pct,
           qs.verdict AS is_correct, qs.settled AS settled,
           qs.trade_uuid AS trade_uuid,
           a.created_at, a.reasoning, a.reasoning_path,
           a.regime_at_decision,
           a.factor_ranking, a.event_struct, a.persona_weight, a.persona_match_id,
           a.paper_trading_run_id,
           -- v200 P0+ 完整性 + 字段来源追溯
           a.completeness_score, a.field_sources, a.missing_fields,
           a.llm_field_count, a.rule_field_count,
           a.business_kb_field_count, a.historical_field_count,
           -- v200+ Q6: signal_quality_v200 8 类规则所需字段
           a.market_category, a.is_sibling, a.hard_data_evidence,
           a.asset_nature, a.parent_id,
           -- v398: 降级决策不产出洞察（它没有真分析, 反思内容只是"没看见"）
           a.analysis_degraded,
           -- v482: 决策的代码代次, 反思行继承它 (旧代教训才能失效)
           a.code_fingerprint,
           -- JOIN raw_news 拿 fingerprint / first_seen_ts / content
           n.content_fingerprint, n.first_seen_ts, n.content AS news_content,
           -- parent decision event_type 用于 sibling event_type 比对
           (SELECT event_type_code FROM ai_decisions
             WHERE id = a.parent_id LIMIT 1) AS parent_event_type
      FROM ai_decisions a
      JOIN quick_sim_trades qs ON qs.decision_id = a.id
 LEFT JOIN trade_reflections_v2 t ON t.decision_id = a.id
 LEFT JOIN raw_news n ON n.id = a.news_id
     WHERE a.paper_trading_run_id IS NOT NULL
       AND qs.settled = 1
       AND t.id IS NULL
       -- v200 P0+ 严格过滤: completeness_score 太低的不入深度反思,
       -- 留不下"基于脏数据的经验污染反思系统". 默认阈值 0.5.
       AND COALESCE(a.completeness_score, 1.0) >= 0.5
  ORDER BY a.id ASC
     LIMIT 50
"""

_LAYER_MAP = {
    "BUY":  ("filter", "direction", "trend_buy"),
    "SELL": ("filter", "direction", "trend_sell"),
    "HOLD": ("analysis", "decision", "no_trade"),
}


# ─────────────────────────────────────────────────────────────────────────────
# v232.5 反思数字落地 — 量化锚点计数
# 让 root_cause / lesson 不再是空话模板。用户反馈 2026-10-06:
#   「反思系统的反思不够深入,没有抓到问题的核心本质」—— 直接表现就是
#   root_cause 只有"事件强度错判"这种文字判断,没有 pnl 量级、盈亏比、
#   ATR、同 layer 历史胜率等可验证的数字。
#
# 锚点分类(每类最多算 1 次,总分封顶 5):
#   1. 具体 pnl 量级 (pnl=+0.0123 / -0.05%)
#   2. 具体价格/百分比 (XAU=4147 / 0.16%)
#   3. MFE/MAE 区间 (mfe=0.8% mae=-0.5%)
#   4. 同 layer 历史统计 (近 30 条 / 命中率 / 盈亏比)
#   5. 反事实/跨资产预期 (反向 pnl=+0.02 / 与 BTC 相关 r=+0.4)
# ─────────────────────────────────────────────────────────────────────────────
_PNL_RE = re.compile(r"pnl\s*[=≈]\s*[+\-]?\d+\.?\d*")
_PCT_RE = re.compile(r"[+\-]?\d+\.?\d+\s*%")
_PRICE_RE = re.compile(r"(?:=|≈|at|@\s*)\s*\d{2,6}(?:\.\d+)?")
_MFE_MAE_RE = re.compile(r"(?:mfe|mae)\s*[=:≈]?\s*[+\-]?\d+\.?\d*\s*%?")
_HIST_RE = re.compile(r"(?:近|过去|过去\s*\d+|近\s*\d+)\s*(?:条|笔|次|天)|命中率|盈亏比|hit_rate|胜率")
_CROSS_RE = re.compile(r"反向|反事实|counterfactual|跨资产|cross[-_]asset|相关[系数]?|[=:]\s*[+\-]?0\.\d+|r\s*=")


def _count_evidence_anchors(text: str) -> int:
    """统计反思文本里的量化锚点数 [0, 5]。空字符串 → 0。"""
    if not text:
        return 0
    score = 0
    if _PNL_RE.search(text):
        score += 1
    if _PRICE_RE.search(text) or _PCT_RE.search(text):
        score += 1
    if _MFE_MAE_RE.search(text):
        score += 1
    if _HIST_RE.search(text):
        score += 1
    if _CROSS_RE.search(text):
        score += 1
    return min(score, 5)


def _compute_reflection_self_score(evidence: float, completeness: float, depth: int) -> float:
    """反思自评可信度 [0, 1]。
    0.4*evidence/5 + 0.3*completeness + 0.3*depth/10
    < 0.3 反思基本不可信(数据残缺), > 0.7 高可信度。
    """
    try:
        ev = max(0.0, min(5.0, float(evidence))) / 5.0
    except Exception:
        ev = 0.0
    try:
        cp = max(0.0, min(1.0, float(completeness)))
    except Exception:
        cp = 0.0
    try:
        dp = max(0.0, min(10.0, float(depth))) / 10.0
    except Exception:
        dp = 0.0
    score = round(0.4 * ev + 0.3 * cp + 0.3 * dp, 4)
    # v482: 一个数字锚点都没有的反思, 自评分不能超过 0.3。
    # 旧口径只看"字段齐不齐 + depth 公式分", 模板空话也能拿高分; 现在先把
    # "没有任何可验证数字"的条目压到不可信区间, 再谈其它权重。
    if evidence <= 0:
        score = min(score, 0.3)
    return score


def _is_sibling_decision(d: Dict[str, Any]) -> int:
    """1 = 同 parent_id / 同 content_fingerprint / 同 1 分钟 first_seen_ts 衍生。
    0 = 独立决策(默认)。
    用于反思统计时把同批次合并到独立桶,避免假多样性放大胜率/失败率。
    """
    try:
        if d.get("parent_id"):
            return 1
        # content_fingerprint 同 hash → 同新闻衍生(已在 _DEDUPE_SELECT 里 join raw_news)
        fp = (d.get("content_fingerprint") or "").strip()
        if fp and d.get("first_seen_ts"):
            conn = db.get_connection()
            try:
                row = conn.execute(
                    "SELECT COUNT(*) FROM ai_decisions a "
                    "LEFT JOIN raw_news n ON n.id = a.news_id "
                    "WHERE n.content_fingerprint = ? "
                    "  AND a.id != ? "
                    "  AND ABS(strftime('%s', a.created_at) - ?) < 60 "
                    "  AND a.id IS NOT NULL",
                    (fp, int(d.get("decision_id") or 0),
                     int(d.get("first_seen_ts") or 0)),
                ).fetchone()
                if row and int(row[0] or 0) > 0:
                    return 1
            finally:
                try:
                    conn.close()
                except Exception:
                    pass
        return 0
    except Exception:
        return 0


def _classify_root_cause(d: Dict[str, Any]) -> Dict[str, str]:
    """v188: 用 reflection_ontology.infer_layer_sub_signal 替代旧的 3 类线性码。

    输入: ai_decisions 行 (dict)
    输出: {"layer", "sub_layer", "signal_name", "root_cause", "lesson", "verification"}

    内部流程:
      1. 调 ontology.infer_layer_sub_signal(d) 反推精确三层标签
      2. ontology.get_signal_template() 取 (root_cause/lesson/verification) 模板
      3. WIN 时附加正向反馈; LOSS/PARTIAL 时用模板

    v200 P0+: 严格完整性过滤. completeness_score < 0.5 时不入深度分析.
      顶层 source_breakdown/completeness_at_reflection 写到 trade_reflections_v2,
      让 daemon_loop 也能看到本条反思是基于什么数据质量的.
    """
    from review import reflection_ontology as _onto

    # v200+ Q6+: 为 event_logic/concept_match 预加载资产驱动因果图谱节点。
    # rule 32 (concept_misaligned) 需要比较 factor_ranking 概念与 asset_driver_nodes
    # 核心因素, 若有 ≥ 1 个因子名字与 CG 因素重叠才算"对齐". 加载失败 / 没驱动节点
    # 时降级到「unknown」, 不影响其他规则.
    _tgt_asset = str(d.get("target_asset") or "").upper()
    if _tgt_asset and "asset_knowledge" not in d:
        try:
            import causal_graph as _cg
            d["asset_knowledge"] = _cg.get_knowledge(_tgt_asset)
        except Exception:
            d["asset_knowledge"] = {"core_factors": [], "condition_factors": []}

    inferred = _onto.infer_layer_sub_signal(d)
    layer = inferred["layer"]
    sub_layer = inferred["sub_layer"]
    signal_name = inferred["signal_name"]
    tmpl = _onto.get_signal_template(layer, sub_layer, signal_name)

    action = str(d.get("suggested_action") or "HOLD").upper()
    pnl = d.get("forward_pnl") or 0.0
    is_correct = str(d.get("is_correct") or "").upper()
    pnl_val = float(pnl) if isinstance(pnl, (int, float)) else 0.0

    # v232.5: 数字落地片段(pnl/mfe/mae/方向判定)
    mfe = d.get("mfe_pct")
    mae = d.get("mae_pct")
    mfe_s = f"mfe={float(mfe):+.4f}" if isinstance(mfe, (int, float)) else ""
    mae_s = f"mae={float(mae):+.4f}" if isinstance(mae, (int, float)) else ""
    pnl_s = f"pnl={pnl_val:+.4f}"
    direction_verdict = "对" if (
        (action == "BUY" and pnl_val > 0) or
        (action == "SELL" and pnl_val < 0)
    ) else "错"

    # v482: 反事实口径(全文只算一次) —— 反向机械反演必须扣两次往返成本。
    # 旧写法 `-pnl` 把成本加回去一次, 会让"反向做就能赚"的错觉进 lesson。
    cost_pct = 0.0
    try:
        import cost_model as _cm
        cost_pct = float(_cm.round_trip_cost_bps(_tgt_asset or "")) / 100.0
    except Exception:
        cost_pct = 0.0
    cost_bps_i = int(round(cost_pct * 100))
    cf_gross_rev = -(pnl_val + cost_pct) if isinstance(pnl, (int, float)) else None
    cf_net_rev = -(pnl_val + 2.0 * cost_pct) if isinstance(pnl, (int, float)) else None

    # v232.5: 同 layer 历史命中率(锚点 4)
    layer_hitrate_s = ""
    try:
        conn = db.get_connection()
        try:
            row = conn.execute(
                "SELECT n_total, n_win, n_loss FROM decision_layer_signals "
                "WHERE layer = ? AND sub_layer = ? AND signal_name = ? "
                "  AND (asset = ? OR asset = '*') "
                "ORDER BY (asset = ?) DESC LIMIT 1",
                (layer, sub_layer, signal_name, _tgt_asset, _tgt_asset),
            ).fetchone()
            if row and int(row[0] or 0) > 0:
                hr = round(int(row[1] or 0) / int(row[0]), 4)
                layer_hitrate_s = (f"近 {int(row[0])} 条同 layer 命中率 {hr*100:.1f}% "
                                   f"(W{int(row[1] or 0)}/L{int(row[2] or 0)})")
        finally:
            try:
                conn.close()
            except Exception:
                pass
    except Exception:
        pass

    # v232.5: 跨资产锚点(锚点 5)
    cross_s = ""
    try:
        conn = db.get_connection()
        try:
            row = conn.execute(
                "SELECT cross_asset_sync_score, kline_pattern FROM trade_reflections_v2 "
                "WHERE asset = ? AND decision_id != ? AND enrich_status = 'ok' "
                "ORDER BY id DESC LIMIT 1",
                (_tgt_asset, int(d.get("decision_id") or 0)),
            ).fetchone()
            if row and row[0] is not None:
                cs = float(row[0])
                pat = str(row[1] or "")
                cross_s = f"跨资产同步 r={cs:+.2f} K线形态={pat or 'n/a'}"
        finally:
            try:
                conn.close()
            except Exception:
                pass
    except Exception:
        pass

    if is_correct == "WIN" or (isinstance(pnl, (int, float)) and pnl > 0):
        # WIN: 模板 + 数字锚点
        root = (f"{tmpl['root_cause']} → 本次 {action} 顺势兑现({pnl_s} | "
                f"方向判定 {direction_verdict} | {mfe_s} {mae_s})。"
                + (f" 对照: {layer_hitrate_s}。" if layer_hitrate_s else ""))
        lesson = (f"{tmpl['lesson']} → 在相似条件下保留 {action} 路径(weight +0.05)。"
                  f"参考 {pnl_s}。"
                  + (f" {cross_s}。" if cross_s else ""))
        verification = tmpl.get("verification", "回放近 30 条同 layer 信号 hit_rate ≥ 60% 即有效")
    elif is_correct == "LOSS" or (isinstance(pnl, (int, float)) and pnl < 0):
        # LOSS: 模板 + 数字锚点(方向错/弱)
        root = (f"{tmpl['root_cause']} | hint: {inferred.get('root_cause_hint', '')}。"
                f"本次 {action} 失败({pnl_s} | 方向判定 {direction_verdict} | "
                f"{mfe_s} {mae_s})。"
                + (f" 对照: {layer_hitrate_s}。" if layer_hitrate_s else ""))
        # v482 反事实口径修正(锚点 5):
        # 原来写 `反向预期 pnl≈{-pnl}` —— pnl 已是扣费后的净值, 再取负等于把往返
        # 成本**加回去一次**(13093 那条: 净 -0.4041 → 文案 +0.4041, 凭空多出 0.4%),
        # 读起来像"只要反向做就能赚", 实际反向机械反演也是亏的。
        # 正确口径: 反向毛利 = -(净pnl + 成本), 反向净值 = -(净pnl + 2*成本)。
        counterfactual_s = ""
        if cf_net_rev is not None:
            counterfactual_s = (f"机械反演净值≈{cf_net_rev:+.4f}"
                                f"(已扣往返{cost_bps_i}bp, 反向同样亏, 不是可执行方案)")
        if isinstance(pnl, (int, float)) and pnl < -0.005:
            lesson = (f"{tmpl['lesson']} → 权重应下调(-0.1, 下限 0.1)。"
                      f"参考 {pnl_s} | {counterfactual_s} | {mae_s}。"
                      + (f" {cross_s}。" if cross_s else ""))
        else:
            lesson = (f"{tmpl['lesson']} → 中等亏损, 暂不下调权重。"
                      f"参考 {pnl_s} | {mae_s}。")
        verification = tmpl.get("verification", "回放同 layer 触发率 ≤ 35% 即有效")
    else:
        # 未结算: 没数字可挂, 锚点 = 0(空话)
        root = "未结算/未标记正确性, 等 settle 后补分析。"
        lesson = "等待 settle=1 时立即跑 worker。"
        verification = "settled=1 时补跑本条反思"

    cf_json = ""
    if cf_net_rev is not None:
        cf_json = json.dumps({
            "basis": "mechanical",          # 机械反演, 不是"本可赚到"的收益
            "gross_rev_pct": round(cf_gross_rev, 6),
            "net_rev_pct": round(cf_net_rev, 6),
            "cost_bps": cost_bps_i,
            "note": "反向机械反演, 已扣往返成本; 仅作锚点, 不作为可执行收益",
        }, ensure_ascii=False)

    return {
        "layer": layer, "sub_layer": sub_layer, "signal_name": signal_name,
        # v190: ontology_signal 拼合列 — 与 error_layer/sub_layer/signal 同步,
        # 便于单列 WHERE / GROUP BY。三字段任一为空时降级用 layer 兜底。
        "ontology_signal": f"{layer}/{sub_layer}/{signal_name}" if (layer and signal_name) else "unknown",
        "root_cause": root, "lesson": lesson, "verification": verification,
        "counterfactual": cf_json,          # v482: 结构化反事实(毛/净/成本/口径)
    }


def _depth_score(d: Dict[str, Any], rc: Dict[str, str]) -> int:
    score = 0
    if d.get("event_struct"): score += 2
    if d.get("factor_ranking"): score += 2
    if d.get("event_strength"): score += 1
    if d.get("impact_score") is not None: score += 1
    if d.get("market_confirmation") and d.get("market_confirmation") != "unknown": score += 1
    if d.get("mfe_pct") is not None or d.get("mae_pct") is not None: score += 2
    if rc.get("signal_name") and rc["signal_name"] != "unknown": score += 1
    return min(score, 10)


# v301-fix: 原来 VALUES 有 41 个占位符，但列数和参数都只有 40 个 —— 每次插入
# 都报 "41 values for 40 columns"，worker 从未成功落库。
# 同时改为 UPSERT：trade_reflections_v2.decision_id 是 UNIQUE，且
# decision_engine.settle_one() / bridge_server._insert_settlement_reflection()
# 也会用**同一个 id 空间**（trade_decisions.id）往这张表写结算行 —— 两者
# 必然重叠，裸 INSERT 会撞唯一键。
#
# 写保护（关键）：本表混了两个来源，用 paper_run_id 区分 ——
#   · 反思行：本 worker 写的，paper_run_id IS NOT NULL（decision_id = ai_decisions.id）
#   · 结算行：engine/bridge 写的，paper_run_id IS NULL（decision_id = trade_decisions.id）
# 冲突时只更新**自己那类**行，跨来源 id 撞车则跳过（记 warning），绝不覆盖
# 别的决策的反思数据。
_INSERT_SQL = """
    INSERT INTO trade_reflections_v2
        (decision_id, paper_run_id, asset, action,
         entry_price, exit_price, pnl_pct, mfe_pct, mae_pct, settled_at,
         analysis_layer_scores, filter_layer_chain,
         reasoning_chain, counterfactual,
         error_layer, error_sub_layer, error_signal,
         layer, sub_layer, signal_name,            -- v188 ontology 三层分类
         ontology_signal,                          -- v190 拼合列
         root_cause, lesson, verification,           -- v188 verification 入库
         applied_experience_ids,
         depth_score, created_at,
         source_breakdown, completeness_at_reflection, skipped_low_completeness,
         evidence_strength_score, reflection_self_score, is_sibling,  -- v232.5 数字落地 + 自检
         dimension, subcategory, hit, actual_outcome_pct,              -- v309 P0-1 三维度
         confidence_at_time, reflection_text, settled,
         trade_uuid, settle_source,                                    -- v404 绑定成交 + 取价来源
         origin)                                                       -- v413 来源分账
    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
            ?, ?, ?, ?, ?, ?, ?,
            ?,
            ?, ?, ?,
            ?, ?,
            ?, ?, ?,
            ?, ?, ?,
            ?, ?, ?, ?, ?, ?, ?,
            ?, ?, 'paper')
    ON CONFLICT(decision_id) DO UPDATE SET
        trade_uuid=excluded.trade_uuid,
        settle_source=excluded.settle_source,
        paper_run_id=excluded.paper_run_id,
        asset=excluded.asset,
        action=excluded.action,
        entry_price=excluded.entry_price,
        exit_price=excluded.exit_price,
        pnl_pct=excluded.pnl_pct,
        mfe_pct=excluded.mfe_pct,
        mae_pct=excluded.mae_pct,
        settled_at=excluded.settled_at,
        analysis_layer_scores=excluded.analysis_layer_scores,
        filter_layer_chain=excluded.filter_layer_chain,
        reasoning_chain=excluded.reasoning_chain,
        counterfactual=excluded.counterfactual,
        error_layer=excluded.error_layer,
        error_sub_layer=excluded.error_sub_layer,
        error_signal=excluded.error_signal,
        layer=excluded.layer,
        sub_layer=excluded.sub_layer,
        signal_name=excluded.signal_name,
        ontology_signal=excluded.ontology_signal,
        root_cause=excluded.root_cause,
        lesson=excluded.lesson,
        verification=excluded.verification,
        applied_experience_ids=excluded.applied_experience_ids,
        depth_score=excluded.depth_score,
        source_breakdown=excluded.source_breakdown,
        completeness_at_reflection=excluded.completeness_at_reflection,
        skipped_low_completeness=excluded.skipped_low_completeness,
        evidence_strength_score=excluded.evidence_strength_score,
        reflection_self_score=excluded.reflection_self_score,
        is_sibling=excluded.is_sibling,
        dimension=excluded.dimension,
        subcategory=excluded.subcategory,
        hit=excluded.hit,
        actual_outcome_pct=excluded.actual_outcome_pct,
        confidence_at_time=COALESCE(excluded.confidence_at_time,
                                      trade_reflections_v2.confidence_at_time),
        reflection_text=excluded.reflection_text,
        settled=MAX(trade_reflections_v2.settled, excluded.settled),
        origin=excluded.origin                                    -- v413: 有结算的 paper 行接管来源
    WHERE trade_reflections_v2.paper_run_id IS NOT NULL
"""


def _resolve_dimension_for_decision(
    d: Dict[str, Any],
    conn: sqlite3.Connection,
    rc: Dict[str, str],
) -> tuple:
    """解析该笔反思的 (dimension, subcategory) —— v309 P0-1。

    取数优先级 (越精确越靠前):
      1. decision_errors 里该 decision_id 的错误码 (E001a-E033) —— 程序化校验,
         最精确;
      2. attribution_reviews 的 reason_code (LLM 23 类失败框架);
      3. v188 ontology 三层标签 (layer/sub_layer) —— 兜底, 保证每条反思都有维度。

    口径定义在 reflection_dimension_map.py (单一权威, 零依赖, 可单测)。
    任何异常都降级为 ("", "") —— 绝不猜维度, 也不让维度解析失败阻断反思落库。
    """
    try:
        from review import reflection_dimension_map as _rdm
    except Exception:
        return ("", "")
    decision_id = int(d.get("decision_id") or 0)
    codes: list = []
    if decision_id:
        try:
            # v399 P1: 带上 severity —— resolve_dimension 按「严重度优先」归类,
            # 否则最高频的低危码(E001a 缺事实, INFO)会把三维细分退化成一种
            # (reflection_dimension_map 里记的 G11 根因)。severity 缺失时按 0 处理。
            rows = conn.execute(
                "SELECT error_code, severity FROM decision_errors WHERE decision_id = ?",
                (decision_id,),
            ).fetchall()
            codes = [
                f"{r[0]}:{int(r[1] or 0)}" if r and r[0] else None
                for r in rows
            ]
            codes = [c for c in codes if c]
        except Exception:
            codes = []
    reason_code = None
    if decision_id:
        try:
            row = conn.execute(
                "SELECT reason_code FROM attribution_reviews "
                "WHERE trade_id = ? ORDER BY id DESC LIMIT 1",
                (decision_id,),
            ).fetchone()
            if row and row[0] is not None:
                reason_code = row[0]
        except Exception:
            reason_code = None
    try:
        return _rdm.resolve_dimension(
            error_codes=codes,
            reason_code=reason_code,
            layer=rc.get("layer") or "",
            sub_layer=rc.get("sub_layer") or "",
            signal_name=rc.get("signal_name") or "",
        ) or ("", "")
    except Exception:
        return ("", "")


def _insert_one(d: Dict[str, Any]) -> Optional[int]:
    rc = _classify_root_cause(d)
    depth = _depth_score(d, rc)
    applied = [int(d.get("persona_match_id") or 0)] if d.get("persona_match_id") else []
    conn = db.get_connection()
    # v399 P1: 错误码写入口径接通 —— 反思落库前即时补跑程序化校验器。
    # 此前 run_reflection.py 是独立任务，实测全库只写了 23 条(4.7%)，
    # 维度解析因此几乎永远回落到 ontology 兜底（pattern_recognition 占 80%）。
    # 这里按决策补一次；save_errors 是 INSERT OR IGNORE，重复跑天然幂等。
    # 降级决策已在 main() 里跳过，不会走到这里。
    try:
        from review import reflection_checkers as _checkers
        _errs = _checkers.run_all_checkers(d, conn=conn)
        if _errs:
            _checkers.save_errors(_errs, conn=conn)
    except Exception as exc:
        print(f"[PAPER-REFLECT] checker skipped: {type(exc).__name__}: {exc}",
              flush=True)
    # v200 P0+: 源分布 + completeness 快照(本条反思当时 ai_decisions 的数据质量)
    _src_breakdown = json.dumps({
        "llm": int(d.get("llm_field_count") or 0),
        "rule": int(d.get("rule_field_count") or 0),
        "business_kb": int(d.get("business_kb_field_count") or 0),
        "historical": int(d.get("historical_field_count") or 0),
    }, ensure_ascii=False)
    _comp_at = float(d.get("completeness_score") or 1.0)
    _skipped = 1 if _comp_at < 0.5 else 0
    # v232.5: 数字落地分 + 自检可信度 + sibling 标记
    _evidence_score = float(_count_evidence_anchors(
        (rc.get("root_cause") or "") + " " + (rc.get("lesson") or "")
    ))
    _self_score = _compute_reflection_self_score(_evidence_score, _comp_at, depth)
    _sibling = _is_sibling_decision(d)
    # ── v309 P0-1: 三维度 (A/B/C) 归因 ──
    # 维度口径在 reflection_dimension_map.py (单一权威), 本文件只负责取数 + 落库。
    # 取数顺序: 该决策的 decision_errors 错误码 (最精确) → reason_code →
    #           v188 ontology 三层标签 (兜底)。
    # 拿不到维度就写空串 —— 绝不用"默认桶"凑数, 那会让三维度统计失真。
    _dim, _subcat = _resolve_dimension_for_decision(d, conn, rc)
    _pnl = d.get("forward_pnl")
    try:
        # v404 铁律 4：没结算收益就是 None，不写 0 —— 0 会被统计成"真平盘"
        # v469(P0+)：forward_pnl 已经是百分数（-0.5 表示 -0.5%），原先再乘 100
        # 会把 -0.5% 记成 -50%，反思结论系统性放大 100 倍。
        _outcome_pct = float(_pnl) if _pnl is not None else None
    except (TypeError, ValueError):
        _outcome_pct = None
    _verdict = str(d.get("is_correct") or "").strip().upper()
    _hit = 1 if _verdict == "WIN" else 0
    _settled = 1 if (d.get("settled") or _verdict in ("WIN", "LOSS")) else 0
    _sent = d.get("sentiment_score")
    if _sent is None:
        _conf_at = None      # 没算出置信度 ≠ 置信度 0（铁律 4 / v399 口径）
    else:
        try:
            _conf_at = max(0.0, min(1.0, abs(float(_sent))))
        except (TypeError, ValueError):
            _conf_at = None
        if _conf_at is not None and _conf_at < 1e-9:
            _conf_at = None   # v403：0 是"没算出来"，不是"很低"
    _refl_text = (rc.get("root_cause") or "")[:400]
    try:
        cur = conn.execute(
            _INSERT_SQL,
            (
                int(d["decision_id"]),
                int(d.get("paper_trading_run_id") or 0),
                d.get("target_asset") or "",
                d.get("suggested_action") or "HOLD",
                d.get("entry_price"), d.get("exit_price"),
                d.get("forward_pnl"), d.get("mfe_pct"), d.get("mae_pct"),
                d.get("created_at") or "",
                json.dumps({"event_strength": d.get("event_strength"),
                            "impact_score": d.get("impact_score"),
                            "attention_score": d.get("attention_score")},
                           ensure_ascii=False),
                json.dumps({"event_type": d.get("event_type_code"),
                            "market_confirm": d.get("market_confirmation"),
                            "direct_catalyst": d.get("direct_catalyst")},
                           ensure_ascii=False),
                json.dumps(rc, ensure_ascii=False),
                rc.get("counterfactual") or "",   # v482: 结构化反事实(毛/净/成本)
                rc["layer"], rc["sub_layer"], rc["signal_name"],  # error_layer/sub/signal (LOSS 时用)
                rc["layer"], rc["sub_layer"], rc["signal_name"],  # ontology (LOSS/WIN 都用)
                rc.get("ontology_signal", f"{rc['layer']}/{rc['sub_layer']}/{rc['signal_name']}"),  # v190 拼合
                rc["root_cause"], rc["lesson"],
                rc.get("verification", ""),
                json.dumps(applied),
                depth,
                # v309 P1: 原来写 time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime())
                # → 格式是 'T' 分隔 (OK), 但**依赖机器本地时区**: 部署到 UTC 机器上
                # 写的就是 UTC, 而读取侧 (bridge_server) 一律按北京时间解读 → 差 8 小时。
                # 现在显式用北京时区, 落库格式与读取侧口径彻底统一。
                time_utils.bj_now_str(),
                # v200 P0+ 严谨兜底元数据
                _src_breakdown,
                _comp_at,
                _skipped,
                # v232.5 数字落地 + 自检分 + sibling
                _evidence_score,
                _self_score,
                _sibling,
                # v309 P0-1 三维度 + 实际结果 (供 by-dimension / reflections/summary)
                _dim, _subcat, _hit, _outcome_pct,
                _conf_at, _refl_text, _settled,
                # v404: 绑定成交 + 取价来源（结算表是唯一真值源）
                d.get("trade_uuid") or "",
                "quick_sim_trades",
            ),
        )
        # v482: 教训绑代码代次 —— 反思行继承决策行的 code_fingerprint,
        # 换代(算法/关键参数变化)后旧教训可按指纹失效, 不再无限期进 prompt。
        try:
            _cfp = str(d.get("code_fingerprint") or "")
            if not _cfp:
                try:
                    from code_fingerprint import fingerprint as _fp
                    _cfp = _fp()
                except Exception:
                    _cfp = ""
            if _cfp:
                conn.execute(
                    "UPDATE trade_reflections_v2 SET code_fingerprint=? "
                    "WHERE decision_id=? AND COALESCE(code_fingerprint,'')=''",
                    (_cfp, int(d["decision_id"])),
                )
        except Exception as _e_cfp:
            # v487：这里原来是 `pass` —— 指纹回填失败会静默不留痕，
            # 事后无法判断"教训没绑代次"是真没指纹还是写库失败。改为留痕。
            print(f"[paper-reflection] code_fingerprint 回填失败 decision={d.get('decision_id')}: "
                  f"{type(_e_cfp).__name__}: {_e_cfp}", flush=True)
        # v188: 同步写入 decision_layer_signals 聚合表(颗粒度统计)
        try:
            is_loss = 1 if (str(d.get("is_correct") or "").upper() == "LOSS"
                            or (isinstance(d.get("forward_pnl"), (int, float))
                                and d["forward_pnl"] < -0.005)) else 0
            is_win = 1 if (str(d.get("is_correct") or "").upper() == "WIN"
                           or (isinstance(d.get("forward_pnl"), (int, float))
                               and d["forward_pnl"] > 0.005)) else 0
            conn.execute("""
                INSERT INTO decision_layer_signals
                    (layer, sub_layer, signal_name, asset, action,
                     n_total, n_loss, n_win, last_seen_ts, last_decision_id,
                     updated_at,
                     llm_count, rule_count, business_kb_count, historical_count)
                VALUES (?, ?, ?, ?, ?, 1, ?, ?, ?, ?, datetime('now'),
                        ?, ?, ?, ?)
                ON CONFLICT(layer, sub_layer, signal_name, asset, action) DO UPDATE SET
                    n_total = n_total + 1,
                    n_loss = n_loss + ?,
                    n_win = n_win + ?,
                    last_seen_ts = ?,
                    last_decision_id = ?,
                    updated_at = datetime('now'),
                    -- v200 P0+: 按 source 分桶计数(精确量化 LLM vs 我们自己模型贡献)
                    llm_count = llm_count + ?,
                    rule_count = rule_count + ?,
                    business_kb_count = business_kb_count + ?,
                    historical_count = historical_count + ?
            """, (
                rc["layer"], rc["sub_layer"], rc["signal_name"],
                d.get("target_asset") or "*",
                d.get("suggested_action") or "*",
                is_loss, is_win,
                int(time.time()), int(d["decision_id"]),
                int(d.get("llm_field_count") or 0),
                int(d.get("rule_field_count") or 0),
                int(d.get("business_kb_field_count") or 0),
                int(d.get("historical_field_count") or 0),
                is_loss, is_win,
                int(time.time()), int(d["decision_id"]),
                int(d.get("llm_field_count") or 0),
                int(d.get("rule_field_count") or 0),
                int(d.get("business_kb_field_count") or 0),
                int(d.get("historical_field_count") or 0),
            ))
        except Exception as exc:
            print(f"[PAPER-REFLECT] decision_layer_signals upsert failed: "
                  f"{type(exc).__name__}: {exc}", flush=True)
        # v200+ P1+: LOSS 反思闭环 → 写 decision_reflection.bias_text,
        # 让 _load_prev_decision(asset) 下次同品种决策 prompt 能读到本次教训,
        # 形成"看反思/下次更稳"的 self-loop。
        # 字段语义对齐 market_style_reflection_loop (window/sample/win/loss/avg_pnl/trigger),
        # 但窗口粒度改为 per_decision (单条反思)，trigger=paper_v2_loss 标记来源。
        # 写入失败一律静默降级，不阻断主 INSERT。
        try:
            _is_loss_evt = 1 if (
                str(d.get("is_correct") or "").upper() == "LOSS"
                or (isinstance(d.get("forward_pnl"), (int, float))
                    and d["forward_pnl"] < -0.005)
            ) else 0
            if _is_loss_evt:
                _lesson = (rc.get("lesson") or "").strip()[:180]
                if _lesson:
                    _asset_for_bias = (d.get("target_asset") or "").upper()
                    _fwd_pnl = d.get("forward_pnl")
                    try:
                        _avg_pnl_val = float(_fwd_pnl) if isinstance(_fwd_pnl, (int, float)) else 0.0
                    except Exception:
                        _avg_pnl_val = 0.0
                    # v497：原来的 bias_text 是**按错误码套模板**（"single-source 应降
                    # confidence 上限 0.5 → 权重应下调"），没有本笔的任何数字；而这行
                    # bias_text 会被 ai_worker._load_prev_decision 注入下一次同资产决策的
                    # prompt（"反思: …"）。模板越空，"看反思下次更稳"这个闭环越没有信息量。
                    # 改成：模板 + 本笔实际亏损 + 同资产近 7 天战绩，并给一条可执行动作。
                    try:
                        _prev_rf = conn.row_factory      # get_connection 默认 tuple
                        conn.row_factory = sqlite3.Row
                        _hist = conn.execute(
                            "SELECT COUNT(*) n, SUM(win_n) w, SUM(loss_n) l "
                            "FROM decision_reflection WHERE asset=? "
                            "AND created_at >= datetime('now','-7 day')",
                            (_asset_for_bias,),
                        ).fetchone()
                        conn.row_factory = _prev_rf
                        _h_n = int(_hist["n"] or 0) if _hist else 0
                        _h_w = int(_hist["w"] or 0) if _hist else 0
                        _h_l = int(_hist["l"] or 0) if _hist else 0
                    except Exception:
                        _h_n = _h_w = _h_l = 0
                        try:
                            conn.row_factory = _prev_rf
                        except Exception:
                            pass

                    # v497：forward_pnl / avg_pnl_pct 已经是百分数（-0.6048 = -0.6%），
                    # 再乘 100 会把 "亏 0.6%" 写成 "亏 60%"。第 549 行早就有同样的注释。
                    _pnl_txt = (f"{_avg_pnl_val:+.2f}%" if _avg_pnl_val else "未知")
                    _bias = f"{_lesson}｜本笔 {_asset_for_bias} {_pnl_txt}"
                    if _h_n:
                        _bias += f"，同资产近 7 天 {_h_w} 胜 {_h_l} 亏"
                    _sug = (
                        f"下一笔 {_asset_for_bias} 决策：confidence 上限压到 0.50，"
                        f"要求至少 2 个独立来源同向才允许下单"
                        + (f"；已累计 {_h_l} 笔亏损，信号权重再降 0.1（下限 0.1）" if _h_l >= 3 else "")
                    )
                    # depth_score：沿用 worker 自己的 _depth_score()（0-10，衡量输入
                    # 证据完整度），换成 0-100 标度；有可执行动作再 +25，上限 100。
                    _depth = min(100, int(depth) * 10 + (25 if _sug else 0))
                    conn.execute(
                        "INSERT INTO decision_reflection "
                        "(asset, window, n_decisions, win_n, loss_n, flat_n, avg_pnl_pct, "
                        "bias_text, trigger, ts, depth_score, actionable_suggestion) "
                        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                        (_asset_for_bias, "per_decision", 1, 0, 1, 0,
                         _avg_pnl_val, _bias, "paper_v2_loss", int(time.time()),
                         int(_depth), _sug),
                    )
        except Exception as _exc:
            print(f"[PAPER-REFLECT] decision_reflection insert skipped: "
                  f"{type(_exc).__name__}: {_exc}", flush=True)
        conn.commit()
        return int(cur.lastrowid)
    except Exception as exc:
        print(f"[PAPER-REFLECT] insert failed: {type(exc).__name__}: {exc}", flush=True)
        return None
    finally:
        conn.close()


def main() -> int:
    cycle_start_ts = time.time()
    # v399 P2: 给未分类的洞察补写"可测假设 / 只能观察"判定（幂等）。
    # 放在每轮开头：新洞察进来后 5 分钟内就有 observe_only 标记，
    # 晋升链路（weekly_insight_apply）与指标 S19 才看得到真实占比。
    try:
        import insight_hypothesis as _ih
        _c0 = db.get_connection()
        try:
            _n = _ih.ensure_classified(_c0)
        finally:
            _c0.close()
        if _n:
            print(f"[PAPER-REFLECT] insights classified={_n}", flush=True)
    except Exception as exc:
        print(f"[PAPER-REFLECT] insight classify skipped: {type(exc).__name__}: {exc}",
              flush=True)
    conn = db.get_connection()
    try:
        # v186 fix: db.get_connection() 不设 row_factory,rows 是 tuple,dict(r) 失败
        # 显式设 row_factory=sqlite3.Row 让 dict(r) 工作
        import sqlite3 as _sq3
        _orig_rf = conn.row_factory
        conn.row_factory = _sq3.Row
        rows = conn.execute(_DEDUPE_SELECT).fetchall()
        items = [dict(r) for r in rows]
        conn.row_factory = _orig_rf
    except Exception as exc:
        print(f"[PAPER-REFLECT] select failed: {type(exc).__name__}: {exc}", flush=True)
        return 0
    finally:
        conn.close()
    inserted = 0
    unknown_count = 0  # v188 P0-3: 监控"分类器盲区"
    unknown_samples: List[Dict[str, Any]] = []
    miss_rate_sum = 0.0  # v189: 累计 miss_rate 用于 daily journal
    llm_invoked_n = 0    # v189: 标记 P0-2 llm_invoked=1 的次数
    # v189: 拉当天的 run_id (无则用 'LIVE:{date}')
    try:
        from review import reflection_journal as _rj
    except Exception:
        _rj = None  # type: ignore
    live_run_id = f"LIVE:{time.strftime('%Y%m%d')}"
    degraded_skipped = 0  # v398: 降级决策没有真分析, 不产出洞察
    for d in items:
        try:
            if int(d.get("analysis_degraded") or 0) == 1:
                degraded_skipped += 1
                continue
        except (TypeError, ValueError):
            pass
        rid = _insert_one(d)
        if rid:
            inserted += 1
            # 触发一次轻量分类(不再次走 DB)查兜底
            try:
                from review import reflection_ontology as _onto
                _sig = _onto.infer_layer_sub_signal(d)
                if _sig["signal_name"] == "unknown":
                    unknown_count += 1
                    if len(unknown_samples) < 3:
                        unknown_samples.append({
                            "decision_id": d.get("decision_id"),
                            "asset": d.get("target_asset"),
                            "action": d.get("suggested_action"),
                            "is_correct": d.get("is_correct"),
                            "forward_pnl": d.get("forward_pnl"),
                            "event_strength": d.get("event_strength"),
                            "no_trade_reason": (d.get("no_trade_reason") or "")[:50],
                        })
            except Exception:
                pass
            # v189: 写 reflection_cycle_log(每条一条,供 daily journal 聚合)
            if _rj is not None:
                try:
                    outcome = "pending"
                    if str(d.get("is_correct") or "").upper() == "WIN":
                        outcome = "win"
                    elif str(d.get("is_correct") or "").upper() == "LOSS":
                        outcome = "loss"
                    latency_ms = int((time.time() - cycle_start_ts) * 1000)
                    # miss_rate 用 missed_signal=1 决策的占比(粗估,需 ai_decisions.missed_signal)
                    try:
                        miss_flag = int(d.get("missed_signal") or 0)
                    except Exception:
                        miss_flag = 0
                    miss_rate_sum += miss_flag
                    _rj.record_cycle(
                        decision_id=int(d["decision_id"]),
                        run_id=live_run_id,
                        reflection_id=int(rid) if rid else None,
                        outcome=outcome,
                        llm_invoked=miss_flag,   # 这里粗略用 miss_flag 计数
                        latency_ms=latency_ms,
                        ontology_signal=str(_sig.get("signal_name", "") if "_sig" in dir() else ""),
                    )
                except Exception as exc:
                    print(f"[PAPER-REFLECT] record_cycle skipped: "
                          f"{type(exc).__name__}: {exc}", flush=True)
    print(f"[PAPER-REFLECT] processed={len(items)}, inserted={inserted}", flush=True)
    if items:
        unknown_rate = unknown_count * 100.0 / len(items)
        print(f"[PAPER-REFLECT-MONITOR] unknown_rate={unknown_rate:.1f}% "
              f"({unknown_count}/{len(items)})", flush=True)
        if unknown_samples:
            print(f"[PAPER-REFLECT-MONITOR] unknown_samples={unknown_samples}",
                  flush=True)
    try:
        import experience_reflector as _er
        _er.adaptive_review_and_apply()
    except Exception as exc:
        print(f"[PAPER-REFLECT] adaptive_review_and_apply skipped: "
              f"{type(exc).__name__}: {exc}", flush=True)
    # v188: 跑完一轮后顺便扫一遍 missed_signal(写入 ai_decisions.missed_signal)
    try:
        import missed_signal_detector as _msd
        msd_stats = _msd.detect_for_recent(window_hours=24, limit=100)
        if msd_stats.get("marked", 0):
            print(f"[PAPER-REFLECT] missed_signal marked={msd_stats['marked']}/"
                  f"{msd_stats['scanned']}", flush=True)
    except Exception as exc:
        print(f"[PAPER-REFLECT] missed_signal detect skipped: "
              f"{type(exc).__name__}: {exc}", flush=True)
    # v321.3 P0: 漏检信号 -> **真正写进反思表** (需求 #7 的数据流闭环)。
    # 旧实现只把 missed_signal 标到 ai_decisions 上, 而 _DEDUPE_SELECT 用
    # `JOIN quick_sim_trades ... AND qs.settled=1` (INNER JOIN) —— HOLD 决策
    # 永不开仓、永不结算, 于是漏检信号天然进不了反思系统 (实测交集 = 0)。
    # 本模块走独立写入路径, 不依赖 quick_sim_trades。
    try:
        import missed_signal_reflection as _msr
        _msr_stats = _msr.run_days(days=2, limit=300)
        if _msr_stats.get("written"):
            print(f"[PAPER-REFLECT] 漏检反思写入 {_msr_stats['written']} 条 "
                  f"(候选 {_msr_stats['candidates']})", flush=True)
    except Exception as exc:
        print(f"[PAPER-REFLECT] missed_signal_reflection skipped: "
              f"{type(exc).__name__}: {exc}", flush=True)
    # v230: 跑完一轮后对刚插入的反思做深度增强(K线/跨资产/抢跑/运气 vs 信号/自循环)
    if inserted > 0:
        try:
            from review import reflection_deep as _rd
            deep_stats = _rd.enrich_pending_loop(batch_size=inserted, max_per_run=inserted * 2)
            print(f"[PAPER-REFLECT-DEEP] enriched={deep_stats.get('ok', 0)}/"
                  f"partial={deep_stats.get('partial', 0)}/"
                  f"failed={deep_stats.get('failed', 0)} scanned={deep_stats.get('scanned', 0)}",
                  flush=True)
        except Exception as exc:
            print(f"[PAPER-REFLECT-DEEP] enrich_pending_loop skipped: "
                  f"{type(exc).__name__}: {exc}", flush=True)
    # v189: 周期结束 → 自动 rollup_daily(today),更新 reflection_daily_journal
    if inserted > 0 and _rj is not None:
        try:
            today = time.strftime("%Y-%m-%d")
            rj_ret = _rj.rollup_daily(journal_date=today, run_id=live_run_id)
            if rj_ret.get("ok"):
                print(f"[PAPER-REFLECT-JOURNAL] rollup_daily({today}) n={rj_ret.get('n_decisions')} "
                      f"win_rate={rj_ret.get('win_rate')} unknown_rate={rj_ret.get('unknown_rate'):.1%}",
                      flush=True)
        except Exception as exc:
            print(f"[PAPER-REFLECT-JOURNAL] rollup_daily skipped: "
                  f"{type(exc).__name__}: {exc}", flush=True)
    if degraded_skipped:
        print(f"[PAPER-REFLECT] 降级决策跳过 {degraded_skipped} 条"
              f"（没有真分析, 不产出洞察）", flush=True)
    return inserted


def daemon_loop(interval_s: int = 300) -> None:
    print(f"[PAPER-REFLECT] daemon started | interval={interval_s}s", flush=True)
    try:
        while True:
            try:
                main()
            except KeyboardInterrupt:
                raise
            except Exception as exc:
                print(f"[PAPER-REFLECT] daemon loop error: "
                      f"{type(exc).__name__}: {exc}", flush=True)
            time.sleep(int(interval_s))
    except KeyboardInterrupt:
        print("\n[PAPER-REFLECT] daemon stopped (KeyboardInterrupt)", flush=True)


if __name__ == "__main__":
    interval = int(os.environ.get("PAPER_REFLECT_INTERVAL_S", "300"))
    daemon_loop(interval_s=interval)