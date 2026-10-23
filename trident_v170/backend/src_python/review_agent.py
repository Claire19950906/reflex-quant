#!/usr/bin/env python3
"""AI 交易复盘 — 总结分析 + 失败原因反思 + 经验回灌。

三个环节：
1. 复盘总结（Review）：对一段时间的已结算交易聚合 + LLM 生成报告
   （整体表现、按维度分组盈亏、规律发现）。
2. 失败原因反思（Root Cause）：失败单按 23 类框架分组归类（LLM），
   统计占比 + 典型例子。
3. 经验回灌（Learning）：把复盘结论沉淀为 review_insights 规则，
   注入后续 AI 分析 prompt（增进判断能力）。

失败原因框架（分析失败单时给出）：
  1 数据缺失        决策时行情/宏观数据不可用或不完整
  2 非活跃时段      非开市时间/主力休息/深夜低流动性
  3 忽略关键因素    新闻或盘面已提供但未纳入判断的因素
  4 游戏规则变化    市场结构/波动率/流动性发生突变
  5 不可监控信息    盘口/庄家行为/未公开信息
  6 模型判断错误    方向/幅度/时机/周期判断错误
  7 信号质量差      噪声新闻、低置信、间接催化
  8 时间窗口不匹配  预期影响时长与实际结算窗口不符
  9 数据滞后        新闻发布到行情反应的时差
  10 策略参数不匹配 阈值/持仓周期与市场特性不符

──────────────────────────────────────────────────────────────────────────────
v189 反思系统新增「事件逻辑链层」（layer=event_logic），权重最大（最高）：
  反思系统主要工作 = 分析分析页（reasoning_path / factor_ranking / factor_chain）
  的推理链/概念/性质是否正确，错在哪一步、怎么改。
  本层独立于 v181 的 6 层（数据/时机/信号/判断/执行/环境策略）+ gate 层，
  在排序/统计/沉淀时优先级最高，原 23 类失败归因保留不动。
  24 因果图谱推理链错位  reasoning_path ↔ asset_driver_edges 因果链断裂或方向错
  25 概念错位/反共识缺位  factor_ranking 概念词 ≠ asset_driver_nodes 核心驱动
                        或 反共识/反向思考缺位导致单边叙事
  26 跨资产传导方向错     cross_asset 持有的 UST/DXY/原油 方向 与 目标品种方向矛盾
"""

from __future__ import annotations

import json
import os
import re
import sqlite3
import time
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

import config
import db

# v176.7 P1-4: 反思归因 → 因果图谱改进候选（kg_improvements）自动落库
# review_agent 保存归因明细后，若 verdict=错 且 reason_code ∈ KG 框架相关项，
# 自动调 kg_improvements.add_proposal 推一条 status=pending 候选，
# 供归因复核页 (段 4) 人工审批 → reconcile worker 异步应用到 causal_graph。
try:
    import kg_improvements  # noqa: E402  (放在 review_agent 内,避免循环 import)
    _KG_IMP_OK = True
except Exception:  # pragma: no cover
    kg_improvements = None
    _KG_IMP_OK = False

_FAILURE_FRAMEWORK = """\
失败原因分类框架（对每条失败交易给一个主要原因 code，可加次要原因）：
  1 数据缺失：决策时行情/宏观数据不可用或不完整（可查 decision_context 是否 down/unavailable）
  2 非活跃时段：非开市时间/主力休息/深夜低流动性（看 entry_time 的星期/时段）
  3 忽略关键因素：新闻或盘面已提供但判断时未纳入的因素
  4 游戏规则变化：市场结构/波动率/流动性发生突变
  5 不可监控信息：盘口/庄家行为/未公开信息（我们无法监控的）
  6 模型判断错误：方向本身判错（把中性/事后统计/评估性消息当成即时看涨或看跌催化），或方向对但幅度/时机/周期错
  7 信号质量差/影响度误判：噪声新闻、低置信、间接催化、事后统计/评估性消息（无新增即时催化），或高估/低估该信息对目标品类的影响力
  8 时间窗口不匹配：预期影响时长与实际结算窗口（2h/长周期）不符
  9 数据滞后：新闻发布到行情反应的时差
  10 策略参数不匹配：信号阈值/持仓周期与市场特性不符
  11 市场状态误判：震荡市用了趋势逻辑，或趋势市用了反转逻辑（看 prediction_type 与走势）
  12 新闻重复/旧闻：同源重复推送或旧闻重发被当成新消息，行情已定价
  13 联动传导忽略：忽略美债/美元/原油/加密的跨资产传导方向与时滞，或忽略美元指数/美债收益率等定价锚突变
  14 关键价位/技术位：新闻利多但价格撞前高/支撑阻力位，冲高回落
  15 仓位/止损设置不当：止损太紧被正常波动扫掉、太松吃大亏，或名义本金与波动不匹配
  16 入场时机不当：仅在方向/题材判断正确、但入场点选错（过早追高/过晚错过）时才归此类；若方向或题材本身判错，优先归 6 或 7
  17 模型推理降级：LLM 输出降级为关键词推断/修复（reasoning_path 含'关键词推断'），判断质量差
  18 数据错误：行情/宏观数据本身错误（脏数据、时区/复权/单位/断点/插值错、单一数据源失真），区别于"数据缺失"
  19 联动数据缺失/错误：跨资产联动数据（美债/美元/原油/加密）未采集到或采集错误，区别于"采到但判断时没用"的 13
  20 成交滑点/成本：滑点、手续费、点差、模拟盘-实盘价差吃掉利润
  21 系统通道故障：行情断流、下单延迟、网络超时、worker/模型调用失败等系统自身故障
  22 新闻已定价/抢跑：新闻到达时行情已被高频/做市商走完，入场即接盘；或价格在发布时间前已泄露
  23 经验误用/过拟合：历史沉淀的经验规则被错误套用到不适用的市场状态
  24 因果图谱推理链错位：reasoning_path 中的因果链与资产核心驱动节点（asset_driver_nodes）的因果边 (asset_driver_edges) 断裂或方向错
  25 概念错位/反共识缺位：factor_ranking 中的概念词与 asset_driver_nodes 核心驱动节点不对应；或推理过程未做反向思考、反共识检验就下结论
  26 跨资产传导方向错：当前 cross_asset 持有的 UST/DXY/原油等方向与目标品种方向矛盾（如 DXY 跌但目标品种做空）
"""

_REASON_NAMES = {
    1: "数据缺失", 2: "非活跃时段", 3: "忽略关键因素", 4: "游戏规则变化",
    5: "不可监控信息", 6: "模型判断错误", 7: "信号质量差",
    8: "时间窗口不匹配", 9: "数据滞后", 10: "策略参数不匹配",
    11: "市场状态误判", 12: "新闻重复/旧闻", 13: "联动传导忽略",
    14: "关键价位/技术位", 15: "仓位/止损设置不当", 16: "入场时机不当",
    17: "模型推理降级",
    18: "数据错误", 19: "联动数据缺失/错误", 20: "成交滑点/成本",
    21: "系统通道故障", 22: "新闻已定价/抢跑", 23: "经验误用/过拟合",
    # v189 事件逻辑链层（最高权重；反思系统主要工作）
    24: "因果图谱推理链错位", 25: "概念错位/反共识缺位", 26: "跨资产传导方向错",
}

# 失败原因的可观测性：auto=系统/规则可自动判定 / llm=需模型判断 / hidden=无法观测（兜底）。
# 未列出的 code 默认视为 'llm'。
_REASON_OBSERVABILITY: Dict[int, str] = {
    1: "auto",    # 数据缺失（decision_context 空/不可用）
    2: "auto",    # 非活跃时段（entry_time 时段/星期）
    7: "auto",    # 信号质量差/题材误判（事后统计/评估消息 + 避险 BUY + 实亏 → 硬归 7）
    12: "auto",   # 新闻重复/旧闻（hash/同源去重可判）
    17: "auto",   # 模型推理降级（reasoning_path 含'关键词推断'）
    21: "auto",   # 系统通道故障（模型调用失败/超时标记）
    # v189 事件逻辑链层（程序化校验器 reflection_checkers.check_event_logic 产出）
    24: "auto",   # 因果图谱推理链错位（reasoning_path ↔ asset_driver_edges）
    25: "auto",   # 概念错位/反共识缺位（factor_ranking ↔ asset_driver_nodes + 反向思考词）
    26: "auto",   # 跨资产传导方向错（cross_asset ↔ target_asset）
    5: "hidden",  # 不可监控信息
    22: "hidden", # 新闻已定价/抢跑（部分不可观测）
}


def reason_observability(code: int) -> str:
    """返回失败原因 code 的可观测性标签（auto/llm/hidden），未知 code 默认 llm。"""
    return _REASON_OBSERVABILITY.get(int(code), "llm")


# ── v181 分层归因：23 类失败 → 6 层（数据/时机/信号/判断/执行/环境策略）+ 门卫层 ──
# 三个结构性盲点的落点：
#   ① 门卫漏报/误报独立成层（layer='gate'），不新增线性 code 24/25、不污染 23 类归因链；
#   ② HOLD 复用 8/22/2/12（中间态样本，给 code 8/22 提供真实归因入口）；
#   ③ 结算口径噪音用 settle_flags → verdict_uncertain 隔离（不沉淀经验）。
_LAYER_NAMES: Dict[str, str] = {
    # v189 事件逻辑链层「最高权重」—— 排序/统计/沉淀都优先；原 23 类 + gate 全部保留
    "event_logic": "事件逻辑链层",
    "data": "数据层", "timing": "时机层", "signal": "信号层",
    "judgment": "判断层", "execution": "执行层", "regime": "环境/策略层",
    "gate": "门卫层",
}

# 层优先级（数字越小权重越大）：event_logic=1（最高），原 v181 6 层 = 2..7，gate = 8
# 排序/统计/沉淀时按此权重排；保持原 6 层 + gate 的语义不动
_LAYER_WEIGHT: Dict[str, int] = {
    "event_logic": 1,
    "data": 2, "timing": 3, "signal": 4,
    "judgment": 5, "execution": 6, "regime": 7,
    "gate": 8,
}


def layer_weight(layer: str) -> int:
    """返回 layer 的权重（数字越小越高）。未知 layer 返回 999（兜底最低）。"""
    return _LAYER_WEIGHT.get(str(layer or ""), 999)


def layer_name(layer: str) -> str:
    """返回 layer 的中文名（与 _LAYER_NAMES 一致）。"""
    return _LAYER_NAMES.get(str(layer or ""), str(layer or "—"))


def list_layers_ordered() -> List[Tuple[str, str]]:
    """返回按权重排序的 (layer_key, 中文名) 列表（最高权重在前）。"""
    return [(k, _LAYER_NAMES[k]) for k, _ in sorted(_LAYER_WEIGHT.items(), key=lambda kv: kv[1])]


_REASON_TO_LAYER: Dict[int, str] = {
    1: "data", 18: "data", 19: "data", 21: "data",
    2: "timing", 8: "timing", 9: "timing", 16: "timing",
    7: "signal", 12: "signal", 22: "signal",
    3: "judgment", 6: "judgment", 11: "judgment", 13: "judgment", 14: "judgment",
    15: "execution", 17: "execution", 20: "execution",
    4: "regime", 5: "regime", 10: "regime", 23: "regime",
    # v189 事件逻辑链层（最高权重）
    24: "event_logic", 25: "event_logic", 26: "event_logic",
}

# ── v181.1 sublayer 两级受控枚举（23 类 + gate） ──
# 数据结构：code → [(l1_key, [(l2_key, l2_label), ...]), ...]
#   · l1 第一项为该 code 的默认 l1
#   · l1 下 l2 第一项为该 l1 的默认 l2
#   · LLM prompt 中按 l1 + l2_keys 列示；label 在前端/统计展示时按需查
_SUBLAYER_TWO_LEVEL: Dict[int, List[Tuple[str, List[Tuple[str, str]]]]] = {
    1: [
        ("feed_unavailable", [
            ("mt5_disconnect", "MT5 断连"),
            ("bloomberg_disconnect", "Bloomberg 断连"),
            ("refinitiv_disconnect", "Refinitiv 断连"),
            ("wind_disconnect", "Wind 断连"),
            ("choice_disconnect", "Choice 断连"),
            ("api_timeout_cme", "CME API 超时"),
            ("api_timeout_lme", "LME API 超时"),
            ("api_timeout_sse", "SSE API 超时"),
        ]),
        ("context_unavailable", [
            ("snapshot_missing_mt5", "MT5 快照缺失"),
            ("feature_calc_fail", "特征计算失败"),
            ("context_field_empty", "上下文字段为空"),
            ("regex_no_match", "正则无匹配"),
        ]),
        ("macro_unavailable", [
            ("us_macro_nfp_missing", "美 NFP 缺失"),
            ("us_macro_cpi_missing", "美 CPI 缺失"),
            ("us_macro_fomc_missing", "美 FOMC 缺失"),
            ("us_macro_ppi_missing", "美 PPI 缺失"),
            ("cn_macro_pmi_missing", "中 PMI 缺失"),
            ("cn_macro_lpr_missing", "中 LPR 缺失"),
            ("cn_macro_cpi_missing", "中 CPI 缺失"),
            ("cn_macro_m2_missing", "中 M2 缺失"),
            ("eu_macro_ecb_missing", "欧 ECB 缺失"),
            ("eu_macro_hicp_missing", "欧 HICP 缺失"),
            ("jp_macro_boj_missing", "日 BOJ 缺失"),
            ("jp_macro_cpi_missing", "日 CPI 缺失"),
            ("uk_macro_boe_missing", "英 BOE 缺失"),
        ]),
        ("related_unavailable", [
            ("ust_yield_missing", "UST 收益率缺失"),
            ("cgb_yield_missing", "CGB 收益率缺失"),
            ("jgb_yield_missing", "JGB 收益率缺失"),
            ("bund_missing", "Bund 缺失"),
            ("gilt_missing", "Gilt 缺失"),
            ("dxy_missing", "DXY 美元指数缺失"),
            ("vix_missing", "VIX 缺失"),
            ("oil_wti_missing", "WTI 原油缺失"),
            ("oil_brent_missing", "Brent 原油缺失"),
            ("gold_missing", "黄金缺失"),
            ("copper_missing", "铜缺失"),
            ("crypto_corr_missing", "加密相关性缺失"),
            ("spx_missing", "SPX 缺失"),
            ("hsi_missing", "HSI 缺失"),
            ("n225_missing", "N225 缺失"),
            ("dax_missing", "DAX 缺失"),
            ("ftse_missing", "FTSE 缺失"),
        ]),
    ],
    # === code 2 追加锚点 ===
    2: [
        ("market_closed", [
            ("us_market_weekend", "美股周末休市"),
            ("us_market_holiday_mlk", "美股 MLK 日"),
            ("us_market_holiday_presidents", "美股总统日"),
            ("us_market_holiday_good_friday", "美股耶稣受难日"),
            ("us_market_holiday_memorial", "美股阵亡将士纪念日"),
            ("us_market_holiday_juneteenth", "美股六月节"),
            ("us_market_holiday_july4", "美股独立日"),
            ("us_market_holiday_labor", "美股劳动节"),
            ("us_market_holiday_thanksgiving", "美股感恩节"),
            ("us_market_holiday_christmas", "美股圣诞节"),
            ("us_market_holiday_nyd", "美股新年"),
            ("cn_market_weekend", "A 股周末休市"),
            ("cn_market_holiday_cny", "A 股春节"),
            ("cn_market_holiday_tomb_sweeping", "A 股清明"),
            ("cn_market_holiday_labor", "A 股劳动节"),
            ("cn_market_holiday_dragon_boat", "A 股端午"),
            ("cn_market_holiday_mid_autumn", "A 股中秋"),
            ("cn_market_holiday_national_day", "A 股国庆"),
            ("hk_market_weekend", "港股周末休市"),
            ("hk_market_holiday_lap_chun", "港股春节"),
            ("hk_market_holiday_ching_ming", "港股清明"),
            ("hk_market_holiday_good_friday", "港股耶稣受难日"),
            ("hk_market_holiday_easter_mon", "港股复活节星期一"),
            ("hk_market_holiday_labour", "港股劳动节"),
            ("hk_market_holiday_buddha", "港股佛诞"),
            ("hk_market_holiday_tuen_n", "港股端午"),
            ("hk_market_holiday_national", "港股国庆"),
            ("hk_market_holiday_christmas", "港股圣诞节"),
            ("hk_market_holiday_boxing", "港股节礼日"),
            # v213: 韩国 KOSPI（SKHYNIX 主体）。周末 + 节假日 + 临时休市全列。
            ("kr_market_weekend", "韩股周末休市"),
            ("kr_market_holiday_new_year", "韩股新年"),
            ("kr_market_holiday_seollal", "韩股春节"),
            ("kr_market_holiday_independence_movement", "韩独立运动纪念日"),
            ("kr_market_holiday_children", "韩儿童节"),
            ("kr_market_holiday_buddha", "韩佛诞"),
            ("kr_market_holiday_memorial", "韩显忠日"),
            ("kr_market_holiday_liberation", "韩光复节"),
            ("kr_market_holiday_chuseok", "韩中秋"),
            ("kr_market_holiday_national", "韩国庆日"),
            ("kr_market_holiday_hangul", "韩谚文日"),
            ("kr_market_holiday_christmas", "韩股圣诞节"),
            ("eu_market_weekend", "欧股周末休市"),
            ("eu_market_holiday_easter", "欧股复活节"),
            ("eu_market_holiday_may_day", "欧股劳动节"),
            ("eu_market_holiday_christmas", "欧股圣诞节"),
            ("eu_market_holiday_nyd", "欧股新年"),
            ("uk_market_weekend", "英股周末休市"),
            ("uk_market_holiday_easter", "英股复活节"),
            ("uk_market_holiday_may_day", "英股劳动节"),
            ("uk_market_holiday_spring_bank", "英股春季银行日"),
            ("uk_market_holiday_summer_bank", "英股夏季银行日"),
            ("uk_market_holiday_christmas", "英股圣诞节"),
            ("uk_market_holiday_boxing", "英股节礼日"),
            ("jp_market_weekend", "日股周末休市"),
            ("jp_market_holiday_new_year", "日股新年"),
            ("jp_market_holiday_coming_of_age", "日股成人节"),
            ("jp_market_holiday_golden_week", "日股黄金周"),
            ("jp_market_holiday_ocean", "日股海之日"),
            ("jp_market_holiday_mountain", "日股山之日"),
            ("jp_market_holiday_autumn", "日股秋分日"),
            ("jp_market_holiday_sports", "日股体育节"),
            ("jp_market_holiday_culture", "日股文化节"),
        ]),
        ("low_liquidity", [
            ("asia_pre_us", "亚早盘"),
            ("us_lunch", "美午休"),
            ("cn_lunch", "A 股午休"),
            ("eu_lunch", "欧午休"),
            ("post_us_pre_asia", "美后亚前"),
            ("pre_eu_open", "欧开盘前"),
            ("crypto_late_weekend", "加密周末末期"),
            ("fx_asia_session", "外汇亚盘"),
            ("commodity_off_peak", "商品非高峰"),
            # v213: 韩股 11:30-12:30 午休，订单簿变薄，价格易跳变
            ("kr_lunch_break", "韩股午休"),
        ]),
    ],
    # === code 3 追加锚点 ===
    3: [
        ("context_ignored", [
            ("tech_level_missed_us_equity", "美股技术位忽略"),
            ("tech_level_missed_cn_equity", "A 股技术位忽略"),
            ("tech_level_missed_crypto", "加密技术位忽略"),
            ("tech_level_missed_fx", "外汇技术位忽略"),
            ("volume_missed", "量能忽略"),
            ("pattern_missed", "形态忽略"),
            ("trend_strength_missed", "趋势强度忽略"),
        ]),
        ("cross_asset_ignored", [
            ("us_to_cn_missed", "美→A 联动忽略"),
            ("cn_to_us_missed", "A→美 联动忽略"),
            ("eu_to_us_missed", "欧→美 联动忽略"),
            ("us_to_eu_missed", "美→欧 联动忽略"),
            ("jp_to_us_missed", "日→美 联动忽略"),
            ("uk_to_us_missed", "英→美 联动忽略"),
            ("crypto_to_equity_missed", "加密→权益联动忽略"),
            ("equity_to_fx_missed", "权益→外汇联动忽略"),
            ("bond_to_equity_missed", "债券→权益联动忽略"),
        ]),
        ("macro_ignored", [
            ("us_event_fomc_missed", "美 FOMC 忽略"),
            ("us_event_nfp_missed", "美 NFP 忽略"),
            ("us_event_cpi_missed", "美 CPI 忽略"),
            ("us_event_ppi_missed", "美 PPI 忽略"),
            ("cn_event_lpr_missed", "中 LPR 忽略"),
            ("cn_event_pmi_missed", "中 PMI 忽略"),
            ("cn_event_cpi_missed", "中 CPI 忽略"),
            ("eu_event_ecb_missed", "欧 ECB 忽略"),
            ("eu_event_hicp_missed", "欧 HICP 忽略"),
            ("jp_event_boj_missed", "日 BOJ 忽略"),
            ("uk_event_boe_missed", "英 BOE 忽略"),
        ]),
        ("pricing_anchor_ignored", [
            ("us_yield_missed", "美债收益率忽略"),
            ("cn_yield_missed", "中债收益率忽略"),
            ("jp_yield_missed", "日债收益率忽略"),
            ("uk_gilt_missed", "英 Gilt 忽略"),
            ("de_bund_missed", "德 Bund 忽略"),
            ("dxy_missed", "DXY 忽略"),
            ("gold_break_missed", "黄金突破忽略"),
            ("copper_gold_missed", "铜金比忽略"),
        ]),
        ("sentiment_ignored", [
            ("positioning_cftc_missed", "CFTC 持仓忽略"),
            ("positioning_cfets_missed", "CFETS 持仓忽略"),
            ("flow_etf_missed", "ETF 资金流忽略"),
            ("flow_foreign_missed", "外资流向忽略"),
            ("flow_margin_missed", "融资融券忽略"),
            ("risk_sentiment_missed", "风险情绪忽略"),
        ]),
    ],
    4: [
        ("volatility_regime", [
            ("vol_spike_us_equity", "美股波动率飙升"),
            ("vol_spike_cn_equity", "A 股波动率飙升"),
            ("vol_spike_crypto", "加密波动率飙升"),
            ("vol_spike_fx", "外汇波动率飙升"),
            ("vol_spike_commodity", "商品波动率飙升"),
            ("vol_crush", "波动率骤降"),
            ("vol_regime_switch", "波动率体制切换"),
            ("vix_term_inversion", "VIX 期限倒挂"),
        ]),
        ("liquidity_regime", [
            ("liquidity_drunk", "流动性泛滥"),
            ("liquidity_dryup", "流动性枯竭"),
            ("fragmentation_us", "美股流动性碎片化"),
            ("fragmentation_cn", "A 股流动性碎片化"),
            ("fragmentation_eu", "欧股流动性碎片化"),
            ("central_bank_liq_fed", "美联储流动性"),
            ("tape_change", "行情结构变化"),
            ("on_run_off_balance", "表外资金进出"),
        ]),
        ("structure_regime", [
            ("us_sec_rule", "美 SEC 规则"),
            ("cftc_rule", "美 CFTC 规则"),
            ("csrc_rule", "中 CSRC 规则"),
            ("esma_rule", "欧 ESMA 规则"),
            ("fca_rule", "英 FCA 规则"),
            ("fsa_rule", "日 FSA 规则"),
            ("hk_sfc_rule", "港 SFC 规则"),
            ("market_maker_exit", "做市商退出"),
            ("algo_shift", "算法切换"),
            ("dark_pool_change", "暗池结构变化"),
            ("etf_flow_shift", "ETF 资金切换"),
        ]),
        ("participant_change", [
            ("us_retail_flood", "美散户涌入"),
            ("cn_institutional_exit", "A 股机构离场"),
            ("crypto_whale_activity", "加密巨鲸活动"),
            ("pension_rotation", "养老金调仓"),
            ("sovereign_buyer", "主权买方"),
            ("retail_to_quant_rotation", "散户→量化切换"),
        ]),
    ],
    # === code 5 追加锚点 ===
    5: [
        ("market_regime", [
            ("regime_switch_bull_us_equity", "美股切牛市"),
            ("regime_switch_bear_us_equity", "美股切熊市"),
            ("regime_switch_bull_cn_equity", "A 股切牛市"),
            ("regime_switch_bear_cn_equity", "A 股切熊市"),
            ("regime_switch_crypto", "加密切体制"),
            ("regime_choppy_us_equity", "美股震荡"),
            ("regime_low_vol_chop_us_equity", "美股低波震荡"),
            ("regime_high_vol_us_equity", "美股高波"),
            ("regime_crisis", "危机模式"),
            ("regime_normal", "常态模式"),
        ]),
        ("regulatory", [
            ("sec_new_rule_us_equity", "美 SEC 新规"),
            ("cftc_new_rule_us", "美 CFTC 新规"),
            ("csrc_new_rule_cn", "中 CSRC 新规"),
            ("esma_new_rule_eu", "欧 ESMA 新规"),
            ("fca_new_rule_uk", "英 FCA 新规"),
            ("fsa_new_rule_jp", "日 FSA 新规"),
            ("hk_sfc_new_rule", "港 SFC 新规"),
            ("mifid_change_eu", "欧 MiFID 变化"),
            ("basel_change_global", "巴塞尔协议变化"),
            ("cftc_position_limit_us", "美 CFTC 持仓上限"),
        ]),
        ("policy_direction", [
            ("fed_pivot_us", "美联储转向"),
            ("ecb_pivot_eu", "欧 ECB 转向"),
            ("boj_pivot_jp", "日 BOJ 转向"),
            ("pboc_pivot_cn", "中 PBOC 转向"),
            ("boe_pivot_uk", "英 BOE 转向"),
            ("fed_capital_rule_us", "美资本规则"),
            ("ecb_tltro_change_eu", "欧 TLTRO 变化"),
        ]),
        ("structural", [
            ("microstructure_shift_us_equity", "美股微观结构变"),
            ("microstructure_shift_crypto", "加密微观结构变"),
            ("etf_creation_change_us_equity", "美股 ETF 创设变化"),
            ("option_listing_change", "期权上市变化"),
            ("leverage_rule_change_us", "美杠杆规则变化"),
            ("position_limit_change_cn_equity", "A 股市仓位限制变"),
        ]),
        ("participant", [
            ("active_pension_entry_us_equity", "美养老金入场"),
            ("retail_exit_us_equity", "美散户离场"),
            ("foreign_pull_out_em", "EM 外资撤离"),
            ("sovereign_entry_gold", "黄金主权买入"),
            ("swap_dealer_exit_us_equity", "美 Swap Dealer 退出"),
        ]),
        ("technology", [
            ("ai_driven_quant", "AI 量化主导"),
            ("blockchain_change_crypto", "加密区块链变化"),
            ("tokenization_us_equity", "美股代币化"),
            ("settlement_change_us_equity", "美股结算变化"),
            ("t_plus_change_cn_equity", "A 股 T+N 变化"),
        ]),
    ],
    # === code 6 追加锚点 ===
    6: [
        ("asset_property", [
            ("credit_rate_misclass_treasury", "国债属性误判"),
            ("credit_rate_misclass_cb", "央行票据属性误判"),
            ("credit_rate_misclass_corp", "公司债属性误判"),
            ("commodity_misclass_energy", "能源属性误判"),
            ("commodity_misclass_metal", "金属属性误判"),
            ("commodity_misclass_agri", "农产品属性误判"),
            ("crypto_misclass_major", "主流币属性误判"),
            ("crypto_misclass_alt", "山寨币属性误判"),
            ("crypto_misclass_stable", "稳定币属性误判"),
            ("fx_misclass_major", "主要货币属性误判"),
            ("fx_misclass_em", "新兴市场货币属性误判"),
        ]),
        ("theme_direction", [
            ("macro_theme_misread_us", "美宏观题材误判"),
            ("macro_theme_misread_cn", "中宏观题材误判"),
            ("macro_theme_misread_eu", "欧宏观题材误判"),
            ("sector_theme_misread_tech", "科技板块题材误判"),
            ("sector_theme_misread_fin", "金融板块题材误判"),
            ("sector_theme_misread_energy", "能源板块题材误判"),
            ("event_theme_misread_earnings", "财报事件误判"),
            ("event_theme_misread_ma", "并购事件误判"),
            ("event_theme_misread_ipo", "IPO 事件误判"),
            ("policy_misread_fed", "美联储政策误判"),
            ("policy_misread_cb", "央行政策误判"),
            ("policy_misread_moff", "财政部政策误判"),
            ("policy_misread_treasury", "财政部声明误判"),
        ]),
        ("sign_direction", [
            ("long_short_inverted_equity", "权益多空反向"),
            ("long_short_inverted_fx", "外汇多空反向"),
            ("long_short_inverted_crypto", "加密多空反向"),
            ("neutral_treated_directional_bond", "债券中性当方向"),
            ("neutral_treated_directional_fx", "外汇中性当方向"),
        ]),
        ("magnitude", [
            ("overestimate_btc", "BTC 幅度高估"),
            ("overestimate_spx", "SPX 幅度高估"),
            ("overestimate_fx", "外汇幅度高估"),
            ("overestimate_commodity", "商品幅度高估"),
            ("underestimate_btc", "BTC 幅度低估"),
            ("underestimate_spx", "SPX 幅度低估"),
            ("underestimate_altcoin", "山寨币幅度低估"),
            ("directional_amplitude_missed", "方向幅度漏判"),
            ("volatility_amplitude_missed", "波动幅度漏判"),
        ]),
        ("cycle", [
            ("stage_misread_early_cycle", "早期周期阶段误判"),
            ("stage_misread_mid_cycle", "中期周期阶段误判"),
            ("stage_misread_late_cycle", "晚期周期阶段误判"),
            ("duration_misread_days", "天级周期误判"),
            ("duration_misread_weeks", "周级周期误判"),
            ("duration_misread_intraday", "日内周期误判"),
            ("inflection_missed", "拐点漏判"),
        ]),
        ("correlation", [
            ("pair_misclass_btc_eth", "BTC/ETH 相关性误判"),
            ("pair_misclass_ust_dxy", "UST/DXY 相关性误判"),
            ("pair_misclass_oil_cad", "油/加元相关性误判"),
            ("pair_misclass_gold_silver", "金银相关性误判"),
            ("pair_misclass_spx_vix", "SPX/VIX 相关性误判"),
            ("lead_lag_misread_ust_to_equity", "UST→权益领先误判"),
            ("lead_lag_misread_dxy_to_em", "DXY→EM 领先误判"),
            ("decoupling_missed_crypto_equity", "加密与权益脱钩漏判"),
        ]),
    ],
    # === code 7 追加锚点 ===
    7: [
        ("signal_source", [
            ("low_rep_twitter", "低信誉 Twitter"),
            ("low_rep_aggregator", "低信誉聚合源"),
            ("low_rep_opinion_blog", "低信誉观点博客"),
            ("low_rep_unknown_news", "低信誉未知源"),
            ("noisy_twitter", "噪声 Twitter"),
            ("noisy_commentary", "噪声评论"),
            ("opinion_article", "观点文章"),
            ("aggregator_duplicate_reuters", "Reuters 重复聚合"),
            ("aggregator_duplicate_bloomberg", "Bloomberg 重复聚合"),
        ]),
        ("catalyst_type", [
            ("posthoc_eval_us_retail", "事后美零售评估"),
            ("posthoc_eval_cn_industrial", "事后中工业评估"),
            ("analyst_commentary_goldman", "高盛分析师评论"),
            ("analyst_upgrade_ms", "摩根士丹利上调"),
            ("analyst_downgrade_jp_morgan", "摩根大树下调"),
            ("retrospective", "回顾性"),
            ("indirect_impact", "间接影响"),
            ("pre_announced", "预先公告"),
        ]),
        ("impact_estimate", [
            ("impact_overestimate_btc", "BTC 影响高估"),
            ("impact_overestimate_spx", "SPX 影响高估"),
            ("impact_overestimate_fx", "外汇影响高估"),
            ("impact_overestimate_commodity", "商品影响高估"),
            ("impact_underestimate_altcoin", "山寨币影响低估"),
            ("impact_underestimate_fx", "外汇影响低估"),
            ("scope_misread_regional", "区域范围误判"),
            ("scope_misread_sector", "板块范围误判"),
        ]),
        ("confidence_calib", [
            ("overconfident", "过度自信"),
            ("underconfident", "信心不足"),
            ("miscalibrated", "校准失衡"),
        ]),
    ],
    8: [
        ("cycle_truncated", [
            ("long_cycle_in_2h_us_equity", "美股长周期落 2h"),
            ("long_cycle_in_2h_cn_equity", "A 股长周期落 2h"),
            ("long_cycle_in_2h_fx", "外汇长周期落 2h"),
            ("long_cycle_in_2h_crypto", "加密长周期落 2h"),
            ("weekly_news_in_2h", "周级新闻落 2h"),
            ("structural_news_in_2h", "结构性新闻落 2h"),
        ]),
        ("cycle_overrun", [
            ("scalping_in_long_horizon", "剥头皮落长周期"),
            ("intraday_vol_in_daily_cn_equity", "A 股日内波动落日线"),
            ("intraday_vol_in_daily_us_equity", "美股日内波动落日线"),
            ("intraday_vol_in_daily_crypto", "加密日内波动落日线"),
        ]),
        ("reaction_window", [
            ("too_tight_us_session", "美盘窗口太紧"),
            ("too_tight_cn_session", "A 股窗口太紧"),
            ("too_loose_eu_session", "欧盘窗口太松"),
            ("too_loose_fx_session", "外汇窗口太松"),
            ("window_misaligned_asia_open", "亚开窗口错位"),
        ]),
    ],
    9: [
        ("news_latency", [
            ("feed_delay_reuters", "Reuters 延迟"),
            ("feed_delay_bloomberg", "Bloomberg 延迟"),
            ("feed_delay_twitter", "Twitter 延迟"),
            ("manual_pipeline_delay", "手工流水线延迟"),
        ]),
        ("quote_latency", [
            ("mt5_delay", "MT5 延迟"),
            ("bloomberg_delay", "Bloomberg 行情延迟"),
            ("cme_delay", "CME 延迟"),
            ("cffex_delay", "CFFEX 延迟"),
            ("snapshot_stale", "快照陈旧"),
            ("tick_stale", "Tick 陈旧"),
        ]),
        ("data_release_delay", [
            ("api_delay_fed", "美联储 API 延迟"),
            ("api_delay_bls", "BLS API 延迟"),
            ("api_delay_nbs_cn", "国统局 API 延迟"),
            ("api_delay_ecb", "ECB API 延迟"),
            ("api_delay_boj", "BOJ API 延迟"),
            ("api_delay_boe", "BOE API 延迟"),
        ]),
    ],
    # === code 10 追加锚点 ===
    10: [
        ("cycle_phase", [
            ("bull_phase_us_equity", "美股牛市阶段"),
            ("bull_phase_cn_equity", "A 股牛市阶段"),
            ("bull_phase_crypto", "加密牛市阶段"),
            ("bear_phase_us_equity", "美股熊市阶段"),
            ("bear_phase_cn_equity", "A 股熊市阶段"),
            ("bear_phase_commodity", "商品熊市阶段"),
            ("sideways_phase_us_equity", "美股盘整阶段"),
            ("sideways_phase_cn_equity", "A 股盘整阶段"),
            ("transition_phase_us_equity", "美股转换阶段"),
            ("transition_phase_cn_equity", "A 股转换阶段"),
        ]),
        ("policy_regime", [
            ("rate_hike_us", "美加息"),
            ("rate_cut_us", "美降息"),
            ("rate_hike_cn", "中加息"),
            ("rate_cut_cn", "中降息"),
            ("qe_active_us", "美 QE"),
            ("qt_active_us", "美 QT"),
            ("policy_reversal_us", "美政策反转"),
            ("policy_reversal_cn", "中政策反转"),
            ("policy_reversal_eu", "欧政策反转"),
            ("policy_reversal_jp", "日政策反转"),
        ]),
        ("macro_drivers", [
            ("us_recession_signal", "美衰退信号"),
            ("cn_recovery_signal", "中复苏信号"),
            ("eu_stagflation_signal", "欧滞胀信号"),
            ("jp_reflation_signal", "日再通胀信号"),
            ("em_capital_outflow", "EM 资本外流"),
            ("dm_capital_inflow", "DM 资本流入"),
        ]),
    ],
    11: [
        ("rule_logic", [
            ("trigger_threshold_missed_us_equity", "美股触发阈值漏判"),
            ("trigger_threshold_missed_cn_equity", "A 股触发阈值漏判"),
            ("trigger_threshold_missed_crypto", "加密触发阈值漏判"),
            ("trigger_threshold_missed_fx", "外汇触发阈值漏判"),
            ("logic_branch_misread_bull", "多头逻辑分支误判"),
            ("logic_branch_misread_bear", "空头逻辑分支误判"),
            ("logic_branch_misread_neutral", "中性逻辑分支误判"),
            ("condition_combinator_wrong", "条件组合错误"),
            ("rule_off_us_market", "美股规则未启"),
            ("rule_off_cn_market", "A 股规则未启"),
            ("rule_off_crypto", "加密规则未启"),
        ]),
        ("rule_age", [
            ("rule_overfit_us_equity", "美股规则过拟合"),
            ("rule_overfit_cn_equity", "A 股规则过拟合"),
            ("rule_overfit_crypto", "加密规则过拟合"),
            ("rule_stale_btc", "BTC 规则过期"),
            ("rule_stale_spx", "SPX 规则过期"),
            ("rule_stale_fx", "外汇规则过期"),
            ("rule_stale_commodity", "商品规则过期"),
            ("rule_decay_after_2024", "2024 后规则衰减"),
        ]),
        ("hedge_logic", [
            ("hedge_ratio_us_equity", "美股对冲比例"),
            ("hedge_ratio_cn_equity", "A 股对冲比例"),
            ("hedge_ratio_crypto", "加密对冲比例"),
            ("hedge_off_us_equity", "美股对冲关闭"),
            ("hedge_off_cn_equity", "A 股对冲关闭"),
            ("hedge_off_crypto", "加密对冲关闭"),
            ("cross_hedge_misclass_bond_equity", "债→股交叉对冲误判"),
        ]),
    ],
    12: [
        ("duplicate_source", [
            ("dup_reuters", "Reuters 重复"),
            ("dup_bloomberg", "Bloomberg 重复"),
            ("dup_wind", "Wind 重复"),
            ("dup_twitter", "Twitter 重复"),
            ("dup_xinhua", "新华社重复"),
            ("dup_cnbc", "CNBC 重复"),
            ("dup_aggregator", "聚合器重复"),
        ]),
        ("old_news", [
            ("old_days_us", "美旧闻（日级）"),
            ("old_days_cn", "中旧闻（日级）"),
            ("old_weeks_us", "美旧闻（周级）"),
            ("old_months_us", "美旧闻（月级）"),
            ("old_holiday_replay", "节假日重播"),
            ("old_reprint", "转载"),
        ]),
        ("stale_priced", [
            ("priced_us_equity", "美股已定价"),
            ("priced_cn_equity", "A 股已定价"),
            ("priced_crypto", "加密已定价"),
            ("priced_fx", "外汇已定价"),
            ("priced_commodity", "商品已定价"),
        ]),
    ],
    13: [
        ("missed_hedge", [
            ("missed_us_equity", "美股漏对冲"),
            ("missed_cn_equity", "A 股漏对冲"),
            ("missed_crypto", "加密漏对冲"),
            ("missed_fx", "外汇漏对冲"),
            ("missed_commodity", "商品漏对冲"),
            ("missed_bond", "债券漏对冲"),
        ]),
        ("missed_consensus", [
            ("consensus_path_us_equity", "美股共识路径漏"),
            ("consensus_path_cn_equity", "A 股共识路径漏"),
            ("consensus_path_crypto", "加密共识路径漏"),
            ("consensus_path_fx", "外汇共识路径漏"),
            ("consensus_path_commodity", "商品共识路径漏"),
        ]),
        ("missed_causal_chain", [
            ("causal_bond_to_equity", "债→股因果链漏"),
            ("causal_dxy_to_em", "DXY→EM 因果链漏"),
            ("causal_oil_to_fx", "油→外汇因果链漏"),
            ("causal_gold_to_aud", "金→澳元因果链漏"),
            ("causal_spx_to_btc", "SPX→BTC 因果链漏"),
            ("causal_dxy_to_gold", "DXY→金 因果链漏"),
        ]),
        ("missed_second_order", [
            ("second_order_policy", "政策二阶漏"),
            ("second_order_earnings", "财报二阶漏"),
            ("second_order_geopolitics", "地缘二阶漏"),
            ("second_order_china_passthrough", "中二阶传递漏"),
            ("second_order_crypto_to_alt", "加密→山寨二阶漏"),
        ]),
    ],
    # === code 14 追加锚点 ===
    14: [
        ("asset_class", [
            ("us_equity_hit", "美股命中"),
            ("cn_equity_hit", "A 股命中"),
            ("hk_equity_hit", "港股命中"),
            ("jp_equity_hit", "日股命中"),
            ("eu_equity_hit", "欧股命中"),
            ("uk_equity_hit", "英股命中"),
            ("em_equity_hit", "EM 权益命中"),
            ("crypto_hit", "加密命中"),
            ("fx_hit", "外汇命中"),
            ("commodity_hit", "商品命中"),
            ("bond_hit", "债券命中"),
            ("index_hit", "指数命中"),
        ]),
        ("direction_pick", [
            ("long_pick_us_equity", "美股多头命中"),
            ("short_pick_us_equity", "美股空头命中"),
            ("long_pick_cn_equity", "A 股多头命中"),
            ("short_pick_cn_equity", "A 股空头命中"),
            ("long_pick_crypto", "加密多头命中"),
            ("short_pick_crypto", "加密空头命中"),
            ("long_pick_fx", "外汇多头命中"),
            ("short_pick_fx", "外汇空头命中"),
            ("long_pick_commodity", "商品多头命中"),
            ("short_pick_commodity", "商品空头命中"),
            ("neutral_treated_directional", "中性当方向"),
        ]),
        ("direction_timing", [
            ("early_entry_us_equity", "美股早入"),
            ("early_entry_cn_equity", "A 股早入"),
            ("early_entry_crypto", "加密早入"),
            ("early_entry_fx", "外汇早入"),
            ("early_entry_commodity", "商品早入"),
            ("late_entry_us_equity", "美股晚入"),
            ("late_entry_cn_equity", "A 股晚入"),
            ("late_entry_crypto", "加密晚入"),
            ("late_entry_fx", "外汇晚入"),
            ("late_entry_commodity", "商品晚入"),
            ("missed_turn_us_equity", "美股漏转折"),
            ("missed_turn_cn_equity", "A 股漏转折"),
            ("missed_turn_crypto", "加密漏转折"),
        ]),
        ("execution_quality", [
            ("slippage_us_equity", "美股滑点"),
            ("slippage_cn_equity", "A 股滑点"),
            ("slippage_crypto", "加密滑点"),
            ("slippage_fx", "外汇滑点"),
            ("slippage_commodity", "商品滑点"),
            ("spread_us_equity", "美股点差"),
            ("spread_cn_equity", "A 股点差"),
            ("spread_crypto", "加密点差"),
            ("partial_fill_us_equity", "美股部分成交"),
            ("partial_fill_cn_equity", "A 股部分成交"),
            ("partial_fill_crypto", "加密部分成交"),
        ]),
    ],
    15: [
        ("venue", [
            ("mt5_slippage", "MT5 滑点"),
            ("mt5_reject", "MT5 拒单"),
            ("cme_slippage", "CME 滑点"),
            ("cme_reject", "CME 拒单"),
            ("cffex_slippage", "CFFEX 滑点"),
            ("cffex_reject", "CFFEX 拒单"),
            ("binance_slippage", "Binance 滑点"),
            ("binance_reject", "Binance 拒单"),
            ("ib_slippage", "IB 滑点"),
            ("ib_reject", "IB 拒单"),
            ("sse_reject", "SSE 拒单"),
            ("hkex_reject", "HKEX 拒单"),
        ]),
        ("order_type", [
            ("market_against_us_equity", "美股市价逆单"),
            ("market_against_cn_equity", "A 股市价逆单"),
            ("market_against_crypto", "加密市价逆单"),
            ("limit_misfetch", "限价未撮"),
            ("stop_misfetch", "止损未撮"),
            ("oco_misfetch", "OCO 未撮"),
        ]),
        ("latency", [
            ("venue_latency_us", "美股场所延迟"),
            ("venue_latency_cn", "A 股场所延迟"),
            ("venue_latency_eu", "欧股场所延迟"),
            ("venue_latency_crypto", "加密场所延迟"),
            ("venue_latency_fx", "外汇场所延迟"),
            ("venue_latency_commodity", "商品场所延迟"),
            ("quote_race", "行情抢跑"),
            ("ack_timeout", "确认超时"),
        ]),
    ],
    16: [
        ("entry_window", [
            ("premature_us_equity", "美股入场过早"),
            ("premature_cn_equity", "A 股入场过早"),
            ("premature_crypto", "加密入场过早"),
            ("premature_fx", "外汇入场过早"),
            ("premature_commodity", "商品入场过早"),
            ("delayed_us_equity", "美股入场过晚"),
            ("delayed_cn_equity", "A 股入场过晚"),
            ("delayed_crypto", "加密入场过晚"),
            ("delayed_fx", "外汇入场过晚"),
            ("delayed_commodity", "商品入场过晚"),
            ("no_confirmation_us_equity", "美股无确认入场"),
            ("no_confirmation_cn_equity", "A 股无确认入场"),
            ("no_confirmation_crypto", "加密无确认入场"),
        ]),
        ("anchor_pick", [
            ("wrong_breakout_us_equity", "美股突破位错"),
            ("wrong_breakout_cn_equity", "A 股突破位错"),
            ("wrong_breakout_crypto", "加密突破位错"),
            ("wrong_breakout_fx", "外汇突破位错"),
            ("wrong_breakout_commodity", "商品突破位错"),
            ("wrong_pullback_us_equity", "美股回调位错"),
            ("wrong_pullback_cn_equity", "A 股回调位错"),
            ("wrong_pullback_crypto", "加密回调位错"),
            ("wrong_range_us_equity", "美股区间位错"),
            ("wrong_range_cn_equity", "A 股区间位错"),
        ]),
        ("event_window", [
            ("before_fomc", "FOMC 前入场"),
            ("before_nfp", "NFP 前入场"),
            ("before_cpi", "CPI 前入场"),
            ("before_earnings", "财报前入场"),
            ("before_opec", "OPEC 前入场"),
            ("during_session_close", "收盘中入场"),
            ("during_session_open", "开盘中入场"),
        ]),
    ],
    # === code 17 追加锚点 ===
    17: [
        ("fallback", [
            ("gpt4_keyword_fallback", "GPT-4 关键词降级"),
            ("deepseek_keyword_fallback", "DeepSeek 关键词降级"),
            ("claude_keyword_fallback", "Claude 关键词降级"),
            ("local_keyword_fallback", "本地模型关键词降级"),
        ]),
        ("quality", [
            ("gpt4_quality_drift_us_equity", "GPT-4 美股质量漂移"),
            ("gpt4_quality_drift_crypto", "GPT-4 加密质量漂移"),
            ("deepseek_quality_drift_cn_equity", "DeepSeek A 股质量漂移"),
            ("deepseek_quality_drift_crypto", "DeepSeek 加密质量漂移"),
            ("claude_quality_drift_macro", "Claude 宏观质量漂移"),
            ("local_quality_drift", "本地模型质量漂移"),
        ]),
        ("invalid", [
            ("invalid_format_gpt4", "GPT-4 格式无效"),
            ("invalid_format_deepseek", "DeepSeek 格式无效"),
            ("invalid_format_claude", "Claude 格式无效"),
            ("invalid_format_local", "本地模型格式无效"),
        ]),
        ("unrecognized", [
            ("unrecognized_asset_gpt4", "GPT-4 未识别标的"),
            ("unrecognized_asset_deepseek", "DeepSeek 未识别标的"),
            ("unrecognized_asset_claude", "Claude 未识别标的"),
            ("unrecognized_asset_local", "本地模型未识别标的"),
        ]),
        ("depth", [
            ("shallow_us_equity", "美股浅推理"),
            ("shallow_cn_equity", "A 股浅推理"),
            ("shallow_crypto", "加密浅推理"),
            ("shallow_fx", "外汇浅推理"),
            ("shallow_commodity", "商品浅推理"),
            ("no_quote_reasoning", "无引文推理"),
        ]),
    ],
    18: [
        ("feed_unavailable", [
            ("mt5_disconnect", "MT5 断连"),
            ("bloomberg_disconnect", "Bloomberg 断连"),
            ("refinitiv_disconnect", "Refinitiv 断连"),
            ("wind_disconnect", "Wind 断连"),
            ("choice_disconnect", "Choice 断连"),
            ("binance_disconnect", "Binance 断连"),
            ("ib_disconnect", "IB 断连"),
            ("api_timeout", "API 超时"),
        ]),
        ("data_corrupt", [
            ("outlier_us_equity", "美股异常值"),
            ("outlier_cn_equity", "A 股异常值"),
            ("outlier_crypto", "加密异常值"),
            ("outlier_fx", "外汇异常值"),
            ("outlier_commodity", "商品异常值"),
            ("outlier_bond", "债券异常值"),
            ("missing_bar_us_equity", "美股 K 线缺失"),
            ("missing_bar_cn_equity", "A 股 K 线缺失"),
            ("missing_bar_crypto", "加密 K 线缺失"),
            ("missing_bar_fx", "外汇 K 线缺失"),
            ("missing_bar_commodity", "商品 K 线缺失"),
            ("missing_bar_bond", "债券 K 线缺失"),
        ]),
        ("feature_calc", [
            ("indicator_calc_fail_us_equity", "美股指标算崩"),
            ("indicator_calc_fail_cn_equity", "A 股指标算崩"),
            ("indicator_calc_fail_crypto", "加密指标算崩"),
            ("indicator_calc_fail_fx", "外汇指标算崩"),
            ("indicator_calc_fail_commodity", "商品指标算崩"),
            ("indicator_calc_fail_bond", "债券指标算崩"),
            ("nan_blow_up_us_equity", "美股 NaN 蔓延"),
            ("nan_blow_up_cn_equity", "A 股 NaN 蔓延"),
            ("nan_blow_up_crypto", "加密 NaN 蔓延"),
        ]),
    ],
    19: [
        ("dimension_unavailable", [
            ("ohlcv_missing_us_equity", "美股 OHLCV 缺失"),
            ("ohlcv_missing_cn_equity", "A 股 OHLCV 缺失"),
            ("ohlcv_missing_crypto", "加密 OHLCV 缺失"),
            ("ohlcv_missing_fx", "外汇 OHLCV 缺失"),
            ("ohlcv_missing_commodity", "商品 OHLCV 缺失"),
            ("ohlcv_missing_bond", "债券 OHLCV 缺失"),
            ("onchain_metric_missing_btc", "BTC 链上指标缺失"),
            ("onchain_metric_missing_eth", "ETH 链上指标缺失"),
            ("onchain_metric_missing_alt", "山寨链上指标缺失"),
            ("orderbook_missing_us_equity", "美股订单簿缺失"),
            ("orderbook_missing_crypto", "加密订单簿缺失"),
            ("orderbook_missing_cn_equity", "A 股订单簿缺失"),
            ("funding_rate_missing", "资金费率缺失"),
            ("open_interest_missing", "持仓量缺失"),
        ]),
        ("snapshot_missing", [
            ("snapshot_us_equity", "美股快照缺失"),
            ("snapshot_cn_equity", "A 股快照缺失"),
            ("snapshot_hk_equity", "港股快照缺失"),
            ("snapshot_jp_equity", "日股快照缺失"),
            ("snapshot_eu_equity", "欧股快照缺失"),
            ("snapshot_uk_equity", "英股快照缺失"),
            ("snapshot_crypto", "加密快照缺失"),
            ("snapshot_fx", "外汇快照缺失"),
            ("snapshot_commodity", "商品快照缺失"),
            ("snapshot_bond", "债券快照缺失"),
            ("snapshot_index", "指数快照缺失"),
        ]),
    ],
    20: [
        ("state_interpret", [
            ("trend_misread_us_equity", "美股趋势误读"),
            ("trend_misread_cn_equity", "A 股趋势误读"),
            ("trend_misread_crypto", "加密趋势误读"),
            ("trend_misread_fx", "外汇趋势误读"),
            ("trend_misread_commodity", "商品趋势误读"),
            ("trend_misread_bond", "债券趋势误读"),
            ("vol_misread_us_equity", "美股波动误读"),
            ("vol_misread_cn_equity", "A 股波动误读"),
            ("vol_misread_crypto", "加密波动误读"),
            ("vol_misread_fx", "外汇波动误读"),
            ("vol_misread_commodity", "商品波动误读"),
            ("vol_misread_bond", "债券波动误读"),
        ]),
        ("signal_extract", [
            ("signal_missed_us_equity", "美股信号漏"),
            ("signal_missed_cn_equity", "A 股信号漏"),
            ("signal_missed_crypto", "加密信号漏"),
            ("signal_missed_fx", "外汇信号漏"),
            ("signal_missed_commodity", "商品信号漏"),
            ("signal_missed_bond", "债券信号漏"),
            ("signal_false_us_equity", "美股假信号"),
            ("signal_false_cn_equity", "A 股假信号"),
            ("signal_false_crypto", "加密假信号"),
            ("signal_false_fx", "外汇假信号"),
            ("signal_false_commodity", "商品假信号"),
            ("signal_false_bond", "债券假信号"),
        ]),
    ],
    # === code 21 追加锚点 ===
    21: [
        ("feed_failure", [
            ("mt5_feed_fail", "MT5 推送失败"),
            ("bloomberg_feed_fail", "Bloomberg 推送失败"),
            ("wind_feed_fail", "Wind 推送失败"),
            ("refinitiv_feed_fail", "Refinitiv 推送失败"),
            ("choice_feed_fail", "Choice 推送失败"),
            ("binance_feed_fail", "Binance 推送失败"),
            ("ib_feed_fail", "IB 推送失败"),
            ("cme_feed_fail", "CME 推送失败"),
            ("cffex_feed_fail", "CFFEX 推送失败"),
            ("rss_feed_fail", "RSS 推送失败"),
        ]),
        ("order_failure", [
            ("mt5_order_fail", "MT5 下单失败"),
            ("ib_order_fail", "IB 下单失败"),
            ("cme_order_fail", "CME 下单失败"),
            ("cffex_order_fail", "CFFEX 下单失败"),
            ("binance_order_fail", "Binance 下单失败"),
            ("risk_reject_us_equity", "美股风控拒单"),
            ("risk_reject_cn_equity", "A 股风控拒单"),
            ("risk_reject_crypto", "加密风控拒单"),
        ]),
        ("worker_failure", [
            ("worker_crash", "Worker 崩溃"),
            ("worker_timeout", "Worker 超时"),
            ("worker_restart_loop", "Worker 重启循环"),
            ("worker_queue_full", "Worker 队列满"),
            ("worker_state_lost", "Worker 状态丢失"),
        ]),
        ("network_failure", [
            ("vpn_drop", "VPN 断连"),
            ("proxy_timeout", "代理超时"),
            ("dns_fail", "DNS 失败"),
            ("tcp_reset", "TCP 重置"),
            ("nat_fail", "NAT 失败"),
        ]),
    ],
    22: [
        ("priced_pre_run", [
            ("front_run_us_equity", "美股抢跑"),
            ("front_run_cn_equity", "A 股抢跑"),
            ("front_run_crypto", "加密抢跑"),
            ("front_run_fx", "外汇抢跑"),
            ("front_run_commodity", "商品抢跑"),
            ("front_run_bond", "债券抢跑"),
        ]),
        ("event_already_priced", [
            ("priced_fomc", "FOMC 已定价"),
            ("priced_nfp", "NFP 已定价"),
            ("priced_cpi", "CPI 已定价"),
            ("priced_earnings", "财报已定价"),
            ("priced_opec", "OPEC 已定价"),
            ("priced_central_bank", "央行已定价"),
            ("priced_election", "选举已定价"),
        ]),
        ("leak_priced", [
            ("leaked_us_equity", "美股泄漏"),
            ("leaked_cn_equity", "A 股泄漏"),
            ("leaked_crypto", "加密泄漏"),
            ("leaked_fx", "外汇泄漏"),
            ("leaked_commodity", "商品泄漏"),
        ]),
    ],
    23: [
        ("experience_misuse", [
            ("overfit_id_us_equity", "美股经验过拟合 #id"),
            ("overfit_id_cn_equity", "A 股经验过拟合 #id"),
            ("overfit_id_crypto", "加密经验过拟合 #id"),
            ("overfit_id_fx", "外汇经验过拟合 #id"),
            ("overfit_id_commodity", "商品经验过拟合 #id"),
            ("overfit_id_bond", "债券经验过拟合 #id"),
            ("stale_rule_misuse", "陈规则误用"),
        ]),
        ("generalization_fail", [
            ("failed_cross_us_cn", "美→A 迁移失效"),
            ("failed_cross_crypto_equity", "加密→权益失效"),
            ("failed_cross_equity_fx", "权益→外汇失效"),
            ("failed_cross_bond_equity", "债→股失效"),
            ("failed_cross_us_eu", "美→欧失效"),
            ("rule_off_target", "规则未启"),
        ]),
        ("experience_outdated", [
            ("outdated_us_equity", "美股经验过期"),
            ("outdated_cn_equity", "A 股经验过期"),
            ("outdated_crypto", "加密经验过期"),
            ("outdated_fx", "外汇经验过期"),
            ("outdated_commodity", "商品经验过期"),
            ("outdated_bond", "债券经验过期"),
        ]),
    ],
    # ── v189 事件逻辑链层（24/25/26，最高权重）──────────────────────────
    # 主战场 = 分析页（reasoning_path / factor_ranking / factor_chain）
    # 24 = 因果图谱推理链错位：reasoning_path ↔ asset_driver_edges
    24: [
        ("reasoning_path_break", [
            ("missing_step_0", "总框架缺失"),
            ("missing_step_1", "因素定位缺失"),
            ("missing_step_2", "驱动力缺失"),
            ("missing_step_3", "水位博弈缺失"),
            ("missing_step_4", "跨资产资金缺失"),
            ("missing_step_5", "反共识结论缺失"),
            ("missing_step_6", "逻辑自检缺失"),
            ("missing_step_7", "总结缺失"),
        ]),
        ("chain_direction_wrong", [
            ("direction_invert_us_equity", "美股方向反"),
            ("direction_invert_cn_equity", "A 股方向反"),
            ("direction_invert_crypto", "加密方向反"),
            ("direction_invert_fx", "外汇方向反"),
            ("direction_invert_commodity", "商品方向反"),
            ("direction_invert_bond", "债券方向反"),
            ("chain_orphan", "悬空链节点"),
        ]),
        ("driver_weight_mismatch", [
            ("weight_overfit_one_node", "单一节点权重过拟合"),
            ("weight_under_core_node", "核心节点权重过小"),
            ("node_orphan_no_edge", "节点无因果边"),
        ]),
    ],
    # 25 = 概念错位/反共识缺位：factor_ranking 概念词 ↔ asset_driver_nodes
    25: [
        ("concept_misalign", [
            ("concept_no_driver_match", "概念无图谱节点对应"),
            ("concept_partial_match", "部分核心驱动缺位"),
            ("concept_poller_term", "概念词为题材表述"),
            ("concept_alias_no_match", "同义词未匹配"),
        ]),
        ("counter_consensus_missing", [
            ("no_counter_thinking", "无反向思考"),
            ("counter_thinking_partial", "反向思考不充分"),
            ("monologue_one_sided", "单边叙事"),
        ]),
        ("sentiment_driver_mismatch", [
            ("sentiment_no_driver_link", "情绪无驱动连接"),
            ("driver_no_sentiment_link", "驱动无情绪连接"),
            ("score_minus_one_with_label", "当前市场为情绪化的催化剂"),
        ]),
    ],
    # 26 = 跨资产传导方向错：cross_asset ↔ target_asset 方向矛盾
    26: [
        ("ust_yield_invert", [
            ("ust_yield_up_target_long", "UST 升但目标品种做多"),
            ("ust_yield_down_target_short", "UST 降但目标品种做空"),
            ("ust_curve_inversion_ignored", "UST 曲线倒挂被忽略"),
        ]),
        ("dxy_invert", [
            ("dxy_down_target_long_usd", "目标品种做多但 USD 同向（避险应反）"),
            ("dxy_up_target_short_usd", "目标品种做空但 USD 反向"),
            ("dxy_breakout_ignored", "USD 突破被忽略"),
        ]),
        ("oil_invert", [
            ("oil_down_target_long_oil", "原油跌但目标品种做多"),
            ("oil_up_target_short_oil", "原油涨但目标品种做空"),
            ("oil_shock_ignored", "原油冲击被忽略"),
        ]),
        ("crypto_invert", [
            ("btc_down_target_long_crypto", "BTC 跌但同向做多"),
            ("btc_up_target_short_crypto", "BTC 涨但反向做空"),
            ("crypto_decouple_ignored", "加密脱钩被忽略"),
        ]),
    ],
}


def _get_sublayer_levels(code: int) -> List[Tuple[str, List[Tuple[str, str]]]]:
    """返回 (l1, [(l2_key, l2_label), ...]) 列表。"""
    return _SUBLAYER_TWO_LEVEL.get(int(code), [])


def parse_sublayer(code: int, raw: Any) -> Tuple[str, str]:
    """校验回退：返回 (l1, l2)。

    输入可为 "l1"、"l1:l2"、或单 key。
      · l1 不在该 code 枚举内 → default_l1(code)（该 code 的第一个 l1）
      · l1 合法 + l2 不在子枚举内 → default_l2(code, l1)（该 l1 下的第一个 l2）
      · l1 是回退值（非原合法）→ l2 清空，避免误填误导
      · 无 l2 时空串
    """
    layers = _get_sublayer_levels(code)
    if not layers:
        return "", ""
    s = str(raw or "").strip()
    if ":" in s:
        l1_raw, l2_raw = s.split(":", 1)
        l1_in = l1_raw.strip()
        l2_in = l2_raw.strip()
        l1_match = any(it[0] == l1_in for it in layers)
        if not l1_match:
            return layers[0][0], ""  # l1 回退，l2 清空
        l2_enum = next((it[1] for it in layers if it[0] == l1_in), [])
        l2 = l2_in if any(x[0] == l2_in for x in l2_enum) else (l2_enum[0][0] if l2_enum else "")
        return l1_in, l2
    l1_in = s
    l1_match = any(it[0] == l1_in for it in layers)
    if not l1_match:
        return layers[0][0], ""  # l1 回退，l2 清空
    l2_enum = next((it[1] for it in layers if it[0] == l1_in), [])
    return l1_in, (l2_enum[0][0] if l2_enum else "")


def format_sublayer(code: int, l1: str, l2: str = "") -> str:
    """拼成 'l1:l2'；无 l2 时仅 'l1'。"""
    return f"{l1}:{l2}" if l2 else (l1 or "")


# ── gate 门卫层（独立分支，code=-1 走这里） ──
_GATE_SUBLAYER: List[Tuple[str, List[Tuple[str, str]]]] = [
    ("gate_miss", [
        ("focus_no_signal_but_real", "FOCUS 未出信号但有真实催化"),
        ("mech_bypass_missed", "MECH 误关闭导致漏单"),
        ("consensus_false_safe", "CONSENSUS 假安全"),
    ]),
    ("gate_false_positive", [
        ("focus_overfit", "FOCUS 过拟合误报"),
        ("mech_thresh_too_loose", "MECH 阈值过松"),
        ("consensus_thresh_wrong", "CONSENSUS 阈值错"),
    ]),
]


def parse_gate_sublayer(raw: Any) -> Tuple[str, str]:
    """gate 层 sublayer 校验回退。"""
    s = str(raw or "").strip()
    if ":" in s:
        l1_raw, l2_raw = s.split(":", 1)
        l1 = l1_raw.strip() if any(it[0] == l1_raw.strip() for it in _GATE_SUBLAYER) else _GATE_SUBLAYER[0][0]
        l2_enum = next((it[1] for it in _GATE_SUBLAYER if it[0] == l1), [])
        l2 = l2_raw.strip() if any(x[0] == l2_raw.strip() for x in l2_enum) else (l2_enum[0][0] if l2_enum else "")
    else:
        l1 = s if any(it[0] == s for it in _GATE_SUBLAYER) else _GATE_SUBLAYER[0][0]
        l2 = ""
    return l1, l2


def _build_sublayer_prompt_block() -> str:
    """生成 LLM prompt 用的 sublayer 候选枚举块（注入 _analyze_*_llm 的 prompt）。"""
    lines = ["# sublayer 两级受控枚举（必须从下面选，禁止自创；输出格式 l1 或 l1:l2）"]
    for code in sorted(_SUBLAYER_TWO_LEVEL.keys()):
        cname = _REASON_NAMES.get(code, str(code))
        lines.append(f"\n# code {code} {cname}")
        for l1, l2s in _SUBLAYER_TWO_LEVEL[code]:
            keys = [k for k, _ in l2s]
            lines.append(f"   {l1}: {' / '.join(keys)}")
    if _GATE_SUBLAYER:
        lines.append("\n# gate 门卫层（独立分支）")
        for l1, l2s in _GATE_SUBLAYER:
            keys = [k for k, _ in l2s]
            lines.append(f"   {l1}: {' / '.join(keys)}")
    return "\n".join(lines) + "\n"

# HOLD 样本独占归因子集：市场未突破/新闻未驱动 → 只能是这 4 类（8 时间窗口 / 22 已定价 / 12 旧闻 / 2 非活跃）
_HOLD_REASON_CODES: Dict[int, str] = {2: "非活跃时段", 8: "时间窗口不匹配", 12: "新闻重复/旧闻", 22: "新闻已定价/抢跑"}


def reason_layer(code: int) -> str:
    """失败 code → 层名；未知 code 默认 'judgment'。"""
    return _REASON_TO_LAYER.get(int(code), "judgment")


def _detect_session(asset: str, entry_time: str) -> Optional[Dict[str, str]]:
    """判定 entry_time 是否处于该品种的非活跃时段，供 code 2 自动产出。

    规则（上海时区）：
      · 加密（BTC/ETH/SOL/…）24h 连续，无「非活跃」一说 → 返回 None。
      · 周六/周日全天 → 非活跃（传统市场休市，流动性稀薄）。
      · 工作日 23:00–06:59（欧美收盘后、亚洲盘前）→ 低流动性窗口。
    """
    if not entry_time:
        return None
    asset_u = str(asset or "").upper()
    _CRYPTO_24H = {"BTC", "ETH", "SOL", "BNB", "XRP", "DOGE", "ADA", "LTC", "DOT", "LINK", "AVAX", "MATIC"}
    if asset_u in _CRYPTO_24H:
        return None
    try:
        dt = datetime.fromisoformat(str(entry_time).replace("Z", "+00:00"))
    except (ValueError, TypeError):
        return None
    wd = dt.weekday()  # 0=周一
    hour = dt.hour
    if wd >= 5:
        return {"session": "non_active", "detail": f"周末(星期{'六' if wd == 5 else '日'})入场"}
    if hour < 7 or hour >= 23:
        return {"session": "non_active", "detail": f"深夜低流动性({hour:02d}:{dt.minute:02d})入场"}
    return None

_SUCCESS_FRAMEWORK = """\
成功原因分类框架（对每条成功交易给一个主要原因 code，可加次要原因）：
  1 数据完备：决策时行情/宏观数据齐全，判断有据
  2 活跃时段：开市/主力活跃时段入场，流动性好、反应充分
  3 关键因素捕捉：正确纳入了新闻/盘面的核心驱动因素
  4 催化剂直接：新闻是直接催化（direct_catalyst=true），影响明确
  5 高置信信号：信心度高、事件强度强（event_strength high/strong）
  6 模型判断正确：方向/幅度/时机判断准确
  7 时间窗口匹配：预期影响时长与结算窗口匹配（2h/长周期内兑现）
  8 及时响应：新闻发布后早期入场（event_phase early），吃到主升段
  9 跨资产验证：美债/美元/原油/加密联动方向一致，互相印证
  10 市场状态匹配：趋势市追势/震荡市打反转，判断与市场状态一致
"""

# ── v182 反思改进·改进建议 prompt 段 ──
# 在 _analyze_trades_llm / _analyze_holds_llm 输出归因 JSON 末尾追加一个
# improvement_suggestion 字段，结构化描述"如果给你一个旋钮，你会调哪个参数"。
# 候选参数清单限制可调项（防止 LLM 自创），规则严格。
_IMPROVEMENT_PROMPT_BLOCK = """\
# ── v182 改进建议（结构化 JSON，并入 JSON 字典） ──
# 在每条归因 JSON 末尾追加 improvement_suggestion。规则：
#   ① 仅当归因明确指向「可调参数」时输出 primary_param；否则 improvement_suggestion = null
#   ② param_key 必须从 PARAM_KEYS 白名单中选，禁止自创
#   ③ direction 必须是 up / down / on / off 之一
#   ④ magnitude ∈ [0.01, 0.5]（比例）
#   ⑤ confidence ∈ [0, 1]
# 输出格式（必须与归因 JSON 同层追加 improvement_suggestion 字段）：
#   "improvement_suggestion": {
#     "primary_param": {"key": "<PARAM_KEYS>", "direction": "up|down|on|off",
#                       "magnitude": 0.05, "confidence": 0.7,
#                       "rationale": "<50字内调整依据>"},
#     "secondary_params": [{"key":"...", "direction":"...", "magnitude": 0.1,
#                            "confidence": 0.5, "rationale": "..."}],
#     "affected_dimension": "asset_class|action|signal_layer|model_quality",
#     "evidence_summary": "<200字内依据>"
#   }
# 重要：improvement_suggestion 与归因同层；如归因为 HOLD 且无调参建议，置 null。
"""

# 可调参数白名单（落库时强校验，禁止 LLM 输出非白名单 key）
# 这些参数存在于 paper_trading_settings / strategies / strategy_versions / quick_sim 准入逻辑中。
_V182_PARAM_KEYS = frozenset({
    # paper_trading_settings 运行态全局参数
    "buy_min_score", "sell_min_score", "tp1_mult", "tp2_mult",
    "tp1_ratio", "tp2_ratio", "tp_enabled", "gate_enabled",
    "news_trade_max_age_seconds", "market_structure_enabled",
    # strategies / strategy_versions 下的 signal_params
    "signal_threshold", "notional_usdt", "leverage", "trailing_callback_rate",
    "holding_horizon_minutes", "min_event_strength",
    "require_direct_catalyst", "asset_filter",
})


def v182_param_keys() -> List[str]:
    """返回 v182 可调参数白名单（落库时强校验）。"""
    return sorted(_V182_PARAM_KEYS)


def _validate_improvement_suggestion(raw: Any) -> Dict[str, Any]:
    """校验 LLM 返回的 improvement_suggestion JSON。非法回退到空 dict。

    校验规则：
      · primary_param.key 必须在 _V182_PARAM_KEYS 白名单
      · direction ∈ {up, down, on, off}
      · magnitude ∈ [0.01, 0.5]
      · confidence ∈ [0, 1]
      · rationale 200 字以内
      · affected_dimension ∈ {asset_class, action, signal_layer, model_quality, ""}
    """
    out: Dict[str, Any] = {
        "primary_param": {},
        "secondary_params": [],
        "affected_dimension": "",
        "evidence_summary": "",
    }
    if not isinstance(raw, dict):
        return out
    pp = raw.get("primary_param") or {}
    if isinstance(pp, dict):
        key = str(pp.get("key") or "").strip()
        if key in _V182_PARAM_KEYS:
            direction = str(pp.get("direction") or "").strip().lower()
            if direction in ("up", "down", "on", "off"):
                try:
                    magnitude = float(pp.get("magnitude") or 0.0)
                except (TypeError, ValueError):
                    magnitude = 0.0
                magnitude = max(0.0, min(0.5, magnitude))
                try:
                    confidence = float(pp.get("confidence") or 0.0)
                except (TypeError, ValueError):
                    confidence = 0.0
                confidence = max(0.0, min(1.0, confidence))
                rationale = str(pp.get("rationale") or "").strip()[:200]
                out["primary_param"] = {
                    "key": key, "direction": direction,
                    "magnitude": magnitude, "confidence": confidence,
                    "rationale": rationale,
                }
    sp = raw.get("secondary_params") or []
    if isinstance(sp, list):
        for item in sp[:2]:
            if not isinstance(item, dict):
                continue
            k = str(item.get("key") or "").strip()
            d = str(item.get("direction") or "").strip().lower()
            if k not in _V182_PARAM_KEYS or d not in ("up", "down", "on", "off"):
                continue
            try:
                m = float(item.get("magnitude") or 0.0)
            except (TypeError, ValueError):
                m = 0.0
            try:
                c = float(item.get("confidence") or 0.0)
            except (TypeError, ValueError):
                c = 0.0
            out["secondary_params"].append({
                "key": k, "direction": d,
                "magnitude": max(0.0, min(0.5, m)),
                "confidence": max(0.0, min(1.0, c)),
                "rationale": str(item.get("rationale") or "").strip()[:200],
            })
    dim = str(raw.get("affected_dimension") or "").strip()
    if dim in ("asset_class", "action", "signal_layer", "model_quality"):
        out["affected_dimension"] = dim
    ev = str(raw.get("evidence_summary") or "").strip()
    out["evidence_summary"] = ev[:1000]
    return out

_SUCCESS_REASON_NAMES = {
    1: "数据完备", 2: "活跃时段", 3: "关键因素捕捉", 4: "催化剂直接",
    5: "高置信信号", 6: "模型判断正确", 7: "时间窗口匹配",
    8: "及时响应", 9: "跨资产验证", 10: "市场状态匹配",
}

_SCOPE_WHERE = {
    "today": "AND date(ad.created_at) = date('now', 'localtime')",
    "week": "AND datetime(ad.created_at, 'localtime') >= datetime('now', 'localtime', '-7 days')",
    "all": "",
}


def _window_where_for_decisions(lo: Optional[int], hi: Optional[int]) -> str:
    """把 (lo_ts, hi_ts) UTC 秒窗口翻译成 ai_decisions.created_at 的 WHERE 子句。

    v180 P0+：与 /api/replay/stats、/api/review/snapshot 的窗口口径同源 —— 用
    datetime(?, 'unixepoch', 'localtime') 表达"开仓时间 ∈ [lo, hi]"，与全局
    PeriodSelector 选定时段一一对应（不再依赖 scope 的 now() 锚定）。

    返回值已含 "AND " 前缀；若 lo/hi 都为空则返回 ""。
    """
    clauses: List[str] = []
    if lo is not None:
        clauses.append("datetime(ad.created_at, 'localtime') >= datetime(?, 'unixepoch', 'localtime')")
    if hi is not None:
        clauses.append("datetime(ad.created_at, 'localtime') <= datetime(?, 'unixepoch', 'localtime')")
    return "AND " + " AND ".join(clauses) if clauses else ""


def _window_where_for_quick_sim(lo: Optional[int], hi: Optional[int]) -> str:
    """quick_sim_trades 的窗口 WHERE：按 created_at（UTC ISO 字符串）过滤。"""
    clauses: List[str] = []
    if lo is not None:
        clauses.append("datetime(qt.created_at, 'localtime') >= datetime(?, 'unixepoch', 'localtime')")
    if hi is not None:
        clauses.append("datetime(qt.created_at, 'localtime') <= datetime(?, 'unixepoch', 'localtime')")
    return "AND " + " AND ".join(clauses) if clauses else ""


def _resolve_window_clause(
    scope: str, lo: Optional[int], hi: Optional[int], table: str
) -> Tuple[str, tuple]:
    """统一窗口过滤：优先用 (lo, hi) 显式窗口；为空时退回 scope 离散档。

    table ∈ {"decisions", "quick_sim"} —— 决定走哪条 WHERE 生成器（前者用
    ai_decisions.created_at，后者用 quick_sim_trades.created_at）。
    """
    if lo is not None or hi is not None:
        if table == "decisions":
            where = _window_where_for_decisions(lo, hi)
        else:
            where = _window_where_for_quick_sim(lo, hi)
        params: tuple = ()
        if lo is not None:
            params += (int(lo),)
        if hi is not None:
            params += (int(hi),)
        return where, params
    # 退回旧 scope 离散档（保持向后兼容 /api/review/run?scope=today|week|all）
    if table == "decisions":
        return _SCOPE_WHERE.get(scope, ""), ()
    # quick_sim 旧逻辑（line 1601-1605）
    if scope == "today":
        return (
            "AND date(qt.created_at, 'localtime') = date('now', 'localtime')",
            (),
        )
    if scope == "week":
        return (
            "AND datetime(qt.created_at, 'localtime') >= datetime('now', 'localtime', '-7 days')",
            (),
        )
    return "", ()


def _now() -> str:
    return datetime.now(config.TZ_SHANGHAI).strftime("%Y-%m-%d %H:%M:%S")


# ---------------------------------------------------------------------------
# LLM 调用（独立通道：OpenRouter 优先走代理，失败降级 MiniMax）
# ---------------------------------------------------------------------------

def _llm(prompt: str, system: str, max_tokens: int = 1400, timeout: int = 90) -> Optional[str]:
    """轻量 LLM 调用，返回文本。

    复用 ai_worker 的模型池（config.AI_MODEL_ROSTER）+ 熔断降级（_resolve_model_cfg）
    + 代理/直连会话分流，替代此前硬编码 OpenRouter gpt-4o-mini + MiniMax 的手写请求。
    旧实现硬编码模型且无视 use_proxy——OpenRouter 直连会 SSL 校验失败(SSLError)，
    导致归因/复盘主通道"像没启动"；现在与主分析共用同一套可靠通道。
    """
    from engine import ai_worker as _aw

    messages = [{"role": "system", "content": system}, {"role": "user", "content": prompt}]
    cfgs: List[Dict[str, Any]] = []
    for m in config.AI_MODEL_ROSTER:
        try:
            c = _aw._resolve_model_cfg(m)
            if c:
                cfgs.append(c)
        except Exception:
            continue
    mm = _aw._minimax_fallback_cfg()
    if mm:
        cfgs.append(mm)
    for cfg in cfgs:
        if not cfg or not cfg.get("api_key"):
            continue
        label = cfg.get("label") or cfg.get("id")
        session = _aw._LLM_PROXY_SESSION if cfg.get("use_proxy") else _aw._LLM_SESSION
        url = f"{cfg.get('api_base')}/chat/completions"
        headers = {"Authorization": f"Bearer {cfg.get('api_key')}", "Content-Type": "application/json"}
        payload = {
            "model": cfg.get("request_model") or cfg.get("id"),
            "messages": messages,
            "max_tokens": max_tokens,
            "temperature": 0.3,
            **(cfg.get("extra_body") or {}),
        }
        for _attempt in range(2):
            try:
                resp = session.post(url, json=payload, headers=headers, timeout=timeout)
                if resp.status_code == 429:
                    print(f"[REVIEW] LLM {label} HTTP 429", flush=True)
                    time.sleep(2.0)
                    continue
                if resp.status_code >= 400:
                    print(f"[REVIEW] LLM {label} HTTP {resp.status_code}", flush=True)
                    break
                data = resp.json()
                return data["choices"][0]["message"]["content"]
            except Exception as exc:
                print(f"[REVIEW] LLM {label} {type(exc).__name__}", flush=True)
                break
    return None


def _parse_json(text: Optional[str]) -> Any:
    if not text:
        return None
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    # 容忍 markdown 代码块 / 前后杂文本：提取最外层 JSON 对象或数组
    # 优先 array（LLM 经常返回 [{...},{...}] 整体；object 正则会贪婪吞掉外层 [...] 的内容）
    for pattern in (r"\[.*\]", r"\{.*\}"):
        match = re.search(pattern, text, re.DOTALL)
        if match:
            try:
                return json.loads(match.group(0))
            except json.JSONDecodeError:
                continue
    return None


# ---------------------------------------------------------------------------
# 数据查询
# ---------------------------------------------------------------------------

def _verdict_by_pnl(forward_pnl) -> str:
    """真实持仓成败判定：按 forward_pnl 符号（>0 WIN / <0 LOSS / 其余 HOLD）。

    权威依据为真实持仓自己的结算结果（forward_pnl 已按分级止盈加权），
    不再借用快速评测（quick_sim_trades）的短周期 verdict。
    """
    try:
        pnl = float(forward_pnl)
    except (TypeError, ValueError):
        return "HOLD"
    if pnl > 0:
        return "WIN"
    if pnl < 0:
        return "LOSS"
    return "HOLD"


def _fetch_trades(
    conn: sqlite3.Connection,
    scope: str = "all",
    limit: int = 500,
    lo: Optional[int] = None,
    hi: Optional[int] = None,
) -> List[Dict[str, Any]]:
    """取已结算的真实模拟持仓（含新闻、决策快照）。scope: today | week | all。

    成败判定按 forward_pnl 符号（_verdict_by_pnl），即真实持仓自己的结算结果。

    v200 P0+ 严谨兜底: SELECT 加 completeness_score + field_sources, 同时
      WHERE 加完整性过滤(completeness_score >= 0.7). 太低的数据不参与
      经验 / 因果链提炼, 避免脏数据污染 review_insights → prompt.

    v290 P0+（反思系统时段统一）：
      新增 lo/hi (UTC 秒窗口)。窗口非空时按 created_at ∈ [lo, hi] 过滤，
      取代 scope 的离散档（保证与全局 PeriodSelector 同源同窗）。
      scope 仍保留作为 fallback —— lo/hi 都为 None 时退回旧 scope 逻辑，
      不破坏 /api/review/run?scope=today|week|all 的向后兼容。
    """
    where, where_params = _resolve_window_clause(scope, lo, hi, "decisions")
    rows = conn.execute(
        f"""SELECT ad.id, ad.target_asset AS asset, UPPER(ad.suggested_action) AS action,
                   qs.verdict AS stored_verdict,
                   qs.pnl_pct AS forward_pnl, qs.mfe_pct, qs.mae_pct,
                   qs.entry_price, qs.exit_price,
                   datetime(qs.entry_ts, 'unixepoch', '+8 hours') AS entry_time,
                   ad.created_at,
                   ad.expected_horizon, ad.event_strength, ad.reasoning,
                   ad.decision_context,
                   -- 反思校验器所需字段（reflection_checkers）
                   ad.sentiment_score, ad.factor_ranking,
                   ad.hard_data_evidence, ad.data_anchor_ts,
                   rn.content AS news_text, rn.timestamp AS news_time,
                   rn.gate_core_impact, rn.gate_main_asset, rn.gate_kind,
                   rn.gate_strength, rn.gate_verdict,
                   -- v200 P0+ 严谨兜底元数据(过滤脏数据 + 标记 source_breakdown)
                   ad.completeness_score, ad.field_sources, ad.missing_fields,
                   ad.llm_field_count, ad.rule_field_count,
                   ad.business_kb_field_count, ad.historical_field_count
            FROM ai_decisions ad
            LEFT JOIN quick_sim_trades qs ON qs.decision_id = ad.id AND qs.settled = 1
            LEFT JOIN raw_news rn ON rn.id = ad.news_id
            WHERE qs.settled = 1 AND qs.entry_price IS NOT NULL AND qs.entry_price > 0
              AND COALESCE(ad.completeness_score, 1.0) >= 0.7
            {where}
            ORDER BY ad.id DESC
            LIMIT ?""",
        where_params + (limit,),
    ).fetchall()
    out = []
    for row in rows:
        item = dict(row)
        stored = (item.get("stored_verdict") or "").strip().upper()
        item["is_correct"] = stored if stored in ("WIN", "LOSS", "HOLD") else _verdict_by_pnl(item.get("forward_pnl"))
        out.append(item)
    return out


def _fetch_quick_sim_trades(
    conn: sqlite3.Connection,
    scope: str = "all",
    limit: int = 500,
    lo: Optional[int] = None,
    hi: Optional[int] = None,
) -> List[Dict[str, Any]]:
    """取已结算的快速评测单（quick_sim_trades，按配置的短周期模拟结算）。

    与真实持仓分开统计（时间尺度不同：真实持仓按 expected_horizon 结算，
    快速评测按配置时长）——成败判定按 quick_sim 的 pnl_pct 符号。
    通过 JOIN ai_decisions / raw_news 补齐归因所需的新闻/决策上下文字段。

    v290 P0+：lo/hi 非空时按 qt.created_at ∈ [lo, hi] 过滤，与真实持仓同窗。
    """
    where, where_params = _resolve_window_clause(scope, lo, hi, "quick_sim")
    rows = conn.execute(
        f"""SELECT qt.id, qt.asset, qt.action, qt.pnl_pct AS forward_pnl,
                   qt.entry_price, qt.exit_price, qt.max_price, qt.min_price,
                   datetime(qt.entry_ts, 'unixepoch', '+8 hours') AS entry_time,
                   qt.created_at, qt.verdict, qt.gate_passed, qt.gate_reason,
                   qt.score, qt.horizon_minutes,
                   ad.id AS decision_id, ad.agent_model_id, ad.expected_horizon,
                   ad.event_strength, ad.market_category, ad.target_asset,
                   ad.reasoning, ad.decision_context,
                   -- 反思校验器所需字段（reflection_checkers）— 从 ai_decisions 拿
                   ad.sentiment_score, ad.factor_ranking,
                   ad.hard_data_evidence, ad.data_anchor_ts,
                   rn.content AS news_text, rn.timestamp AS news_time,
                   rn.gate_core_impact, rn.gate_main_asset, rn.gate_kind,
                   rn.gate_strength, rn.gate_verdict,
                   -- v200 P0+ 严谨兜底元数据
                   ad.completeness_score, ad.field_sources, ad.missing_fields
            FROM quick_sim_trades qt
            LEFT JOIN ai_decisions ad ON ad.id = qt.decision_id
            LEFT JOIN raw_news rn ON rn.id = COALESCE(qt.news_id, ad.news_id)
            WHERE qt.settled = 1 AND qt.verdict IN ('WIN', 'LOSS')
              AND COALESCE(ad.completeness_score, 1.0) >= 0.7
            {where}
            ORDER BY qt.id DESC
            LIMIT ?""",
        where_params + (limit,),
    ).fetchall()
    out = []
    for row in rows:
        item = dict(row)
        # 快速评测判定以 quick_sim 自身三维 verdict 为准（结算时已写 WIN/LOSS）
        stored = (item.get("verdict") or "").strip().upper()
        item["is_correct"] = stored if stored in ("WIN", "LOSS", "HOLD") else _verdict_by_pnl(item.get("forward_pnl"))
        out.append(item)
    return out


def _agg_range(conn: sqlite3.Connection, since: Optional[str] = None, until: Optional[str] = None) -> Dict[str, Any]:
    """某时间区间内已结算交易的聚合（since/until 为本地时间字符串，含边界）。"""
    clauses = ["qs.settled = 1", "qs.entry_price IS NOT NULL", "qs.entry_price > 0"]
    params: List[Any] = []
    if since:
        clauses.append("datetime(ad.created_at, 'localtime') >= datetime(?, 'localtime')")
        params.append(since)
    if until:
        clauses.append("datetime(ad.created_at, 'localtime') < datetime(?, 'localtime')")
        params.append(until)
    rows = conn.execute(
        f"""SELECT COALESCE(ad.target_asset, '') AS asset,
                   UPPER(ad.suggested_action) AS action,
                   COALESCE(NULLIF(qs.verdict, ''), 'HOLD') AS is_correct,
                   qs.pnl_pct AS forward_pnl
            FROM ai_decisions ad
            JOIN quick_sim_trades qs ON qs.decision_id = ad.id
            WHERE {' AND '.join(clauses)}""",
        tuple(params),
    ).fetchall()
    return _aggregate([dict(row) for row in rows])


def _feedback_comparison(conn: sqlite3.Connection) -> Dict[str, Any]:
    """回灌效果验证：以第一条生效经验（active=1 最早 created_at）为界，
    对比「经验回灌前」与「回灌后」的胜率/盈亏，判断经验是否真的增进了判断。"""
    first = conn.execute(
        "SELECT MIN(created_at) FROM review_insights WHERE active = 1"
    ).fetchone()[0]
    if not first:
        return {"insight_started_at": None, "before": _agg_range(conn), "after": None}
    before = _agg_range(conn, until=first)
    after = _agg_range(conn, since=first)
    return {
        "insight_started_at": first,
        "before": before,
        "after": after,
        "winrate_delta": (
            round((after.get("winrate") or 0) - (before.get("winrate") or 0), 4)
            if after.get("winrate") is not None and before.get("winrate") is not None
            else None
        ),
    }


def _model_stats(conn: sqlite3.Connection) -> List[Dict[str, Any]]:
    """模型维度复盘：按 agent_model_id 统计胜率/盈亏，指导模型选择。"""
    rows = conn.execute(
        """SELECT COALESCE(NULLIF(ad.agent_model_id, ''), '(未知)') AS model,
                  COUNT(*) AS total,
                  SUM(CASE WHEN qs.verdict = 'WIN' THEN 1 ELSE 0 END) AS wins,
                  SUM(CASE WHEN qs.verdict = 'LOSS' THEN 1 ELSE 0 END) AS losses,
                  AVG(qs.pnl_pct) AS avg_pnl,
                  SUM(qs.pnl_pct) AS total_pnl
           FROM ai_decisions ad
           JOIN quick_sim_trades qs ON qs.decision_id = ad.id
           WHERE qs.settled = 1 AND qs.entry_price IS NOT NULL AND qs.entry_price > 0
           GROUP BY ad.agent_model_id
           ORDER BY total DESC""",
    ).fetchall()
    out = []
    for row in rows:
        item = dict(row)
        decided = int(item.get("wins") or 0) + int(item.get("losses") or 0)
        item["decided"] = decided
        item["winrate"] = round(int(item.get("wins") or 0) / decided, 4) if decided else None
        out.append(item)
    return out


def _insight_hit_stats(conn: sqlite3.Connection) -> Dict[int, Dict[str, int]]:
    """逐条经验的【命中归因】统计：{经验id: {total, wins, losses}}。

    数据来源：ai_decisions.insight_hit_ids（建仓时记录"本条命中了哪几条经验"，
    由引擎注入经验时写入）。只统计真正建过仓的交易（entry_price>0）——
    命中但 HOLD/未建仓的样本没有盈亏，不该参与胜率。
    """
    out: Dict[int, Dict[str, int]] = {}
    try:
        rows = conn.execute(
            "SELECT insight_hit_ids, forward_pnl, is_correct FROM ai_decisions"
            " WHERE settled = 1 AND entry_price IS NOT NULL AND entry_price > 0"
            "   AND insight_hit_ids IS NOT NULL AND insight_hit_ids != ''"
        ).fetchall()
    except sqlite3.OperationalError:
        return out
    for r in rows:
        raw = r["insight_hit_ids"] if isinstance(r, dict) is False else r["insight_hit_ids"]
        try:
            ids = json.loads(raw) if isinstance(raw, str) else raw
        except Exception:
            continue
        if not isinstance(ids, list):
            continue
        verdict = (r["is_correct"] or "").strip().upper()
        pnl = r["forward_pnl"]
        if verdict == "HOLD":
            continue  # 三维判定未达标（市场未突破窗口）不参与胜率
        if verdict not in ("WIN", "LOSS"):
            # is_correct 缺失/空（仅历史迁移数据）→ 按盈亏符号兜底
            try:
                p = float(pnl)
            except (TypeError, ValueError):
                continue
            verdict = "WIN" if p > 0 else ("LOSS" if p < 0 else "HOLD")
            if verdict == "HOLD":
                continue
        for i in ids:
            try:
                key = int(i)
            except (TypeError, ValueError):
                continue
            b = out.setdefault(key, {"total": 0, "wins": 0, "losses": 0})
            b["total"] += 1
            if verdict == "WIN":
                b["wins"] += 1
            else:
                b["losses"] += 1
    return out


def _evaluate_insights(conn: sqlite3.Connection) -> Dict[str, Any]:
    """经验自动启停评估 —— 【按命中归因】，双向可恢复。

    旧实现的两个缺陷（已修）：
      ① 用"该经验创建之后的全局胜率"判断单条经验 —— 归因不成立；所有经验共享
         同一判定窗口，一荣俱荣一损俱损（实测曾一次性停用全部 110 条）。
      ② 只遍历 active=1 的经验 → 停用后永不复查，单向不可恢复。

    新实现：
      · 用 insight_hit_ids 统计"命中该经验的交易"胜率（真正的单条归因）
      · 样本 ≥ config.INSIGHT_MIN_SAMPLE：
          命中胜率 < INSIGHT_DEACTIVATE_WINRATE → 停用
      · 已停用经验：命中胜率回升 ≥ INSIGHT_REACTIVATE_WINRATE → 自动恢复启用
      · 样本不足的经验【不动】（不因为"没数据"就杀它）
      · 已停用 + 样本不足 → 恢复启用（rescued_no_sample）：旧"连坐"口径的遗留误杀，
        本身没有任何针对它的命中证据；若因"没数据"就永久停在停用，等于把误杀永久化。
        恢复是可逆的——一旦积累够样本且胜率差，会被重新停用。
    """
    min_n = max(3, int(getattr(config, "INSIGHT_MIN_SAMPLE", 10) or 10))
    de_th = float(getattr(config, "INSIGHT_DEACTIVATE_WINRATE", 0.35) or 0.35)
    re_th = float(getattr(config, "INSIGHT_REACTIVATE_WINRATE", 0.55) or 0.55)
    stats = _insight_hit_stats(conn)
    if not stats:
        # 一条命中数据都没有：无法评估胜率，但"停用且无样本"仍是误杀遗留 → 一并恢复
        return _rescue_unmeasured(conn)

    try:
        rows = conn.execute(
            "SELECT id, rule, active, COALESCE(manual_deactivated, 0) AS manual_deactivated"
            " FROM review_insights WHERE kind = 1 ORDER BY id"
        ).fetchall()
    except sqlite3.OperationalError:
        return {"evaluated": 0, "deactivated": [], "reactivated": [], "skipped_no_sample": 0}

    deactivated: List[str] = []
    reactivated: List[str] = []
    rescued: List[str] = []
    evaluated = 0
    skipped = 0
    for ins in rows:
        iid = int(ins["id"])
        s = stats.get(iid)
        is_active = int(ins["active"] or 0) == 1
        # 人工停用的：自动逻辑一律不动（既不救回、也不因胜率回升而启用）
        manual = int(ins["manual_deactivated"] or 0) == 1
        if not s or s["total"] < min_n:
            skipped += 1  # 样本不足：不评估胜率，也不因"没数据"杀经验
            if not is_active and not manual:
                # 但"已停用 + 无样本 + 非人工"是误杀遗留（没有任何针对它的命中证据）
                # → 恢复启用；日后样本够了且胜率差，会被重新停用。
                conn.execute(
                    "UPDATE review_insights SET active = 1 WHERE id = ?", (iid,)
                )
                rescued.append(str(ins["rule"]))
            continue
        decided = s["wins"] + s["losses"]
        if decided <= 0:
            skipped += 1
            continue
        wr = s["wins"] / decided
        evaluated += 1
        if is_active and wr < de_th:
            conn.execute(
                "UPDATE review_insights SET active = 0, manual_deactivated = 0 WHERE id = ?",
                (iid,),
            )
            deactivated.append(str(ins["rule"]))
        elif (not is_active) and (not manual) and wr >= re_th:
            conn.execute(
                "UPDATE review_insights SET active = 1, manual_deactivated = 0 WHERE id = ?",
                (iid,),
            )
            reactivated.append(str(ins["rule"]))
    if deactivated or reactivated or rescued:
        conn.commit()
    return {
        "evaluated": evaluated,
        "deactivated": deactivated,
        "reactivated": reactivated,
        "rescued_no_sample": rescued,
        "skipped_no_sample": skipped,
    }


def _rescue_unmeasured(conn: sqlite3.Connection) -> Dict[str, Any]:
    """一条命中数据都没有时：把"停用且无样本、且非人工停用"的经验恢复启用。

    与主路径的 rescued_no_sample 同义，只是没有 stats 可分。恢复可逆；
    人工停用的（manual_deactivated=1）不动。
    """
    out: Dict[str, Any] = {"evaluated": 0, "deactivated": [], "reactivated": [],
                           "rescued_no_sample": [], "skipped_no_sample": 0}
    try:
        rows = conn.execute(
            "SELECT id, rule FROM review_insights"
            " WHERE active = 0 AND kind = 1 AND COALESCE(manual_deactivated, 0) = 0 ORDER BY id"
        ).fetchall()
    except sqlite3.OperationalError:
        return out
    for ins in rows:
        conn.execute("UPDATE review_insights SET active = 1 WHERE id = ?", (int(ins["id"]),))
        out["rescued_no_sample"].append(str(ins["rule"]))
    if out["rescued_no_sample"]:
        conn.commit()
    return out


def _aggregate(trades: List[Dict[str, Any]]) -> Dict[str, Any]:
    decided = [t for t in trades if t["is_correct"] in ("WIN", "LOSS")]
    wins = sum(1 for t in decided if t["is_correct"] == "WIN")
    losses = sum(1 for t in decided if t["is_correct"] == "LOSS")
    pnl = [float(t["forward_pnl"] or 0.0) for t in trades if t["forward_pnl"] is not None]
    by_asset: Dict[str, Dict[str, Any]] = {}
    for t in trades:
        a = str(t["asset"] or "?")
        bucket = by_asset.setdefault(a, {"total": 0, "wins": 0, "losses": 0, "pnl": 0.0})
        bucket["total"] += 1
        if t["is_correct"] == "WIN":
            bucket["wins"] += 1
        elif t["is_correct"] == "LOSS":
            bucket["losses"] += 1
        bucket["pnl"] += float(t["forward_pnl"] or 0.0)
    agg = {
        "total": len(trades),
        "decided": len(decided),
        "wins": wins,
        "losses": losses,
        "holds": len(trades) - len(decided),
        "winrate": round(wins / len(decided), 4) if decided else None,
        "avg_pnl": round(sum(pnl) / len(pnl), 4) if pnl else None,
        "total_pnl": round(sum(pnl), 4),
        "by_asset": by_asset,
    }
    # ── 反思系统 hook：对每条 trade 跑程序化校验器，落库 decision_errors ──
    # 不影响原有 aggregate 统计；任何 checker 异常都不会破坏主流程
    try:
        from reflection_checkers import run_all_checkers, save_errors
        import sqlite3 as _sqlite
        import config as _cfg
        _conn = _sqlite.connect(_cfg.DB_PATH, timeout=30)
        _conn.execute("PRAGMA journal_mode=WAL")
        _conn.execute("PRAGMA busy_timeout=30000")
        _conn.row_factory = _sqlite.Row  # 让 reflection_checkers 用列名访问
        try:
            _reflect_count = 0
            for t in trades:
                # 优先用 ai_decisions.id（_fetch_trades 用 ad.id），其次 decision_id（quick_sim 用）
                did = t.get("id") or t.get("decision_id")
                if not did:
                    continue
                # 构造满足 checker 签名的 dict（reflection_checkers.run_all_checkers）
                checker_dec = {
                    "id": did,
                    "suggested_action": t.get("action"),
                    "sentiment_score": t.get("sentiment_score"),
                    "factor_ranking": t.get("factor_ranking"),
                    "hard_data_evidence": t.get("hard_data_evidence"),
                    "data_anchor_ts": t.get("data_anchor_ts"),
                    "reasoning": t.get("reasoning"),
                    "target_asset": t.get("asset"),
                    "forward_pnl": t.get("forward_pnl"),
                    "news_text": t.get("news_text"),
                    "gate_verdict": t.get("gate_verdict"),
                }
                errs = run_all_checkers(checker_dec, conn=_conn)
                if errs:
                    _reflect_count += save_errors(errs, conn=_conn)
            agg["_reflection_errors_inserted"] = _reflect_count
        finally:
            _conn.close()
    except Exception as _e:
        agg["_reflection_errors_inserted"] = 0
        agg["_reflection_error"] = str(_e)[:200]
    return agg


# ---------------------------------------------------------------------------
# 复盘总结 + 失败原因分析
# ---------------------------------------------------------------------------

def _fallback_summary(trades: List[Dict[str, Any]], agg: Dict[str, Any]) -> Dict[str, Any]:
    """LLM 失败 / 超时 / 未配模型时的兜底总结模板。

    始终产出 overview / strengths / weaknesses / patterns / improvements /
    logical_reflection / first_principles 七字段，
    保证反思页即使 LLM 不可用也始终有"复盘总结"文字，而不是只有 31 笔数字。

    数字均来自 agg（已由 _aggregate 计算），保证 overview 描述与
    `curAgg?.total/winrate/total_pnl` 严格一致。
    """
    total = int(agg.get("total") or 0)
    decided = int(agg.get("decided") or 0)
    wins = int(agg.get("wins") or 0)
    losses = int(agg.get("losses") or 0)
    wr_raw = agg.get("winrate")
    wr_pct = f"{wr_raw * 100:.1f}%" if isinstance(wr_raw, (int, float)) else "—"
    total_pnl = float(agg.get("total_pnl") or 0.0)
    by_asset = agg.get("by_asset") or {}

    # ── 找最佳 / 最差品种 ──
    best_asset: Optional[str] = None
    worst_asset: Optional[str] = None
    best_pnl = None
    worst_pnl = None
    for a, b in by_asset.items():
        pnl = float(b.get("pnl") or 0.0)
        if best_pnl is None or pnl > best_pnl:
            best_pnl = pnl
            best_asset = a
        if worst_pnl is None or pnl < worst_pnl:
            worst_pnl = pnl
            worst_asset = a

    # ── 整体综述（≤120 字） ──
    if total == 0:
        overview = "本期无已结算交易样本，无法生成复盘总结（请等待持仓到期结算）。"
    elif decided == 0:
        overview = (
            f"本期共 {total} 笔持仓，均未到期结算（待 expected_horizon 到期后会自动进入统计）。"
            "建议保持当前策略节奏，不要因为暂时没有胜负判定而调整阈值。"
        )
    else:
        sign = "+" if total_pnl >= 0 else ""
        verdict = "整体盈利" if total_pnl > 0 else ("整体亏损" if total_pnl < 0 else "盈亏持平")
        asset_hint = ""
        if best_asset and worst_asset and best_asset != worst_asset:
            asset_hint = f"；{best_asset}表现最佳（{best_pnl:+.2f}%），{worst_asset}表现最弱（{worst_pnl:+.2f}%）"
        overview = (
            f"本期 {total} 笔已结算 {decided} 笔，胜率 {wr_pct}，总盈亏 {sign}{total_pnl:.2f}%，{verdict}{asset_hint}。"
            "本总结由本地规则引擎生成（LLM 暂未给出深度文本）。"
        )

    # ── 强项 / 弱项（按品种数据机械化推出，不再笼统"样本不足"） ──
    strengths: List[str] = []
    weaknesses: List[str] = []
    if decided > 0 and isinstance(wr_raw, (int, float)) and wr_raw >= 0.55:
        strengths.append(f"整体胜率 {wr_pct} ≥ 55%，决策方向偏好正确")
    elif decided > 0 and isinstance(wr_raw, (int, float)) and wr_raw < 0.45:
        weaknesses.append(f"整体胜率 {wr_pct} < 45%，建议复盘入场阈值与信号强度")
    if best_asset and best_pnl is not None and best_pnl > 0:
        strengths.append(f"{best_asset} 净盈亏 {best_pnl:+.2f}%，是当前最稳的品种")
    if worst_asset and worst_pnl is not None and worst_pnl < -1.0:
        weaknesses.append(f"{worst_asset} 净盈亏 {worst_pnl:+.2f}%，是主要亏损来源，建议降权重")
    if not strengths:
        strengths.append("样本不足或方向中性，暂无可量化强项")
    if not weaknesses:
        weaknesses.append("样本不足或方向中性，暂无可量化弱项")

    patterns: List[str] = [
        f"样本量 {decided}/{total}，{'信号方向判断较为稳定' if wins >= losses else '信号方向判断倾向于负胜率'}",
        f"按品种拆分: {len(by_asset)} 个品种贡献了本期全部交易",
    ]
    improvements: List[str] = [
        "等更多笔交易结算后由 LLM 生成更细粒度的归因（基于本次 + 历史样本）",
        "观察下一周期是否继续出现同样的胜率/盈亏结构，再决定参数调整",
    ]

    # ── 逻辑推理反思（按可观察到的样本机械化推出，LLM 不可用时也有兜底） ──
    logical_reflection: List[str] = [
        "本次样本仅依据本周期已结算笔数做推理，跨周期/跨品种结构性偏差无法在 fallback 中识别（需后续 LLM 提炼）",
        "胜率偏低时常见逻辑错位：把同一新闻在 5 分钟内重复触发视为多个独立信号（实为同源噪声放大）",
        "把『表态/统计』类文本当成『即时催化』处理是典型题材误判，需检查规则被识别为反身映射",
    ]
    # ── 第一性原理总结规律（不依赖样本归纳，从市场微结构/资金行为/时间尺度等底层推出） ──
    first_principles: List[str] = [
        "真钱 > 利润：资金的实际进出决定方向，新闻只是触发器，没有资金坐实的'利好'大概率回吐",
        "时间尺度对齐：分钟级噪声 ≠ 日级趋势 ≠ 周级周期，跨尺度推断必然失败",
        "对称风险：同一信号在 ±1 个时间窗口外入场可能完全反向，'等确认'比'抢首根 K'更稳",
        "反馈环：政策/利率/汇率/商品价格互相牵引，单品种孤立判断容易踩雷",
    ]

    pattern_audits = _fallback_pattern_audits(patterns)
    return {
        "overview": overview,
        "strengths": strengths,
        "weaknesses": weaknesses,
        "patterns": patterns,
        "improvements": improvements,
        "logical_reflection": logical_reflection,
        "first_principles": first_principles,
        # v233: 规律发现"为什么 / 适用 / 是否过拟合 / 引用注意"深入自审。
        # fallback 路径下没有 LLM 参与，因此 evidence / why_chain 全部留空，
        # 仅给出 confidence=null + 一行保守 caution，提示"LLM 待生成"。
        "pattern_audits": pattern_audits,
        "_source": "fallback_template",  # 标记，方便前端展示"L L M 待生成"
    }


# ──────────────────────────────────────────────────────────────────────────
# v233: 「规律发现」深入自审 — 递归"为什么"直到问不动为止
# ──────────────────────────────────────────────────────────────────────────
#
# 设计动机：用户反馈 2026-10-09 — 复盘里的「规律发现」只给了结论，
# 没有交代：
#   1. 这条规律为什么成立（根因 / 机制 / 数据支撑）
#   2. 适用条件是什么（什么时候成立 / 什么时候可能反向）
#   3. 会不会只是过拟合 / 样本噪音（按品种、按时段、按题材的小样本相关）
#   4. 引用时该有多大的信心 / 应注意什么（避免被当成"铁律"使用）
#
# 落地：每条 pattern 调用 LLM 一次起，按"最关键的问题"开始自审，
# 模型回答后可以继续追问（next_question 非空就继续），直到：
#   - LLM 显式给出 next_question=null（表示"问不动了"）
#   - 触达 _PATTERN_AUDIT_MAX_DEPTH 上限（兜底，避免 token 失控）
#   - LLM 直接给出 stop_reason="contradicted_by_evidence"（证据反方）
#
# 关键不变量：
#   - factual 字段必须有 evidence 支撑；evidence 只能引用参考交易里的 #id
#   - 出现引用了"不存在的 #id"会被 _validate_audit_evidence 标记为 fabricated_evidence
#   - LLM 不可用 / 解析失败时仍返回骨架（confidence=null），前端不会"空着"，
#     也不会被当作"高信心"显示
#   - 单次复盘最多审 _PATTERN_AUDIT_MAX_PATTERNS 条，避免 token 爆炸
# ──────────────────────────────────────────────────────────────────────────

_PATTERN_AUDIT_MAX_PATTERNS = 3   # 单次复盘最多深入自审几条规律
_PATTERN_AUDIT_MAX_DEPTH = 3      # 单条规律的"为什么"递归上限（收紧：聚焦涨跌逻辑，避免无限发散）
_PATTERN_AUDIT_TRADE_BRIEF_N = 8  # 每条规律 prompt 中附带的最少参考交易笔数

# 递归自审的第一问（模型可基于 evidence 自己提出 next_question 续问）。
# v233+ 修订：原问题是"为什么规律成立"，过于泛化，模型容易跑到哲学/语义/历史沿革。
# 改为锚定金融产品涨跌的驱动因子与因果链，保持自审方向聚焦在「涨跌逻辑」上。
_PATTERN_AUDIT_FIRST_QUESTION = (
    "这条规律背后的「涨跌逻辑」是什么？即：是哪些因子（资金/情绪/事件/技术结构/宏观）"
    "驱动了所描述的涨跌方向？请用 evidence 引用具体 #id 笔；"
    "若本期数据无法支撑因果链，请直接承认'驱动机制未明'，不要编造 id。"
)

_PATTERN_AUDIT_SYSTEM = (
    "你是量化复盘的「涨跌逻辑自审者」。\n"
    "对给定的「规律发现」做深入自审，核心目标是盘清驱动涨跌的逻辑（而不是反复讨论真理、不涉历史溯源）。\n"
    "每轮回答严格遵循 JSON（只输出 JSON）：\n"
    "{\"answer\": str, \"next_question\": str|null, "
    "\"factual\": true|false|null, \"evidence\": [\"#<id> …\", …], "
    "\"conditions_apply\": [str], \"conditions_fail\": [str], "
    "\"overfit_risks\": [str], \"confidence\": 0-1|null, \"caution\": str, "
    "\"stop_reason\": \"no_further_why|max_depth|contradicted_by_evidence\"}\n"
    "硬约束：\n"
    "1) evidence 只许引用本期已提供的 #id 笔；编造 id 一律记 fabricated_evidence。\n"
    "2) factual=false 或 confidence<0.4 时，把 caution 写成'勿作为铁律引用'。\n"
    "3) 问不出更深问题时 next_question=null 并把 stop_reason='no_further_why'。\n"
    "4) 若与 evidence 矛盾，把 stop_reason='contradicted_by_evidence' 并把 "
    "caution 写成'被本期证据反驳，暂不采纳'。\n"
    "5) 【主线约束——决定自审是否 偏离主题】追问必须 锚定在涨跌逻辑相关维度，"
    "即：驱动因子（资金/情绪/事件/技术/宏观）、因果传导链、反例触发场景、阶段性失效条件。"
    "下列方向视为偏离，须立即把 next_question=null 并把 stop_reason='no_further_why'："
    "a) 哲学/语义层面的反思（如'什么是规律''真理是否可被经验验证'等）；"
    "b) 历史溯源叙事（如'该规律源自某次危机''古典学派如何定义'等与本期涨跌无关的考据）；"
    "c) 与涨跌机制无关的纯统计话题（如协整检验公式、计量模型术语科普）。"
    "若不确定某问是否相关，倾向收尾而非继续追问。\n"
    "6) 其他因素可以适度纳入：overfit_risks（小样本 / 题材特殊 / 时段聚集）、"
    "引用注意（confidence 受 fabricated_evidence 影响），但不得喧宾夺主，"
    "evidence 与 conditions_* 的主体必须围绕涨跌驱动 / 反例条件。"
)


def _format_trades_for_audit(trades: List[Dict[str, Any]], max_n: Optional[int] = None) -> str:
    """给 pattern-audit prompt 提供精简的参考交易行（前 N 条 + 总样本数）。"""
    if not trades:
        return "（本期无可参考交易）"
    n = int(max_n or _PATTERN_AUDIT_TRADE_BRIEF_N)
    head = trades[:n]
    lines = []
    for t in head:
        tid = t.get("id") or t.get("decision_id") or "?"
        asset = str(t.get("asset") or "?")
        action = str(t.get("action") or "?")
        verdict = str(t.get("is_correct") or "?")
        pnl = t.get("forward_pnl")
        pnl_s = f"{float(pnl):+.2f}%" if pnl not in (None, "") else "?"
        ctx = t.get("decision_context") or ""
        try:
            ctx_obj = json.loads(ctx) if isinstance(ctx, str) else ctx
            ctx_s = ctx_obj.get("status") if isinstance(ctx_obj, dict) else ""
        except Exception:
            ctx_s = ""
        news = re.sub(r"\[hash:[a-fA-F0-9]+\]\s*", "", str(t.get("news_text") or ""))[:60]
        lines.append(
            f"  #{tid} {asset} {action} {verdict} pnl={pnl_s} | {ctx_s} | {news}"
        )
    if len(trades) > n:
        lines.append(f"  … 共 {len(trades)} 笔，仅展示前 {n} 条")
    return "\n".join(lines)


def _format_agg_for_audit(agg: Dict[str, Any]) -> str:
    """audit prompt 中用一行说明整体聚合指标（避免把 by_asset 整表塞进去）。"""
    if not agg:
        return "（无聚合数据）"
    wr = agg.get("winrate")
    wr_s = f"{wr * 100:.1f}%" if isinstance(wr, (int, float)) else "N/A"
    by_asset = agg.get("by_asset") or {}
    assets = ", ".join(
        f"{a}({b.get('total', 0)}笔/{b.get('pnl', 0.0):+.2f}%)"
        for a, b in sorted(by_asset.items(), key=lambda kv: -kv[1].get("total", 0))[:5]
    )
    return (
        f"总数 {agg.get('total', 0)} / 已判 {agg.get('decided', 0)} / "
        f"胜 {agg.get('wins', 0)} / 负 {agg.get('losses', 0)} / "
        f"胜率 {wr_s} / 总盈亏 {agg.get('total_pnl', 0.0):+.2f}% / 品种: {assets or 'N/A'}"
    )


def _fallback_pattern_audits(patterns: List[str]) -> List[Dict[str, Any]]:
    """LLM 不可用 / 解析失败时的规律自审骨架：每条 pattern 留空自审 + 一行保守 caution。

    v233+ 修订：caution 文案从泛化的「结合 first_principles 自行甄别」收紧为
    「涨跌逻辑待人工审查」——与本轮主线（涨跌逻辑）保持一致。
    """
    audits: List[Dict[str, Any]] = []
    for p in patterns or []:
        audits.append({
            "claim": str(p),
            "factual": None,
            "evidence": [],
            "conditions_apply": [],
            "conditions_fail": [],
            "overfit_risks": [],
            "confidence": None,
            "caution": (
                "LLM 暂未生成涨跌逻辑自审；请手动核对该规律背后的驱动因子"
                "（资金/情绪/事件/技术/宏观）与反例触发条件，"
                "再结合 _fallback_summary 的整体数据与 first_principles 甄别。"
            ),
            "why_chain": [],
            "depth_reached": 0,
            "stop_reason": "no_llm",
            "fabricated_evidence": [],
        })
    return audits


def _normalize_audit_payload(raw: Any, claim: str) -> Dict[str, Any]:
    """把 LLM 返回的单步回答规范成 audit dict（缺失字段用 None/[] 兜底）。"""
    if not isinstance(raw, dict):
        return {
            "answer": "",
            "next_question": None,
            "factual": None,
            "evidence": [],
            "conditions_apply": [],
            "conditions_fail": [],
            "overfit_risks": [],
            "confidence": None,
            "caution": "",
            "stop_reason": "parse_failed",
        }

    def _list_of_str(v: Any) -> List[str]:
        if isinstance(v, list):
            return [str(x).strip() for x in v if str(x).strip()]
        if isinstance(v, str) and v.strip():
            return [v.strip()]
        return []

    def _opt_str(v: Any) -> Optional[str]:
        if v is None:
            return None
        s = str(v).strip()
        return s or None

    def _opt_float(v: Any) -> Optional[float]:
        if v is None or v == "":
            return None
        try:
            f = float(v)
        except (TypeError, ValueError):
            return None
        if f != f:  # NaN
            return None
        return max(0.0, min(1.0, f))

    def _opt_bool(v: Any) -> Optional[bool]:
        if isinstance(v, bool):
            return v
        if isinstance(v, str):
            s = v.strip().lower()
            if s in ("true", "yes", "1"):
                return True
            if s in ("false", "no", "0"):
                return False
        return None

    return {
        "answer": str(raw.get("answer") or "").strip(),
        "next_question": _opt_str(raw.get("next_question")),
        "factual": _opt_bool(raw.get("factual")),
        "evidence": _list_of_str(raw.get("evidence")),
        "conditions_apply": _list_of_str(raw.get("conditions_apply")),
        "conditions_fail": _list_of_str(raw.get("conditions_fail")),
        "overfit_risks": _list_of_str(raw.get("overfit_risks")),
        "confidence": _opt_float(raw.get("confidence")),
        "caution": str(raw.get("caution") or "").strip(),
        "stop_reason": str(raw.get("stop_reason") or "").strip() or None,
    }


def _validate_audit_evidence(audit: Dict[str, Any], trades: List[Dict[str, Any]]) -> List[str]:
    """把 evidence 中引用了「不存在于本期交易 #id」的条目挑出来（不删，只标 fabricated）。

    返回值是该条 audit 中被标记的伪造条目列表（汇总到 summary._audit_fabricated_count）。
    """
    valid_ids = {
        str(t.get("id") or t.get("decision_id") or "").strip()
        for t in trades or []
    }
    valid_ids.discard("")
    fabricated: List[str] = []
    for ev in audit.get("evidence") or []:
        if not isinstance(ev, str):
            continue
        # 抓形如 #123 的引用（数字 id）；纯文字不校验（不强制每条都引用 #id）
        for m in re.finditer(r"#(\d+)", ev):
            if m.group(1) not in valid_ids:
                fabricated.append(ev)
                break
    return fabricated


def _audit_pattern_deep(
    pattern: Any,
    trades: List[Dict[str, Any]],
    agg: Dict[str, Any],
    llm_call: Optional[Any] = None,
    max_depth: Optional[int] = None,
) -> Dict[str, Any]:
    """对单条 pattern 做"递归为什么"深入自审。

    返回 dict 包含：claim / factual / evidence / conditions_apply / conditions_fail /
    overfit_risks / confidence / caution / why_chain（Q→A 列表）/ depth_reached /
    stop_reason / fabricated_evidence。

    终止条件（任一触发即停）：
      - LLM 返回 next_question 为 null / 空
      - 触达 max_depth 上限
      - LLM 返回 stop_reason='contradicted_by_evidence'
      - LLM 调用本身失败（返回 None）→ 退化为 stop_reason='llm_failed'
    """
    claim = pattern if isinstance(pattern, str) else str(pattern)
    call = llm_call or _llm
    depth_cap = max(1, int(max_depth or _PATTERN_AUDIT_MAX_DEPTH))

    audit: Dict[str, Any] = {
        "claim": claim,
        "factual": None,
        "evidence": [],
        "conditions_apply": [],
        "conditions_fail": [],
        "overfit_risks": [],
        "confidence": None,
        "caution": "",
        "why_chain": [],
        "depth_reached": 0,
        "stop_reason": "no_response",
        "fabricated_evidence": [],
    }

    # pattern 太长 / trades 为空时，直接给一份骨架（避免 LLM 跑空 prompt）。
    if not claim.strip() or not trades:
        audit["stop_reason"] = "empty_input"
        audit["caution"] = "输入为空（无规律文字或无可参考交易），无法自审。"
        return audit

    trade_brief = _format_trades_for_audit(trades)
    agg_brief = _format_agg_for_audit(agg)
    chain: List[Dict[str, str]] = []
    current_q = _PATTERN_AUDIT_FIRST_QUESTION
    last_normalized: Dict[str, Any] = {}

    for depth in range(1, depth_cap + 1):
        chain_text = "\n".join(
            f"  Q{idx + 1}: {qa['question']}\n  A{idx + 1}: {qa['answer']}"
            for idx, qa in enumerate(chain)
        ) or "（无）"
        prompt = (
            f"本期复盘数据：\n{agg_brief}\n\n"
            f"参考交易（前 {_PATTERN_AUDIT_TRADE_BRIEF_N} 条 / 共 {len(trades)} 笔）：\n{trade_brief}\n\n"
            f"待自审的规律：{claim}\n\n"
            f"已有的自审问答链：\n{chain_text}\n\n"
            f"本轮最关键的问题：{current_q}\n\n"
            "请基于上述数据回答（严格遵循 system 中的 JSON schema）。"
        )
        raw_text: Optional[str] = None
        try:
            raw_text = call(prompt, _PATTERN_AUDIT_SYSTEM, 900, 60)
        except TypeError:
            # 测试桩可能签名不匹配；按位置参数兜底
            try:
                raw_text = call(prompt, _PATTERN_AUDIT_SYSTEM)
            except Exception:
                raw_text = None
        except Exception:
            raw_text = None

        parsed = _parse_json(raw_text) if raw_text else None
        norm = _normalize_audit_payload(parsed, claim)
        # 若 LLM 实际未产出可用文本，把 stop_reason 显式置为 llm_failed，
        # 避免被默认 "no_further_why" 误导成"问不动了"。
        if not raw_text or parsed is None:
            norm["stop_reason"] = "llm_failed"
        chain.append({
            "question": current_q,
            "answer": norm.get("answer") or "",
        })
        audit["depth_reached"] = depth
        last_normalized = norm

        # 终止判断
        stop_reason = norm.get("stop_reason") or ""
        if stop_reason == "contradicted_by_evidence":
            audit["stop_reason"] = "contradicted_by_evidence"
            break
        if not norm.get("next_question"):
            audit["stop_reason"] = stop_reason or "no_further_why"
            break
        if depth >= depth_cap:
            audit["stop_reason"] = stop_reason or "max_depth"
            break
        # 续问
        current_q = norm["next_question"]

    # 用最后一轮的结论填充顶层字段（why_chain 已记录过程）
    audit.update({
        "factual": last_normalized.get("factual"),
        "evidence": last_normalized.get("evidence") or [],
        "conditions_apply": last_normalized.get("conditions_apply") or [],
        "conditions_fail": last_normalized.get("conditions_fail") or [],
        "overfit_risks": last_normalized.get("overfit_risks") or [],
        "confidence": last_normalized.get("confidence"),
        "caution": last_normalized.get("caution") or "",
        "why_chain": chain,
    })
    # evidence 真实性自检（标记"引用了不存在的 #id"的条目）
    audit["fabricated_evidence"] = _validate_audit_evidence(audit, trades)
    # 若 fabricated 命中且 factual 未显式否认，强制把 confidence 压到 0.4 以下
    if audit["fabricated_evidence"] and (audit["confidence"] is None or audit["confidence"] >= 0.4):
        audit["confidence"] = 0.3
        if "编造" not in audit["caution"]:
            audit["caution"] = (
                (audit["caution"] + "；") if audit["caution"] else ""
            ) + "evidence 中疑似编造了不存在的 #id，置信度被强制压低，勿作为铁律引用。"
    return audit


def _audit_patterns_batch(
    patterns: List[Any],
    trades: List[Dict[str, Any]],
    agg: Dict[str, Any],
    llm_call: Optional[Any] = None,
    max_patterns: Optional[int] = None,
    max_depth: Optional[int] = None,
) -> List[Dict[str, Any]]:
    """批量调用 _audit_pattern_deep，对外暴露的入口。空 patterns → []。"""
    if not patterns:
        return []
    cap = int(max_patterns or _PATTERN_AUDIT_MAX_PATTERNS)
    selected = list(patterns)[:cap]
    audits: List[Dict[str, Any]] = []
    for p in selected:
        audits.append(
            _audit_pattern_deep(p, trades, agg, llm_call=llm_call, max_depth=max_depth)
        )
    return audits


def _review_summary_llm(trades: List[Dict[str, Any]], agg: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    lines = [
        f"交易总数 {agg['total']}，已判定 {agg['decided']}（胜 {agg['wins']} / 负 {agg['losses']}），"
        f"胜率 {agg['winrate'] * 100 if agg['winrate'] is not None else 0:.1f}% 或样本不足，"
        f"总盈亏 forward_pnl {agg['total_pnl']:.2f}%"
    ]
    lines.append("按品种：")
    for asset, b in sorted(agg["by_asset"].items(), key=lambda kv: -kv[1]["total"]):
        wr = b["wins"] / (b["wins"] + b["losses"]) * 100 if (b["wins"] + b["losses"]) else None
        wr_s = f"{wr:.0f}%" if wr is not None else "N/A"
        lines.append(f"  {asset}: {b['total']}笔 胜{wr_s} 盈亏{b['pnl']:+.2f}%")
    lines.append("最近 20 笔明细（id/资产/方向/胜负/盈亏/新闻摘要/决策摘要）：")
    for t in trades[:20]:
        ctx = t.get("decision_context") or "{}"
        try:
            ctx_obj = json.loads(ctx)
            ctx_s = f"行情:{ctx_obj.get('status')}" if isinstance(ctx_obj, dict) else ""
        except (json.JSONDecodeError, TypeError):
            ctx_s = ""
        news = re.sub(r"\[hash:[a-fA-F0-9]+\]\s*", "", t.get("news_text") or "")[:80]
        lines.append(
            f"  #{t['id']} {t['asset']} {t['action']} {t['is_correct'] or '?'} pnl={t['forward_pnl']}"
            f" | {t.get('entry_time')} | {ctx_s} | {news} | {_gate_short(t)}"
        )
    prompt = (
        "你是量化交易复盘分析师。基于以下模拟盘交易数据，输出 JSON：\n"
        "{\"overview\": \"整体表现总结<=120字\", "
        "\"strengths\": [\"强项<=40字\", ...], "
        "\"weaknesses\": [\"弱项<=40字\", ...], "
        "\"patterns\": [\"发现的规律/相关性<=60字\", ...], "
        "\"improvements\": [\"改进建议<=60字\", ...], "
        "\"logical_reflection\": [\"逻辑推理反思<=60字，针对本次交易逻辑链条的自我审视：因果倒置/样本偏差/锚定近因/忽视反馈环/把统计当催化/把表态当兑现等认知错位<=80字\", ...], "
        "\"first_principles\": [\"第一性原理总结规律<=60字，不依赖样本归纳而是从市场微结构/资金行为/时间尺度等底层推出'颠扑不破'的几条原则<=80字\", ...]}\n"
        "只输出 JSON。\n\n" + "\n".join(lines)
    )
    result = _parse_json(_llm(prompt, "你是量化交易复盘分析师，只输出 JSON。", max_tokens=1900))
    return result if isinstance(result, dict) else None


# 事后统计/评估性消息的负面损失措辞 —— 用于识别「把统计/评估消息当成即时避险看涨催化」的题材误判。
_NEG_DAMAGE_WORDS = ("损失", "遇难", "死亡", "伤亡", "重创", "破坏", "摧毁", "代价")
_REVIEW_STAT_WORDS = ("估计", "评估", "统计", "累计", "今年以来", "迄今", "达", "亿美元", "亿元")

# 「官员/机构发言表态」信号 —— 表态是即时事件催化，不是媒体整理的事后统计。
# 典型区分：
#   表态（即时催化）：「乌克兰经济部长：……损失预计将达到100亿美元」
#   统计（事后汇总）：「据统计，……累计损失已达100亿美元」
_SPOKESPERSON_RE = re.compile(
    r"(?:部长|总统|总理|首相|发言人|主席|秘书长|行长|官员|军方|将军|大使|议员"
    r"|政府|央行|财政部|国防部|总统府|克里姆林宫|白宫|国务院|外长|财长)"
    r"[^：:。；,，\n]{0,12}[：:]"
)
_SPEECH_VERB_RE = re.compile(
    r"(?:部长|总统|总理|首相|发言人|主席|秘书长|行长|官员|军方|将军|大使|议员"
    r"|政府|央行|财政部|国防部|总统府|克里姆林宫|白宫|国务院|外长|财长)"
    r"[^：:。；,，\n]{0,12}(?:表示|警告|指出|宣布|声明|表态|强调|敦促|呼吁|透露|证实|披露|称|说)"
)
_EN_STATEMENT_RE = re.compile(
    r"(?:minister|president|prime minister|spokesman|spokesperson|official|governor"
    r"|treasury|central bank|defense minister|foreign minister|pentagon|kremlin|white house)"
    r"[^:.\n]{0,20}[:]"
    r"|(?:minister|president|spokesman|spokesperson|official)[^.\n]{0,20}"
    r"(?:says|said|warns|warned|states|stated|announces|announced|declares|declared"
    r"|vows|vowed|urges|urged|confirms|confirmed)",
    re.I,
)

# 股票/商品/加密品种集合与「新闻主体品种 vs 交易品种」错配识别 —— 归因「提到 A 就扯 B」的影响力误判。
_CRYPTO_ASSETS = {
    "BTC", "ETH", "SOL", "BNB", "XRP", "DOGE", "ADA", "AVAX", "LINK", "DOT",
    "WBTC", "BTCUSDT", "ETHUSDT", "SOLUSDT", "BNBUSDT", "XRPUSDT", "DOGEUSDT",
}
_COMMODITY_ASSETS = {
    "XAU", "XAUUSD", "XAUUSDT", "XAG", "SILVER", "WTI", "CLUSDT", "BRENT",
    "CADJ", "GC", "CL", "COPPER", "HG",
}
# 源明确标注的股票品种标签，如「[美股 NVDA]」「[A股 XX]」——出现即说明新闻主体是股票。
_EQUITY_TAG_RE = re.compile(r"\[(?:美股|A股|港股|欧股|日股|英股|美股期货)[^\]]*\]")
# 强股票主题词（几乎无歧义，加密/商品新闻不会把这些当主语）。
_EQUITY_STOCK_HINTS = (
    "NVDA", "英伟达", "特斯拉", "TSLA", "苹果", "AAPL", "微软", "MSFT",
    "谷歌", "GOOG", "亚马逊", "AMZN", "Meta", "META", "台积电", "TSM",
    # 巴菲特系 / 美股公司名（源不打 [美股 XX] 标签时靠此兜底）
    "伯克希尔", "哈撒韦", "Berkshire", "巴菲特", "Buffett",
    # 美股主要指数 / 市场代称（无标签时的强股票信号）
    "标普500", "S&P500", "S&P 500", "纳斯达克", "道琼斯", "道指",
    "华尔街", "美股", "科技股", "大盘", "股指",
    # 通用股票词
    "股价", "个股", "财报", "IPO", "市值", "市盈率", "市净率",
)


def _is_official_statement(text: str) -> bool:
    """判断新闻是否为「官员/机构发言表态」（即时事件催化），而非媒体整理的事后统计。"""
    if not text:
        return False
    if _SPOKESPERSON_RE.search(text) or _SPEECH_VERB_RE.search(text):
        return True
    return bool(_EN_STATEMENT_RE.search(text))


def _is_cross_asset_mismatch(t: Dict[str, Any]) -> bool:
    """判断该单是否「新闻主体品种与交易品种错配」：新闻是股票主题，却交易加密/商品。

    用客观事实（源自带品种标签 / 强股票主题词）判定，命中即代表「提到 A 就扯 B」的
    影响力误判（归 code 7），而不是入场时机问题（code 16）。
    """
    asset = str(t.get("asset") or "").upper()
    if asset not in _CRYPTO_ASSETS and asset not in _COMMODITY_ASSETS:
        return False  # 交易标的本身不是加密/商品，无法判断错配
    news = str(t.get("news_text") or t.get("content") or "")
    if not news:
        return False
    if _EQUITY_TAG_RE.search(news):
        return True
    return any(h in news for h in _EQUITY_STOCK_HINTS)


def _is_loss_trade(t: Dict[str, Any]) -> bool:
    """判定该失败单是否确实亏损（verdict=LOSS 或 pnl 符号为负）。"""
    verdict = str(t.get("is_correct") or "").upper()
    if verdict == "LOSS":
        return True
    pnl = t.get("forward_pnl", t.get("pnl_pct"))
    try:
        return float(pnl) < 0
    except (TypeError, ValueError):
        return False


def _is_keyword_inference(t: Dict[str, Any]) -> bool:
    """判断 LLM 输出是否降级为关键词推断（reasoning 或 reasoning_path 含「关键词推断」）。

    关键词推断说明模型未完成八段完整分析，是低质量输出，归 code 17（模型推理降级）。
    """
    reasoning = str(t.get("reasoning") or "")
    reasoning_path = str(t.get("reasoning_path") or "")
    return "关键词推断" in reasoning or "关键词推断" in reasoning_path


def _is_stats_report_news(text: str) -> bool:
    """保守判断新闻是否为「事后统计/评估性负面损失」消息（非即时新增催化）。"""
    if not text:
        return False
    has_damage = any(w in text for w in _NEG_DAMAGE_WORDS)
    has_review = any(w in text for w in _REVIEW_STAT_WORDS)
    if not (has_damage and has_review):
        return False
    # 官员/机构「发言表态」≠ 事后统计：表态是即时事件催化（如「经济部长：……损失预计将达100亿」），
    # 统计是媒体整理的已发生汇总（如「据统计……累计损失已达100亿」）。冒号引语/引语动词一票排除。
    if _is_official_statement(text):
        return False
    return True


def _ctx_evidence(decision_context: Optional[Any], asset: str, max_len: int = 240) -> str:
    """从决策快照 summary 截取目标品种的行情证据（价格/多周期趋势/技术位），
    供归因用事实反证「方向对错」与「当时技术面是否支持该方向」。"""
    if not decision_context or not asset:
        return ""
    try:
        obj = json.loads(decision_context) if isinstance(decision_context, str) else decision_context
        summary = str(obj.get("summary") or "") if isinstance(obj, dict) else ""
    except (json.JSONDecodeError, TypeError):
        return ""
    if not summary or asset not in summary:
        return ""
    start = summary.find(asset)
    seg = summary[start:]
    m = re.search(r"\n[A-Z]{2,5}\s+\$", seg)  # 到下一个品种行截止
    if m:
        seg = seg[: m.start()]
    return " ".join(seg.split())[:max_len]


def _num(v: Any) -> Optional[float]:
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _gate_short(t: Dict[str, Any]) -> str:
    """门卫四步一句话摘要，供复盘/归因 per-trade 行展示；无则空串。"""
    core = str(t.get("gate_core_impact") or "").strip()
    main = str(t.get("gate_main_asset") or "").strip()
    verdict = str(t.get("gate_verdict") or "").strip()
    strength = _num(t.get("gate_strength"))
    if not core and not verdict:
        return ""
    seg = core[:40] if core else "（空）"
    extras = []
    if main:
        extras.append(f"主{main}")
    if verdict:
        extras.append(verdict)
    if strength is not None:
        extras.append(f"强度{strength:g}")
    return f"门卫:{seg}" + ("(" + "/".join(extras) + ")" if extras else "")


def _gate_line_summary(trades: List[Dict[str, Any]]) -> str:
    """按 主品种×性质 聚合门卫主线交易表现（笔数/胜率/盈亏），供复盘回问"这条筋兑现没"。"""
    buckets: Dict[tuple, Dict[str, Any]] = {}
    for t in trades or []:
        main = str(t.get("gate_main_asset") or "").strip() or "未标注"
        kind = str(t.get("gate_kind") or "").strip() or "未标注"
        key = (main, kind)
        b = buckets.setdefault(key, {"n": 0, "wins": 0, "losses": 0, "pnl": 0.0})
        b["n"] += 1
        verdict = str(t.get("is_correct") or "").upper()
        if verdict == "WIN":
            b["wins"] += 1
        elif verdict == "LOSS":
            b["losses"] += 1
        pnl = _num(t.get("forward_pnl"))
        if pnl is not None:
            b["pnl"] += pnl
    if not buckets:
        return ""
    lines = ["门卫主线 vs 实际结果（按 主品种×性质）："]
    for (main, kind), b in sorted(buckets.items(), key=lambda kv: -kv[1]["n"]):
        decided = b["wins"] + b["losses"]
        wr = f"{b['wins']/decided*100:.0f}%" if decided else "N/A"
        lines.append(f"  {main}×{kind}: {b['n']}笔 胜{wr} 盈亏{b['pnl']:+.2f}%")
    return "\n".join(lines)


def _trade_fact(t: Dict[str, Any]) -> str:
    """结果后的事实：入场→出场走向 + 过程质量（MFE/MAE），用作「方向 vs 时机/止盈」的客观反证。

    MFE/MAE 优先用真实持仓的 mfe_pct/mae_pct；缺失时从快速评测的期间
    最高/最低价（max_price/min_price）+ 方向换算，保证快速评测也能反证方向。
    """
    parts = []
    entry = _num(t.get("entry_price"))
    exitp = _num(t.get("exit_price"))
    if entry and exitp:
        parts.append(f"入场{entry:g}→出场{exitp:g}")
    mfe = _num(t.get("mfe_pct"))
    mae = _num(t.get("mae_pct"))
    if mfe is None and mae is None and entry:
        mx = _num(t.get("max_price"))
        mn = _num(t.get("min_price"))
        if mx is not None and mn is not None:
            action = str(t.get("action") or "").upper()
            # 多头：最高价=最大浮盈、最低价=最大浮亏；空头相反
            if action == "SELL":
                mfe = (entry - mn) / entry * 100
                mae = (mx - entry) / entry * 100
            else:
                mfe = (mx - entry) / entry * 100
                mae = (entry - mn) / entry * 100
    if mfe is not None or mae is not None:
        mfe_s = f"{mfe:+.2f}%" if mfe is not None else "-"
        mae_s = f"{mae:+.2f}%" if mae is not None else "-"
        parts.append(f"MFE{mfe_s}/MAE{mae_s}")
    return " ".join(parts)


def _stage_text(text: Optional[str], stage: str, max_len: int = 160) -> str:
    """从八段式 reasoning_path 文本中抽 [段名] 内容（段名到下一个 [ 或结尾）。"""
    if not text:
        return ""
    s = str(text)
    m = re.search(r"\[" + re.escape(stage) + r"\]([^\[]*)", s)
    if not m:
        return ""
    return " ".join(m.group(1).split()).strip("：: ")[:max_len]


def counterfactual_evidence(t: Dict[str, Any]) -> str:
    """推演时反共识/因果链段写了什么（供归因判断「机理错 vs 漏了对冲因果」）。"""
    path = str(t.get("reasoning") or t.get("reasoning_path") or "")
    anti = _stage_text(path, "反共识结论", 120)
    causal = _stage_text(path, "因果链分析", 120)
    parts = []
    if anti:
        parts.append(f"反共识:{anti}")
    if causal:
        parts.append(f"因果链:{causal}")
    return " | ".join(parts)


def _diagnose_failing_sources() -> List[Dict[str, Any]]:
    """扫 source_status 表，挑出当前失败的 feed（consecutive_failures>0 或 status='error'）。

    返回 [{source_id, category, status, consecutive_failures, last_error}]，按失败次数降序。
    用于把"decision_context 空"映射到具体的 sublayer（不再硬编码 mt5）。
    """
    try:
        conn = db.get_connection()
        conn.row_factory = sqlite3.Row  # 让 dict(r) 拿到列名映射,避免tuple→dict失败
    except Exception:
        return []
    try:
        rows = conn.execute(
            "SELECT source_id, category, status, consecutive_failures, last_error "
            "FROM source_status "
            "WHERE status IN ('error','stale') OR consecutive_failures > 0 "
            "ORDER BY consecutive_failures DESC, last_fail_ts DESC LIMIT 20"
        ).fetchall()
    except Exception:
        return []
    finally:
        try:
            conn.close()
        except Exception:
            pass
    return [dict(r) for r in rows]


# 失败源 → code1 sublayer 映射表（基于 _SUBLAYER_TWO_LEVEL 受控枚举）
# 失败源 source_id 前缀命中即映射，保证 sublayer 在枚举里。
_SOURCE_TO_SUBLAYER: List[Tuple[str, str, str]] = [
    # (source_id 前缀匹配, sublayer l1:l2, 失败提示文本)
    ("mt5_", "context_unavailable:snapshot_missing_mt5", "MT5 行情快照缺失"),
    ("binance_", "context_unavailable:snapshot_missing_binance", "Binance 行情快照缺失"),
    ("fred_", "macro_unavailable:us_macro_fred_missing", "FRED 宏观数据不可用"),
    ("bls_", "macro_unavailable:us_macro_bls_missing", "BLS 劳工数据不可用"),
    ("nfp_", "macro_unavailable:us_macro_nfp_missing", "美 NFP 不可用"),
    ("cpi_", "macro_unavailable:us_macro_cpi_missing", "美 CPI 不可用"),
    ("fomc_", "macro_unavailable:us_macro_fomc_missing", "美 FOMC 不可用"),
    ("ppi_", "macro_unavailable:us_macro_ppi_missing", "美 PPI 不可用"),
    ("dxy_", "related_unavailable:dxy_missing", "DXY 美元指数不可用"),
    ("vix_", "related_unavailable:vix_missing", "VIX 不可用"),
    ("ust_", "related_unavailable:ust_yield_missing", "UST 收益率不可用"),
    ("wti_", "related_unavailable:oil_wti_missing", "WTI 原油不可用"),
    ("xau_", "related_unavailable:gold_missing", "黄金不可用"),
    ("spx_", "related_unavailable:spx_missing", "SPX 不可用"),
    ("hsi_", "related_unavailable:hsi_missing", "HSI 不可用"),
]


def _map_source_to_sublayer(source_id: str) -> Optional[Tuple[str, str]]:
    """source_id → (l1:l2 sublayer, 中文说明)，无匹配返回 None。"""
    sid = (source_id or "").lower()
    for prefix, sub, _ in _SOURCE_TO_SUBLAYER:
        if sid.startswith(prefix):
            return sub, next((t for p, _, t in _SOURCE_TO_SUBLAYER if p == prefix), sub)
    return None


def _rule_based_failure(t: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """确定性优先：先用硬条件判失败原因，命中则跳过 LLM（避免模型对可确定原因乱猜）。

    只保留可客观判定的硬条件（确定性），其余交给 LLM 结合全量信息归因：
    1) decision_context 为空/「{}」= 行情快照完全不可用（数据缺失，code 1）。
       v222：sublayer 不再硬编码 mt5，而是查 source_status 表匹配正在失败的 feed
       （如 FRED 连续失败 6 次 → macro_unavailable:fred_*_missing）。
    2) 无主品种却输出方向单（asset 为空/NONE）= 模型未识别标的（推理降级，code 17）。
    3) reasoning/reasoning_path 含「关键词推断」= 模型未完成完整分析（推理降级，code 17）。
    4) 事后统计/评估性负面损失消息 + 黄金 BUY + 实亏 = 题材误判（code 7）。
       例：「据统计，今年俄罗斯空袭对乌克兰基础设施造成的破坏累计已达100亿美元」+ 黄金 BUY + 亏，
       不允许再被 LLM 机械归为入场时机（code 16），因为方向/题材本身就是误判
       （统计/评估消息≠即时避险催化）。仅当方向是 BUY、品种是典型避险（黄金类）、
       新闻是 _is_stats_report_news 命中（且非官员表态/非即时催化）时才命中。
    """
    ctx = str(t.get("decision_context") or "").strip()
    if not ctx or ctx in ("", "{}"):
        # v222：从 source_status 反查具体失败源，sublayer 跟着真实失败走
        failing = _diagnose_failing_sources()
        failing_ids = [f.get("source_id", "") for f in failing]
        sublayer = "context_unavailable:snapshot_missing_mt5"
        reason_text = "决策时行情快照缺失"
        evidence_detail = "决策快照不可用"
        if failing:
            # 按 _SOURCE_TO_SUBLAYER 顺序找第一个匹配的（macro > related > mt5）
            for src_id in failing_ids:
                mapped = _map_source_to_sublayer(src_id)
                if mapped:
                    sublayer, reason_text = mapped
                    break
            failing_summary = "、".join(
                f"{f['source_id']}({f['consecutive_failures']}连失)" for f in failing[:5]
            )
            evidence_detail = f"决策快照空；当前失败源: {failing_summary}"
        return {
            "reason_code": 1,
            "layer": "data",
            "sublayer": sublayer,
            "reason_text": reason_text,
            "signal": f"decision_context 空/{{}} + {len(failing)} 个源失败",
            "evidence": evidence_detail,
            "failing_sources": failing_ids[:10],  # 留作 UI 展示
            "confidence": 0.95,
        }
    action = str(t.get("action") or "").upper()
    asset = str(t.get("asset") or "").upper()
    # —— 模型推理降级（code 17）确定性优先 ——
    # 1) 无主品种却输出方向单：模型未识别标的（旧版「NONE+方向单」漏洞脏数据）
    if (not asset or asset == "NONE") and action in ("BUY", "SELL"):
        return {
            "reason_code": 17,
            "layer": "execution",
            "sublayer": "unrecognized:unrecognized_asset_gpt4",
            "reason_text": "模型未识别主品种却输出方向单（推理降级脏单）",
            "signal": "asset 为空/NONE 却发布 BUY/SELL",
            "evidence": "target_asset 缺失",
            "confidence": 0.95,
        }
    # 2) reasoning 降级为关键词推断（判断质量差）
    if action in ("BUY", "SELL") and _is_keyword_inference(t):
        return {
            "reason_code": 17,
            "layer": "execution",
            "sublayer": "fallback:gpt4_keyword_fallback",
            "reason_text": "模型输出降级为关键词推断，判断质量差",
            "signal": "reasoning/reasoning_path 含「关键词推断」",
            "evidence": "未完成八段完整分析",
            "confidence": 0.95,
        }
    # —— 题材误判/信号质量差（code 7）确定性判定 ——
    # 事后统计/评估性负面损失消息（_is_stats_report_news 命中）+ 黄金 BUY + 实亏 →
    # 确定性归类为「题材误判」，避免 LLM 机械归为入场时机（code 16）。
    # 仅黄金类（XAU/XAUUSD/XAUUSDT/GC/GOLD）触发，其他品种（如 BTC/WTI）即使
    # 命中统计性消息也交给 LLM 综合判断，因为其消息来源属性更复杂（可能即时催化）。
    _SAFE_HAVEN_FOR_RULE7 = {"XAU", "XAUUSD", "XAUUSDT", "GC", "GOLD"}
    if (action == "BUY"
            and asset in _SAFE_HAVEN_FOR_RULE7
            and _is_loss_trade(t)
            and _is_stats_report_news(str(t.get("news_text") or t.get("content") or ""))):
        return {
            "reason_code": 7,
            "layer": "signal",
            "sublayer": "catalyst_type:posthoc_eval_us_retail",
            "reason_text": "事后统计/评估性负面损失消息被误判为即时避险催化（题材误判）",
            "signal": "避险资产 BUY + 事后统计消息 + 实亏",
            "evidence": "新闻为事后汇总非即时催化",
            "confidence": 0.85,
        }
    return None


def _session_to_sublayer(asset: str, sess: Dict[str, str]) -> str:
    """根据 _detect_session 的判定 + asset 选 code 2 的合法 l1:l2 sublayer。

    周末按 asset 选对应市场休市 l2；深夜低流动性按 asset 类型选 low_liquidity 子项。

    v213 (海力士坑修复):
      · 优先尝试接入 market_hours 模块（统一时区/交易日历/午休时段），
        避免「跨周末/午休」类错误归因。
      · SKHYNIX 单独走 kr_market_weekend / kr_lunch_break，不再误归 us_market。
    """
    detail = sess.get("detail", "")
    a = (asset or "").upper()
    _CRYPTO = {"BTC", "ETH", "SOL", "BNB", "XRP", "DOGE", "ADA", "LTC", "DOT", "LINK", "AVAX", "MATIC"}
    # v213: 韩股相关标的（SKHYNIX 在 KOSPI 上市，000660 是其 ticker）
    _KR_EQUITY = {"SKHYNIX", "000660"}
    is_weekend = "周末" in detail
    if is_weekend:
        if a in _CRYPTO:
            return "low_liquidity:crypto_late_weekend"
        # v213: 韩股先于陆港股分支前
        if a in _KR_EQUITY:
            return "market_closed:kr_market_weekend"
        if a.endswith(".HK") or any(a.startswith(x) for x in ("0", "1", "2", "3", "4", "5", "6", "7", "8", "9")) and len(a) <= 5:
            return "market_closed:hk_market_weekend"
        if any(a.startswith(x) for x in ("SH", "SZ", "BJ")) or any(a.startswith(x) for x in ("6", "0", "3")):
            return "market_closed:cn_market_weekend"
        if a.endswith(".T") or a.endswith(".JP"):
            return "market_closed:jp_market_weekend"
        if a in {"DAX", "ESTX50", "SX5E", "EURUSD", "EURGBP"}:
            return "market_closed:eu_market_weekend"
        if a in {"FTSE", "GBPUSD", "GBPEUR", "UKX"}:
            return "market_closed:uk_market_weekend"
        return "market_closed:us_market_weekend"
    # 深夜低流动性（v213: 韩股午休分支）
    if a in _CRYPTO:
        return "low_liquidity:asia_pre_us"
    if a in _KR_EQUITY:
        # 优先看是否是韩股午休（11:30-12:30 KST），否则按"亚早盘"兜底
        # market_hours 在该时段会返回 sub_status=lunch_break
        try:
            import market_hours as _mh
            st_now = _mh.status_for("SKHYNIX")
            if st_now and st_now.get("sub_status") == "lunch_break":
                return "low_liquidity:kr_lunch_break"
        except Exception:
            pass
        return "low_liquidity:asia_pre_us"
    if a.endswith(".HK") or any(a.startswith(x) for x in ("0", "1", "2", "3", "4", "5", "6", "7", "8", "9")) and len(a) <= 5:
        return "low_liquidity:asia_pre_us"
    if a.endswith(".T") or a.endswith(".JP"):
        return "low_liquidity:asia_pre_us"
    if a in {"EURUSD", "EURGBP", "EURJPY", "DAX", "ESTX50"}:
        return "low_liquidity:eu_lunch"
    if a in {"GBPUSD", "GBPEUR", "GBPJPY", "FTSE", "UKX"}:
        return "low_liquidity:eu_lunch"
    if a in {"USDJPY", "AUDUSD", "NZDUSD", "USDCAD"}:
        return "low_liquidity:fx_asia_session"
    return "low_liquidity:asia_pre_us"


def _rule_based_hold(t: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """HOLD 样本的确定性归因优先（复用 2/8/22/12，中间态样本不设专属码）。

    HOLD = 市场未突破阈值（both_idle）或慢反应（slow_reaction），语义是「新闻未驱动行情」。
    能客观判定的只有 code 2（非活跃时段入场）；code 12（旧闻）需要 published_at vs
    entry_time 的旧闻证据，缺失时交 LLM（限 8/22/12 三选一）。

    v181.1: sublayer 走受控枚举（l1:l2），由 _session_to_sublayer 根据 asset+sess 动态选。
    """
    entry_time = str(t.get("entry_time") or t.get("entry_ts") or "")
    asset = str(t.get("asset") or "")
    sess = _detect_session(asset, entry_time)
    if sess:
        sub = _session_to_sublayer(asset, sess)
        return {
            "reason_code": 2,
            "layer": "timing",
            "sublayer": sub,
            "reason_text": f"HOLD：{sess['detail']}，市场本无活跃资金驱动",
            "signal": sess["detail"],
            "evidence": f"entry_time={entry_time}",
            "confidence": 0.85,
        }
    return None


def _clamp_conf(v: Any) -> float:
    """把置信度安全收敛到 0-1，非法值回退 0.5。"""
    try:
        return max(0.0, min(1.0, float(v)))
    except (TypeError, ValueError):
        return 0.5


def normalize_reasons(item: Dict[str, Any],
                      names: Optional[Dict[int, str]] = None,
                      with_observability: bool = False,
                      with_layer: bool = True) -> Dict[str, Any]:
    """把规则/LLM 归因条目统一成多原因结构。

    兼容两种输入：
    1) 多原因新格式：{"decision_id":..., "reasons":[{code,text,confidence,is_primary},...]}
    2) 旧单码格式：{"decision_id":..., "reason_code", "reason_text", "confidence"}

    输出统一含主因字段（reason_code/reason_name/reason_text/confidence）与完整 reasons 数组。
    主因选取：优先标 is_primary 的；否则取第一个并标记为 is_primary。

    with_layer：失败 23 类 / HOLD 归因才填 layer/sublayer；成功 10 类归因 code
    语义与失败不同，不填 layer（否则成功 code 1-10 会被误套失败分层映射）。

    sublayer 校验：走 parse_sublayer(code, raw) 受控枚举回退（v181.1）。
    """
    reasons: List[Dict[str, Any]] = []
    raw = item.get("reasons")
    if isinstance(raw, list):
        for r in raw:
            if not isinstance(r, dict):
                continue
            try:
                code = int(r.get("code", r.get("reason_code")))
            except (TypeError, ValueError):
                code = 6
            sub_raw = str(r.get("sublayer") or "")
            l1, l2 = parse_sublayer(code, sub_raw) if with_layer else ("", "")
            sublayer = format_sublayer(code, l1, l2) if with_layer else ""
            reasons.append({
                "code": code,
                "layer": (str(r.get("layer") or "") or reason_layer(code)) if with_layer else "",
                "sublayer": sublayer,
                "text": str(r.get("text") or r.get("reason_text") or "")[:60],
                "signal": str(r.get("signal") or ""),
                "evidence": str(r.get("evidence") or ""),
                "confidence": _clamp_conf(r.get("confidence")),
                "is_primary": bool(r.get("is_primary")),
            })
    if not reasons:
        try:
            code = int(item.get("reason_code"))
        except (TypeError, ValueError):
            code = 6
        sub_raw = str(item.get("sublayer") or "")
        l1, l2 = parse_sublayer(code, sub_raw) if with_layer else ("", "")
        sublayer = format_sublayer(code, l1, l2) if with_layer else ""
        reasons = [{
            "code": code,
            "layer": reason_layer(code) if with_layer else "",
            "sublayer": sublayer,
            "text": str(item.get("reason_text") or "")[:60],
            "signal": "",
            "evidence": "",
            "confidence": _clamp_conf(item.get("confidence")),
            "is_primary": True,
        }]
    primary = next((r for r in reasons if r.get("is_primary")), reasons[0])
    primary["is_primary"] = True
    out: Dict[str, Any] = {
        "decision_id": item.get("decision_id"),
        "reason_code": primary["code"],
        "reason_name": (names or {}).get(primary["code"], "其他"),
        "reason_text": primary["text"],
        "confidence": primary["confidence"],
        "layer": (primary.get("layer") or reason_layer(primary["code"])) if with_layer else "",
        "sublayer": primary.get("sublayer") or "",
        "reasons": reasons,
    }
    if with_observability:
        out["observability"] = reason_observability(primary["code"])
    return out


def _validate_reason_code(v: Any, code_min: int = 1, code_max: int = 23) -> Optional[int]:
    """把任意值安全收敛到合法失败原因 code（1-23），非法值返回 None。

    为什么必须：LLM 偶尔会输出 _REASON_NAMES 范围外的 code（如 24、0、-1），
    若透传会导致归因榜上出现「未知(N)」占榜首。失败原因定义见 _FAILURE_FRAMEWORK。
    """
    try:
        n = int(v)
    except (TypeError, ValueError):
        return None
    if code_min <= n <= code_max:
        return n
    return None


def _attribution_codes(corrected_code: Optional[int], reason_code: Optional[int],
                       reasons_json: str, *, code_min: int = 1, code_max: int = 23) -> List[int]:
    """展开一条归因记录应计入统计的原因码（多标签计数）。

    人工纠错拍板单码优先；否则展开 AI 多标签 reasons_json；无 reasons_json 回退主因。
    范围校验：超出 [code_min, code_max] 的 code 视为无效丢弃，避免污染统计榜。
    """
    if corrected_code is not None:
        v = _validate_reason_code(corrected_code, code_min, code_max)
        if v is not None:
            return [v]
    if reasons_json:
        try:
            arr = json.loads(reasons_json)
            if isinstance(arr, list):
                codes: List[int] = []
                for it in arr:
                    if isinstance(it, dict):
                        v = _validate_reason_code(
                            it.get("code", it.get("reason_code")), code_min, code_max
                        )
                        if v is not None:
                            codes.append(v)
                if codes:
                    return codes
        except (json.JSONDecodeError, TypeError):
            pass
    if reason_code is not None:
        v = _validate_reason_code(reason_code, code_min, code_max)
        if v is not None:
            return [v]
    return []


def _primary_code(corrected_code: Optional[int], reason_code: Optional[int],
                  reasons_json: str, *, code_min: int = 1, code_max: int = 23) -> Optional[int]:
    """取一条归因记录的【主因 code】（用于单选统计），保证 pct 加和 ≤ 100%。

    优先级：人工纠错 corrected_code > AI 多原因主因 (is_primary=true) >
    AI 多原因第一项 > reason_code。范围校验同 _validate_reason_code。
    返回 None 表示该记录「未归因」（主因提取失败，会被 stats 单列 code=0）。
    """
    if corrected_code is not None:
        v = _validate_reason_code(corrected_code, code_min, code_max)
        if v is not None:
            return v
    if reasons_json:
        try:
            arr = json.loads(reasons_json)
            if isinstance(arr, list):
                # 1) 先找 is_primary=true
                for it in arr:
                    if isinstance(it, dict) and it.get("is_primary"):
                        v = _validate_reason_code(
                            it.get("code", it.get("reason_code")), code_min, code_max
                        )
                        if v is not None:
                            return v
                # 2) 否则取第一项
                for it in arr:
                    if isinstance(it, dict):
                        v = _validate_reason_code(
                            it.get("code", it.get("reason_code")), code_min, code_max
                        )
                        if v is not None:
                            return v
        except (json.JSONDecodeError, TypeError):
            pass
    if reason_code is not None:
        return _validate_reason_code(reason_code, code_min, code_max)
    return None


def _analyze_failures_llm(failures: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """一批失败单按 23 类框架归类，返回 [{decision_id, reason_code, reason_text, confidence}]。

    确定性优先：先跑 _rule_based_failure 命中硬条件（如数据缺失）直接产出，
    剩余才送 LLM，避免模型对可确定原因乱猜导致归因失真。
    """
    if not failures:
        return []
    out: List[Dict[str, Any]] = []
    llm_needed: List[Dict[str, Any]] = []
    for t in failures:
        hard = _rule_based_failure(t)
        if hard is not None:
            out.append({
                "decision_id": t.get("id"),
                "reason_code": hard["reason_code"],
                "reason_text": hard["reason_text"],
                "confidence": hard["confidence"],
                "observability": reason_observability(hard["reason_code"]),
                "layer": hard.get("layer") or reason_layer(int(hard["reason_code"])),
                "sublayer": hard.get("sublayer") or "",
                "failing_sources": hard.get("failing_sources") or [],  # v222：失败源清单
                "reasons": [{
                    "code": int(hard["reason_code"]),
                    "layer": hard.get("layer") or reason_layer(int(hard["reason_code"])),
                    "sublayer": hard.get("sublayer") or "",
                    "text": hard["reason_text"],
                    "signal": hard.get("signal") or "",
                    "evidence": hard.get("evidence") or "",
                    "confidence": float(hard["confidence"]),
                    "is_primary": True,
                }],
            })
        else:
            llm_needed.append(t)
    if not llm_needed:
        return out
    lines = []
    for t in llm_needed[:20]:  # 每批最多 20 条
        asset = str(t.get("asset") or "")
        ctx_s = _ctx_evidence(t.get("decision_context"), asset)
        entry = t.get("entry_time") or ""
        weekday = ""
        try:
            dt = datetime.fromisoformat(str(entry).replace("Z", "+00:00"))
            weekday = f" 星期{['一','二','三','四','五','六','日'][dt.weekday()]} {dt.hour:02d}:{dt.minute:02d}"
        except Exception:
            pass
        fact_s = _trade_fact(t)
        news = re.sub(r"\[hash:[a-fA-F0-9]+\]\s*", "", t.get("news_text") or "")[:100]
        lines.append(
            f"#{t['id']} | {asset} {t['action']} | 判定{t['is_correct']} | "
            f"盈亏{t['forward_pnl']:+.2f}% | 入场{entry}{weekday} | 预期周期{t.get('expected_horizon')} | "
            f"快照:{ctx_s} | 结果:{fact_s} | 新闻: {news} | {_gate_short(t)}"
        )
    prompt = (
        _FAILURE_FRAMEWORK
        + "\n判定失败原因时用事实反证，先分「方向/题材错」还是「时机/止盈错」：\n"
        "① 看「结果」：交易后从未明显浮盈（MFE≈0 或入场后价格直接反向）→ 方向/题材一开始就错 → 归 6 或 7；"
        "曾明显浮盈后回吐（MFE 明显为正而最终亏）→ 方向对、时机/入场点/止盈问题 → 归 14/15/16。\n"
        "② 看「快照」：决策时技术面/趋势数据是否本身与该方向矛盾（矛盾 → 方向错）。\n"
        "③ 事后统计/评估/非即时催化却被当成看涨/看跌催化 → 归 6 或 7。不要因「入场时间距新闻近」就机械归 16。\n"
        "④ 新闻主体品种与交易品种错配（新闻只涉及美股/个股/科技股，却交易 BTC/ETH 等加密或商品）→ 影响力不足/题材误判 → 归 7，绝不归 16。\n"
        "⑤ 门卫结论仅作文本线索：若门卫曾判「可交易」且强度高、或门卫未挖到核心影响而结果实亏，结合快照/结果综合判断是否信号质量差 → 归 7 或 13，不要机械套用。\n"
        "对以下每条失败交易，给出【所有可能的原因】（无上限，一个 is_primary=true 为主因，其余为次要原因）；\n"
        "宁可多列不可漏掉——例如「数据缺失」与「新闻已定价」可以并存，「题材误判」与「联动忽略」可以并存；\n"
        "原因之间用「+」连接（如：1 数据缺失 + 22 新闻已定价 + 13 联动忽略），确保失败根因不互相吞没。\n"
        "输出 JSON 数组：\n"
        "[{\"decision_id\": <id>, \"reasons\": [{\"code\": <1-26>, \"sublayer\": \"<l1 或 l1:l2，从下面枚举选>\", \"text\": \"<=40字\", \"confidence\": 0-1, \"is_primary\": bool}, ...]}, ...]\n"
        "每条必须有 decision_id 和至少一个原因。sublayer 必须从下方枚举中选，格式 l1（如 \"feed_unavailable\"）或 l1:l2（如 \"feed_unavailable:mt5_disconnect\"）。\n\n"
        + _build_sublayer_prompt_block()
        + "\n"
        + "\n".join(lines)
    )
    result = _parse_json(_llm(prompt, "你是量化交易失败归因分析师，只输出 JSON 数组。", max_tokens=4000))
    if isinstance(result, dict):
        result = [result]
    if not isinstance(result, list):
        return out
    for item in result:
        if isinstance(item, dict) and "decision_id" in item:
            item.setdefault("reason_code", 6)
            item.setdefault("reason_text", "")
            out.append(normalize_reasons(item, names=_REASON_NAMES, with_observability=True))
    return out


def _analyze_successes_llm(successes: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """一批成功单按成功原因框架归类，返回 [{decision_id, reason_code, reason_text, confidence}]。"""
    if not successes:
        return []
    lines = []
    for t in successes[:20]:
        ctx = t.get("decision_context") or "{}"
        try:
            ctx_obj = json.loads(ctx)
            ctx_s = f"决策时行情状态: {ctx_obj.get('status')}" if isinstance(ctx_obj, dict) else ""
        except (json.JSONDecodeError, TypeError):
            ctx_s = ""
        entry = t.get("entry_time") or ""
        weekday = ""
        try:
            dt = datetime.fromisoformat(str(entry).replace("Z", "+00:00"))
            weekday = f" 星期{['一','二','三','四','五','六','日'][dt.weekday()]} {dt.hour:02d}:{dt.minute:02d}"
        except Exception:
            pass
        news = re.sub(r"\[hash:[a-fA-F0-9]+\]\s*", "", t.get("news_text") or "")[:100]
        lines.append(
            f"#{t['id']} | {t['asset']} {t['action']} | 判定{t['is_correct']} | "
            f"盈亏{t['forward_pnl']:+.2f}% | 入场{entry}{weekday} | {ctx_s} | 预期周期{t.get('expected_horizon')} | 新闻: {news} | {_gate_short(t)}"
        )
    prompt = (
        _SUCCESS_FRAMEWORK +
        "\n对以下每条成功交易，判断主要原因（可加次要原因），输出 JSON 数组：\n"
        "[{\"decision_id\": <id>, \"reason_code\": <1-10>, \"reason_text\": \"<=40字\", \"confidence\": 0-1}, ...]\n"
        "每条必须有 decision_id 和 reason_code。只输出 JSON 数组。\n\n" + "\n".join(lines)
    )
    result = _parse_json(_llm(prompt, "你是量化交易成功归因分析师，只输出 JSON 数组。", max_tokens=1600))
    if isinstance(result, dict):
        result = [result]
    if not isinstance(result, list):
        return []
    out = []
    for item in result:
        if isinstance(item, dict) and "decision_id" in item:
            item.setdefault("reason_code", 6)
            item.setdefault("reason_text", "")
            out.append(item)
    return out


def _generate_insights_llm(summary: Optional[Dict[str, Any]], failure_stats: Dict[str, Any],
                           gate_summary: str = "") -> List[str]:
    """从复盘总结 + 失败原因统计生成可执行经验规则（回灌用）。"""
    gate_block = f"\n门卫主线表现:\n{gate_summary}" if gate_summary else ""
    # v191 #2: 注入 _KNOWN_PARAM_KEYS 列表，要求输出形如 "[layer.sub.signal_name] 规则文本"
    # weekly_insight_apply 的 _extract_param_key 已在 step 1 命中 xxx.yyy.zzz 直接匹配。
    prompt = (
        "基于以下交易复盘结论，提炼 3-8 条可执行的交易经验规则，"
        "每条 <=60 字、具体可操作（如'XAU 在美债收益率快速上行后 4h 内信号胜率低，应降低置信度'）。\n"
        "v191 新规范：每条规则必须用 [layer.sub_layer.signal_name] 开头标注对应的可调参数，"
        "标注后写规则正文。可选的 param_key 列表（按常用度排序）：\n"
        "  [sizing.position_weight]      仓位权重调整\n"
        "  [sizing.stop_loss_pct]      止损幅度\n"
        "  [sizing.take_profit_pct]    止盈幅度\n"
        "  [sizing.max_position_pct]   最大仓位\n"
        "  [score.confidence_threshold]     置信度门槛\n"
        "  [score.event_strength_threshold] 事件强度门槛\n"
        "  [score.buy_min_score] / [score.sell_min_score] 买卖信号分门槛\n"
        "  [filter.asset_match.main_asset_missed]      主资产漏报过滤\n"
        "  [filter.contradiction_check.sentiment_vs_market] 矛盾信号过滤\n"
        "  [filter.news_quality.partial_skipped]       新闻质量过滤\n"
        "  [analysis.knowledge_inject.exp_misinterpreted] 经验误注\n"
        "示例输出：\"[sizing.position_weight] 加密主流仓位权重应低于贵金属\"、"
        "\"[score.confidence_threshold] XAU 低分 BUY 评测胜率低，应降置信门槛\"。\n"
        "无法对应任何 param_key 的元规则（如同向叙述「样本<10 笔时仅作参考」）允许不带 [] 前缀，"
        "系统会存为软启发/纯文档类。\n"
        "v196 新增（可选）：如果规则明确只适用某品种，可在 param_key 之前加 [XAU]/[BTC]/[WTI]/[ETH]/[SOL]/[*] 资产前缀，"
        "系统会自动剥离并填入 asset 字段，供 ai_worker 决策时按品种匹配注入。"
        "asset=通则（不限定品种）→ 用 [*] 或省略前缀。"
        "示例：[XAU] [score.confidence_threshold] 黄金避险情绪时 BUY 信号置信度门槛上调。\n"
        "输出 JSON 数组字符串：[\"规则1\", \"规则2\", ...]。只输出 JSON 数组。\n\n"
        f"复盘总结: {json.dumps(summary or {}, ensure_ascii=False)[:800]}\n"
        f"失败原因占比: {json.dumps(failure_stats, ensure_ascii=False)[:800]}"
        f"{gate_block}"
    )
    result = _parse_json(_llm(prompt, "你是量化交易策略顾问，只输出 JSON 数组。", max_tokens=1200))
    if isinstance(result, list):
        return [str(item).strip() for item in result if str(item).strip()]
    return []


def _extract_causal_chains_llm(summary: Optional[Dict[str, Any]], failure_stats: Dict[str, Any],
                               success_stats: Dict[str, Any], insights: List[str],
                               gate_summary: str = "") -> List[Dict[str, Any]]:
    """从复盘结果提炼因果链（自动补充到因果链库，AI 分析时命中触发词即注入）。

    输入：复盘总结 + 成败归因 + 已生成经验规则 → 输出结构化因果链：
      [{"name", "cause", "effect", "trigger_words", "note"}]
    cause=宏观/因果起点（如"美元加息"）；effect=传导结果（如"黄金承压/加密被抛"）；
    trigger_words=后续新闻命中即注入的关键词（逗号分隔）。
    只输出 JSON 数组；无有价值因果 → 空数组（不强造）。
    """
    gate_block = f"\n门卫主线表现（按 主品种×性质 聚合）:\n{gate_summary}" if gate_summary else ""
    prompt = (
        "基于以下量化交易复盘，提炼 1-4 条【有实证价值的宏观/市场因果链】。"
        "因果链必须：因(cause)是明确的宏观/事件起点（如美元加息、OPEC 减产、地缘冲突、"
        "非农超预期、央行扩表），果(effect)是它对黄金/原油/加密/美债/股指等资产的可验证"
        "传导方向，且复盘中有盈亏数据支持（胜率/归因），不是凭空理论。"
        "严禁输出信号级统计规律（如『XAU 低分 SELL 胜率低』『BTC 高分 BUY 胜率高』"
        "『某模型某品种表现差』这类 score/置信度/模型维度规律）——那是经验规则，"
        "不属于因果链库；因果链库只收【宏观事件 → 跨资产传导】类。"
        "输出 JSON 数组：[{\"name\":\"简短名\",\"cause\":\"因\",\"effect\":\"果\","
        "\"trigger_words\":\"触发词,逗号分隔\",\"note\":\"数据支撑或边界说明\"}]。"
        "note 里应注明样本数与主要资产方向（如『样本 12 笔，BTC×利率主线胜率 58%』）。"
        "只输出 JSON 数组；没有实证因果 → 输出 []。\n\n"
        f"复盘总结: {json.dumps(summary or {}, ensure_ascii=False)[:900]}\n"
        f"失败原因: {json.dumps(failure_stats, ensure_ascii=False)[:700]}\n"
        f"成功原因: {json.dumps(success_stats, ensure_ascii=False)[:700]}\n"
        f"经验规则: {json.dumps(insights, ensure_ascii=False)[:900]}"
        f"{gate_block}"
    )
    result = _parse_json(_llm(
        prompt, "你是量化策略顾问，只输出 JSON 数组，因果链需实证支撑。", max_tokens=1200,
    ))
    out: List[Dict[str, Any]] = []
    if isinstance(result, list):
        for item in result:
            if not isinstance(item, dict):
                continue
            cause = str(item.get("cause") or "").strip()
            effect = str(item.get("effect") or "").strip()
            if cause and effect:
                out.append({
                    "name": str(item.get("name") or "").strip()[:60],
                    "cause": cause[:500],
                    "effect": effect[:500],
                    "trigger_words": str(item.get("trigger_words") or "").strip()[:300],
                    "note": str(item.get("note") or "").strip()[:300],
                })
    return out[:4]


def _extract_from_quick_sim_llm(qs_trades: List[Dict[str, Any]], sample_size: int = 40) -> Dict[str, Any]:
    """从快速评测样本（量大、维度全）提炼可沉淀规律。

    快速评测单量远超真实持仓（每个方向性信号都进短周期结算），是挖规律的富矿。
    输入：最近 N 条已结算评测单（含 asset/action/模型/信号分/新闻/盈亏）。
    输出三类（一次 LLM 调用）：
      insights:   [str] 方向性经验规则（如"XAU 低分 BUY 评测胜率低"）→ review_insights
      causal:     [{name,cause,effect,trigger_words,note}] 宏观传导因果 → causal_chains
      prompts:    [str] 提示词/模型层建议（如"某模型 XAU 评测差，考虑调权重"）→ 供展示
    无有价值规律 → 空。只输出 JSON，不强造。
    """
    if not qs_trades:
        return {"insights": [], "causal": [], "prompts": []}
    lines = []
    for t in qs_trades[:sample_size]:
        model = str(t.get("agent_model_id") or t.get("decision_id") or "?")
        score = t.get("score")
        score_s = f"{float(score):+.2f}" if isinstance(score, (int, float)) else "?"
        news = re.sub(r"\[hash:[a-fA-F0-9]+\]\s*", "", t.get("news_text") or "")[:70]
        entry = str(t.get("entry_time") or "")[:16]
        pnl = t.get("forward_pnl")
        pnl_s = f"{float(pnl):+.2f}%" if isinstance(pnl, (int, float)) else "?"
        verdict = t.get("is_correct") or t.get("verdict") or "?"
        lines.append(
            f"#{t.get('id', '?')} | {t.get('asset') or '?'} {t.get('action') or '?'} | "
            f"模型{model[:28]} | score {score_s} | 胜{verdict} pnl={pnl_s} | "
            f"{entry} | {news}"
        )
    # 持仓时长取配置（QUICK_SIM_HORIZON_MINUTES）——写死会让复盘模型读到错误口径。
    # 注意每条样本可能来自不同配置时期，故措辞用"约 N 分钟"而非"固定 N 分钟"。
    _qs_hz = config.QUICK_SIM_HORIZON_MINUTES
    prompt = (
        f"以下是一批【约 {_qs_hz} 分钟快速评测】的已结算结果（每个方向性信号按建仓时"
        f"配置的约 {_qs_hz} 分钟模拟结算，样本比真实持仓多，但只代表该短周期表现）。"
        "请从样本中提炼**有统计说服力**"
        "的规律，输出 JSON（三类都在一个对象里）：\n"
        "{\n"
        "  \"insights\": [\"经验规则<=50字（如：[XAU] XAU 在 score<0.4 的 BUY 快速评测胜率低，应降置信度）\", ...],\n"
        "  \"causal\": [{\"name\":\"简短名\",\"cause\":\"宏观事件/政策起点\",\"effect\":\"对资产的可验证传导\","
        "\"trigger_words\":\"触发词,逗号分隔\",\"note\":\"数据支撑\"}],\n"
        "  \"prompts\": [\"提示词/模型建议<=60字（如：gpt-4o-mini 对黄金 SELL 评测差，"
        "考虑调权重或加约束）\", ...]\n"
        "}\n"
        "要求：①每条规律至少有 3 个同模式样本支撑，样本不足的规律不要写；②insights 收信号级/"
        "统计级规律（score 区间、资产×方向胜率、模型偏好等）；causal 只收【宏观事件/政策 → "
        "跨资产传导】类因果（因=地缘冲突、美联储决议、非农、OPEC 减产、央行扩表等事件起点，"
        "果=对黄金/原油/加密/美债/股指的传导方向），信号级统计规律一律放 insights、严禁进 causal；"
        "③prompts 是针对提示词或模型通道的调整建议；④没有就说 []，绝不编造。"
        "⑤causal 的 note 注明样本量与主要资产方向。\n"
        "⑥v196：insights 若明确限定某品种，可在前面加 [XAU]/[BTC]/[WTI]/[ETH]/[SOL] 资产前缀，"
        "供 ai_worker 决策时按品种匹配注入；通则不写。\n\n"
        + "\n".join(lines)
    )
    result = _parse_json(_llm(
        prompt, "你是量化评测分析师，从快速评测样本提炼有实证的规律，只输出 JSON。",
        max_tokens=1600,
    ))
    if not isinstance(result, dict):
        return {"insights": [], "causal": [], "prompts": []}
    insights = [str(x).strip()[:120] for x in result.get("insights") or [] if str(x).strip()]
    causal_out = []
    for item in result.get("causal") or []:
        if not isinstance(item, dict):
            continue
        cause = str(item.get("cause") or "").strip()
        effect = str(item.get("effect") or "").strip()
        if cause and effect:
            causal_out.append({
                "name": str(item.get("name") or "").strip()[:60],
                "cause": cause[:500],
                "effect": effect[:500],
                "trigger_words": str(item.get("trigger_words") or "").strip()[:300],
                "note": str(item.get("note") or "").strip()[:300],
            })
    prompts = [str(x).strip()[:160] for x in result.get("prompts") or [] if str(x).strip()]
    return {
        "insights": insights[:6],
        "causal": causal_out[:4],
        "prompts": prompts[:4],
    }


# ---------------------------------------------------------------------------
# 主流程 + 存储
# ---------------------------------------------------------------------------

def _review_block(conn: sqlite3.Connection, trades: List[Dict[str, Any]]) -> Dict[str, Any]:
    """对一组已结算交易做：总结 + 成败归因 + 统计（真实持仓 / 快速评测共用）。

    返回 {trades, failures, successes, summary, aggregate, failure_stats,
           success_stats, failure_results, success_results}
    """
    agg = _aggregate(trades)
    failures = [t for t in trades if t["is_correct"] == "LOSS"]
    successes = [t for t in trades if t["is_correct"] == "WIN"]
    # 复盘总结：先尝试 LLM（深度文本），失败时 fallback 到本地规则模板，
    # 保证反思页 overview 字段始终非空，避免出现"31笔交易但没有复盘总结"。
    summary = _review_summary_llm(trades, agg)
    if summary is None or not (summary.get("overview") or "").strip():
        summary = _fallback_summary(trades, agg)
    # v233: 规律发现深入自审（递归"为什么 / 适用 / 是否过拟合 / 引用注意"）。
    # 无论 summary 来自 LLM 还是 fallback，都在这里统一挂上 pattern_audits。
    # 防御：pattern_audits 已存在（fallback 路径）时跳过，避免重复自审。
    if not isinstance(summary, dict):
        summary = {"patterns": []}
    if not summary.get("pattern_audits"):
        summary["pattern_audits"] = _audit_patterns_batch(
            summary.get("patterns") or [], trades, agg
        )
    failure_results: List[Dict[str, Any]] = []
    for i in range(0, len(failures), 20):
        failure_results.extend(_analyze_failures_llm(failures[i:i + 20]))
    success_results: List[Dict[str, Any]] = []
    for i in range(0, len(successes), 20):
        success_results.extend(_analyze_successes_llm(successes[i:i + 20]))
    failure_stats: Dict[str, Any] = {}
    for item in failure_results:
        code = item.get("reason_code")
        name = _REASON_NAMES.get(code, f"其他({code})")
        failure_stats[name] = failure_stats.get(name, 0) + 1
    success_stats: Dict[str, Any] = {}
    for item in success_results:
        code = item.get("reason_code")
        name = _SUCCESS_REASON_NAMES.get(code, f"其他({code})")
        success_stats[name] = success_stats.get(name, 0) + 1
    return {
        "trades": trades,
        "failures": failures,
        "successes": successes,
        "summary": summary,
        "aggregate": agg,
        "failure_results": failure_results,
        "success_results": success_results,
        "failure_stats": failure_stats,
        "success_stats": success_stats,
    }


def run_review(
    conn: sqlite3.Connection,
    scope: str = "week",
    limit: int = 500,
    lo: Optional[int] = None,
    hi: Optional[int] = None,
) -> Dict[str, Any]:
    """执行一次复盘：真实持仓 + 快速评测分开总结/归因，经验与因果链一并沉淀。

    v290 P0+（反思系统时段统一）：新增 lo/hi (UTC 秒) 显式窗口参数。
    窗口非空时按 created_at ∈ [lo, hi] 过滤，与全局 PeriodSelector 同源同窗，
    避免顶部卡片（ReviewOverview）与其他面板时段脱钩。scope 仍保留作为 fallback。
    """
    # ── 真实持仓（按 expected_horizon 结算，权威口径）──
    trades = _fetch_trades(conn, scope, limit, lo=lo, hi=hi)
    real = _review_block(conn, trades)
    # ── 快速评测（短周期模拟结算，独立统计不稀释真实持仓口径）──
    qs_trades = _fetch_quick_sim_trades(conn, scope, limit, lo=lo, hi=hi)
    qs = _review_block(conn, qs_trades)
    summary_main = real["summary"] or qs["summary"]
    # 两块都给了总结时，取真实持仓为主并附注快速评测样本量
    if summary_main is not None:
        summary_main = dict(summary_main)
        summary_main["_quick_sim_samples"] = len(qs_trades)
    # ── 经验规则提炼：历史交易(real) 与 快速评测(qs) 分开标注来源 ──
    real_insights = _generate_insights_llm(
        summary_main, real["failure_stats"], gate_summary=_gate_line_summary(trades),
    )
    # 因果链分开提炼：真实持仓统计 → auto_real；快速评测提炼 → auto_quick_sim
    real_causal = _extract_causal_chains_llm(
        real["summary"] or qs["summary"], real["failure_stats"], real["success_stats"], real_insights,
        gate_summary=_gate_line_summary(trades),
    )
    for ch in real_causal:
        ch["origin"] = "auto_real"
    # ── 快速评测样本级提炼（量大维度全 → 挖统计看不到的规律）──
    qs_mining = _extract_from_quick_sim_llm(qs_trades, sample_size=40)
    qs_insights = qs_mining.get("insights", [])
    qs_causal = qs_mining.get("causal", [])
    for ch in qs_causal:
        ch["origin"] = "auto_quick_sim"
    causal_chains_auto = real_causal + qs_causal
    prompt_suggestions = qs_mining.get("prompts", [])
    # 落库时区分来源：历史交易规则 origin='real'；快速评测规则 origin='quick_sim'
    insights_all = real_insights + qs_insights
    # 补充分析：回灌前后对比 / 模型维度 / 经验评估
    feedback = _feedback_comparison(conn)
    models = _model_stats(conn)
    evaluation = _evaluate_insights(conn)
    report = {
        "scope": scope,
        "generated_at": _now(),
        # v290 P0+：把窗口元数据也带回,前端报告可标注「按全局 PeriodSelector 选定时段」
        "window": {
            "from_ts": lo,
            "to_ts": hi,
            "source": "period_selector" if (lo is not None or hi is not None) else "scope_fallback",
        },
        "samples": len(trades),
        "quick_sim_samples": len(qs_trades),
        "summary": real["summary"] or {},
        "aggregate": real["aggregate"],
        "feedback": feedback,
        "models": models,
        "insight_evaluation": evaluation,
        "failures": {
            "analyzed": len(real["failure_results"]),
            "failed_trades": len(real["failures"]),
            "stats": real["failure_stats"],
            "details": real["failure_results"][:100],
        },
        "successes": {
            "analyzed": len(real["success_results"]),
            "won_trades": len(real["successes"]),
            "stats": real["success_stats"],
            "details": real["success_results"][:100],
        },
        # 快速评测独立块（短周期模拟口径，与真实持仓分开）
        "quick_sim": {
            "samples": len(qs_trades),
            "aggregate": qs["aggregate"],
            "summary": qs["summary"] or {},
            "failures": {
                "analyzed": len(qs["failure_results"]),
                "failed_trades": len(qs["failures"]),
                "stats": qs["failure_stats"],
                "details": qs["failure_results"][:100],
            },
            "successes": {
                "analyzed": len(qs["success_results"]),
                "won_trades": len(qs["successes"]),
                "stats": qs["success_stats"],
                "details": qs["success_results"][:100],
            },
        },
        "insights": insights_all,
        # 每条经验标注来源（real=历史交易复盘 / quick_sim=快速评测提炼）
        "insights_by_origin": {
            "real": real_insights,
            "quick_sim": qs_insights,
        },
        "causal_chains_auto": causal_chains_auto,
        # 快速评测提炼的提示词/模型调整建议（展示用，不自动改）
        "prompt_suggestions": prompt_suggestions,
        # 归因复核闭环：失败归因明细对齐成落库行（API 层在 save_report 后用 report_id 落库）
        "_attribution_rows": build_attribution_rows(real["failures"], real["failure_results"])
        + build_attribution_rows(qs["failures"], qs["failure_results"]),
    }
    return report


def daemon_loop(interval_s: int = 86400, scope: str = "week", limit: int = 500) -> None:
    """v189-fix: 周期循环复盘（run_review + save_report + save_insights + 因果链沉淀）。

    设计：
      - 默认 24h/次（UTC 23:30 跑，~25min 后 launchd 23:55 journal rollup 接力消费）
      - env 覆盖：
          REVIEW_DAEMON_INTERVAL_S (默认 86400)
          REVIEW_DAEMON_SCOPE      (默认 "week")
      - 复用 _run_review_sync 等价逻辑（手动连接 + 自动 commit/rollback）
      - 单次失败不退出，只 print 日志；KeyboardInterrupt 优雅退出
      - 由 api_server lifespan 在独立线程中启动
    """
    import time as _t
    import db as _db
    import causal_chains  # v189-fix: add_chains_dedup 在 daemon 内调用
    import monitor_llm_value as _mlv  # v200 P0+ LLM 增量价值监控
    interval_s = max(300, int(os.environ.get("REVIEW_DAEMON_INTERVAL_S", interval_s)))
    scope = str(os.environ.get("REVIEW_DAEMON_SCOPE", scope))
    if scope not in ("today", "week", "all"):
        scope = "week"
    print(f"[REVIEW-DAEMON] started | interval={interval_s}s scope={scope}", flush=True)
    try:
        while True:
            try:
                conn = _db.get_connection()
                conn.row_factory = sqlite3.Row
                try:
                    t0 = _t.time()
                    report = run_review(conn, scope=scope, limit=limit)
                    rid = save_report(conn, scope, report)
                    save_attribution_details(
                        conn, "review", rid, report.get("_attribution_rows") or []
                    )
                    by_origin = report.get("insights_by_origin") or {}
                    added_real = save_insights(
                        conn, by_origin.get("real") or [],
                        source_samples=int(report.get("samples") or 0), origin="real",
                    )
                    added_qs = save_insights(
                        conn, by_origin.get("quick_sim") or [],
                        source_samples=int(report.get("quick_sim_samples") or 0),
                        origin="quick_sim",
                    )
                    causal_result = causal_chains.add_chains_dedup(
                        report.get("causal_chains_auto") or [], conn
                    )
                    # v200 P0+ LLM 增量价值监控 (24h 周期)
                    try:
                        _mlv_ev = _mlv.run_evaluation_and_persist(conn, window_days=30)
                        _mlv_v = _mlv_ev.get("verdict", "insufficient_data")
                        _mlv_n = (_mlv_ev.get("llm_n", 0), _mlv_ev.get("not_llm_n", 0))
                    except Exception as _mlv_e:
                        _mlv_v = f"error: {type(_mlv_e).__name__}"
                        _mlv_n = (0, 0)
                    dur = _t.time() - t0
                    print(f"[REVIEW-DAEMON] OK report_id={rid} scope={scope} "
                          f"samples={report.get('samples', 0)} qs={report.get('quick_sim_samples', 0)} "
                          f"insights=+{added_real + added_qs} causal=+{causal_result.get('added', 0)} "
                          f"llm_value_verdict={_mlv_v} llm_n={_mlv_n[0]} not_llm_n={_mlv_n[1]} "
                          f"dur={dur:.1f}s", flush=True)
                finally:
                    try:
                        conn.close()
                    except Exception:
                        pass
            except KeyboardInterrupt:
                raise
            except Exception as exc:
                print(f"[REVIEW-DAEMON] err: {type(exc).__name__}: {exc}", flush=True)
            _t.sleep(interval_s)
    except KeyboardInterrupt:
        print("\n[REVIEW-DAEMON] stopped (KeyboardInterrupt)", flush=True)


def save_report(conn: sqlite3.Connection, scope: str, report: Dict[str, Any]) -> int:
    # 把聚合指标、成功归类一并存入 summary（历史报告展示 + 前端历史趋势用）
    summary_payload = dict(report.get("summary") or {})
    summary_payload["_aggregate"] = report.get("aggregate") or {}
    summary_payload["_successes"] = report.get("successes") or {}
    # 持久化来源标注（历史报告页能区分"历史交易提炼 vs 快速评测提炼"）
    summary_payload["_insights_by_origin"] = report.get("insights_by_origin") or {}
    summary_payload["_prompt_suggestions"] = report.get("prompt_suggestions") or []
    summary_payload["_quick_sim"] = report.get("quick_sim") or {}
    cur = conn.execute(
        """INSERT INTO review_reports (scope, generated_at, summary, failures, insights, samples)
           VALUES (?, ?, ?, ?, ?, ?)""",
        (
            scope,
            report.get("generated_at") or _now(),
            json.dumps(summary_payload, ensure_ascii=False),
            json.dumps(report.get("failures") or {}, ensure_ascii=False),
            json.dumps(report.get("insights") or [], ensure_ascii=False),
            int(report.get("samples") or 0),
        ),
    )
    conn.commit()
    return int(cur.lastrowid)


def _similar(rule: str, existing: str) -> float:
    """经验规则文本相似度（0-1），用于防堆积去重。"""
    from difflib import SequenceMatcher
    return SequenceMatcher(None, rule, existing).ratio()


def save_insights(conn: sqlite3.Connection, insights: List[str], source_samples: int = 0,
                  origin: str = "real") -> int:
    """经验规则入库（按文本 + 相似度去重，带来源样本数与提炼来源）。

    origin: 'real'=历史交易复盘 / 'quick_sim'=快速评测提炼 / 'rule'=归因规则

    v196: 解析 [XAU]/[BTC]/[WTI]/[ETH]/[*] 前缀作为 asset 字段；
          剥离前缀后写入 rule（避免与后续相似度去重互相干扰）。
          已知 asset 列表（从 _KNOWN_ASSET_CODES 取），前缀不匹配 → asset=''（通则）。
    """
    if origin not in ("real", "quick_sim", "rule"):
        origin = "real"
    added = 0
    existing = [row[0] for row in conn.execute(
        "SELECT rule FROM review_insights WHERE active = 1"
    ).fetchall()]
    for raw_rule in insights:
        if not raw_rule:
            continue
        # v196: 解析 asset 前缀
        asset_code, rule = _extract_asset_prefix(raw_rule)
        if not rule or rule in existing:
            continue
        # 相似度去重：与已有经验高度相似视为冗余，跳过
        if any(_similar(rule, old) > 0.62 for old in existing):
            continue
        conn.execute(
            "INSERT INTO review_insights"
            " (rule, asset, scope, active, source_samples, origin)"
            " VALUES (?, ?, 'review', 1, ?, ?)",
            (rule, asset_code, source_samples, origin),
        )
        existing.append(rule)
        added += 1
        # v189-7: per-row commit, 避免 N 条 insight 同一事务持锁 ~N×5ms.
        try:
            conn.commit()
        except sqlite3.OperationalError as exc:
            # 单条 insight 入库失败 (lock) 不影响其他 insight.
            print(f"[review_agent.save_insights] skip (lock): {type(exc).__name__}: {str(exc)[:80]}")
    return added


_KNOWN_ASSET_CODES = frozenset({"XAU", "BTC", "ETH", "WTI", "SOL",
                                "CN_STOCK", "US_STOCK", "INDIVIDUAL_STOCK",
                                "SKHYNIX"})


def _extract_asset_prefix(text: str) -> Tuple[str, str]:
    """v196: 解析 ' [XAU] XAU 在 ...' 前缀。

    匹配：[XAU]、[BTC]、[ETH]、[WTI]、[SOL]、[CN_STOCK]、[US_STOCK]、
          [INDIVIDUAL_STOCK]、[SKHYNIX]、[*]（*=通则显式标注）。
    命中 → 返回 (asset_code, 剥离前缀后的 rule)。
    未命中 → 返回 ('', text) (向后兼容)。
    """
    if not text:
        return "", ""
    s = text.strip()
    # 检测形如 "[XXX] ..." 的前缀（XXX 是已知 asset code 或 *）
    m = re.match(r"^\[(\*|[A-Z_]+)\]\s+", s)
    if not m:
        return "", text
    code = m.group(1)
    if code == "*":
        return "", s[m.end():]
    if code in _KNOWN_ASSET_CODES:
        return code, s[m.end():]
    # [OTHERS] 这类不认识的 → 视为通则（保留原文，不剥离）
    return "", text


def build_attribution_rows(trades: List[Dict[str, Any]], results: List[Dict[str, Any]],
                           match_id_key: str = "id", stored_id_key: str = "id",
                           result_id_key: str = "decision_id") -> List[Dict[str, Any]]:
    """把归因结果与原始交易对齐成落库行，供 save_attribution_details 使用。

    match_id_key：原始交易里用于匹配归因键的字段；stored_id_key：落库到 trade_id 的字段。
    （review_agent 复盘用 trade.id；history_review 用 decision_id 匹配、trade.id 落库。）
    """
    by = {r.get(result_id_key): r for r in results
          if isinstance(r, dict) and r.get(result_id_key) is not None}
    rows: List[Dict[str, Any]] = []
    for t in trades:
        if not isinstance(t, dict):
            continue
        r = by.get(t.get(match_id_key))
        if not r:
            continue
        rows.append({
            "trade_id": t.get(stored_id_key),
            "asset": t.get("asset") or "",
            "action": t.get("action") or "",
            "verdict": t.get("is_correct") or t.get("verdict") or "",
            "core_impact": t.get("gate_core_impact") or "",
            "pnl_pct": t.get("forward_pnl") if t.get("forward_pnl") is not None else t.get("pnl_pct"),
            "reason_code": r.get("reason_code"),
            "reason_text": r.get("reason_text") or "",
            "confidence": r.get("confidence"),
            "reasons_json": json.dumps(r.get("reasons") or [], ensure_ascii=False),
            "failing_sources": r.get("failing_sources") or [],  # v222：失败源清单
        })
    return rows


def save_attribution_details(conn: sqlite3.Connection, source: str, ref_id: int,
                             rows: List[Dict[str, Any]]) -> int:
    """落库归因明细（同 source+ref_id 先清后写，幂等重跑不断增）。

    review 归因额外按 trade_id 去重：复盘 scope（week）的窗口天然重叠，同一笔
    失败交易会在多期 report 里重复出现，若不去重会随复盘次数累计漂移，导致
    FAILURE STATS 面板「复盘归因/失败总数」与库不一致。去重时把旧行的人工
    纠错字段继承到新行，避免清洗掉历史反馈（最终归因以人工纠错为准）。
    """
    conn.execute("DELETE FROM attribution_reviews WHERE source=? AND ref_id=?", (source, ref_id))
    n = 0
    # v176.7 P1-4: 在落库归因后，自动给「错」的归因推 kg_improvement 候选。
    # verdict 约定: 'correct' / 'wrong' / 'partial'（来自 LLM 总结的归因）
    # reason_code 是失败框架编号（1-24，含因果链错位 24）。
    # 这里只对 verdict=wrong 的行调用 add_proposal，避免 noise。
    kg_proposals_pushed = 0
    for r in rows:
        tid = r.get("trade_id")
        carried: Dict[str, Any] = {}
        if source == "review" and tid:
            old = conn.execute(
                "SELECT human_status, corrected_code, corrected_text, note, reviewed_at "
                "FROM attribution_reviews WHERE source='review' AND trade_id=? "
                "ORDER BY id DESC LIMIT 1",
                (tid,),
            ).fetchone()
            if old:
                carried = {
                    "human_status": old[0] if old[0] is not None else "pending",
                    "corrected_code": old[1],
                    "corrected_text": old[2] if old[2] is not None else "",
                    "note": old[3] if old[3] is not None else "",
                    "reviewed_at": old[4],
                }
                conn.execute(
                    "DELETE FROM attribution_reviews WHERE source='review' AND trade_id=?",
                    (tid,),
                )
        conn.execute(
            """INSERT INTO attribution_reviews
               (source, ref_id, trade_id, asset, action, verdict, core_impact, pnl_pct,
                reason_code, reason_text, confidence, reasons_json, failing_sources,
                human_status, corrected_code, corrected_text, note, reviewed_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (source, ref_id, tid, r.get("asset"), r.get("action"),
             str(r.get("verdict") or ""), r.get("core_impact") or "",
             r.get("pnl_pct"), r.get("reason_code"),
             r.get("reason_text"), r.get("confidence"),
             r.get("reasons_json") or "",
             json.dumps(r.get("failing_sources") or [], ensure_ascii=False),
             carried.get("human_status") or "pending",
             carried.get("corrected_code"),
             carried.get("corrected_text") or "",
             carried.get("note") or "",
             carried.get("reviewed_at")),
        )
        n += 1

        # v176.7 P1-4: 错归因自动推 kg_improvement 候选（去重：同 trade+reason_code 同 ref_id 不重复）。
        # verdict 字段实际取值: 'WIN'/'LOSS'/'HOLD'（review_agent._verdict_by_pnl）。
        # 仅 LOSS 触发；reason_code=24 因果链错位最优先，其它次之。
        if _KG_IMP_OK and str(r.get("verdict") or "").upper() == "LOSS":
            try:
                asset_v = (r.get("asset") or "").strip().upper()
                rc = str(r.get("reason_code") or "")
                target = f"trade:{tid} asset:{asset_v}" if tid else f"asset:{asset_v}"
                # 去重: 同 trade+reason_code 已 pending/approved 才不再推
                dup = conn.execute(
                    "SELECT id FROM kg_improvements WHERE target=? AND status IN ('pending','approved') LIMIT 1",
                    (target[:200],),
                ).fetchone()
                if not dup:
                    kind = "reweight" if rc in ("6", "24") else "add_edge"
                    payload = {
                        "trade_id": tid,
                        "asset": asset_v,
                        "action": r.get("action") or "",
                        "reason_code": rc,
                        "reason_text": (r.get("reason_text") or "")[:500],
                        "pnl_pct": r.get("pnl_pct"),
                        "source_attribution_ref": ref_id,
                    }
                    rs = kg_improvements.add_proposal(
                        kind=kind,
                        target=target[:200],
                        reason=f"归因错(ref={ref_id}, {rc}): {(r.get('reason_text') or '')[:300]}",
                        confidence=float(r.get("confidence") or 0.5),
                        sample_size=1,
                        impact=f"trade={tid} asset={asset_v}",
                        payload=payload,
                        source=f"review_agent:{source}",
                        conn=conn,
                    )
                    if rs.get("ok"):
                        kg_proposals_pushed += 1
            except Exception:
                pass  # 候选失败不影响归因主链路
    conn.commit()
    # 主链路完成后打印一条统计（方便观察推到多少）
    if kg_proposals_pushed:
        print(f"[KG-IMPROVE] pushed {kg_proposals_pushed} pending proposals from {source} ref={ref_id}", flush=True)
    return n


def clear_review_attributions(conn: sqlite3.Connection, scope: str,
                              except_ref_id: Optional[int] = None) -> int:
    """清理某一 scope 的历史复盘归因，只保留最新一次复盘（FAILURE STATS 数据同步）。

    ``attribution_reviews`` 对同 source+ref_id 幂等，但每次复盘 ref_id(=report_id)
    都不同，旧复盘归因不会自动清除，导致 review 归因随复盘次数累计漂移（
    前端显示与库不一致）。复盘前调用本函数清掉同 scope 旧 report 的归因。
    """
    sql = (
        "DELETE FROM attribution_reviews WHERE source='review' AND ref_id IN "
        "(SELECT id FROM review_reports WHERE scope=?)"
    )
    args: List[Any] = [scope]
    if except_ref_id is not None:
        sql += " AND ref_id != ?"
        args.append(int(except_ref_id))
    cur = conn.execute(sql, args)
    conn.commit()
    return cur.rowcount


def list_attributions(status: str = "pending", limit: int = 200) -> List[Dict[str, Any]]:
    """按复核状态列归因明细（pending/agree/corrected/all）。

    v222：返回 dict 时自动解析 reasons_json / failing_sources 为 Python 对象，
    让复核面板同时展示主因 + 全部次因 + 失败源清单（不再吞掉）。
    """
    conn = db.get_connection()
    conn.row_factory = sqlite3.Row
    try:
        if status == "all":
            rows = conn.execute(
                "SELECT * FROM attribution_reviews ORDER BY id DESC LIMIT ?",
                (min(max(limit, 1), 500),),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM attribution_reviews WHERE human_status=? ORDER BY id DESC LIMIT ?",
                (status, min(max(limit, 1), 500)),
            ).fetchall()
        out: List[Dict[str, Any]] = []
        for r in rows:
            d = dict(r)
            rj = d.get("reasons_json") or ""
            if rj:
                try:
                    d["reasons"] = json.loads(rj)
                except Exception:
                    d["reasons"] = []
            else:
                d["reasons"] = []
            fs = d.get("failing_sources") or ""
            if fs:
                try:
                    d["failing_sources"] = json.loads(fs)
                except Exception:
                    d["failing_sources"] = []
            else:
                d["failing_sources"] = []
            out.append(d)
        return out
    finally:
        conn.close()


# ── 归因统计缓存（前端 60s 轮询，避免每次全表扫描） ──
_STATS_CACHE: Dict[str, Tuple[float, Dict[str, Any]]] = {}
_STATS_CACHE_TTL_SEC = 30.0
_STATS_CACHE_KEY = "main"


def _invalidate_attribution_stats_cache() -> None:
    """写路径（reset/feedback/unreset）后调用，让下一次 stats 重算。"""
    _STATS_CACHE.pop(_STATS_CACHE_KEY, None)


def attribution_stats() -> Dict[str, Any]:
    """跨来源统一汇总失败归因（news 回溯 + review 复盘），展示哪类失败最多。

    最终归因码以人工纠错为准（corrected_code 优先，否则用 AI 初判 reason_code）。
    reset 状态 = 用户手动重置的"已归档"记录，不计入汇总（避免"怎么还显示 87"）。

    主因聚合（单选）：每条记录只取 1 个主因 ——
    人工纠错 > AI 多原因主因 (is_primary=true) > AI 多原因第一项 > reason_code，
    保证 pct 加和 ≤ 100%。多原因明细请见 per-trade 明细面板。

    未归因：主因提取失败的记录（脏数据 / 老库兼容），独立列 code=0 让用户看见缺口。
    """
    now_ts = time.time()
    cached = _STATS_CACHE.get(_STATS_CACHE_KEY)
    if cached and now_ts - cached[0] < _STATS_CACHE_TTL_SEC:
        return cached[1]

    conn = db.get_connection()
    conn.row_factory = sqlite3.Row
    try:
        total = conn.execute(
            "SELECT COUNT(*) FROM attribution_reviews WHERE human_status != 'reset'"
        ).fetchone()[0]
        reset_count = conn.execute(
            "SELECT COUNT(*) FROM attribution_reviews WHERE human_status = 'reset'"
        ).fetchone()[0]
        source_rows = conn.execute(
            "SELECT source, COUNT(*) AS n FROM attribution_reviews "
            "WHERE human_status != 'reset' GROUP BY source"
        ).fetchall()
        status_rows = conn.execute(
            "SELECT human_status, COUNT(*) AS n FROM attribution_reviews "
            "WHERE human_status != 'reset' GROUP BY human_status"
        ).fetchall()
        rows = conn.execute(
            "SELECT corrected_code, reason_code, reasons_json FROM attribution_reviews "
            "WHERE human_status != 'reset'"
        ).fetchall()
        counter: Dict[int, int] = {}
        unattr = 0
        for r in rows:
            code = _primary_code(r["corrected_code"], r["reason_code"], r["reasons_json"])
            if code is None:
                unattr += 1
                continue
            counter[code] = counter.get(code, 0) + 1
        reasons: List[Dict[str, Any]] = []
        for code, n in sorted(counter.items(), key=lambda kv: -kv[1]):
            reasons.append({
                "code": code,
                "name": _REASON_NAMES.get(code, f"未知({code})"),
                "count": n,
                "pct": round(n / total * 100, 1) if total else 0.0,
            })
        if unattr > 0:
            reasons.append({
                "code": 0,
                "name": "未归因/数据缺失",
                "count": unattr,
                "pct": round(unattr / total * 100, 1) if total else 0.0,
            })
            reasons.sort(key=lambda x: -x["count"])
    finally:
        conn.close()
    out = {
        "total": total,
        "sources": {str(r["source"]): int(r["n"]) for r in source_rows},
        "statuses": {str(r["human_status"]): int(r["n"]) for r in status_rows},
        "reasons": reasons,
        # 让前端展示更直观（sources/news + sources/review 拼起来 = total）
        "news_count": int(next((r["n"] for r in source_rows if r["source"] == "news"), 0)),
        "review_count": int(next((r["n"] for r in source_rows if r["source"] == "review"), 0)),
        "unattributed": unattr,
        "reset_count": int(reset_count),
        "cache_age_sec": 0.0,
    }
    _STATS_CACHE[_STATS_CACHE_KEY] = (now_ts, out)
    return out


def reset_attributions(scope: str = "all", days: int = 0) -> Dict[str, Any]:
    """重置失败归因（软删除 = 把 human_status 改为 'reset'，数据可恢复）。

    防止用户看到大量 pending 归因而焦虑；保留数据避免破坏归因上下文。
    scope:
      - all: 全部 source 的 pending 归因
      - news: 仅新闻回溯
      - review: 仅复盘归因
    days: 仅重置 N 天前的记录（0=全部）
    """
    if scope not in ("all", "news", "review"):
        return {"ok": False, "error": f"invalid scope: {scope}"}
    conn = db.get_connection()
    try:
        if scope == "all":
            where_src = "1=1"
        else:
            where_src = f"source=?"
            # 在 params 里动态传 source
        where = f"human_status='pending' AND {where_src}"
        params: List[Any] = []
        if scope != "all":
            params.append(scope)
        if days and days > 0:
            where += f" AND created_at < datetime('now', ?)"
            params.append(f"-{int(days)} days")
        # 先数影响行数（要可观测）
        cur = conn.execute(f"SELECT COUNT(*) FROM attribution_reviews WHERE {where}", tuple(params))
        before = cur.fetchone()[0]
        # 软删除
        update_params: List[Any] = list(params)
        cur = conn.execute(
            f"UPDATE attribution_reviews SET human_status='reset', reviewed_at=datetime('now') WHERE {where}",
            tuple(update_params),
        )
        deleted = cur.rowcount
        conn.commit()
    finally:
        conn.close()
    # 写路径：下次 stats 必须重算
    _invalidate_attribution_stats_cache()
    # 重读 stats
    return {
        "ok": True,
        "scope": scope,
        "days": days,
        "before": int(before),
        "reset": int(deleted),
        "stats": attribution_stats(),
    }


def unreset_attributions(scope: str = "all", days: int = 0) -> Dict[str, Any]:
    """撤销重置（reset → pending），重新放入复核队列。

    与 reset_attributions 反向。适用「重置后想恢复部分归因继续复核」。
    仅改 human_status，不动其他字段；reviewed_at 保留为原重置时间，便于审计。
    scope: all / news / review（与 reset_attributions 同语义）
    days: 仅撤销 N 天前的记录（0=全部）
    """
    if scope not in ("all", "news", "review"):
        return {"ok": False, "error": f"invalid scope: {scope}"}
    conn = db.get_connection()
    try:
        where = "human_status='reset'"
        params: List[Any] = []
        if scope != "all":
            where += " AND source=?"
            params.append(scope)
        if days and days > 0:
            where += " AND created_at < datetime('now', ?)"
            params.append(f"-{int(days)} days")
        # 先数影响行数
        cur = conn.execute(
            f"SELECT COUNT(*) FROM attribution_reviews WHERE {where}", tuple(params)
        )
        before = cur.fetchone()[0]
        conn.execute(
            f"UPDATE attribution_reviews SET human_status='pending' WHERE {where}",
            tuple(params),
        )
        conn.commit()
    finally:
        conn.close()
    _invalidate_attribution_stats_cache()
    return {
        "ok": True,
        "scope": scope,
        "days": days,
        "before": int(before),
        "unreset": int(before),
        "stats": attribution_stats(),
    }


def gate_attribution_stats() -> Dict[str, Any]:
    """按门卫主线（core_impact 这条筋）聚合归因明细，回看每条筋的实际兑现。

    core_impact 文本本身已含主资产×性质，聚合它即得到"哪条筋胜率高/总亏/主要败因"。
    归因码取人工纠错优先（corrected_code），否则 AI 初判 reason_code。
    """
    conn = db.get_connection()
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(
            "SELECT core_impact, verdict, pnl_pct, "
            "COALESCE(corrected_code, reason_code) AS code "
            "FROM attribution_reviews WHERE core_impact <> ''"
        ).fetchall()
        buckets: Dict[str, Dict[str, Any]] = {}
        for r in rows:
            line = str(r["core_impact"]).strip()
            key = line[:60]
            b = buckets.setdefault(
                key, {"main_line": line, "n": 0, "win": 0, "loss": 0, "pnl": 0.0, "codes": {}}
            )
            b["n"] += 1
            v = str(r["verdict"] or "").upper()
            if v == "WIN":
                b["win"] += 1
            elif v == "LOSS":
                b["loss"] += 1
            pnl = r["pnl_pct"]
            if isinstance(pnl, (int, float)):
                b["pnl"] += float(pnl)
            if r["code"] is not None:
                c = int(r["code"])
                b["codes"][c] = b["codes"].get(c, 0) + 1
        lines = []
        for b in sorted(buckets.values(), key=lambda x: -x["n"]):
            decided = b["win"] + b["loss"]
            top_code = max(b["codes"], key=b["codes"].get) if b["codes"] else None
            lines.append({
                "main_line": b["main_line"],
                "n": b["n"],
                "winrate": round(b["win"] / decided * 100, 1) if decided else None,
                "net_pnl": round(b["pnl"], 2),
                "top_reason_code": top_code,
                "top_reason_name": _REASON_NAMES.get(top_code, "") if top_code else "",
            })
    finally:
        conn.close()
    return {"lines": lines}


def apply_attribution_feedback(conn: sqlite3.Connection, attribution_id: int, status: str,
                               corrected_code: Optional[int] = None,
                               corrected_text: str = "", note: str = "") -> Dict[str, Any]:
    """人工复核/纠错一条归因。纠错且 code 不同则自动回灌经验规则（rule_pushed 防重复）。"""
    row = None
    prev_rf = conn.row_factory
    conn.row_factory = sqlite3.Row
    try:
        row = conn.execute(
            "SELECT * FROM attribution_reviews WHERE id=?", (int(attribution_id),)
        ).fetchone()
    finally:
        conn.row_factory = prev_rf
    if row is None:
        return {"ok": False, "error": "attribution not found"}
    row = dict(row)
    now = _now()
    conn.execute(
        """UPDATE attribution_reviews
           SET human_status=?, reviewed_at=?, corrected_code=?, corrected_text=?, note=?
           WHERE id=?""",
        (status, now, corrected_code, corrected_text or "", note or "", int(attribution_id)),
    )
    pushed = False
    rule_text = ""
    if status == "corrected" and corrected_code is not None:
        old_code = row.get("reason_code")
        try:
            code_changed = int(corrected_code) != int(old_code or -1)
        except (TypeError, ValueError):
            code_changed = False
        if code_changed and not row.get("rule_pushed"):
            old_name = _REASON_NAMES.get(int(old_code or 0), "未知")
            new_name = _REASON_NAMES.get(int(corrected_code), "未知")
            ctx = f"{row.get('asset') or ''} {row.get('action') or ''} {row.get('verdict') or ''}".strip()
            rule_text = f"归因纠错：{ctx} 原判「{old_name}」应判「{new_name}」"
            if note:
                rule_text += f"（{note}）"
            save_insights(conn, [rule_text], source_samples=1, origin="rule")
            conn.execute(
                "UPDATE attribution_reviews SET rule_pushed=1 WHERE id=?", (int(attribution_id),)
            )
            pushed = True
    conn.commit()
    # 写路径：人工纠错会改变 corrected_code，下次 stats 必须重算
    _invalidate_attribution_stats_cache()
    return {"ok": True, "pushed_rule": pushed, "rule": rule_text}


# ── 分析经验种子：硬铁律（kind=0，不可违背、不参与胜率停用）+ 软启发（kind=1，可证伪） ──
ANALYSIS_EXPERIENCE_SEEDS: List[Dict[str, Any]] = [
    # 硬铁律 —— 边界公理，管稳定
    {"kind": 0, "scope": "all", "rule": "只依据真实、可追溯的数据源；数据缺失标 N/A，绝不臆造/编造数据。", "counter_factual": ""},
    {"kind": 0, "scope": "all", "rule": "官员/机构「发言表态」是即时事件催化，不是媒体「事后统计」；不得把表态误判为事后统计性消息。", "counter_factual": ""},
    {"kind": 0, "scope": "all", "rule": "事后统计/评估性消息大概率已定价（新闻已抢跑），不据此直接给方向。", "counter_factual": ""},
    {"kind": 0, "scope": "all", "rule": "八段推演与 20 字段结构不可省略；每个结论必须 score 锚定，asset_impact 打分必须带事实依据。", "counter_factual": ""},
    {"kind": 0, "scope": "all", "rule": "方向不确定时给 HOLD 并说明理由，绝不因「要出结论」而强行买卖。", "counter_factual": ""},
    # 软启发 —— 可证伪假说（人工维护种子，结算后按真实胜率升/降权或停用）
    {"kind": 1, "scope": "review", "rule": "避险叙事下黄金走强概率高，但资金坐实前不追多。", "counter_factual": "若未见真实买盘/持仓增持续证明资金流入，则不追多"},
    {"kind": 1, "scope": "review", "rule": "突发供应中断（炼厂爆炸/停产）通常短线利多油价，但价格已抢跑时追多赔率差。", "counter_factual": "若价格已因该事件大幅拉升，追多赔率差，反而防回落"},
    {"kind": 1, "scope": "review", "rule": "官员表态引发的短期波动，需匹配真实资金流才可跟随。", "counter_factual": "若仅表态、无资金流/持仓配合，则不作方向依据"},
]


def seed_analysis_experience(conn: Optional[sqlite3.Connection] = None) -> int:
    """幂等播种硬铁律/软启发种子：相同 rule 已存在则不重复插入（不删除/重置用户已有经验）。

    硬铁律 kind=0 不参与胜率停用；软启发 origin='seeded'（人工维护，结算后验证）。
    """
    own = conn is None
    conn = conn or db.get_connection()
    added = 0
    try:
        for seed in ANALYSIS_EXPERIENCE_SEEDS:
            rule = seed["rule"]
            if conn.execute("SELECT 1 FROM review_insights WHERE rule=?", (rule,)).fetchone():
                continue
            conn.execute(
                "INSERT INTO review_insights"
                " (rule, asset, scope, active, source_samples, origin, kind, counter_factual)"
                " VALUES (?, '', ?, 1, 0, 'seeded', ?, ?)",
                (rule, seed["scope"], seed["kind"], seed["counter_factual"]),
            )
            added += 1
        conn.commit()
    finally:
        if own:
            conn.close()
    return added


def build_active_insights_section(conn: Optional[sqlite3.Connection] = None) -> str:
    """生效经验的注入分区（结构化标题 + 逐条内容），供拼进 AI 分析的 prompt。

    为什么单独成区：此前是把经验用 " | " 挤成一行塞进 user prompt，条数一多就成
    一坨、模型难以逐条对照，且样本数（判断可靠性的唯一依据）在拼接时丢失。
    这里改为与「品种专项规则」同风格的独立分区：标题 + 使用方式 + 逐条编号。

    优先级：本分区是「经验层」，低于通用规则与品种专项规则 —— 分区内已写明，
    避免经验与用户设定的规则互相打架。使用方式里也强调不得因引用经验而省略
    八段结构 / 20 字段 / score 锚定（防结果漂移）。

    条数由 config.REVIEW_INSIGHTS_LIMIT 控制（默认 0 = 不限，启用多少注入多少）；
    无生效经验 → 返回 ""（不注入、零开销）。
    """
    limit = max(0, int(getattr(config, "REVIEW_INSIGHTS_LIMIT", 0) or 0))
    min_samples = max(1, int(getattr(config, "REVIEW_INSIGHTS_MIN_SAMPLES", 5) or 5))
    # limit=0 表示不限 —— SQL 的 LIMIT 0 会变成"什么都不取"，故给个足够大的值
    sql_limit = limit if limit > 0 else 100000
    own = conn is None
    conn = conn or db.get_connection()
    try:
        conn.row_factory = sqlite3.Row
        # 硬铁律：kind=0，强制约束，不受样本门槛，不参与胜率停用
        hard_rows = conn.execute(
            "SELECT id, rule FROM review_insights"
            " WHERE active = 1 AND kind = 0 ORDER BY id"
        ).fetchall()
        # 软启发：人工种子(origin='seeded')始终注入；系统自动沉淀需满足样本门槛
        soft_rows = conn.execute(
            "SELECT id, rule, COALESCE(origin, 'real') AS origin, source_samples,"
            " COALESCE(counter_factual, '') AS counter_factual"
            " FROM review_insights"
            " WHERE active = 1 AND kind = 1 AND (origin = 'seeded' OR source_samples >= ?)"
            " ORDER BY source_samples DESC, id DESC LIMIT ?",
            (min_samples, sql_limit),
        ).fetchall()
        if not hard_rows and not soft_rows:
            return ""
        lines: List[str] = []
        if hard_rows:
            lines.append("═══ 硬铁律（不可违背 · 必守）═══")
            lines.append("以下为系统强制约束，任何情况下不得违背；不参与命中/胜率统计。")
            for i, r in enumerate(hard_rows, 1):
                lines.append(f"H{i}. {r['rule']}")
            lines.append("")
        if soft_rows:
            origin_label = {"real": "历史交易", "quick_sim": "快速评测", "rule": "归因规则", "seeded": "人工维护"}
            lines.append("═══ 软启发（统计倾向 · 可被反证）═══")
            lines.append("以下为可证伪的经验倾向，与本次新闻事实冲突时以新闻事实为准。")
            lines.append("真正采纳了哪条，就在输出 JSON 的 insight_hit_ids 里填它的 [EX#id]（没采纳填 []）。")
            for i, r in enumerate(soft_rows, 1):
                label = origin_label.get(str(r["origin"] or "real"), "历史交易")
                n = int(r["source_samples"] or 0)
                tag = f"{n} 样本" if n >= min_samples else "种子/待验证"
                lines.append(f"{i}. [EX#{r['id']} · {label} · {tag}] {r['rule']}")
                if r["counter_factual"]:
                    lines.append(f"   反例：{r['counter_factual']}")
        return "\n".join(lines)
    except Exception:
        return ""
    finally:
        if own:
            conn.close()


def load_active_insights(
    conn: Optional[sqlite3.Connection] = None,
    limit: int = 0,
    min_samples: int = 0,
) -> List[str]:
    """加载生效经验规则（供 AI 分析 prompt 回灌）。

    默认 limit=0（不限）/ min_samples=0（用配置门槛）—— 统一由 config 的
    REVIEW_INSIGHTS_LIMIT / REVIEW_INSIGHTS_MIN_SAMPLES 控制，避免"函数默认值"
    与"配置项"两处口径打架。显式传参可覆盖（测试用）。
    """
    eff_limit = limit if limit > 0 else max(0, int(getattr(config, "REVIEW_INSIGHTS_LIMIT", 0) or 0))
    eff_min = min_samples if min_samples > 0 else max(
        1, int(getattr(config, "REVIEW_INSIGHTS_MIN_SAMPLES", 5) or 5)
    )
    return [item["rule"] for item in load_active_insight_items(conn, eff_limit, eff_min)]


def load_active_insight_items(
    conn: Optional[sqlite3.Connection] = None,
    limit: int = 0,
    min_samples: int = 0,
) -> List[Dict[str, Any]]:
    """加载生效经验（含 id 与来源 origin），供注入 prompt 与记录命中（insight_hit_ids）。

    limit=0 = 不限（受 config.REVIEW_INSIGHTS_LIMIT 约束，默认也是不限）；
    min_samples=0 = 用 config.REVIEW_INSIGHTS_MIN_SAMPLES。
    """
    eff_limit = limit if limit > 0 else max(0, int(getattr(config, "REVIEW_INSIGHTS_LIMIT", 0) or 0))
    sql_limit = eff_limit if eff_limit > 0 else 100000
    eff_min = min_samples if min_samples > 0 else max(
        1, int(getattr(config, "REVIEW_INSIGHTS_MIN_SAMPLES", 5) or 5)
    )
    own = conn is None
    conn = conn or db.get_connection()
    try:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            "SELECT id, rule, COALESCE(origin, 'real') AS origin, source_samples"
            " FROM review_insights"
            " WHERE active = 1 AND kind = 1 AND (origin = 'seeded' OR source_samples >= ?)"
            " ORDER BY source_samples DESC, id DESC LIMIT ?",
            (eff_min, sql_limit),
        ).fetchall()
        return [{"id": int(r["id"]), "rule": str(r["rule"]), "origin": str(r["origin"] or "real")} for r in rows]
    finally:
        if own:
            conn.close()


def normalize_insight_hit_ids(raw: Any) -> List[int]:
    """把 AI 输出声称采纳的经验 id 规范化为合法 int 列表（去重/升序/过滤非法）。

    AI 可能返回 JSON 字符串、int 列表或混入非法值（字符串 id、负数、幻觉 id 等）；
    这里只做「形状与数值」归一（id 必须为正整数）。是否属于当前生效经验 id，
    由调用方用 load_active_insight_items() 结果二次过滤，避免脏数据污染命中归因。
    """
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except Exception:
            return []
    if not isinstance(raw, list):
        return []
    out: List[int] = []
    for x in raw:
        try:
            v = int(x)
        except (TypeError, ValueError):
            continue
        if v <= 0:
            continue
        if v not in out:
            out.append(v)
    out.sort()
    return out


def load_all_insight_items(
    conn: Optional[sqlite3.Connection] = None,
    limit: int = 500,
) -> List[Dict[str, Any]]:
    """全部经验（含生效与停用，带 id/origin），供前端按来源手动清理。

    与 load_active_insight_items 的区别：那个只给注入 prompt 的"生效且样本达标"子集；
    这个给全量视图，让用户能删除任意一条（含已停用、无法再验证的旧经验）。
    """
    own = conn is None
    conn = conn or db.get_connection()
    try:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            "SELECT id, rule, COALESCE(origin, 'real') AS origin, active,"
            " source_samples, created_at"
            " FROM review_insights ORDER BY active DESC, id DESC LIMIT ?",
            (max(1, int(limit)),),
        ).fetchall()
        return [
            {
                "id": int(r["id"]),
                "rule": str(r["rule"]),
                "origin": str(r["origin"] or "real"),
                "active": int(r["active"] or 0),
                "source_samples": int(r["source_samples"] or 0),
                "created_at": str(r["created_at"] or ""),
            }
            for r in rows
        ]
    finally:
        if own:
            conn.close()


def load_deactivated_insights(
    conn: Optional[sqlite3.Connection] = None,
    limit: int = 10,
) -> List[Dict[str, Any]]:
    """最近被自动停用的经验（active=0，按入库倒序），供前端展示空态原因。

    停用原因：命中该经验的交易胜率偏低（_evaluate_insights 按命中归因自动停用，
    或人工在提示库/复盘页手动停用）。展示为灰色，让用户知道经验不是丢了而是被停用。
    注：已改为双向可恢复 —— 命中胜率回升到阈值以上会自动恢复启用。

    返回含 id 与 origin：前端要靠它们显示来源标记（快速评测/历史交易）并支持手动删除。
    """
    own = conn is None
    conn = conn or db.get_connection()
    try:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            "SELECT id, rule, source_samples, created_at, COALESCE(origin, 'real') AS origin"
            " FROM review_insights"
            " WHERE active = 0 ORDER BY id DESC LIMIT ?",
            (max(1, int(limit)),),
        ).fetchall()
        return [
            {
                "id": int(r["id"]),
                "rule": str(r["rule"]),
                "source_samples": int(r["source_samples"] or 0),
                "created_at": str(r["created_at"] or ""),
                "origin": str(r["origin"] or "real"),
            }
            for r in rows
        ]
    finally:
        if own:
            conn.close()


def count_insights(active: bool, conn: Optional[sqlite3.Connection] = None) -> int:
    """统计生效 / 停用的经验条数（供前端说明空态原因）。"""
    own = conn is None
    conn = conn or db.get_connection()
    try:
        row = conn.execute(
            "SELECT COUNT(*) FROM review_insights WHERE active = ?", (1 if active else 0,)
        ).fetchone()
        return int(row[0] or 0)
    finally:
        if own:
            conn.close()


def toggle_insight(insight_id: int, conn: Optional[sqlite3.Connection] = None) -> Dict[str, Any]:
    """手动启用/停用一条经验（1↔0），返回切换后的状态。

    为什么需要：提供人工出口，不必等自动评估。此前自动停用是单向的（只遍历
    active=1 的经验、停用后永不复查），曾出现全部经验"连坐"停用后无法恢复；
    现已改为按命中归因 + 双向可恢复，本接口仍保留供人工即时干预。
    """
    own = conn is None
    conn = conn or db.get_connection()
    try:
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            "SELECT active FROM review_insights WHERE id = ?", (int(insight_id),)
        ).fetchone()
        if row is None:
            return {"ok": False, "error": "经验不存在"}
        next_active = 0 if int(row["active"] or 0) else 1
        # 记下"是不是人工停用的"：自动自愈要跳过人工停用，否则重启会把它复活
        conn.execute(
            "UPDATE review_insights SET active = ?, manual_deactivated = ? WHERE id = ?",
            (next_active, 1 if next_active == 0 else 0, int(insight_id)),
        )
        conn.commit()
        return {"ok": True, "id": int(insight_id), "active": next_active}
    finally:
        if own:
            conn.close()


def delete_insight(insight_id: int, conn: Optional[sqlite3.Connection] = None) -> Dict[str, Any]:
    """硬删除一条经验规则（含生效与停用）。

    用途：重置模拟盘会保留复盘经验（设计如此），用户需要能手动清理
    —— 尤其是来源=quick_sim 的经验，其底层样本可能已被重置删除、无法再验证。
    """
    own = conn is None
    conn = conn or db.get_connection()
    try:
        cur = conn.execute("DELETE FROM review_insights WHERE id = ?", (int(insight_id),))
        conn.commit()
        if cur.rowcount == 0:
            return {"ok": False, "error": "经验不存在"}
        return {"ok": True, "id": int(insight_id)}
    finally:
        if own:
            conn.close()


def delete_insights_by_origin(origin: str, conn: Optional[sqlite3.Connection] = None) -> Dict[str, Any]:
    """按来源批量删除经验（origin: real / quick_sim / rule）。"""
    key = (origin or "").strip()
    if key not in ("real", "quick_sim", "rule"):
        return {"ok": False, "error": "来源只能是 real / quick_sim / rule"}
    own = conn is None
    conn = conn or db.get_connection()
    try:
        # COALESCE 兼容历史数据里 origin 为空的行（按 real 归类，与读取时一致）
        cur = conn.execute(
            "DELETE FROM review_insights WHERE COALESCE(origin, 'real') = ?", (key,)
        )
        conn.commit()
        return {"ok": True, "origin": key, "deleted": int(cur.rowcount)}
    finally:
        if own:
            conn.close()


def latest_reports(conn: Optional[sqlite3.Connection] = None, limit: int = 5) -> List[Dict[str, Any]]:
    own = conn is None
    conn = conn or db.get_connection()
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(
            "SELECT * FROM review_reports ORDER BY id DESC LIMIT ?", (limit,),
        ).fetchall()
        out = []
        for row in rows:
            item = dict(row)
            for key in ("summary", "failures", "insights"):
                try:
                    item[key] = json.loads(item.get(key) or "{}")
                except json.JSONDecodeError:
                    item[key] = {} if key != "insights" else []
            # 历史报告的成功归类存在 summary._successes，提升到顶层（与 run 接口结构一致）
            summary_obj = item.get("summary")
            if isinstance(summary_obj, dict):
                if summary_obj.get("_successes"):
                    item["successes"] = summary_obj["_successes"]
                # 来源标注与快速评测块（持久化在 summary 内，提升到顶层供前端展示）
                item["insights_by_origin"] = summary_obj.get("_insights_by_origin") or {}
                item["prompt_suggestions"] = summary_obj.get("_prompt_suggestions") or []
                item["quick_sim"] = summary_obj.get("_quick_sim") or {}
            else:
                item.setdefault("successes", None)
            out.append(item)
        return out
    finally:
        if own:
            conn.close()


# ---------------------------------------------------------------------------
# 参考信号提炼（AI 分析新新闻时参考的结构化信号，从历史已结算交易统计）
# ---------------------------------------------------------------------------

_MIN_SIGNAL_SAMPLES = 3  # 信号最少样本数，避免小样本噪音


def _parse_ts(text: Optional[str]) -> Optional[int]:
    """时间字符串转 unix 秒（支持 ISO 带时区与 'YYYY-MM-DD HH:MM:SS'）。"""
    if not text:
        return None
    s = str(text).strip()
    try:
        return int(datetime.fromisoformat(s).timestamp())
    except ValueError:
        pass
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%dT%H:%M:%S"):
        try:
            return int(datetime.strptime(s[:19], fmt).timestamp())
        except ValueError:
            continue
    return None


def _signal_agg(items: List[Dict[str, Any]]) -> Dict[str, Any]:
    """统计一组交易的胜率/盈亏（verdict 判定）。"""
    total = len(items)
    wins = sum(1 for it in items if it.get("verdict") == "WIN")
    pnl = sum(float(it.get("pnl_pct") or 0.0) for it in items)
    return {
        "total": total,
        "wins": wins,
        "losses": total - wins,
        "winrate": round(wins / total, 4) if total else None,
        "total_pnl": round(pnl, 4),
    }


def _interval_bucket(minutes: Optional[int]) -> Optional[str]:
    """新闻发布 → 入场的间隔分桶。"""
    if minutes is None:
        return None
    if minutes < 15:
        return "15分钟内"
    if minutes < 60:
        return "15-60分钟"
    return "60分钟后"


_PHASE_NAMES = {"early": "早期入场", "mid": "中期入场", "late": "晚期入场"}
_STRENGTH_NAMES = {"high": "高", "medium": "中", "low": "低"}


def _signal_summary(label: str, agg: Dict[str, Any], kind: str) -> str:
    """把一条信号统计提炼成一句提示词风格的总结（价值话语），供人/AI 直接理解。"""
    total = agg["total"]
    wr = agg["winrate"] or 0.0
    pnl = agg["total_pnl"]
    pct = f"{wr * 100:.0f}%"
    small = total < 8
    if kind == "assets":
        if wr >= 0.6 and pnl > 0:
            s = f"{label} 是高质量信号品种：{total}笔胜率{pct}且稳定盈利（{pnl:+.2f}%），可重点参考"
        elif wr >= 0.6 and pnl <= 0:
            s = f"{label} 胜率高（{pct}）但盈亏为负，警惕单笔亏损吃掉胜率优势"
        elif wr < 0.45:
            s = f"{label} 历史胜率仅{pct}，属于低质量信号，需谨慎交易"
        else:
            s = f"{label} {total}笔胜率{pct}、盈亏{pnl:+.2f}%，信号质量中等"
    elif kind == "directions":
        s = f"{label} 方向{total}笔胜率{pct}、盈亏{pnl:+.2f}%"
        if wr >= 0.55:
            s += "，该方向历史更可靠，可适当提高置信度"
        elif wr < 0.45:
            s += "，该方向历史偏弱，建议降低置信度"
    elif kind == "phases":
        s = f"{label} {total}笔胜率{pct}"
        s += "，把握入场时机更佳" if wr >= 0.55 else "，入场时机把握一般"
    elif kind == "strengths":
        s = f"{label} 强度事件{total}笔胜率{pct}、盈亏{pnl:+.2f}%"
        if wr >= 0.6:
            s += "，此类事件催化强，值得关注"
        elif wr < 0.45:
            s += "，此类事件催化弱，谨慎对待"
    elif kind == "intervals":
        s = f"新闻后{label}入场{total}笔胜率{pct}"
        if wr >= 0.6:
            s += "，历史验证的高胜率窗口，建议等待市场消化后再入场（避免追第一波）"
        elif wr < 0.45:
            s += "，追第一波历史表现差，建议延迟入场"
        else:
            s += "，入场时机无显著优势"
    elif kind == "combos":
        s = f"{label} {total}笔胜率{pct}"
        if wr >= 0.6:
            s += "，该品种×方向组合历史质量高，可提高置信度"
        elif wr < 0.45:
            s += "，该品种×方向组合历史偏弱，谨慎交易或降低置信度"
        else:
            s += "，该组合信号质量一般"
    else:
        s = f"{label} {total}笔胜率{pct}"
    if small:
        s += f"（样本{total}笔偏少，仅作参考）"
    return s


def derive_reference_signals(conn: Optional[sqlite3.Connection] = None) -> Dict[str, Any]:
    """从已结算模拟交易提炼参考信号：品种 / 多空方向 / 事件阶段 / 事件强度 / 入场时机。

    信号即「AI 分析新新闻时可参考的统计规律」，样本 >= _MIN_SIGNAL_SAMPLES 才输出。
    """
    own = conn is None
    conn = conn or db.get_connection()
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(
            """SELECT qt.asset, qt.action, qt.verdict, qt.pnl_pct, qt.entry_ts,
                      ad.event_phase, ad.event_strength, ad.expected_horizon,
                      rn.timestamp AS news_time
               FROM quick_sim_trades qt
               JOIN ai_decisions ad ON ad.id = qt.decision_id
               JOIN raw_news rn ON rn.id = qt.news_id
               WHERE qt.settled = 1 AND qt.verdict IN ('WIN', 'LOSS')"""
        ).fetchall()
    finally:
        if own:
            conn.close()

    raw = [dict(r) for r in rows]
    groups: Dict[str, Dict[str, List[Dict[str, Any]]]] = {
        "assets": {}, "directions": {}, "phases": {}, "strengths": {}, "intervals": {}, "combos": {},
    }
    for t in raw:
        asset = str(t.get("asset") or "?")
        action = "做多" if t.get("action") == "BUY" else "做空"
        phase = _PHASE_NAMES.get(t.get("event_phase"), str(t.get("event_phase") or "未知"))
        strength = _STRENGTH_NAMES.get(t.get("event_strength"), str(t.get("event_strength") or "未知"))
        interval = _interval_bucket(
            _interval_minutes(t.get("news_time"), t.get("entry_ts"))
        )
        groups["assets"].setdefault(asset, []).append(t)
        groups["directions"].setdefault(action, []).append(t)
        groups["phases"].setdefault(phase, []).append(t)
        groups["strengths"].setdefault(strength, []).append(t)
        groups["combos"].setdefault(f"{asset}×{action}", []).append(t)
        if interval:
            groups["intervals"].setdefault(interval, []).append(t)

    def _build(key: str) -> List[Dict[str, Any]]:
        out = []
        for label, items in groups[key].items():
            if len(items) < _MIN_SIGNAL_SAMPLES:
                continue
            agg = _signal_agg(items)
            agg["label"] = label
            agg["summary"] = _signal_summary(label, agg, key)
            out.append(agg)
        # 按样本量降序（样本多更可靠），样本相同按胜率降序
        out.sort(key=lambda x: (-x["total"], -(x["winrate"] or 0.0)))
        return out

    overall = _signal_agg(raw)
    return {
        "overall": overall,
        "assets": _build("assets"),
        "directions": _build("directions"),
        "phases": _build("phases"),
        "strengths": _build("strengths"),
        "intervals": _build("intervals"),
        "combos": _build("combos"),
    }


def _interval_minutes(news_time: Optional[str], entry_ts: Optional[int]) -> Optional[int]:
    ts = _parse_ts(news_time)
    if ts is None or not entry_ts:
        return None
    return max(0, int((int(entry_ts) - ts) / 60))



def auto_review_attributions(min_confidence: float = 0.85, batch_max: int = 200,
                             only_source: Optional[str] = None, dry_run: bool = False):
    """v202 R3-fix: 批量自动通过高置信度的 pending 归因复核（human_status='pending'）。

    与 R2 同构思路：补齐"审批入口缺失"环节。

    规则：
      ① human_status='pending'
      ② confidence >= min_confidence
      ③ verdict='LOSS'（自动通过仅限失败归因，胜出归因留给人工）

    动作：
      - human_status = 'auto_reviewed'（区别于人工 'agree'/'corrected'）
      - reviewed_at = NOW()
      - 保留 corrected_code/text 为 NULL（留给人工后续纠错）
      - 不触发 rule_pushed（留给 reconcile worker）

    不会修改任何人工已审核的行（human_status != 'pending' 全部跳过）。
    """
    conn = db.get_connection()
    conn.row_factory = sqlite3.Row
    try:
        where = "human_status='pending' AND verdict='LOSS' AND confidence >= ?"
        params: List[Any] = [float(min_confidence)]
        if only_source:
            where += " AND source=?"
            params.append(str(only_source))
        rows = conn.execute(
            f"SELECT id, source, ref_id, trade_id, asset, verdict, reason_code, confidence "
            f"FROM attribution_reviews WHERE {where} ORDER BY id DESC LIMIT ?",
            (*params, int(batch_max)),
        ).fetchall()
        approved_ids: List[int] = []
        skipped_ids: List[int] = []
        for r in rows:
            cid = int(r["id"])
            try:
                conf = float(r["confidence"] or 0)
            except (TypeError, ValueError):
                conf = 0.0
            if conf < min_confidence:
                skipped_ids.append(cid)
                continue
            if not dry_run:
                cur = conn.execute(
                    "UPDATE attribution_reviews SET "
                    "human_status='auto_reviewed', reviewed_at=datetime('now','localtime'), "
                    "note = COALESCE(NULLIF(note, ''), '') || "
                    "  CASE WHEN note='' OR note IS NULL THEN '' ELSE ' | ' END || "
                    "  'R3-auto: conf=' || ? "
                    "WHERE id=? AND human_status='pending'",
                    (f"{conf:.2f}", cid),
                )
                if cur.rowcount > 0:
                    approved_ids.append(cid)
                else:
                    skipped_ids.append(cid)
            else:
                approved_ids.append(cid)
        if not dry_run:
            conn.commit()
        return {
            "scanned": len(rows),
            "approved": approved_ids,
            "skipped": skipped_ids,
            "dry_run": bool(dry_run),
            "threshold_confidence": min_confidence,
            "only_source": only_source,
        }
    finally:
        conn.close()
