#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
reflection_dimension_map.py — v309 P0-1: 错误码 → A/B/C 三维度 + 细分小类 (单一权威表)
===========================================================================
为什么需要这个模块
------------------
/api/v237/missed-signals/by-dimension 的「三维度错因」卡片读的是
trade_reflections_v2.dimension ∈ {A,B,C} + .subcategory, 但此前:

  1. db.py 的权威 schema 里**没有** dimension/subcategory/hit/actual_outcome_pct/
     confidence_at_time/reflection_text/settled 这些列 (bridge_server 在读,
     schema 没定义 → 迁移后仍然缺列 → 整个 reflections 分支 500 后被 except 吞掉);
  2. 即使有列, 也**没有任何写入方** → 卡片恒显示 n=0。

维度语义 (与 frontend/components/v237/MissedSignalsDimensionSummary.tsx 的
DIM_META 一致, 也与 bridge_server.py 顶部「trade_reflections_v2.subcategory 命名
约定」注释一致 —— 两边必须对齐, 否则前端按 A/B/C 配色会串):

  A 买卖信号判断   对市场还不够了解 · 心理预期计算与形态预期未达标
      market_regime_misread / pattern_recognition /
      psychological_anchor / volume_price_divergence
      + v326 机制类 (该做的买卖被规则拦下, 与"认知错"区分开):
        risk_limit_blocked / batch_conflict_blocked / strength_gate_blocked
  B 信息分析方向   方向错误 · 逻辑框架错了 · 因果链倒置
      cause_effect_reversed / event_chain_break /
      macro_misattribute / news_direction_inverted
  C 技术错误       数据缺失 · 信号慢 · 接口超时 · 计算异常
      calibration_drift / data_feed_timeout /
      feed_missing / signal_delay

四条输入路径 (按精确度优先级):
  1. decision_errors.error_code      E001a-E033 逐决策精确错误码 ← 最精确
  2. trade_decisions.deny_reason     机器记录的拒单机制 (资产额度/同批连坐/强度闸门) ← v326
  3. attribution_reviews.reason_code review_agent 23 类失败框架 1-26
  4. trade_reflections_v2.layer/sub_layer  v188 ontology 三层标签 (兜底)

v326 为什么加第 2 条路径: 实测 A 类 2626 条里 2377 条是 trade_decisions.deny_reason
决定的 (资产白名单 1481 / 同批次兄弟 613 / 强度闸门 283), 但旧分类器只用
"该资产有因果链" 这一条证据就把它们全判成 market_regime_misread —— 于是 A 类
100% 挤在一个小类 (S10 集中度 53%)。拒单原因是机器记录的事实, 比 LLM 猜的
reason_code 更硬, 所以排在它前面。

设计约束: 本模块**零外部依赖** (只用 typing) —— 可被 pytest 直接 import,
不碰 DB / config / dotenv, 便于离线单测。
"""
from __future__ import annotations

from typing import Dict, Iterable, List, Optional, Sequence, Tuple

DIMENSION_A = "A"
DIMENSION_B = "B"
DIMENSION_C = "C"

DIMENSION_LABELS: Dict[str, str] = {
    DIMENSION_A: "买卖信号判断",
    DIMENSION_B: "信息分析方向",
    DIMENSION_C: "技术错误",
}

DIMENSION_DESCS: Dict[str, str] = {
    DIMENSION_A: "对市场还不够了解 · 心理预期计算与形态预期未达标",
    DIMENSION_B: "方向错误 · 逻辑框架错了 · 因果链倒置",
    DIMENSION_C: "数据缺失 · 信号慢 · 接口超时 · 计算异常",
}

# 12 个细分小类 (A/B/C 各 4 个) —— 命名与 bridge_server.py 注释里的约定一致
SUBCATEGORY_LABELS: Dict[str, str] = {
    # A
    "market_regime_misread": "市场状态误读",
    "pattern_recognition": "形态/信号识别错",
    "psychological_anchor": "心理锚定(惯性/过度自信)",
    "volume_price_divergence": "量价背离",
    # A 机制类 (v326): 信号判断没错, 是执行规则把该做的单拦掉了
    "risk_limit_blocked": "资产额度/白名单拒单",
    "batch_conflict_blocked": "同批次连坐拒单",
    "strength_gate_blocked": "强度闸门拒单",
    # B
    "cause_effect_reversed": "因果倒置",
    "event_chain_break": "事件链断裂",
    "macro_misattribute": "宏观/跨资产归因错",
    "news_direction_inverted": "方向判反",
    "unverified_legacy_label": "证据不足(无法归因)",
    # C
    "calibration_drift": "置信度/样本校准漂移",
    "data_feed_timeout": "接口超时/抓取失败",
    "feed_missing": "数据缺失",
    "signal_delay": "信号慢/数据过期",
}

# 各维度下允许的小类 (写入时校验, 防止拼错名字)
SUBCATEGORIES_BY_DIMENSION: Dict[str, List[str]] = {
    DIMENSION_A: ["market_regime_misread", "pattern_recognition",
                  "psychological_anchor", "volume_price_divergence",
                  "risk_limit_blocked", "batch_conflict_blocked",
                  "strength_gate_blocked"],
    DIMENSION_B: ["cause_effect_reversed", "event_chain_break",
                  "macro_misattribute", "news_direction_inverted",
                  # v326: 历史行标记 —— 有方向标签但缺可验证证据时用它,
                  # 语义是"证据不足, 不做归因"。它不是错因小类, 但必须是**合法值**,
                  # 否则数据契约检查 (C8) 会把 1611 条历史行判成"非法枚举"。
                  "unverified_legacy_label"],
    DIMENSION_C: ["calibration_drift", "data_feed_timeout",
                  "feed_missing", "signal_delay",
                  # v409: 结构性缺口（该品种本地根本没有行情源）单独一档,
                  # 与"有源但断档"(feed_missing) 分开 —— 前者改代码解决不了。
                  "feed_no_source"],
}


# ─────────────────────────────────────────────────────────────────────────────
# 路径 1: E 码 (reflection_checkers.py 的 E001a-E033) → (dimension, subcategory)
# ─────────────────────────────────────────────────────────────────────────────
# 归类依据 = 该错误码检查的**问题性质**, 不是它出现在哪一层:
#   · 事实/数据/时延/接口/计算类问题            → C 技术错误
#   · 因果/逻辑/方向/概念/叙事类判断问题        → B 信息分析方向
#   · 对市场形态/心理预期/量价关系的认知问题    → A 买卖信号判断
# E001 的 6 个子码全部是"字段缺失" → 属 C (数据缺失/不完整), 但 E001c
# (缺因子方向) 与 E001e (缺跨资产验证) 直接影响方向判断 → 归 B。
E_CODE_TO_DIMENSION: Dict[str, Tuple[str, str]] = {
    # ── C 技术错误: 数据/字段/时延/样本/接口 ──
    "E001a": (DIMENSION_C, "feed_missing"),        # 缺 hard_data_evidence 事实
    "E001b": (DIMENSION_C, "feed_missing"),        # 缺 data_anchor_ts 时间锚点
    "E001c": (DIMENSION_B, "cause_effect_reversed"),  # 缺因子方向 → 方向无从推导
    "E001d": (DIMENSION_C, "feed_missing"),        # 缺事件链关联 (news_id/atom_ids)
    "E001e": (DIMENSION_B, "macro_misattribute"),  # 缺跨资产数值互证 → 宏观传导无据
    "E001f": (DIMENSION_C, "calibration_drift"),   # 缺回测样本 → 置信度无基准
    "E001g": (DIMENSION_C, "feed_no_source"),      # v409 该品种本地无行情源（结构性）
    "E001h": (DIMENSION_C, "feed_missing"),        # v409 有源但断档/过期（可修）
    "E001i": (DIMENSION_C, "feed_missing"),        # v409 符号无法识别/未配映射（可修）
    "E001j": (DIMENSION_C, "signal_delay"),        # v409 休市无新价（时点问题，非故障）
    "E002": (DIMENSION_C, "signal_delay"),         # 数据过期 (>30min)
    "E006": (DIMENSION_C, "calibration_drift"),    # 样本不足
    "E019": (DIMENSION_C, "feed_missing"),         # LLM incomplete (完整性<0.7)
    "E020": (DIMENSION_C, "calibration_drift"),    # 样本质量不满足硬规则
    # ── A 买卖信号判断: 形态/心理/量价认知 ──
    "E007": (DIMENSION_A, "market_regime_misread"),   # 事件过旧/市场反向 → 市场状态读错
    "E008": (DIMENSION_A, "pattern_recognition"),     # 品种不匹配 → 认错对象/形态
    "E009": (DIMENSION_A, "psychological_anchor"),    # 置信虚高 (score高/证据少)
    "E010": (DIMENSION_A, "volume_price_divergence"), # MFE/MAE≈0 决策无市场反应 → 量价背离
    "E013": (DIMENSION_A, "psychological_anchor"),    # 叙事偏置 → 心理锚定
    "E016": (DIMENSION_A, "market_regime_misread"),   # 跨资产方向错 → 市场状态读错
    # ── B 信息分析方向: 因果/逻辑/概念/方向 ──
    "E003": (DIMENSION_B, "news_direction_inverted"), # 逻辑矛盾 (action 与因子反向)
    "E004": (DIMENSION_B, "cause_effect_reversed"),  # 因果断裂 (因子全不在图谱)
    "E005": (DIMENSION_B, "event_chain_break"),       # 未引用任何硬铁律 → 规则链断
    "E011": (DIMENSION_B, "cause_effect_reversed"),  # 事实错 → 因果依据不实
    "E012": (DIMENSION_B, "event_chain_break"),       # 链过长 (>8 跳)
    "E014": (DIMENSION_B, "event_chain_break"),       # 推理链断裂 (八段缺段)
    "E015": (DIMENSION_B, "cause_effect_reversed"),  # 概念错位/反共识缺位
    "E017": (DIMENSION_B, "macro_misattribute"),     # 跨资产无据传导
    "E018": (DIMENSION_B, "cause_effect_reversed"),  # 历史同类反例
    "E021": (DIMENSION_B, "event_chain_break"),       # 假设/前提未明示
    "E022": (DIMENSION_B, "event_chain_break"),       # 反事实未考虑
    "E024": (DIMENSION_B, "cause_effect_reversed"),  # 历史反例未引
    "E025": (DIMENSION_B, "news_direction_inverted"), # 跨资产方向不一致
    "E026": (DIMENSION_B, "cause_effect_reversed"),  # 因果机制不明确
    "E028": (DIMENSION_B, "event_chain_break"),       # 自相矛盾
    "E029": (DIMENSION_B, "event_chain_break"),       # 推不出结论
    "E030": (DIMENSION_B, "cause_effect_reversed"),  # 假二择一
    "E031": (DIMENSION_B, "cause_effect_reversed"),  # 循环论证
    "E032": (DIMENSION_B, "event_chain_break"),       # 关键术语歧义
    "E033": (DIMENSION_B, "cause_effect_reversed"),  # 后此谬因 (时序≠因果)
    # ── C 技术错误: 校准类 (置信度与证据量不匹配 = 校准口径问题) ──
    "E023": (DIMENSION_C, "calibration_drift"),    # 置信未校准 (高置信却亏损)
    "E027": (DIMENSION_C, "calibration_drift"),    # 置信度-证据量不匹配
}

# 兼容粗码: E001 (未拆子码时的旧写法) → 数据缺失
E_CODE_TO_DIMENSION.setdefault("E001", (DIMENSION_C, "feed_missing"))

# E 码的**定义顺序**索引 —— resolve_dimension 用它做确定性排序。
# 顺序即字典字面量顺序 (C 技术错误 → A 买卖信号 → B 信息分析方向),
# 刻意让"技术硬伤"优先于"认知/方向问题"。
_E_CODE_ORDER: Dict[str, int] = {
    code: i for i, code in enumerate(E_CODE_TO_DIMENSION.keys())
}


# ─────────────────────────────────────────────────────────────────────────────
# 路径 2: review_agent 的 23 类失败框架 reason_code (1-26) → (dimension, subcategory)
# ─────────────────────────────────────────────────────────────────────────────
# 与 E 码同源但粒度不同 (LLM 归因 vs 程序化校验), 维度语义必须一致, 否则
# 同一笔交易在 attribution_reviews 与 trade_reflections_v2 会被算进不同桶。
REASON_CODE_TO_DIMENSION: Dict[int, Tuple[str, str]] = {
    1: (DIMENSION_C, "feed_missing"),          # 数据缺失
    2: (DIMENSION_C, "signal_delay"),          # 非活跃时段
    3: (DIMENSION_B, "event_chain_break"),     # 忽略关键因素
    4: (DIMENSION_A, "market_regime_misread"), # 游戏规则变化 (市场结构突变)
    5: (DIMENSION_C, "feed_missing"),          # 不可监控信息
    6: (DIMENSION_B, "news_direction_inverted"),  # 模型判断错误
    7: (DIMENSION_B, "news_direction_inverted"),  # 信号质量差/影响度误判
    8: (DIMENSION_C, "signal_delay"),          # 时间窗口不匹配
    9: (DIMENSION_C, "signal_delay"),          # 数据滞后
    10: (DIMENSION_C, "calibration_drift"),    # 策略参数不匹配
    11: (DIMENSION_A, "market_regime_misread"),  # 市场状态误判
    12: (DIMENSION_C, "feed_missing"),          # 新闻重复/旧闻
    13: (DIMENSION_B, "macro_misattribute"),   # 联动传导忽略
    14: (DIMENSION_A, "market_regime_misread"),  # 关键价位/技术位
    15: (DIMENSION_C, "calibration_drift"),    # 仓位/止损设置不当
    16: (DIMENSION_A, "market_regime_misread"),  # 入场时机不当
    17: (DIMENSION_C, "calibration_drift"),    # 模型推理降级
    18: (DIMENSION_C, "feed_missing"),          # 数据错误
    19: (DIMENSION_C, "feed_missing"),          # 联动数据缺失/错误
    20: (DIMENSION_C, "calibration_drift"),    # 成交滑点/成本
    21: (DIMENSION_C, "data_feed_timeout"),     # 系统通道故障
    22: (DIMENSION_A, "psychological_anchor"),   # 新闻已定价/抢跑
    23: (DIMENSION_C, "calibration_drift"),    # 经验误用/过拟合
    24: (DIMENSION_B, "event_chain_break"),     # 因果图谱推理链错位
    25: (DIMENSION_B, "cause_effect_reversed"),  # 概念错位/反共识缺位
    26: (DIMENSION_B, "news_direction_inverted"),  # 跨资产传导方向错
}


# ─────────────────────────────────────────────────────────────────────────────
# 路径 2 (v326): trade_decisions.deny_reason → (dimension, subcategory)
# ─────────────────────────────────────────────────────────────────────────────
# 为什么需要这条路径: 漏检反思里，"系统为什么没做这笔本该做的单" 的**唯一
# 机器事实**就是 deny_reason。旧分类器不看它，只用"该资产有因果链"一条证据，
# 于是把资产白名单/同批次连坐/强度闸门三种完全不同的机制全判成"市场状态误读"
# （A 类 2626 条 100% 一个桶）。
#
# 匹配用**子串**而不是精确相等: deny_reason 是自由文本，同一机制在不同版本里
# 拼法不同（"asset_BTC" / "R8:同批次兄弟决策=9>3" / "资产 XAU 不在模拟盘允许交易列表"）。
# 顺序敏感 —— 先匹配到的赢，所以把更具体的规则放前面。
DENY_REASON_PATTERNS: Tuple[Tuple[str, Tuple[str, str], str], ...] = (
    # 同批次兄弟决策连坐 (R8 规则) —— 必须排在 asset_ 前面? 不会冲突, 但先列更具体的
    ("r8", (DIMENSION_A, "batch_conflict_blocked"), "同批次兄弟决策超限被连坐拒单"),
    ("同批次兄弟", (DIMENSION_A, "batch_conflict_blocked"), "同批次兄弟决策超限被连坐拒单"),
    ("high_strength", (DIMENSION_A, "strength_gate_blocked"), "事件强度过高被强度闸门拦下"),
    ("强度", (DIMENSION_A, "strength_gate_blocked"), "事件强度过高被强度闸门拦下"),
    # 资产额度 / 白名单
    ("asset_", (DIMENSION_A, "risk_limit_blocked"),
     "资产额度/集中度上限已满, 单子被风控拒掉"),
    ("不在模拟盘允许交易列表", (DIMENSION_A, "risk_limit_blocked"), "该资产不在允许交易列表"),
    # 方向类 (B)
    ("方向与分数符号矛盾", (DIMENSION_B, "news_direction_inverted"), "LLM 方向与分数符号矛盾"),
    ("no_direction", (DIMENSION_B, "news_direction_inverted"), "新闻未给出明确方向"),
    # 技术/校准类 (C)
    ("行情源不可用", (DIMENSION_C, "feed_missing"), "行情源不可用, 取不到价格"),
    ("置信度", (DIMENSION_C, "calibration_drift"),
     "统一置信度低于闸门 (信号虚高/校准漂移)"),
    ("低于闸门", (DIMENSION_C, "calibration_drift"),
     "统一置信度低于闸门 (信号虚高/校准漂移)"),
    ("超过时效门槛", (DIMENSION_C, "signal_delay"), "新闻超过时效门槛 (信号慢)"),
    ("时效", (DIMENSION_C, "signal_delay"), "新闻超过时效门槛 (信号慢)"),
)


def classify_deny_reason(reason: Optional[str]) -> Optional[Tuple[str, str, str]]:
    """把 trade_decisions.deny_reason 映射成 (dimension, subcategory, 证据说明)。

    返回 None 表示这条拒单原因无法归类 —— 调用方应继续走后面的路径，
    **不要**让 None 落成某个默认桶 (那正是 v325 之前 A 类塌成一桶的机制)。
    """
    text = str(reason or "").strip()
    if not text:
        return None
    low = text.lower()
    for pat, mapping, note in DENY_REASON_PATTERNS:
        if pat.lower() in low:
            return mapping[0], mapping[1], note
    return None


# ─────────────────────────────────────────────────────────────────────────────
# 路径 3: v188 ontology 三层标签 (layer, sub_layer) → (dimension, subcategory)
# ─────────────────────────────────────────────────────────────────────────────
# 兜底路径: 没有 E 码也没有 reason_code 时, 用反思 worker 写进
# trade_reflections_v2 的 ontology 标签反推维度。粒度最粗, 但保证
# "每条反思都有维度" —— 否则前端三维度卡片会因缺行而漏统计。
LAYER_TO_DIMENSION: Dict[str, Tuple[str, str]] = {
    # 事件逻辑链层 (E014-E017) — 推理链/概念/跨资产方向
    "event_logic": (DIMENSION_B, "event_chain_break"),
    # 推理逻辑层 (E021-E027) — 假设/反事实/校准/历史/因果/置信
    "reasoning_logic": (DIMENSION_B, "cause_effect_reversed"),
    # 形式逻辑层 (E028-E033) — 自相矛盾/推不出结论/循环论证
    "logic_analysis": (DIMENSION_B, "event_chain_break"),
    # 分析层 — 因子/情绪/结构/知识注入
    "analysis": (DIMENSION_B, "cause_effect_reversed"),
    # 过滤层 — 闸门/阈值/一致性
    "filter": (DIMENSION_A, "market_regime_misread"),
    # 数据层 — feed/延迟/覆盖
    "data": (DIMENSION_C, "feed_missing"),
    # 时机层 — 时滞/交易时段
    "timing": (DIMENSION_C, "signal_delay"),
    # 执行层 — 滑点/仓位
    "execution": (DIMENSION_C, "calibration_drift"),
    # 策略层 — 阈值参数
    "strategy": (DIMENSION_C, "calibration_drift"),
    # 门卫层 — 闸门结论
    "gate": (DIMENSION_A, "market_regime_misread"),
}

# sub_layer 精化 (同 layer 下更准的小类)。key = (layer, sub_layer)
#
# 硬约束: 同一 layer 的所有 sub_layer 必须落在**同一维度**, 否则同一个
# layer 会在 A/B/C 之间摇摆, 统计口径自相矛盾 (前端按维度配色会串桶)。
# 下面的 test_all_ontology_mappings_valid 会守住这条约束。
SUB_LAYER_TO_DIMENSION: Dict[Tuple[str, str], Tuple[str, str]] = {
    # data → C 技术错误
    ("data", "latency"): (DIMENSION_C, "data_feed_timeout"),
    ("data", "feed_status"): (DIMENSION_C, "data_feed_timeout"),
    ("data", "coverage"): (DIMENSION_C, "feed_missing"),
    ("data", "cleanliness"): (DIMENSION_C, "feed_missing"),
    ("data", "freshness"): (DIMENSION_C, "signal_delay"),
    # timing → C 技术错误 (时滞/数据慢)。注意: 时段判断本身是市场状态认知,
    # 但该层语义核心是"信号来得太晚/不在活跃时段", 归 C 才与 layer 一致。
    ("timing", "timelag_to_event"): (DIMENSION_C, "signal_delay"),
    ("timing", "market_session"): (DIMENSION_C, "signal_delay"),
    ("timing", "event_window"): (DIMENSION_C, "signal_delay"),
    # analysis → B 信息分析方向 (因子/情绪/结构判断)
    ("analysis", "sentiment_score"): (DIMENSION_B, "news_direction_inverted"),
    # 注意: ("analysis","missed_signal") 与 ("analysis","news_source_class")
    # 这两条**故意不放进本表**。层默认维度是兜底口径, 而这两类历史行的正确
    # 维度(A / C)与 analysis 层默认 B 不一致 —— 放进来会破坏
    # test_all_ontology_mappings_valid 的"子层不得跨大类"不变量。
    # 历史行由 reclassify_legacy_reflections.py 一次性重判 (v338):
    #   analysis/missed_signal     → A/market_regime_misread (与 missed_signals
    #                                表的 A2_market_regime_missed 同口径)
    #   analysis/news_source_class → C/feed_missing
    ("analysis", "event_struct"): (DIMENSION_B, "cause_effect_reversed"),
    ("analysis", "macro_context"): (DIMENSION_B, "macro_misattribute"),
    ("analysis", "cross_asset_transmission"): (DIMENSION_B, "macro_misattribute"),
    ("analysis", "factor_chain"): (DIMENSION_B, "cause_effect_reversed"),
    ("analysis", "llm_output"): (DIMENSION_B, "cause_effect_reversed"),
    # filter → A 买卖信号判断 (闸门/阈值/品种匹配 = 信号能不能打)
    ("filter", "contradiction_check"): (DIMENSION_A, "psychological_anchor"),
    ("filter", "strength_threshold"): (DIMENSION_A, "psychological_anchor"),
    ("filter", "asset_match"): (DIMENSION_A, "market_regime_misread"),
    # execution → C 技术错误 (滑点/仓位 = 成本与执行口径)
    ("execution", "slippage"): (DIMENSION_C, "calibration_drift"),
    ("execution", "position_sizing"): (DIMENSION_C, "calibration_drift"),
    # strategy → C 技术错误 (阈值参数 = 校准口径)
    ("strategy", "regime_filter"): (DIMENSION_C, "calibration_drift"),
    # event_logic → B 信息分析方向
    ("event_logic", "cross_asset_chain"): (DIMENSION_B, "macro_misattribute"),
    ("event_logic", "concept_match"): (DIMENSION_B, "cause_effect_reversed"),
    ("event_logic", "counter_consensus"): (DIMENSION_B, "cause_effect_reversed"),
    # reasoning_logic → B 信息分析方向
    ("reasoning_logic", "calibration"): (DIMENSION_B, "news_direction_inverted"),
    ("reasoning_logic", "confidence_evidence"): (DIMENSION_B, "news_direction_inverted"),
    ("reasoning_logic", "history"): (DIMENSION_B, "cause_effect_reversed"),
}


# ─────────────────────────────────────────────────────────────────────────────
# 自动结算行的维度归因 (v310 P0)
# ─────────────────────────────────────────────────────────────────────────────
# 背景: bridge_server._insert_settlement_reflection 把「结算结果」直接写进
# trade_reflections_v2 (这张表是反思表), 但原先只写了 layer='decision',
# 该 layer 不在 LAYER_TO_DIMENSION 里 → 维度/小类全空 → 验收口径下
# 2133 行里 1653 行算「非法维度」, 反思维度合法率只有 22.5%,
# 且这些行既进不了 A/B/C 卡片, 也让端点↔库口径对不上。
#
# 这里给结算行一套**明确、可辩护**的归因口径 (与写入方共用, 保证单一权威):
#
#   guard ∈ {SELL_GATE, QUALITY_FILTER}
#       → A / market_regime_misread
#         理由: 闸门/质量过滤 = 「这个信号能不能打」, 属于买卖信号判断层。
#   decision_type == 'ENTRY' 且结算为负 (miss)
#       → B / cause_effect_reversed
#         理由: 方向性决策错了 → 信息分析方向层。
#   decision_type == 'ENTRY' 且结算为正 (hit)
#       → C / calibration_drift
#         理由: 盈利样本同样要进反思统计供 PAV 权重校准使用 (正负样本成对),
#               归到策略/校准口径层, 与 SUBCATEGORIES_BY_DIMENSION[C] 一致。
#   其余 (REJECT 等无实际开仓的记录)
#       → A / market_regime_misread (闸门决策记录, 同上第一条)
#
# 注意: 这套口径**不替代 LLM 深度归因**。structured_reflections / llm_depth_reflection
# 产出的行仍走 E 码优先的精确路径; 本函数只兜底「只有结算结果、没有错误归因」
# 的自动结算行, 保证每行都有合法维度, 不再产生非法枚举。

SETTLEMENT_SUBCATEGORY_BY_GUARD = {
    "SELL_GATE": (DIMENSION_A, "market_regime_misread"),
    "QUALITY_FILTER": (DIMENSION_A, "market_regime_misread"),
}


def classify_settlement_row(guard: Optional[str] = None,
                            decision_type: Optional[str] = None,
                            hit: Optional[int] = None,
                            intended_action: Optional[str] = None,
                            outcome_pct: Optional[float] = None,
                            ) -> Tuple[str, str]:
    """自动结算行的 (dimension, subcategory) 归因。

    参数
    ----
    guard:           trade_decisions.guard (SELL_GATE / QUALITY_FILTER / NONE / ...)
    decision_type:   trade_decisions.decision_type (ENTRY / REJECT / HOLD / ...)
    hit:             1=盈利命中, 0=亏损未命中, None=未知
    intended_action: 决策「本来想做」的方向 (BUY/SELL) —— 闸门类记录用它判定
                     闸门拦对没拦对。
    outcome_pct:     结算后的实际涨跌 (原始口径, 不按方向取负)。

    归因口径 (对闸门类记录尤其重要):
      闸门类记录 (SELL_GATE / QUALITY_FILTER) 的语义是「系统想做 X 但被闸门
      拦下」。**方向判断错** = 闸门把对的信号拦了 → 对市场的形态/心理预期没
      算对 → A 买卖信号判断; 方向判断对 = 闸门拦对了 → 该行不是错误样本,
      作为权重校准的正样本归 C。
      绝不能把全部闸门记录一律归 A —— 那会让 A 类卡片被「非错误」的记录灌满
      (实测 1653 条里 1637 条), 统计彻底失真。

    返回
    ----
    (dimension, subcategory) —— 恒为合法组合, 不会产生非法枚举。
    """
    g = (guard or "").strip().upper()
    dt = (decision_type or "").strip().upper()
    ia = (intended_action or "").strip().upper()

    # 方向是否判错 (outcome 与 intended_action 反向 = 信号方向错了)
    wrong_direction: Optional[bool] = None
    if ia in ("BUY", "SELL") and outcome_pct is not None:
        if ia == "BUY":
            wrong_direction = float(outcome_pct) < 0
        else:
            wrong_direction = float(outcome_pct) > 0

    if g in SETTLEMENT_SUBCATEGORY_BY_GUARD:
        if wrong_direction is True:
            # 闸门把方向正确的信号拦了 → 市场形态/心理预期判断失准
            return (DIMENSION_A, "market_regime_misread")
        # 闸门拦对了 → 非错误样本, 归校准层供权重 PAV 学习
        return (DIMENSION_C, "calibration_drift")

    if dt == "ENTRY":
        if wrong_direction is True:
            # 真实开仓但方向错了 → 信息分析方向层 (逻辑框架/因果链问题)
            return (DIMENSION_B, "cause_effect_reversed")
        if hit == 0 and wrong_direction is not True:
            # 方向没判错却仍亏损 → 更可能是幅度/时机问题 → 技术层
            return (DIMENSION_C, "calibration_drift")
        return (DIMENSION_C, "calibration_drift")

    # 其余 (无 guard 信息的记录): 有方向错判据就按 A, 否则兜底 C
    if wrong_direction is True:
        return (DIMENSION_A, "market_regime_misread")
    return (DIMENSION_C, "calibration_drift")


# ─────────────────────────────────────────────────────────────────────────────
# 公共 API
# ─────────────────────────────────────────────────────────────────────────────

def is_valid_subcategory(dimension: str, subcategory: str) -> bool:
    """小类是否属于该维度 (防拼错名字导致前端按 A/B/C 配色串桶)。"""
    return subcategory in SUBCATEGORIES_BY_DIMENSION.get(dimension, [])


def _norm_code(code) -> str:
    """错误码归一: 去空白 + 补 'E' 前缀 ('e014' / '14' 都能认)。

    子码 (E001a/E001f) 的字母后缀**必须保持原样小写** —— 字典键是
    'E001a' 这种小写后缀写法, 若统一 upper 成 'E001A' 会查不到。
    所以: 数字部分大写归一, 后缀字母只做 strip 不做大小写变换。
    """
    s = str(code or "").strip()
    if not s:
        return ""
    if s.isdigit():
        return "E" + s.zfill(3)
    # 形如 'e014' / 'E014' → 'E014' (纯数字码, 大写 E + 3 位数字)
    if len(s) == 4 and s[0] in "eE" and s[1:].isdigit():
        return "E" + s[1:].zfill(3)
    # 形如 'E14' / 'e14' → 'E014'
    if len(s) == 3 and s[0] in "eE" and s[1:].isdigit():
        return "E0" + s[1:]
    # 形如 'E001a' / 'e001a' / 'E001A' → 'E001a' (前缀大写, 后缀小写 — 字典键写法)
    if len(s) == 5 and s[0] in "eE" and s[1:4].isdigit() and s[4].isalpha():
        return "E" + s[1:4] + s[4].lower()
    return s.upper()


def dimension_for_error_code(code) -> Optional[Tuple[str, str]]:
    """E 码 → (dimension, subcategory)。未知码返回 None (不猜)。"""
    return E_CODE_TO_DIMENSION.get(_norm_code(code))


def dimension_for_reason_code(code) -> Optional[Tuple[str, str]]:
    """review_agent 23 类失败框架 reason_code → (dimension, subcategory)。"""
    try:
        return REASON_CODE_TO_DIMENSION.get(int(code))
    except (TypeError, ValueError):
        return None


def dimension_for_ontology(layer: str, sub_layer: str = "") -> Optional[Tuple[str, str]]:
    """v188 ontology (layer, sub_layer) → (dimension, subcategory)。"""
    l = str(layer or "").strip()
    s = str(sub_layer or "").strip()
    if not l:
        return None
    hit = SUB_LAYER_TO_DIMENSION.get((l, s))
    if hit:
        return hit
    return LAYER_TO_DIMENSION.get(l)


def resolve_dimension(
    error_codes: Optional[Iterable] = None,
    reason_code=None,
    layer: str = "",
    sub_layer: str = "",
    signal_name: str = "",
    deny_reason: str = "",
) -> Optional[Tuple[str, str]]:
    """按精确度优先级解析出唯一的 (dimension, subcategory)。

    优先级: E 码 (最精确) > deny_reason (机器记录的拒单机制) > reason_code > ontology。
    v326 把 deny_reason 插在 reason_code 之前: 前者是闸门的硬件事实，后者是
    LLM 事后猜的失败框架，冲突时信前者。

    确定性 (重要): 同一决策可能命中多个 E 码, 而 decision_errors 的查询
    **没有 ORDER BY** → SQL 返回顺序不保证稳定。若直接按入参顺序取第一个,
    同一笔决策在两次运行中可能落到不同维度 (统计口径漂移)。
    因此排序后再取第一个。

    v2026-10-27 (G11): 支持带严重度的错误码 "E021:3" (3=CRITICAL / 2=WARN /
    1=INFO)。**严重度优先于字典顺序** —— 同一笔决策同时有"数据缺口(INFO)"和
    "推理链断裂(CRITICAL)"时, 应该按更严重的那条归类; 否则绝大多数决策都会被
    最高频的低危码 (E001a 缺事实) 吃掉, 三维细分退化成一种 (门禁 G11 根因之一)。
    同severity时仍按字典定义顺序 (C → A → B: 技术问题优先于认知问题)。
    """
    seen: set = set()
    entries: List[Tuple[int, int, str]] = []
    for raw in (error_codes or []):
        raw_s = str(raw or "").strip().upper()
        if not raw_s:
            continue
        code, _, sev_s = raw_s.partition(":")
        sev = 0
        if sev_s:
            try:
                sev = int(float(sev_s))
            except ValueError:
                sev = 0
        norm = _norm_code(code)
        if not norm or norm in seen:
            continue
        seen.add(norm)
        entries.append((sev, _E_CODE_ORDER.get(norm, len(_E_CODE_ORDER)), norm))
    # 严重度降序 → 字典顺序 (同一严重度内保持"技术问题优先"的既定编排)
    entries.sort(key=lambda e: (-e[0], e[1]))

    for _sev, _order, norm in entries:
        hit = E_CODE_TO_DIMENSION.get(norm)
        if hit:
            return hit
    hit_d = classify_deny_reason(deny_reason)
    if hit_d:
        return hit_d[0], hit_d[1]
    hit = dimension_for_reason_code(reason_code)
    if hit:
        return hit
    hit = dimension_for_ontology(layer, sub_layer)
    if hit:
        return hit
    return None



def classify_reflection_row(row: dict) -> Tuple[str, str]:
    """从一条反思/归因行解析出 (dimension, subcategory)。

    兼容两种行结构:
      · trade_reflections_v2 行 —— 有 layer/sub_layer/signal_name
      · attribution_reviews 行 —— 有 reason_code
    都拿不到时返回 ("", "") (空维度) —— 调用方必须跳过空维度, 不要塞进
    某个"默认桶"里凑数, 那会让三维度统计失真。
    """
    if not isinstance(row, dict):
        return ("", "")
    hit = resolve_dimension(
        error_codes=row.get("error_codes") or row.get("codes"),
        reason_code=row.get("reason_code"),
        layer=row.get("layer") or row.get("error_layer") or "",
        sub_layer=row.get("sub_layer") or row.get("error_sub_layer") or "",
        signal_name=row.get("signal_name") or row.get("error_signal") or "",
        deny_reason=row.get("deny_reason") or row.get("no_trade_reason") or "",
    )
    if not hit:
        return ("", "")
    dimension, subcategory = hit
    if not is_valid_subcategory(dimension, subcategory):
        return ("", "")
    return (dimension, subcategory)


def describe_mapping() -> dict:
    """导出完整映射表 (供 /api 端点与离线审计用)。"""
    return {
        "dimensions": {
            k: {"label": DIMENSION_LABELS[k], "desc": DIMENSION_DESCS[k],
                "subcategories": list(SUBCATEGORIES_BY_DIMENSION[k])}
            for k in (DIMENSION_A, DIMENSION_B, DIMENSION_C)
        },
        "subcategory_labels": dict(SUBCATEGORY_LABELS),
        "error_codes": {k: list(v) for k, v in E_CODE_TO_DIMENSION.items()},
        "reason_codes": {str(k): list(v) for k, v in REASON_CODE_TO_DIMENSION.items()},
        "layers": {k: list(v) for k, v in LAYER_TO_DIMENSION.items()},
        "sub_layers": {f"{a}/{b}": list(v) for (a, b), v in SUB_LAYER_TO_DIMENSION.items()},
    }


__all__ = [
    "DIMENSION_A", "DIMENSION_B", "DIMENSION_C",
    "DIMENSION_LABELS", "DIMENSION_DESCS", "SUBCATEGORY_LABELS",
    "SUBCATEGORIES_BY_DIMENSION", "E_CODE_TO_DIMENSION",
    "REASON_CODE_TO_DIMENSION", "LAYER_TO_DIMENSION", "SUB_LAYER_TO_DIMENSION",
    "is_valid_subcategory", "dimension_for_error_code",
    "dimension_for_reason_code", "dimension_for_ontology",
    "resolve_dimension", "classify_reflection_row", "describe_mapping",
]
