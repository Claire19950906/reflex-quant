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

import json, re, time, os
from typing import Any, Dict, List, Optional

import db

_DEDUPE_SELECT = """
    SELECT a.id AS decision_id, a.target_asset, a.suggested_action, a.sentiment_score,
           a.event_type_code, a.event_strength, a.impact_score, a.heat_score,
           a.attention_score, a.direct_catalyst, a.market_confirmation,
           qs.entry_price AS entry_price, qs.exit_price AS exit_price,
           qs.pnl_pct AS forward_pnl, qs.mfe_pct, qs.mae_pct,
           qs.verdict AS is_correct, qs.settled AS settled,
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
    return round(0.4 * ev + 0.3 * cp + 0.3 * dp, 4)


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
    import reflection_ontology as _onto

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
        # 反事实锚点(锚点 5): 反向预期 pnl ≈ -pnl,作为示意让反思挂数字
        counterfactual_s = f"反向预期 pnl≈{-pnl_val:+.4f}"
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

    return {
        "layer": layer, "sub_layer": sub_layer, "signal_name": signal_name,
        # v190: ontology_signal 拼合列 — 与 error_layer/sub_layer/signal 同步,
        # 便于单列 WHERE / GROUP BY。三字段任一为空时降级用 layer 兜底。
        "ontology_signal": f"{layer}/{sub_layer}/{signal_name}" if (layer and signal_name) else "unknown",
        "root_cause": root, "lesson": lesson, "verification": verification,
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
         evidence_strength_score, reflection_self_score, is_sibling)  -- v232.5 数字落地 + 自检
    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
            ?, ?, ?, ?, ?, ?, ?,
            ?,
            ?, ?, ?,
            ?, ?,
            ?, ?, ?,
            ?, ?, ?)
"""


def _insert_one(d: Dict[str, Any]) -> Optional[int]:
    rc = _classify_root_cause(d)
    depth = _depth_score(d, rc)
    applied = [int(d.get("persona_match_id") or 0)] if d.get("persona_match_id") else []
    conn = db.get_connection()
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
                "",
                rc["layer"], rc["sub_layer"], rc["signal_name"],  # error_layer/sub/signal (LOSS 时用)
                rc["layer"], rc["sub_layer"], rc["signal_name"],  # ontology (LOSS/WIN 都用)
                rc.get("ontology_signal", f"{rc['layer']}/{rc['sub_layer']}/{rc['signal_name']}"),  # v190 拼合
                rc["root_cause"], rc["lesson"],
                rc.get("verification", ""),
                json.dumps(applied),
                depth,
                time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime()),
                # v200 P0+ 严谨兜底元数据
                _src_breakdown,
                _comp_at,
                _skipped,
                # v232.5 数字落地 + 自检分 + sibling
                _evidence_score,
                _self_score,
                _sibling,
            ),
        )
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
                    conn.execute(
                        "INSERT INTO decision_reflection "
                        "(asset, window, n_decisions, win_n, loss_n, flat_n, avg_pnl_pct, "
                        "bias_text, trigger, ts) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                        (_asset_for_bias, "per_decision", 1, 0, 1, 0,
                         _avg_pnl_val, _lesson, "paper_v2_loss", int(time.time())),
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
        import reflection_journal as _rj
    except Exception:
        _rj = None  # type: ignore
    live_run_id = f"LIVE:{time.strftime('%Y%m%d')}"
    for d in items:
        rid = _insert_one(d)
        if rid:
            inserted += 1
            # 触发一次轻量分类(不再次走 DB)查兜底
            try:
                import reflection_ontology as _onto
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
    # v230: 跑完一轮后对刚插入的反思做深度增强(K线/跨资产/抢跑/运气 vs 信号/自循环)
    if inserted > 0:
        try:
            import reflection_deep as _rd
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