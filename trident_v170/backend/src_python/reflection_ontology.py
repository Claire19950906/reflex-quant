#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""v188 反思颗粒度 ontology —— 三层嵌套标签的单一权威定义。

设计原则:
  - 旧 v186 反射只产 (analysis/decision/no_trade) → 94% 命中, 失去诊断价值
  - 本 ontology 把"信号质量差"细化为:
      layer (7)      ← 粗类:出错的系统层
      sub_layer (24) ← 中类:该层下的具体子系统
      signal_name (60+) ← 具体哪个信号字符串

数据流:
  1. paper_reflection_worker._classify_root_cause() 用本 ontology 推断
  2. reflection_checkers 输出加 dim1/dim2/dim3 4 元组
  3. 落 trade_reflections_v2 + decision_layer_signals 聚合表

本模块是 v186 error_layer/error_sub_layer/error_signal 字段的精确化升级,
调用方接口不变,字段含义等价。
"""
from __future__ import annotations

from typing import Any, Dict, List


LAYERS: List[str] = [
    # v189 事件逻辑链层 — 最高权重（排序/统计/沉淀都优先），主战场 = 分析页因果链/概念/反共识/跨资产方向
    "event_logic",
    # v225+ 推理逻辑层 — 次高权重（LLM 推理质量 7 维：假设/反事实/校准/历史/跨资产一致/因果/置信度-证据）
    "reasoning_logic",
    # v231+ 逻辑分析层 — 形式逻辑有效性（自相矛盾 / 推不出结论 / 假二择一 / 循环论证 / 术语歧义 / 后此谬因）
    #       介于 event_logic (链路完整) 与 reasoning_logic (质量诚实) 之间,
    #       关注「推理结构本身的逻辑有效性」, 与 reasoning_logic 互补不重叠
    "logic_analysis",
    "analysis", "filter", "data", "timing", "execution", "strategy", "gate",
]


# v189 层优先级（数字越小越高）；供排序/统计/沉淀使用
# v225+: reasoning_logic 加在 event_logic 之后
# v231+: logic_analysis 加在 reasoning_logic 之后 (3rd tier)
LAYER_WEIGHTS: Dict[str, int] = {
    "event_logic": 1,
    "reasoning_logic": 2,
    "logic_analysis": 3,
    "analysis": 4, "filter": 5, "data": 6, "timing": 7,
    "execution": 8, "strategy": 9, "gate": 10,
}


SUB_LAYERS: Dict[str, List[str]] = {
    # v189 事件逻辑链层（最高权重）— 分析页推理链/概念/反共识/跨资产传导
    "event_logic": [
        "causal_chain",         # reasoning_path 八段推理链完整性
        "concept_match",        # factor_ranking 概念 ↔ asset_driver_nodes
        "counter_consensus",    # 反共识/反向思考是否出现
        "cross_asset_chain",    # 跨资产传导方向（UST/DXY/原油/加密）
        "sentiment_driver_chain", # 情绪↔驱动因子映射
        "macro_state_chain",    # 宏观状态→资产驱动力
        "summary_chain",        # 总结陈述的因果链
    ],
    # v225+ reasoning_logic 层 — LLM 推理质量 7 维细分
    "reasoning_logic": [
        "assumption",               # E021 — 假设/前提是否明示
        "counterfactual",           # E022 — 反事实/反向情景是否考虑
        "calibration",              # E023 — 置信度校准（虚高/虚低）
        "history",                  # E024 — 历史反例/同类案例引用
        "cross_asset_consistency",  # E025 — 跨资产方向一致性
        "mechanism",                # E026 — 因果机制（因→果 关键词齐全）
        "confidence_evidence",      # E027 — 置信度与证据量匹配
    ],
    "analysis": [
        "llm_output", "sentiment_score", "event_struct", "macro_context",
        "factor_chain", "cross_asset_transmission", "knowledge_inject",
        "persona_match", "news_organizer",
        # v200 Q3 修复: news_source_class 6 子层, 处理"宏观/聚合"等数据源
        # 分类与 ontology 子层不对齐导致的错误归类.
        "news_source_class",
        # v200+ Q6: signal_quality_v200 8 子层, 拆分"信号质量差"为可独立诊断的
        # 8 类: 影响大小估错(2) / 产品类别错配(2) / 脏数据 4 种.
        "signal_quality_v200",
    ],
    "filter": [
        "confidence_gate", "contradiction_check", "strength_threshold",
        "asset_match", "temporal_alignment", "gate_chain", "sector_match",
        "fallback_rule",
    ],
    "data": [
        "feed_status", "latency", "coverage", "cleanliness", "freshness",
    ],
    "timing": [
        "timelag_to_event", "market_session", "event_window",
    ],
    "execution": [
        "paper_run_state", "slippage", "position_sizing",
    ],
    "strategy": [
        "buy_min_score", "tp1_mult", "sl_mult", "max_holding_min", "regime_filter",
    ],
    "gate": [
        "gate_verdict", "gate_strength", "gate_core_impact", "gate_main_asset", "gate_kind",
    ],
    # v231+ logic_analysis 层 — reasoning_path 形式逻辑 6 类问题
    "logic_analysis": [
        "formal_validity",    # E028-E033: 自相矛盾 / non_sequitur / 假二择一 / 循环论证 / 歧义 / post_hoc
    ],
}


# SIGNAL_NAMES 字典拆到 reflection_ontology_signals.py(避免此文件超 6k char 编辑上限)。
# 本文件用 helpers 通过 import 引用它。

def _load_signal_names():
    """延迟加载 SIGNAL_NAMES(避免循环 import)。"""
    from reflection_ontology_signals import SIGNAL_NAMES
    return SIGNAL_NAMES


def list_all_signals():
    """展开全部 (layer, sub_layer, signal_name) 三元组。"""
    _SIG = _load_signal_names()
    out = []
    for layer, subs in SUB_LAYERS.items():
        for sub in subs:
            for sig in _SIG.get(layer, {}).get(sub, {}).keys():
                out.append({"layer": layer, "sub_layer": sub, "signal_name": sig})
    return out


def is_valid(layer: str, sub_layer: str, signal_name: str) -> bool:
    """校验 (layer, sub_layer, signal_name) 三元组是否合法。"""
    if layer not in LAYERS:
        return False
    if sub_layer not in SUB_LAYERS.get(layer, []):
        return False
    _SIG = _load_signal_names()
    return signal_name in _SIG.get(layer, {}).get(sub_layer, {})


def get_signal_template(layer: str, sub_layer: str, signal_name: str) -> Dict[str, str]:
    """获取信号的 (root_cause / lesson / verification) 模板。"""
    _SIG = _load_signal_names()
    sig = _SIG.get(layer, {}).get(sub_layer, {}).get(signal_name, {})
    return {
        "root_cause": sig.get("root_cause", f"{layer}/{sub_layer}/{signal_name}"),
        "lesson":     sig.get("lesson", f"识别 {signal_name} → 调整 {sub_layer} 子层逻辑"),
        "verification": sig.get("verification", "回放近 30 条同 sub_layer 信号, 触发率 ≤ 35% 即认为有效"),
    }


# ═══════════════════════════════════════════════════════════════════════════
# 推断规则: 基于 ai_decisions 行反推最贴切的 (layer, sub_layer, signal_name)
# 这是 v188 重写 paper_reflection_worker._classify_root_cause 的核心。
# 推断优先级(自上而下,命中即返回,共 15 条规则 + 1 个兜底)。
# ═══════════════════════════════════════════════════════════════════════════

def infer_layer_sub_signal(d: Dict[str, Any]) -> Dict[str, str]:
    """基于 ai_decisions 行反推三层标签 + root_cause_hint。"""
    action = str(d.get("suggested_action") or "HOLD").upper()
    pnl = d.get("forward_pnl") or 0.0
    is_correct = str(d.get("is_correct") or "").upper()
    sentiment = d.get("sentiment_score")
    event_strength = str(d.get("event_strength") or "").lower()
    market_confirm = str(d.get("market_confirmation") or "").lower()
    reasoning = str(d.get("reasoning") or "").strip()
    event_struct_raw = d.get("event_struct") or ""
    no_trade_reason = str(d.get("no_trade_reason") or "")
    title_text = (str(d.get("title") or "") + " " + str(d.get("news_summary") or "")).lower()
    actor = str(d.get("event_actor") or "").lower()

    # 1: event_struct 解析错(资产不一致)
    if event_struct_raw and isinstance(event_struct_raw, str):
        try:
            import json as _json
            es = _json.loads(event_struct_raw)
            if isinstance(es, dict) and es.get("asset"):
                target = str(d.get("target_asset") or "").upper()
                if target and str(es.get("asset", "")).upper() != target:
                    return {"layer": "analysis", "sub_layer": "event_struct",
                            "signal_name": "asset_mismatch",
                            "root_cause_hint": f"event_struct.asset={es.get('asset')} != target={target}"}
        except Exception:
            pass

    # 2: sentiment 符号与盘面反向
    if isinstance(sentiment, (int, float)) and market_confirm in ("negative", "no"):
        if (sentiment > 0 and action == "SELL") or (sentiment < 0 and action == "BUY"):
            return {"layer": "analysis", "sub_layer": "sentiment_score",
                    "signal_name": "score_sign_wrong",
                    "root_cause_hint": f"sentiment={sentiment:+.2f} 与盘面反向, action={action}"}

    # 3: reasoning 为空 → 兜底填词
    # v200+ Q6: reasoning_path 是完整八段, reasoning 只是摘要. 若 reasoning_path
    # 有内容, reasoning 空不代表"完全没推理", 让事件逻辑层 rules 31-36 优先诊断.
    if (not reasoning or reasoning in (
        "服务端兜底: LLM 未填 reasoning", "数据缺失, 无法定位",
    )) and not str(d.get("reasoning_path") or "").strip():
        return {"layer": "analysis", "sub_layer": "llm_output",
                "signal_name": "empty_reasoning",
                "root_cause_hint": f"reasoning 为空({reasoning[:30]})"}

    # 4: FOMC/央行窗口未识别
    if any(k in title_text for k in ("fomc", "美联储", "fed ", "cpi", "ppi", "非农", "央行", "鲍威尔")):
        if event_strength != "high":
            return {"layer": "analysis", "sub_layer": "macro_context",
                    "signal_name": "fomc_window_missed",
                    "root_cause_hint": "FOMC/CPI 新闻但 event_strength≠high, 窗口未识别"}

    # 5: 已注入经验但仍亏 → 经验被误读
    if d.get("persona_match_id") and is_correct == "LOSS" \
            and (isinstance(pnl, (int, float)) and pnl < -0.005):
        return {"layer": "analysis", "sub_layer": "knowledge_inject",
                "signal_name": "exp_misinterpreted",
                "root_cause_hint": f"已注入经验但仍亏 → 经验被误读?"}

    # 6: 特朗普强信号但仍亏
    if "trump" in actor and (isinstance(pnl, (int, float)) and pnl < -0.005) \
            and action in ("BUY", "SELL"):
        return {"layer": "analysis", "sub_layer": "persona_match",
                "signal_name": "actor_strength_overrated",
                "root_cause_hint": f"actor=trump 但 pnl={pnl:+.4f} → 人物权重虚高"}

    # 7: low_strength 仍 BUY/SELL
    if event_strength == "low" and action in ("BUY", "SELL") and is_correct == "LOSS":
        return {"layer": "filter", "sub_layer": "strength_threshold",
                "signal_name": "low_strength_should_hold",
                "root_cause_hint": f"event_strength=low 仍 {action}, 应强制 HOLD"}

    # 8: sentiment vs 盘面矛盾未识别
    if isinstance(sentiment, (int, float)) and market_confirm in ("negative", "no"):
        return {"layer": "filter", "sub_layer": "contradiction_check",
                "signal_name": "sentiment_vs_market",
                "root_cause_hint": f"sentiment={sentiment:+.2f} 但盘面确认 {market_confirm}"}

    # 9: BUY 但 confidence 低
    confidence = d.get("confidence") or d.get("persona_weight")
    if action == "BUY" and isinstance(confidence, (int, float)) and confidence < 0.55 \
            and is_correct == "LOSS":
        return {"layer": "filter", "sub_layer": "confidence_gate",
                "signal_name": "below_min_score_buy",
                "root_cause_hint": f"BUY 但 confidence={confidence:.2f}<0.55"}

    # 10: 主品种判错(target=NONE)
    if str(d.get("target_asset") or "").upper() in ("NONE", "", "N/A"):
        return {"layer": "filter", "sub_layer": "asset_match",
                "signal_name": "main_asset_missed",
                "root_cause_hint": "target_asset=NONE, asset_match 未找到主品种"}

    # 11: 数据源降级
    if any(k in no_trade_reason for k in ("数据不可用", "数据缺失", "无 K 线", "源降级")):
        return {"layer": "data", "sub_layer": "feed_status",
                "signal_name": "source_degraded",
                "root_cause_hint": f"no_trade_reason={no_trade_reason[:40]}"}

    # 12: 新闻延迟
    if "延迟" in no_trade_reason or "stale" in no_trade_reason.lower():
        return {"layer": "data", "sub_layer": "latency",
                "signal_name": "stale_news",
                "root_cause_hint": f"no_trade_reason 含 '延迟': {no_trade_reason[:40]}"}

    # 13: 入场过晚(入场后立即反向)
    if action in ("BUY", "SELL") and d.get("entry_price") and d.get("exit_price"):
        try:
            entry = float(d["entry_price"]); exit_ = float(d["exit_price"])
            if action == "BUY" and exit_ < entry * 0.99 \
                    and (isinstance(pnl, (int, float)) and pnl < -0.005):
                return {"layer": "timing", "sub_layer": "timelag_to_event",
                        "signal_name": "entered_too_late",
                        "root_cause_hint": "入场后立即反向, 错失方向"}
        except (ValueError, TypeError):
            pass

    # 14: paper 停跑但本应是 BUY/SELL
    if action == "HOLD" and not d.get("paper_trading_run_id") and is_correct == "WIN":
        return {"layer": "execution", "sub_layer": "paper_run_state",
                "signal_name": "run_inactive",
                "root_cause_hint": "paper_run 停跑但本应是 BUY/SELL"}

    # 15: 门卫判错
    if str(d.get("gate_verdict") or "") == "排除" \
            and market_confirm == "yes" and is_correct == "WIN":
        return {"layer": "gate", "sub_layer": "gate_verdict",
                "signal_name": "exclude_was_pass",
                "root_cause_hint": "verdict=排除 但实际应放过(盘面已确认)"}

    # 16: v200+ Q5 — E018 同类新闻历史反例(仅当 d.baseline 锁存在时则触发)
    #     不重复查 signal_performance 拖慢分类器; reflection_checkers.check_historical_counterfactual
    #     是独立 SQL 路径, 与本规则双轨产出, 任一命中即可。
    baseline = d.get("baseline") or {}
    by_type = baseline.get("by_type") or {}
    _evt_code = str(d.get("event_type_code") or "").lower()
    _tgt_asset = str(d.get("target_asset") or "").upper()
    if action in ("BUY", "SELL") and _evt_code and _tgt_asset and isinstance(by_type, dict):
        _sample = by_type.get(_evt_code)
        if isinstance(_sample, dict):
            try:
                _sample_n = int(_sample.get("sample_n") or 0)
                _win_rate = float(_sample.get("win_rate") or 0.0)
                _avg_pnl = float(_sample.get("avg_pnl_4h") or 0.0)
            except (TypeError, ValueError):
                _sample_n = 0
            if _sample_n >= 3 and _win_rate <= 0.33 and _avg_pnl <= -0.005:
                return {"layer": "event_logic", "sub_layer": "counter_consensus",
                        "signal_name": "historical_counterfactual",
                        "root_cause_hint": (f"同类新闻({_evt_code}, {_tgt_asset})过去 30d "
                                            f"n={_sample_n}胜率{_win_rate:.0%} 平均{_avg_pnl:+.2%}, "
                                            f"本次 action={action} 同向但历史反向 → E018 路径")}

    # 17-22: v200 Q3 修复 — news_source_class 6 子层
    # 设计动机: source_status.data_sources 里"宏观/聚合/新闻"分类与 ontology 子层不对齐,
    # 触发 reflection 错误归类到 unknown. 现在显式映射:
    #   - market_category + title 关键词 → news_source_class 子层
    #   - 6 类: macro_only / aggregated_only / single_source / multi_consistent /
    #           multi_conflict / source_degraded
    # 决策字段 `d.source_category` 由 ai_worker 在 INSERT 时写入 (raw_news.source_type).
    _src_cat = str(d.get("source_category") or d.get("source_type") or "").lower()
    _mc2 = str(d.get("market_category") or "").upper()
    if _src_cat or _mc2:
        # 25: asset_category_misclassified — 金融产品大类错配 (v200+ Q6)
        #   必须在 rule 17 之前: AAPL/MACRO 应先识别为"分类错"而非"宏源"
        #   注意: BTC/ETH/SOL/XAU/WTI 等已知非股票品种必须排除,避免误判
        _target_asset_v25 = str(d.get("target_asset") or "").upper()
        if _target_asset_v25 and _target_asset_v25 not in ("NONE", "N/A", ""):
            _non_stock = {"BTC", "ETH", "SOL", "XRP", "ADA", "DOGE", "BNB", "DOT",
                          "MATIC", "AVAX", "LTC", "LINK", "UNI", "ATOM",
                          "XAU", "XAG", "WTI", "BRENT", "NATGAS", "COPPER",
                          "USD", "EUR", "JPY", "GBP", "CHF", "CAD", "AUD", "NZD",
                          "UST", "DXY", "YIELD", "BOND", "GOLD", "OIL", "SILVER",
                          "SPX", "NDX", "DJI", "VIX"}
            if len(_target_asset_v25) <= 5 and _target_asset_v25.isalpha() \
                    and _target_asset_v25 not in _non_stock \
                    and _mc2 in ("MACRO", "", "OTHER"):
                return {"layer": "analysis", "sub_layer": "signal_quality_v200",
                        "signal_name": "asset_category_misclassified",
                        "root_cause_hint": f"target={_target_asset_v25} 像是股票,但 market_category={_mc2 or '空'}"}
            if _mc2 == "STOCK" and _target_asset_v25 in (
                    "USD", "EUR", "JPY", "GBP", "CHF", "UST", "DXY",
                    "YIELD", "BOND", "GOLD", "OIL"):
                return {"layer": "analysis", "sub_layer": "signal_quality_v200",
                        "signal_name": "asset_category_misclassified",
                        "root_cause_hint": f"market_category=STOCK 但 target={_target_asset_v25} 是宏观品种"}

        # 17: macro_source_only (market_category=MACRO 且无微观盘面确认)
        if _mc2 == "MACRO" and market_confirm in ("", "unknown", "no"):
            return {"layer": "analysis", "sub_layer": "news_source_class",
                    "signal_name": "macro_source_only",
                    "root_cause_hint": f"market_category=MACRO 但 market_confirm={market_confirm}, "
                                       f"无盘面印证, 应降 confidence"}
        # 18: aggregated_source_only (RSS 二手源 + 无 cross_net_source)
        if _src_cat in ("aggregated", "rss_jin10", "jin10", "wallstreetcn"):
            if not str(d.get("cross_net_source") or "").strip():
                return {"layer": "analysis", "sub_layer": "news_source_class",
                        "signal_name": "aggregated_source_only",
                        "root_cause_hint": f"source={_src_cat} 二手聚合源, "
                                           f"cross_net_source 空, 应要求原始信源"}
        # 19: single_source_only (cluster_size=1 且无 cross_net_source)
        if int(d.get("cluster_size") or 1) == 1 and not str(d.get("cross_net_source") or "").strip():
            return {"layer": "analysis", "sub_layer": "news_source_class",
                    "signal_name": "single_source_only",
                    "root_cause_hint": "cluster_size=1 单源, 无 cross-validation, 应降 confidence"}
        # 20: multi_source_consistent (cluster_size >= 3 + market_confirm=yes) — 正向 baseline
        if int(d.get("cluster_size") or 1) >= 3 and market_confirm == "yes":
            return {"layer": "analysis", "sub_layer": "news_source_class",
                    "signal_name": "multi_source_consistent",
                    "root_cause_hint": f"cluster_size={d.get('cluster_size')} 多源一致, "
                                       f"market_confirm=yes, baseline 正确"}
        # 21: multi_source_conflict (cluster_size >= 3 + market_confirm=no + LOSS)
        if (int(d.get("cluster_size") or 1) >= 3
                and market_confirm == "no" and is_correct == "LOSS"):
            return {"layer": "filter", "sub_layer": "contradiction_check",
                    "signal_name": "multi_source_conflict",
                    "root_cause_hint": "cluster_size>=3 多源冲突 + market_confirm=no, "
                                       "LLM 选择性引用导致方向错, 应强制 contradiction_check"}
        # 22: source_degraded (no_trade_reason 含"降级/源" 或 source_status.degraded=1)
        if any(k in no_trade_reason for k in ("源降级", "数据源降级", "RSS 失败", "源 degraded")):
            return {"layer": "data", "sub_layer": "feed_status",
                    "signal_name": "source_degraded",
                    "root_cause_hint": f"no_trade_reason={no_trade_reason[:40]}"}

    # v200+ Q6 — 「信号质量差」细分 (signal_quality_v200 8 类)
    # 触发数据源: ai_decisions.impact_score / target_asset / market_category /
    #   is_sibling / hard_data_evidence + raw_news.content_fingerprint / first_seen_ts.
    # paper_reflection_worker._DEDUPE_SELECT 必须 LEFT JOIN raw_news n 才能拿到
    # content_fingerprint / first_seen_ts; 没有 JOIN 时本组规则自然失效.
    _impact_score = d.get("impact_score")
    _target_asset = str(d.get("target_asset") or "").upper()
    _market_cat = str(d.get("market_category") or "").upper()
    _is_sibling = int(d.get("is_sibling") or 0)
    _event_type_code = str(d.get("event_type_code") or "")
    _hard_data_evidence = str(d.get("hard_data_evidence") or "").strip()
    _content_fingerprint = str(d.get("content_fingerprint") or "").strip()
    _first_seen_ts = str(d.get("first_seen_ts") or "").strip()
    _news_content = str(d.get("news_content") or "").strip()

    # 23: magnitude_overestimated — LLM 给高 impact 但实际亏
    try:
        _imp = float(_impact_score) if _impact_score is not None else None
    except (TypeError, ValueError):
        _imp = None
    if _imp is not None and _imp >= 70 \
            and action in ("BUY", "SELL") and is_correct == "LOSS" \
            and isinstance(pnl, (int, float)) and pnl < -0.003:
        return {"layer": "analysis", "sub_layer": "signal_quality_v200",
                "signal_name": "magnitude_overestimated",
                "root_cause_hint": f"impact_score={_imp:.0f} 高估,LOSS pnl={pnl:+.4f}"}

    # 24: magnitude_underestimated — LLM 给低 impact 但实际能赚 (HOLD 错过)
    if _imp is not None and _imp <= 30 \
            and action == "HOLD" and is_correct == "WIN" \
            and isinstance(pnl, (int, float)) and pnl > 0.005:
        return {"layer": "analysis", "sub_layer": "signal_quality_v200",
                "signal_name": "magnitude_underestimated",
                "root_cause_hint": f"impact_score={_imp:.0f} 低估,HOLD 错过 pnl={pnl:+.4f}"}

    # 26: asset_class_misclassified — 金融产品子类错配 (gate_kind vs asset_nature)
    _gate_kind = str(d.get("gate_kind") or "").lower()
    _asset_nature = str(d.get("asset_nature") or "").lower()
    if _gate_kind and _asset_nature:
        _known_conflicts = {
            ("energy", "policy"), ("commodity", "policy"),
            ("tech", "policy"), ("finance", "policy"),
            ("policy", "energy"), ("policy", "commodity"),
        }
        if (_gate_kind, _asset_nature) in _known_conflicts or \
                (_asset_nature, _gate_kind) in _known_conflicts:
            return {"layer": "analysis", "sub_layer": "signal_quality_v200",
                    "signal_name": "asset_class_misclassified",
                    "root_cause_hint": f"gate_kind={_gate_kind} 与 asset_nature={_asset_nature} 冲突"}

    # 27: dirty_news_replay — 老新闻被重新触发 (需 first_seen_ts)
    if _content_fingerprint and _first_seen_ts:
        try:
            from datetime import datetime, timezone
            _fst = datetime.strptime(_first_seen_ts[:19], "%Y-%m-%d %H:%M:%S")
            _fst = _fst.replace(tzinfo=timezone.utc)
            _now = datetime.now(timezone.utc)
            _age_hours = (_now - _fst).total_seconds() / 3600.0
            if _age_hours > 1.0:
                return {"layer": "analysis", "sub_layer": "signal_quality_v200",
                        "signal_name": "dirty_news_replay",
                        "root_cause_hint": f"content_fingerprint={_content_fingerprint[:16]} "
                                           f"已在 {_age_hours:.1f}h 前出现,入库时未去重"}
        except (ValueError, TypeError):
            pass

    # 28: dirty_news_correction — 修正/辟谣/纠错新闻
    _correction_keys = ("澄清", "辟谣", "纠错", "更正", "撤回", "撤回声明",
                       "clarification", "correction", "denied", "retraction",
                       "fact check")
    if any(k in title_text or k in _news_content.lower()
           for k in _correction_keys):
        return {"layer": "analysis", "sub_layer": "signal_quality_v200",
                "signal_name": "dirty_news_correction",
                "root_cause_hint": "标题/正文含澄清/辟谣/纠错关键词,被 LLM 当新事件读"}

    # 29: dirty_news_merged — 多事件合并误读 (需 parent_event_type)
    _parent_event_type = str(d.get("parent_event_type") or "")
    if _is_sibling == 1 and _parent_event_type and _event_type_code \
            and _parent_event_type != _event_type_code:
        return {"layer": "analysis", "sub_layer": "signal_quality_v200",
                "signal_name": "dirty_news_merged",
                "root_cause_hint": f"is_sibling=1 但 event_type={_event_type_code} "
                                   f"!= parent_event_type={_parent_event_type}"}

    # 30: dirty_news_partial — 新闻截断/不完整
    if _news_content and len(_news_content) < 50 and not _hard_data_evidence:
        return {"layer": "analysis", "sub_layer": "signal_quality_v200",
                "signal_name": "dirty_news_partial",
                "root_cause_hint": f"news_content 仅 {len(_news_content)} 字 "
                                   f"且 hard_data_evidence 空,信息不足"}

    # ════════════════════════════════════════════════════════════════════════
    # v200+ Q6: event_logic 6 类推理链完整性规则 (rules 31–36)
    # 触发数据源:
    #   rule 31 (causal_chain)        — reasoning_path 缺中间环节
    #   rule 32 (concept_match)       — factor_ranking 概念 vs asset_driver_nodes
    #   rule 33 (cross_asset_chain)   — 跨资产传导方向缺失 / 反向
    #   rule 34 (sentiment_driver_chain) — sentiment_score 符号 vs factor 方向
    #   rule 35 (macro_state_chain)   — regime ↔ action 不一致
    #   rule 36 (summary_chain)       — 总结段与前 6 段因果链断裂
    # 全部 rule 必须 event_logic 层 (最高权重), 直接打头, 不被 analysis 抢先.
    # 缺字段时整组自然失效 → 降级兜底 (basic). 这是设计意图而非 bug.
    # ════════════════════════════════════════════════════════════════════════
    _reasoning_path = str(d.get("reasoning_path") or "").strip()
    _factor_ranking_raw = d.get("factor_ranking") or ""
    _regime_at_decision = str(d.get("regime_at_decision") or "").strip().lower()
    _asset_knowledge = d.get("asset_knowledge") or {}
    _core_drivers = [str(c.get("factor") or "").lower()
                     for c in (_asset_knowledge.get("core_factors") or [])
                     if isinstance(c, dict)]
    _cond_drivers = [str(c.get("factor") or "").lower()
                     for c in (_asset_knowledge.get("condition_factors") or [])
                     if isinstance(c, dict)]
    _all_kg_drivers = _core_drivers + _cond_drivers
    _factor_ranking_list = []
    if _factor_ranking_raw:
        try:
            import json as _fr_json
            _fr_parsed = _fr_json.loads(_factor_ranking_raw) if isinstance(_factor_ranking_raw, str) else _factor_ranking_raw
            if isinstance(_fr_parsed, list):
                _factor_ranking_list = [str(f).lower() for f in _fr_parsed]
            elif isinstance(_fr_parsed, dict):
                _factor_ranking_list = [str(k).lower() for k in _fr_parsed.keys()]
        except (ValueError, TypeError):
            # 兼容非 JSON: 按逗号 / 中文句号切
            _factor_ranking_list = [s.strip().lower()
                                    for s in str(_factor_ranking_raw).replace("、", ",").split(",")
                                    if s.strip()]

    # 31: causal_chain / reasoning_chain_broken
    #   完整八段链至少有 4 段 marker: 总框架/因素定位/驱动力/水位博弈/跨资产/
    #   反共识/逻辑自检/总结。 reasoning_path 缺这些 marker 视为"推理链断裂".
    #   必须 BUY/SELL 且亏 (LOSS) 才告警, 因为 HOLD 没真推理链可言.
    _reasoning_markers = ("[总框架]", "[因素定位]", "[驱动力]", "[水位博弈]",
                         "[跨资产]", "[反共识]", "[逻辑自检]", "[总结]")
    _rp_full = (_reasoning_path + "\n" + reasoning) if _reasoning_path else reasoning
    if action in ("BUY", "SELL") and is_correct == "LOSS" \
            and isinstance(pnl, (int, float)) and pnl < -0.003 \
            and _rp_full and len(_rp_full) > 10:
        _marker_hits = sum(1 for m in _reasoning_markers if m in _rp_full)
        if _marker_hits < 4:
            return {"layer": "event_logic", "sub_layer": "causal_chain",
                    "signal_name": "reasoning_chain_broken",
                    "root_cause_hint": (f"reasoning_path 仅命中 {_marker_hits} 个段标记 "
                                       f"(总框架/因素定位/驱动力/水位博弈/跨资产/反共识/"
                                       f"逻辑自检/总结), 八段链不完整 → action={action} "
                                       f"基于碎片推理, pnl={pnl:+.4f}")}

    # 32: concept_match / concept_misaligned
    #   factor_ranking 概念 ↔ asset_driver_nodes 核心驱动节点.
    #   若 ≥ 1 个因子与 KG 因素名字重叠 → 对齐; 全不重叠 → 概念脱节.
    #   若 KG 没数据 (空 core_factors) → 本规则自然失效, 不误报.
    if action in ("BUY", "SELL") and is_correct == "LOSS" \
            and isinstance(pnl, (int, float)) and pnl < -0.003 \
            and _factor_ranking_list and _all_kg_drivers:
        _overlap = [f for f in _factor_ranking_list
                    if any((k and f in f and k in f)
                            or (k and k in f and f in k) for k in _all_kg_drivers)]
        if not _overlap:
            return {"layer": "event_logic", "sub_layer": "concept_match",
                    "signal_name": "concept_misaligned",
                    "root_cause_hint": (f"factor_ranking={_factor_ranking_list[:3]} 与 "
                                       f"asset_driver_nodes({_core_drivers[:3]}) 全无重叠, "
                                       f"LLM 自由发挥未对齐 KG, action={action} pnl={pnl:+.4f}")}

    # 35: macro_state_chain / macro_state_misread (排在 33 之前: 宏观状态比跨资产更基础)
    #   regime_at_decision 标记 risk_off / bear / trending_down 等熊市状态, 但
    #   action=BUY 且 reasoning 没解释"为何逆势" → 宏观状态未被消化.
    _bearish_regimes = ("risk_off", "bear", "trending_down", "downtrend",
                       "high_vol", "volatile", "crash")
    if action == "BUY" and is_correct == "LOSS" \
            and isinstance(pnl, (int, float)) and pnl < -0.003 \
            and _regime_at_decision and any(r in _regime_at_decision for r in _bearish_regimes) \
            and _rp_full and len(_rp_full) > 10:
        _rp_lower = _rp_full.lower()
        _macro_aware = any(k in _rp_lower for k in
                          ("regime", "宏观状态", "market state", "逆势", "contrarian",
                           "market regime", "总框架"))
        if not _macro_aware:
            return {"layer": "event_logic", "sub_layer": "macro_state_chain",
                    "signal_name": "macro_state_misread",
                    "root_cause_hint": (f"regime={_regime_at_decision} 熊市, BUY 未提"
                                       f"regime/逆势/contrarian, 宏观状态未触达")}

    # 33: cross_asset_chain / cross_asset_unverified (缺跨资产传导)
    #   涉及 crypto/macro/forex/大宗品种 + 决策 cross 与跨资产传导相关, 但 reasoning
    #   没引用任何跨资产标的 (UST/DXY/OIL/黄金/国债/BTC 等), 即未给传导链.
    #   v200+ 实战: 大部分亏损事件"推理链"完全没跨资产方向, 直接归因不可.
    #   排在 rule 35 之后: 宏观状态先判 (大背景), 跨资产是更具体的传导层.
    _cross_asset_targets = {"BTC", "ETH", "SOL", "XAU", "XAG", "WTI", "BRENT",
                            "USD", "EUR", "JPY", "UST", "DXY", "OIL", "GOLD"}
    _cross_asset_keywords = ("ust", "国债", "美债", "美元", "dxy", "美元指数",
                            "黄金", "白银", "原油", "油价", "btc", "eth",
                            "cross-asset", "跨资产", "传导", "联动",
                            "liquidity", "流动性")
    if action in ("BUY", "SELL") and is_correct == "LOSS" \
            and isinstance(pnl, (int, float)) and pnl < -0.003 \
            and _target_asset in _cross_asset_targets \
            and _rp_full and len(_rp_full) > 10:
        _rp_lower = _rp_full.lower()
        if not any(k in _rp_lower for k in _cross_asset_keywords):
            return {"layer": "event_logic", "sub_layer": "cross_asset_chain",
                    "signal_name": "cross_asset_unverified",
                    "root_cause_hint": (f"target={_target_asset} 必涉及跨资产传导, 但 "
                                       f"reasoning 无 UST/DXY/黄金/原油 等任何跨资产关键词, "
                                       f"传导链缺失, pnl={pnl:+.4f}")}

    # 34: sentiment_driver_chain / sentiment_driver_misaligned
    #   sentiment_score 符号 vs factor_ranking 方向.
    #   sentiment > 0 (看涨) 但 factor_ranking 含"紧缩/衰退/通胀失控/利率上行/避险"
    #   等负向词 → sentiment 与驱动因子方向冲突.
    _negative_driver_keys = ("通缩", "紧缩", "衰退", "recession", "deflation",
                            "通胀失控", "利率上行", "避险", "risk-off",
                            "liquidity squeeze", "流动性收紧", "资金外流")
    if action in ("BUY", "SELL") and isinstance(sentiment, (int, float)) \
            and _factor_ranking_list:
        _fr_joined = " ".join(_factor_ranking_list)
        if sentiment > 0.3 and any(k.lower() in _fr_joined for k in _negative_driver_keys) \
                and action == "BUY" and is_correct == "LOSS":
            return {"layer": "event_logic", "sub_layer": "sentiment_driver_chain",
                    "signal_name": "sentiment_driver_misaligned",
                    "root_cause_hint": (f"sentiment={sentiment:+.2f} 看涨 + factor 含紧缩/衰退等负向词"
                                       f" + action=BUY + LOSS, 情绪与驱动因子方向冲突")}

    # 36: summary_chain / summary_chain_broken
    #   总结段([总结]) 必须回引 [因素定位] 和 [驱动力] 至少 1 条核心结论.
    #   简单做法: reasoning_path 含 [总结] 段 + 总结段之后的 100 字不含任何来自
    #   [因素定位] / [驱动力] 段的关键 token, 视为"总结与前 6 段脱节".
    if action in ("BUY", "SELL") and is_correct == "LOSS" \
            and isinstance(pnl, (int, float)) and pnl < -0.003 \
            and "[总结]" in _rp_full:
        _summary_idx = _rp_full.find("[总结]")
        _summary_text = _rp_full[_summary_idx:_summary_idx + 200]
        _pre_summary = _rp_full[:_summary_idx]
        _anchor_pool = ""
        for _marker in ("[因素定位]", "[驱动力]"):
            _idx = _pre_summary.find(_marker)
            if _idx >= 0:
                _anchor_pool += _pre_summary[_idx:_idx + 60]
        _common_tokens = 0
        if _anchor_pool and _summary_text:
            for _tok in _anchor_pool.split():
                _tok = _tok.strip("[]()【】() ,.;:。,.;:!?\"'")
                if len(_tok) >= 2 and _tok in _summary_text:
                    _common_tokens += 1
        if _anchor_pool and _common_tokens == 0:
            return {"layer": "event_logic", "sub_layer": "summary_chain",
                    "signal_name": "summary_chain_broken",
                    "root_cause_hint": (f"reasoning_path 含 [总结] 段, 但未回引 [因素定位]/"
                                       f"[驱动力] 段任何 2+ 字 token, 总结与因果链脱节")}
    # 显式暴露"未命中规则"的事实,便于 monitor 看到分类器盲区。
    # root_cause_hint 包含 no_trade_reason + action + pnl + market_confirm,
    # 给后续 P1+ (LLM deep reflection) 提供完整输入来反推真实信号。
    return {"layer": "analysis", "sub_layer": "llm_output",
            "signal_name": "unknown",
            "root_cause_hint": (f"未命中 14 条具体规则(action={action}, "
                                f"event_strength={event_strength}, "
                                f"market_confirm={market_confirm}, "
                                f"is_correct={is_correct}, pnl={pnl}, "
                                f"no_trade={no_trade_reason[:30]})")}


__all__ = [
    "LAYERS", "SUB_LAYERS",
    "list_all_signals", "is_valid", "get_signal_template",
    "infer_layer_sub_signal",
]