#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""v188 反思 ontology 的 dim3 信号字典 (从 reflection_ontology.py 拆出)。"""
from __future__ import annotations
from typing import Any, Dict


SIGNAL_NAMES: Dict[str, Dict[str, Dict[str, Dict[str, str]]]] = {
    "analysis": {
        "llm_output": {
            "fabrication_hard": {
                "root_cause": "LLM 虚构数据(无 source/timestamp 引用)",
                "lesson": "E011 触发 → 必须有 hard_data_evidence 才能 BUY",
                "verification": "回放 30 条 LLM 输出, 看 fabrication 比例",
            },
            "narrative_bias": {
                "root_cause": "LLM narrative 单边偏置",
                "lesson": "注入前先做矛盾校验(sentiment vs 盘面)",
                "verification": "A/B 24h: 注入矛盾校验前后 win_rate 对比",
            },
            "context_overload": {
                "root_cause": "LLM 输入上下文过长(>5000 token)",
                "lesson": "压缩 prompt 到 3000 token 内, 关键证据置顶",
                "verification": "prompt 长度 vs win_rate 散点图",
            },
            "empty_reasoning": {
                "root_cause": "reasoning 字段为空(兜底填词/超时截断)",
                "lesson": "LLM 超时则强制 HOLD, 不写入空 reasoning",
                "verification": "近 30 条 empty_reasoning 的 forward_pnl",
            },
            # v188 P0-3: 显式兜底信号 — 14 条规则未命中时落这里,
            # 用于监控分类器盲区(以前是 catch-all narrative_bias,无诊断价值)
            "unknown": {
                "root_cause": "14 条规则未命中, 分类器无法诊断",
                "lesson": "扫 root_cause_hint 找新规则或扩展现有规则",
                "verification": "unknown 占比 ≤ 20% (7d 滚动) 即认为分类器充分",
            },
        },
        "sentiment_score": {
            "score_sign_wrong": {
                "root_cause": "sentiment_score 符号与 narrative 相反",
                "lesson": "score 与 narrative 同向校验, 反向时强制 HOLD",
                "verification": "回放近 30 条 score 符号错案例",
            },
            "score_magnitude_inflated": {
                "root_cause": "sentiment_score 绝对值 > 0.8 但 evidence < 2 条",
                "lesson": "E009 触发 → 置信度虚高需降低 1 档",
                "verification": "回放近 30 条 E009 案例 win_rate",
            },
            "score_outlier": {
                "root_cause": "sentiment 偏离历史均值 > 3σ",
                "lesson": "outlier 强制走 contradiction_check 二次确认",
                "verification": "近 30 条 outlier win_rate",
            },
        },
        "event_struct": {
            "actor_misclassified": {
                "root_cause": "actor / event_type 解析错误",
                "lesson": "event_struct 入库前 schema 校验",
                "verification": "近 30 条 actor 错案例 win_rate",
            },
            "asset_mismatch": {
                "root_cause": "event_struct.asset 与 target_asset 不一致",
                "lesson": "E008 触发 → asset_match 优先于 LLM 选品种",
                "verification": "近 30 条 asset_mismatch 案例 win_rate",
            },
            "causal_missing": {
                "root_cause": "event_struct.causal_chain 缺关键节点",
                "lesson": "E004 触发 → 注入 asset_driver_nodes 因果图谱",
                "verification": "近 30 条 causal_missing 案例",
            },
        },
        "macro_context": {
            "regime_misread": {
                "root_cause": "regime_at_decision 判错",
                "lesson": "regime 输入前先回看 5m K 线波动率",
                "verification": "回放 regime_misread 时段 forward_pnl",
            },
            "macro_anchor_stale": {
                "root_cause": "macro_snapshots 24h 未更新",
                "lesson": "macro_anchor > 4h 应自动拉新一次",
                "verification": "stale 案例的 macro 信号质量",
            },
            "fomc_window_missed": {
                "root_cause": "FOMC/CPI/央行窗口未识别",
                "lesson": "FOMC/央行日前 24h 启用高 volatility 过滤",
                "verification": "回放过去 3 次 FOMC 案例",
            },
        },
        "factor_chain": {
            "priority_inverted": {
                "root_cause": "factor_ranking priority 倒置",
                "lesson": "factor_ranking 入库前与 review_insights 对齐",
                "verification": "近 30 条 priority_inverted 案例",
            },
            "chain_broken": {
                "root_cause": "因子链中间断裂",
                "lesson": "强制每个 factor 必须有上一阶因子的引用",
                "verification": "近 30 条 chain_broken 案例",
            },
            "scoring_arbitrary": {
                "root_cause": "打分差异 < 0.05(几乎随机)",
                "lesson": "最小差分阈值设为 0.1, 否则重排序",
                "verification": "近 30 条 scoring_arbitrary 案例",
            },
        },
        "cross_asset_transmission": {
            "wrong_lead_lag": {
                "root_cause": "跨品种领先/滞后关系倒置",
                "lesson": "用过去 30 天 1h K 线重算 lead-lag",
                "verification": "近 30 条 wrong_lead_lag 案例",
            },
            "no_propagation": {
                "root_cause": "假设会传导但未传导",
                "lesson": "传导未发生 → 强制 HOLD 不跟随",
                "verification": "近 30 条 no_propagation 案例",
            },
        },
        "knowledge_inject": {
            "exp_stale": {
                "root_cause": "注入经验已归档但被加载",
                "lesson": "load_experiences 已过滤 active, 但 LLM 漏读 status 字段",
                "verification": "grep 过去 7d 注入文本里的 archived 字眼",
            },
            "exp_w_misaligned": {
                "root_cause": "经验 weight 与本次场景不匹配",
                "lesson": "weight<0.3 的经验不应当显式引用, 仅作背景",
                "verification": "近 30 条 exp_w<0.3 仍被引用的案例",
            },
            "exp_misinterpreted": {
                "root_cause": "LLM 误读经验(把'反向'读成'正向')",
                "lesson": "经验内容前加 DO/DONT 显式标记方向",
                "verification": "LLM 引用经验的方向一致性检查",
            },
        },
        "persona_match": {
            "actor_strength_overrated": {
                "root_cause": "persona_weight 虚高(把 V 神权重设成特朗普)",
                "lesson": "persona_weight 上限 = 经验库最大权重",
                "verification": "近 30 条 overrated 案例 win_rate",
            },
            "actor_strength_underrated": {
                "root_cause": "persona_weight 虚低(政府官员对核心品种)",
                "lesson": "政府官员对核心品种设最小权重 0.6",
                "verification": "近 30 条 underrated 案例",
            },
            "no_match_for_known_actor": {
                "root_cause": "已知 actor 无经验匹配(经验库覆盖不全)",
                "lesson": "每周日扫描新 actor → 自动建议新增经验",
                "verification": "经验库 actor 覆盖率 vs 实际 actor 集合",
            },
        },
        "news_organizer": {
            "summary_too_short": {
                "root_cause": "新闻摘要截断导致核心信号丢失",
                "lesson": "summary 长度 < 50 字则判 noise",
                "verification": "近 30 条 short summary win_rate",
            },
            "noise_in_summary": {
                "root_cause": "noise 新闻混进核心摘要",
                "lesson": "is_noise=1 的新闻永远不进 summary",
                "verification": "noise-in-summary 频次",
            },
            "dup_in_summary": {
                "root_cause": "重复新闻计入核心摘要",
                "lesson": "同 content_fingerprint 去重, 只取第一条",
                "verification": "近 30 条 dup_in_summary 案例",
            },
        },
        # v200 Q3 修复: news_source_class 子层 (6 类)
        # 设计动机: 之前 source_status.data_sources 里 "宏观"/"聚合"/"新闻" 分类
        #   与 reflection_ontology 子层不对齐, 触发 reflection 错误归类到 unknown.
        # 现在显式列 6 类: macro_source_only / aggregated_source_only /
        #   single_source_only / multi_source_consistent / multi_source_conflict /
        #   source_degraded. 每类对应一类「数据源 metadata 与 LLM 输出不一致」情形.
        "news_source_class": {
            "macro_source_only": {
                "root_cause": "仅宏观数据源(央行/政府/IMF)被引用, 无盘面印证",
                "lesson": "macro-only 应降 confidence 上限 0.6",
                "verification": "近 30 条 macro-only 案例 win_rate",
            },
            "aggregated_source_only": {
                "root_cause": "仅聚合/二手数据源(金十/华尔街见闻), 无原始信源",
                "lesson": "aggregated-only 应强制要求 cross_net_source",
                "verification": "近 30 条 aggregated-only 案例 win_rate",
            },
            "single_source_only": {
                "root_cause": "仅单一信源(单一 RSS/单一推主), 无 cross-validation",
                "lesson": "single-source 应降 confidence 上限 0.5",
                "verification": "近 30 条 single-source 案例 win_rate",
            },
            "multi_source_consistent": {
                "root_cause": "多源一致, LLM 输出方向正确(正向案例, 无需修复)",
                "lesson": "保留作为 baseline",
                "verification": "近 30 条 multi-source-consistent 案例 win_rate",
            },
            "multi_source_conflict": {
                "root_cause": "多源冲突, LLM 选择性引用导致方向错误",
                "lesson": "conflict 时强制要求 contradiction_check 二次确认",
                "verification": "近 30 条 multi-source-conflict 案例 win_rate",
            },
            "source_degraded": {
                "root_cause": "数据源降级(RSS 抓取失败 → 用金十兜底)",
                "lesson": "降级时 confidence 上限 0.55 + 标注 source_degraded_at",
                "verification": "近 30 条 degraded 案例 win_rate",
            },
        },
        # v200+ Q6 修复: signal_quality_v200 子层 (8 类) — 「信号质量差」细分
        # 设计动机: 之前「信号质量差」归在 analysis/llm_output/unknown 兜底,
        #   只知道"LLM 输出差"但无法区分"影响大小估错"vs"产品类别分错"vs"脏数据拉进来".
        # 现在拆 8 类,每类对应一种可独立诊断的信号质量问题:
        #   1) magnitude_overestimated   — 影响大小高估(LLM 70+ 但实际亏)
        #   2) magnitude_underestimated  — 影响大小低估(LLM <30 但实际应能赚到)
        #   3) asset_category_misclassified  — 金融产品大类错配(STOCK→MACRO 等)
        #   4) asset_class_misclassified     — 金融产品子类错配(energy→policy 等)
        #   5) dirty_news_replay         — 老新闻被重新触发(content_fingerprint 重复)
        #   6) dirty_news_correction     — 修正/辟谣/纠错新闻被当新事件
        #   7) dirty_news_merged         — 多事件被合并误读(is_sibling 但 event 不同)
        #   8) dirty_news_partial        — 截断/不完整新闻(字数<50 或 hard_data 空)
        # 触发数据来源: ai_decisions.impact_score / target_asset / market_category /
        #   is_sibling / hard_data_evidence + JOIN raw_news.content_fingerprint / first_seen_ts.
        # 每类都有独立 lesson → 可以被 strategy_param_adjustments 独立抽取,
        # 不用再"全归 LLM 错" → 大幅提升反思系统的诊断价值.
        "signal_quality_v200": {
            "magnitude_overestimated": {
                "root_cause": "LLM 把影响估大了(impact_score>=70 但实际亏)",
                "lesson": "高 impact 触发前必须看 cross_asset 1h K 线印证,否则降 size 50%",
                "verification": "近 30 条 high-impact LOSS 案例 win_rate (期望降 size 后改善)",
            },
            "magnitude_underestimated": {
                "root_cause": "LLM 把影响估小了(impact_score<30 但 HOLD 错过 WIN)",
                "lesson": "low impact 也需扫 hard_data_evidence,若 ≥2 条硬数据则升级 impact 档",
                "verification": "近 30 条 low-impact HOLD 但 4h pnl>+0.5% 案例",
            },
            "asset_category_misclassified": {
                "root_cause": "金融产品大类错配(应是 STOCK/LLM 归 MACRO 等)",
                "lesson": "target_asset 命中具体股票→market_category 必为 STOCK,否则 trigger 反查",
                "verification": "近 30 条 market_category != asset_kind 案例 win_rate",
            },
            "asset_class_misclassified": {
                "root_cause": "金融产品子类错配(应是 ENERGY/LLM 归 POLICY 等)",
                "lesson": "gate_kind 与 asset_nature 不一致时,二次 LLM 重分类",
                "verification": "近 30 条 gate_kind != asset_nature 案例 win_rate",
            },
            "dirty_news_replay": {
                "root_cause": "老新闻被重新触发(content_fingerprint 已在 DB 中)",
                "lesson": "content_fingerprint 入库时强制 7d 去重,7d 内重复应 verdict=排除",
                "verification": "近 30 条同 fingerprint 二次触发案例 win_rate",
            },
            "dirty_news_correction": {
                "root_cause": "修正/辟谣/纠错新闻被 LLM 当新事件读",
                "lesson": "标题含「澄清/辟谣/纠错/更正/撤回」→ verdict=排除,仅 patch 历史",
                "verification": "近 30 条 correction_news 案例 win_rate",
            },
            "dirty_news_merged": {
                "root_cause": "多事件被合并误读(is_sibling=1 但 event_type 不同)",
                "lesson": "is_sibling 决策必带 parent_id,event_type 不同时强制拆分",
                "verification": "近 30 条 sibling_event_type_diff 案例 win_rate",
            },
            "dirty_news_partial": {
                "root_cause": "新闻截断/不完整(content<50字 或 hard_data_evidence 空)",
                "lesson": "partial content → 强制 incompleteness 标记,confidence 上限 0.4",
                "verification": "近 30 条 partial news 案例 win_rate",
            },
        },
    },
    "filter": {
        "confidence_gate": {
            "below_min_score": {
                "root_cause": "综合得分 < buy_min_score 触发兜底 HOLD",
                "lesson": "buy_min_score 阈值校准(回看 7d pnl)",
                "verification": "回放近 30 条 below_min_score 案例 win_rate",
            },
            "below_min_score_buy": {
                "root_cause": "BUY 触发但 confidence < 0.55",
                "lesson": "BUY 必带 confidence >= 0.55, 否则降为 HOLD",
                "verification": "回放近 30 条 below_min_score_buy 案例",
            },
        },
        "contradiction_check": {
            "sentiment_vs_market": {
                "root_cause": "情感偏多 vs 盘面偏空未识别",
                "lesson": "矛盾时强制 contradiction_check 二次确认",
                "verification": "近 30 条 sentiment_vs_market 案例",
            },
            "news_vs_factor": {
                "root_cause": "新闻 vs factor_ranking 方向冲突未识别",
                "lesson": "注入 factor_ranking 到 prompt 顶部",
                "verification": "近 30 条 news_vs_factor 案例",
            },
        },
        "strength_threshold": {
            "low_strength_should_hold": {
                "root_cause": "event_strength=low 仍 BUY/SELL",
                "lesson": "low strength 不应触发 BUY/SELL, 强制 HOLD",
                "verification": "近 30 条 low_strength_should_hold 案例",
            },
            "high_strength_filtered": {
                "root_cause": "event_strength=high 被过滤(过滤过严)",
                "lesson": "strength > 0.8 必入分析层, 不应被过滤",
                "verification": "近 30 条 high_strength_filtered 案例",
            },
        },
        "asset_match": {
            "no_match_should_exclude": {
                "root_cause": "无匹配品种被强关联",
                "lesson": "asset_match=0 → 直接 verdict=排除",
                "verification": "近 30 条 no_match_should_exclude 案例",
            },
            "main_asset_missed": {
                "root_cause": "主品种判定错(WTI→XAU 错挂)",
                "lesson": "event_struct.asset 为空时 → 反查 causal_chain",
                "verification": "近 30 条 main_asset_missed 案例",
            },
        },
        "temporal_alignment": {
            "event_too_old": {
                "root_cause": "事件 ts 距今 > 24h 仍参与决策",
                "lesson": "events.ts 超过 24h 不应触发新决策",
                "verification": "近 30 条 event_too_old 案例",
            },
            "no_event_window": {
                "root_cause": "无事件锚点的盘面走势被当成事件驱动",
                "lesson": "无 raw_news.id 的 K 线异动 → verdict=排除",
                "verification": "近 30 条 no_event_window 案例",
            },
        },
        "gate_chain": {
            "order_wrong": {
                "root_cause": "闸门调用顺序错",
                "lesson": "标准顺序: contradiction_check → strength_threshold → confidence_gate",
                "verification": "近 30 条 order_wrong 案例",
            },
            "skipped_check": {
                "root_cause": "跳过中间闸门直接到 final",
                "lesson": "任何闸门不可跳过, 强制全链执行",
                "verification": "近 30 条 skipped_check 案例",
            },
        },
        "sector_match": {
            "wrong_sector": {
                "root_cause": "板块挂错(科技新闻→能源板块)",
                "lesson": "sector_match 必须用 dim_event_type 表反查",
                "verification": "近 30 条 wrong_sector 案例",
            },
            "no_sector": {
                "root_cause": "无板块标签",
                "lesson": "无 sector 标签则降为 sector_match=uncertain",
                "verification": "近 30 条 no_sector 案例",
            },
        },
        "fallback_rule": {
            "defaulted_hold_unnecessarily": {
                "root_cause": "low_impact 触发兜底 HOLD 但事件实际有效",
                "lesson": "low_impact 不应直接 HOLD, 走低置信度 BUY",
                "verification": "近 30 条 defaulted_hold_unnecessarily 案例",
            },
            "low_impact_wrongly_set": {
                "root_cause": "low_impact 错设(应是 mid)",
                "lesson": "impact_score 阈值校准(回看 7d)",
                "verification": "近 30 条 low_impact_wrongly_set 案例",
            },
        },
    },
    "data": {
        "feed_status": {
            "source_offline": {
                "root_cause": "数据源断流(quote_sources.status)",
                "lesson": "断流时自动降级到金十兜底",
                "verification": "回放近 30 条 offline 案例 win_rate",
            },
            "source_degraded": {
                "root_cause": "数据源降级(用金十兜底)",
                "lesson": "降级时 confidence 上限 0.55",
                "verification": "回放近 30 条 degraded 案例",
            },
        },
        "latency": {
            "stale_quote": {
                "root_cause": "报价延迟 > 60s",
                "lesson": "stale_quote > 60s 应强制 HOLD",
                "verification": "回放近 30 条 stale_quote 案例",
            },
            "stale_news": {
                "root_cause": "新闻延迟 > 5min",
                "lesson": "stale_news > 5min 不参与决策",
                "verification": "回放近 30 条 stale_news 案例",
            },
        },
        "coverage": {
            "asset_not_covered": {
                "root_cause": "品种无 K 线 / 无报价",
                "lesson": "no-coverage 品种直接排除",
                "verification": "近 30 条 asset_not_covered 案例",
            },
            "window_not_covered": {
                "root_cause": "时间窗内无数据(节假日/盘前)",
                "lesson": "window_not_covered 不强制 HOLD, 走低置信度",
                "verification": "近 30 条 window_not_covered 案例",
            },
        },
        "cleanliness": {
            "dup_row": {
                "root_cause": "raw_news 重复行(同 source 同 ts)",
                "lesson": "入库前 content_fingerprint 去重",
                "verification": "重复行占比",
            },
            "format_anomaly": {
                "root_cause": "字段格式异常(price=0 / volume<0)",
                "lesson": "anomaly 入库时打 flag, 决策时过滤",
                "verification": "anomaly 案例 win_rate",
            },
        },
        "freshness": {
            "data_anchor_stale": {
                "root_cause": "data_anchor_ts > 30min",
                "lesson": "E002 触发 → 主动更新或标 N/A",
                "verification": "近 30 条 E002 案例",
            },
            "regime_stale": {
                "root_cause": "regime_at_decision 24h 未更新",
                "lesson": "regime 自动每 4h 刷新",
                "verification": "近 30 条 regime_stale 案例",
            },
        },
    },
    "timing": {
        "timelag_to_event": {
            "entered_too_late": {
                "root_cause": "入场时间 > 事件发生 30min",
                "lesson": "timelag > 30min 应强制 HOLD",
                "verification": "近 30 条 entered_too_late 案例",
            },
            "exited_too_early": {
                "root_cause": "出场时间 < MFE 时间",
                "lesson": "出场需等到 MFE 拐点确认",
                "verification": "近 30 条 exited_too_early 案例",
            },
        },
        "market_session": {
            "illiquid_session": {
                "root_cause": "亚盘 / 美盘前 1h 流动性差",
                "lesson": "illiquid_session 降低 size 50%",
                "verification": "illiquid session win_rate vs normal",
            },
            "weekend_gap": {
                "root_cause": "周末跳空未识别",
                "lesson": "周末缺口周一开盘前 30min 强制 HOLD",
                "verification": "周末案例 win_rate",
            },
        },
        "event_window": {
            "window_too_short": {
                "root_cause": "事件窗口 < 5min(噪声主导)",
                "lesson": "event_window < 5min 强制 HOLD",
                "verification": "近 30 条 window_too_short 案例",
            },
            "window_too_long": {
                "root_cause": "事件窗口 > 4h(信息衰减)",
                "lesson": "event_window > 4h 拆分多个窗口",
                "verification": "近 30 条 window_too_long 案例",
            },
        },
    },
    "execution": {
        "paper_run_state": {
            "run_inactive": {
                "root_cause": "paper_run 已被暂停但仍在写 ai_decisions",
                "lesson": "run_inactive 时所有 decision 应判 noise",
                "verification": "近 30 条 run_inactive 案例",
            },
        },
        "slippage": {
            "slippage_exceeded": {
                "root_cause": "paper 滑点 > 真实滑点",
                "lesson": "slippage > 0.5% 时强制 HOLD",
                "verification": "近 30 条 slippage_exceeded 案例",
            },
        },
        "position_sizing": {
            "size_zero": {
                "root_cause": "仓位=0 应排除但仍写入",
                "lesson": "size=0 直接不入库",
                "verification": "size_zero 案例占比",
            },
        },
    },
    "strategy": {
        "buy_min_score": {
            "threshold_too_loose": {
                "root_cause": "buy_min_score=30 频繁触发 BUY",
                "lesson": "buy_min_score 阈值校准(回看 7d pnl)",
                "verification": "近 30 条 threshold_too_loose 案例 win_rate",
            },
            "threshold_too_tight": {
                "root_cause": "buy_min_score=70 几乎不触发 BUY",
                "lesson": "buy_min_score 阈值校准",
                "verification": "近 30 条 threshold_too_tight 案例",
            },
        },
        "tp1_mult": {
            "tp1_too_tight": {
                "root_cause": "tp1_mult=0.3 过早止盈",
                "lesson": "tp1_mult 校准到 MFE 拐点",
                "verification": "近 30 条 tp1_too_tight 案例",
            },
            "tp1_too_loose": {
                "root_cause": "tp1_mult=2.0 错失 MFE",
                "lesson": "tp1_mult 校准到合理止盈",
                "verification": "近 30 条 tp1_too_loose 案例",
            },
        },
        "sl_mult": {
            "sl_too_loose": {
                "root_cause": "sl_mult=0.05 止损过宽",
                "lesson": "sl_mult 校准到 0.015",
                "verification": "近 30 条 sl_too_loose 案例",
            },
            "sl_too_tight": {
                "root_cause": "sl_mult=0.005 止损过窄(被噪声止损)",
                "lesson": "sl_mult 校准到 0.01",
                "verification": "近 30 条 sl_too_tight 案例",
            },
        },
        "max_holding_min": {
            "too_short": {
                "root_cause": "持仓 < 5min 错过 MFE",
                "lesson": "min_holding_min >= 5",
                "verification": "近 30 条 too_short 案例",
            },
            "too_long": {
                "root_cause": "持仓 > 4h 未止盈",
                "lesson": "max_holding_min <= 240",
                "verification": "近 30 条 too_long 案例",
            },
        },
        "regime_filter": {
            "regime_misclassify": {
                "root_cause": "regime_at_decision 错(应是 high_vol)",
                "lesson": "regime 判读前先看 5m K 波动率",
                "verification": "regime_misclassify 案例 win_rate",
            },
            "filter_off_in_high_vol": {
                "root_cause": "高波动未启用额外过滤",
                "lesson": "high_vol 强制启用 vol_filter",
                "verification": "high_vol 案例 win_rate",
            },
        },
    },
    "gate": {
        "gate_verdict": {
            "exclude_was_pass": {
                "root_cause": "verdict=排除 应放过",
                "lesson": "excluded 新闻实际有效 → 调低 gate_strength 阈值",
                "verification": "近 30 条 exclude_was_pass 案例",
            },
            "pass_was_exclude": {
                "root_cause": "verdict=可交易 应排除",
                "lesson": "pass 新闻实际无效 → 调高 gate_strength 阈值",
                "verification": "近 30 条 pass_was_exclude 案例",
            },
        },
        "gate_strength": {
            "strength_over": {
                "root_cause": "gate_strength 偏高(>9) 与实际 impact 不符",
                "lesson": "strength > 9 时强制 confidence 上限 0.5",
                "verification": "strength_over 案例 win_rate",
            },
            "strength_under": {
                "root_cause": "gate_strength 偏低(<4) 但事件实际有效",
                "lesson": "strength < 4 时仍可触发 BUY",
                "verification": "strength_under 案例 win_rate",
            },
        },
        "gate_core_impact": {
            "core_impact_missed": {
                "root_cause": "core_impact 漏写关键影响",
                "lesson": "core_impact 必填至少 1 条具体事实",
                "verification": "近 30 条 core_impact_missed 案例",
            },
            "core_impact_hallucinated": {
                "root_cause": "core_impact 含虚构事实",
                "lesson": "core_impact 入库前 fact-check",
                "verification": "近 30 条 hallucinated 案例",
            },
        },
        "gate_main_asset": {
            "main_asset_wrong": {
                "root_cause": "gate_main_asset 判错(WTI 当 XAU)",
                "lesson": "main_asset 必走 asset_match 校验",
                "verification": "近 30 条 main_asset_wrong 案例",
            },
            "main_asset_missing": {
                "root_cause": "gate_main_asset 为空",
                "lesson": "main_asset 空时强制走 fallback",
                "verification": "main_asset_missing 案例",
            },
        },
        "gate_kind": {
            "kind_misclassified": {
                "root_cause": "gate_kind 类别错(macro 当 policy)",
                "lesson": "gate_kind 必走 dim_event_type 反查",
                "verification": "近 30 条 kind_misclassified 案例",
            },
            "kind_too_generic": {
                "root_cause": "gate_kind='other' 应细分",
                "lesson": "kind=other 强制 LLM 重分类",
                "verification": "近 30 条 kind_too_generic 案例",
            },
        },
    },
    # v189/v200+: event_logic 事件逻辑链层（最高权重）
    # — 注册 7 个 sub_layer 的 signals, 让 infer_layer_sub_signal 能给 E014-E018 落标签
    "event_logic": {
        "causal_chain": {
            "reasoning_chain_broken": {
                "root_cause": "reasoning_path 八段推理链缺 ≥ 4 段或乱序",
                "lesson": "E014 触发 → 八段推理链必填(总框架/因素定位/驱动力/水位博弈/跨资产/反共识/逻辑自检/总结)",
                "verification": "近 30 条 E014 案例 win_rate",
            },
        },
        "concept_match": {
            "concept_misaligned": {
                "root_cause": "factor_ranking 概念未命中核心 driver 节点",
                "lesson": "E015 触发 → factor_ranking 入 prompt 顶部, 强制对齐 asset_driver_nodes 核心节点",
                "verification": "近 30 条 E015 案例 win_rate",
            },
        },
        "counter_consensus": {
            "counter_thinking_missing": {
                "root_cause": "reasoning 无任何反向思考/风险提示词",
                "lesson": "E015 触发 → 单边叙事强制加反向思考段(风险/盲点/对立)",
                "verification": "近 30 条无反向思考 win_rate",
            },
            # v200+ Q5: 反思系统事件逻辑层加深 — 同类新闻历史反例
            "historical_counterfactual": {
                "root_cause": "同 type_code 同 asset 历史 30d 样本反向(胜率 ≤ 33% 且平均 4h 收益 ≤ -0.5%)",
                "lesson": "E018 触发 → 同类新闻历史反向,本次方向单必须显式引用反例作为风险提示,否则 HOLD",
                "verification": "近 30 条 E018 案例 win_rate (期望: HOLD 占多数)",
            },
        },
        "cross_asset_chain": {
            "cross_asset_direction_wrong": {
                "root_cause": "action 与 cross_asset_likely 同向品种方向相反",
                "lesson": "E016 触发 → action 必与同向品种传导方向保持一致",
                "verification": "近 30 条 E016 案例 win_rate",
            },
            "cross_asset_unverified": {
                "root_cause": "cross_asset_likely entry 无 evidence/news/reasoning 互证",
                "lesson": "E017 触发 → 每条跨资产传导必须有 hard_data/news/reasoning 至少 1 处提及",
                "verification": "近 30 条 E017 案例 win_rate",
            },
        },
        "sentiment_driver_chain": {
            "sentiment_driver_misaligned": {
                "root_cause": "情绪↔驱动力映射错位",
                "lesson": "sentiment_score 符号与 narrative 同向才能成立",
                "verification": "近 30 条 sentiment_driver_misaligned 案例",
            },
        },
        "macro_state_chain": {
            "macro_state_misread": {
                "root_cause": "宏观状态(regime)判错",
                "lesson": "regime 输入前先看 5m K 线波动率",
                "verification": "近 30 条 macro_state_misread 案例 win_rate",
            },
        },
        "summary_chain": {
            "summary_chain_broken": {
                "root_cause": "总结陈述与前 6 段推理链脱节",
                "lesson": "总结段必须显式回引 [因素定位] 和 [驱动力] 的 1-2 条核心结论",
                "verification": "近 30 条 summary_chain_broken 案例 win_rate",
            },
        },
    },
    # v225+ reasoning_logic 层 — 7 个 LLM 推理质量子层 × signal_name
    "reasoning_logic": {
        "assumption": {
            "assumption_unstated": {
                "root_cause": "reasoning_path 完全没出现『假设/前提/若/如果』关键词 → 高风险决策无显式前提",
                "lesson": "E021 触发 → reasoning 第一段必须明示 1-3 条前提（数据/事件/时间窗口）",
                "verification": "近 30 条 E021 案例 win_rate",
            },
        },
        "counterfactual": {
            "counterfactual_missing": {
                "root_cause": "reasoning 无任何『如果错了/反之/what if』反向情景关键词",
                "lesson": "E022 触发 → 强制加反向情景段（『如果反向会怎样』『止损位置』）",
                "verification": "近 30 条 E022 案例 win_rate",
            },
        },
        "calibration": {
            "confidence_overstated": {
                "root_cause": "evidence_confidence ≥ 70 但实际亏损 forward_pnl < -0.5% → 置信度严重虚高",
                "lesson": "E023 触发 → 置信度必须基于硬证据条数 + 历史胜率，亏损后立即降档",
                "verification": "近 30 条 E023 案例校准回归（confidence vs actual win_rate）",
            },
            "confidence_understated": {
                "root_cause": "evidence_confidence ≤ 30 但实际盈利 forward_pnl > 1% → 过度保守",
                "lesson": "E023 变种 → 同类事件下次主动提升置信度档位",
                "verification": "近 30 条 confidence_understated 案例 win_rate",
            },
        },
        "history": {
            "historical_uncited": {
                "root_cause": "reasoning_path 未引用任何历史同类案例（找不到『上次/历史/类似』关键词）",
                "lesson": "E024 触发 → 同类事件前必须引用 1 条历史样本作为基准",
                "verification": "近 30 条 E024 案例 win_rate",
            },
        },
        "cross_asset_consistency": {
            "cross_asset_inconsistent": {
                "root_cause": "action 与 cross_asset_likely 中同向品种方向相反（如 BTC 目标做多但 BTC cross=跌）",
                "lesson": "E025 触发 → action 必与同向品种传导方向一致，否则 HOLD",
                "verification": "近 30 条 E025 案例 win_rate（期望 0% win）",
            },
        },
        "mechanism": {
            "causal_mechanism_vague": {
                "root_cause": "reasoning 因侧/果侧关键词不齐全（缺『因为/所以/因此/导致』完整 因→果 结构）",
                "lesson": "E026 触发 → 推理必须显式写『因为 X 所以 Y → 期望 Z』完整三段",
                "verification": "近 30 条 E026 案例 win_rate",
            },
        },
        "confidence_evidence": {
            "confidence_evidence_mismatch": {
                "root_cause": "evidence_confidence ≥ 65 但 hard_data_evidence < 2 条事实 → 过度自信",
                "lesson": "E027 触发 → 置信度必须与硬证据条数挂钩（每条硬证据 +10，最高 70）",
                "verification": "近 30 条 E027 案例校准回归",
            },
        },
    },
    # v231+ logic_analysis 层 — 形式逻辑有效性 (E028-E033, 详见 reflection_logic_analysis.py)
    "logic_analysis": {
        "formal_validity": {
            "self_contradiction": {
                "root_cause": "reasoning 单句/跨句同时出现正反方向词（看涨+看跌）且无对比标记 → 自相矛盾",
                "lesson": "E028 触发 → 推理必须显式标注『但是/然而/却』等对比标记, 否则高 severity",
                "verification": "近 30 条 E028 案例 win_rate（期望 ~0% — 自相矛盾的决策无内在支撑）",
            },
            "non_sequitur": {
                "root_cause": "[总结] 段引入方向词 (BUY/SELL/看涨/看跌) 但前文 body 不含同方向词 → 结论悬空",
                "lesson": "E029 触发 → 结论必须由前提支撑, 否则降 confidence 1 档",
                "verification": "近 30 条 E029 案例 win_rate（期望 ~30% 以下, 远低于 baseline）",
            },
            "false_dichotomy": {
                "root_cause": "'要么 A 要么 B / 不是 A 就是 B' 等排他性表述无第三选项",
                "lesson": "E030 触发 → 推理避免排他性表述, 显式列出第三选项 (HOLD/减仓/观望/对冲)",
                "verification": "近 30 条 E030 案例 pnl 分布（应出现 ‘两头亏中间赚’ 模式）",
            },
            "circular_reasoning": {
                "root_cause": "[总结] 段与 [因素定位]/[驱动力] 段 token Jaccard ≥ 0.5 → 结论 = 前提",
                "lesson": "E031 触发 → 结论必须新增信息 (预测价格/幅度/时间窗口), 不可仅复述前提",
                "verification": "近 30 条 E031 案例 win_rate（期望 ~10% — 循环论证基本无独立预测价值）",
            },
            "equivocation": {
                "root_cause": "同一术语 (支撑/阻力/强势/弱势) 在文中被赋予互斥修饰 (强 + 破位) → 含义游移",
                "lesson": "E032 触发 → 术语必须精确定义 (指代何种价位/何种周期/何种量级)",
                "verification": "近 30 条 E032 案例 win_rate（歧义术语的决策不确定性高）",
            },
            "post_hoc": {
                "root_cause": "'A 后 B 故因 A' / '紧接着 X 所以 Y' 等时间先 ≠ 因果的强加因果",
                "lesson": "E033 触发 → 因果必须显式写因果机制 (X 通过 Y 引起 Z), 不可仅时序并列",
                "verification": "近 30 条 E033 案例 win_rate（post hoc 推理误归因率 ~70%）",
            },
        },
    },
}
