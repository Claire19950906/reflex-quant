#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""v204+ L2/L3 反馈层泛化配置.

每个 error_code 一个反馈策略, 由 reflection_scheduler 统一遍历:

  - enabled: 是否启用自动反馈 (False = 不参与 L2/L3 周期)
  - feedback_level: critical / high / medium / low / none
      * critical = 立即反馈 (低阈值, 强 veto)
      * high     = 常规反馈 (中阈值, 中 veto)
      * medium   = 默认反馈 (高阈值, 弱 veto, opt-in)
      * low      = 默认关闭, 按需开启
      * none     = 不反馈
  - veto_insights: bool (True = 设 active=0; False = 只标 note)
  - kg_threshold / kg_window_hours: KG proposal 触发阈值
  - alert_threshold / alert_window_hours: alert 触发阈值
  - layer / sub_layer: decision_layer_signals 映射
  - applies_to: KG proposal payload.applies_to 列表
  - description: 错误码中文名 (供 UI / 日志)

设计原则:
  1. **事件逻辑层 (event_logic) 是最高优先级** — E014/15/16 是 v189 后反思系统主要工作,
     但 v202/v203 的 L2 反馈层只对 E020 闭环。v204 把所有 critical/high 错误码全部接入。
  2. **veto 强度分档** — E020 是"事故样本"必须 veto, E014 是"推理链不完整"标 note 即可。
  3. **阈值分档** — critical 码阈值低 (3/h 触发), medium 码阈值高 (10/h 触发)。
  4. **不破坏 backward compat** — 老的 hardcode E020 函数全部保留为 wrapper.

调用:
  from reflection_feedback_config import (
      DEFAULT_FEEDBACK_POLICY, get_policy, get_enabled_codes, get_codes_at_level,
  )

改动历史:
  - v204 (2026-10-05): 初版. 接入 E001-E020 全套, 默认启用 8 个 high/critical 码.
"""
from __future__ import annotations
from typing import Any, Dict, List, Optional


def _p(enabled, level, veto, kg_t, kg_w, alert_t, alert_w,
       layer, sub_layer, applies_to, description):
    """策略表行构造函数 (压缩重复字段)."""
    return {
        'enabled': enabled, 'feedback_level': level, 'veto_insights': veto,
        'kg_threshold': kg_t, 'kg_window_hours': kg_w,
        'alert_threshold': alert_t, 'alert_window_hours': alert_w,
        'layer': layer, 'sub_layer': sub_layer,
        'applies_to': applies_to, 'description': description,
    }


# 默认关闭的码 (medium/low 档, 不自动反馈, 等用户显式 enable):
#   E001 数据缺失 / E002 数据过期 / E003 逻辑矛盾 / E007 市场反向
#   E009 置信度虚高 / E010 抢跑风险 / E019 字段缺失
# 默认启用的码 (critical/high 档, 自动反馈):
#   E004 因果断裂 / E005 规则冲突 / E014 推理链断裂
#   E015 反共识缺位 / E016 跨资产方向错 / E017 跨资产无据传导
#   E018 历史反例不符 / E020 样本质量事故

DEFAULT_FEEDBACK_POLICY: Dict[str, Dict[str, Any]] = {
    # === data 层 ===
    'E001': _p(False, 'medium', False, 10, 6, 15, 6, 'data', 'missing',
                ['reflection_checkers:E001', 'ai_prompts:hard_data_evidence'], '数据缺失'),
    'E002': _p(False, 'medium', False, 10, 6, 15, 6, 'data', 'stale',
                ['reflection_checkers:E002', 'ai_prompts:data_anchor_ts'], '数据过期'),
    # === logic 层 ===
    'E003': _p(False, 'medium', False, 8, 3, 12, 3, 'logic', 'contradict',
                ['reflection_checkers:E003', 'ai_prompts:factor_ranking'], '逻辑矛盾'),
    # === causal 层 ===
    'E004': _p(True, 'high', True, 5, 1, 8, 1, 'causal', 'broken',
                ['reflection_checkers:E004', 'causal_graph:asset_driver_nodes'], '因果断裂'),
    # === rule 层 ===
    'E005': _p(True, 'high', False, 5, 1, 10, 1, 'rule', 'violation',
                ['reflection_checkers:E005', 'review_insights:active'], '规则冲突'),
    # === market 层 ===
    'E007': _p(False, 'medium', False, 10, 6, 15, 6, 'market', 'reverse',
                ['reflection_checkers:E007'], '市场反向'),
    'E009': _p(False, 'medium', False, 8, 3, 12, 3, 'logic', 'confidence',
                ['reflection_checkers:E009', 'ai_prompts:confidence'], '置信度虚高'),
    'E010': _p(False, 'medium', False, 8, 3, 12, 3, 'rule', 'gate',
                ['reflection_checkers:E010', 'decision_guard:gate_verdict'], '抢跑风险'),
    # === event_logic 层 (v189 最高权重) — v204 起全部接入 ===
    'E014': _p(True, 'critical', True, 3, 1, 5, 1, 'event_logic', 'reasoning_broken',
                ['reflection_checkers:E014', 'ai_prompts:reasoning_path'], '推理链断裂'),
    'E015': _p(True, 'critical', True, 3, 1, 5, 1, 'event_logic', 'consensus_missing',
                ['reflection_checkers:E015', 'causal_graph:asset_driver_nodes'], '反共识缺位'),
    'E016': _p(True, 'critical', True, 3, 1, 5, 1, 'event_logic', 'cross_asset_wrong',
                ['reflection_checkers:E016', 'ai_prompts:cross_asset_likely'], '跨资产方向错'),
    'E017': _p(True, 'high', False, 5, 1, 8, 1, 'event_logic', 'cross_asset_unverified',
                ['reflection_checkers:E017'], '跨资产无据传导'),
    'E018': _p(True, 'high', True, 5, 1, 8, 1, 'event_logic', 'historical_counterfactual',
                ['reflection_checkers:E018', 'event_atom:type_code_history'], '历史反例不符'),
    # === completeness 层 ===
    'E019': _p(False, 'medium', False, 10, 6, 15, 6, 'completeness', 'field_missing',
                ['reflection_checkers:E019'], '字段缺失'),
    # === sample 层 (v202+) — 默认保持 v202/v203 行为 ===
    'E020': _p(True, 'critical', True, 5, 1, 10, 1, 'data', 'sample_integrity',
                ['reflection_checkers:check_sample_integrity',
                 'run_reflection:run(require_min_clean)',
                 'verifier:feedback(E020)'], '样本质量事故'),
}


# ---------------------------------------------------------------------------
# 辅助函数
# ---------------------------------------------------------------------------

def get_policy(error_code: str) -> Optional[Dict[str, Any]]:
    """单个错误码的反馈策略; 未知码返回 None."""
    return DEFAULT_FEEDBACK_POLICY.get(error_code)


def get_enabled_codes() -> List[str]:
    """所有启用了反馈的错误码 (enabled=True, feedback_level != 'none')."""
    return [
        c for c, p in DEFAULT_FEEDBACK_POLICY.items()
        if p.get('enabled', False) and p.get('feedback_level', 'none') != 'none'
    ]


def get_codes_at_level(level: str) -> List[str]:
    """特定 feedback_level 的错误码 (用于按档位调度)."""
    return [
        c for c, p in DEFAULT_FEEDBACK_POLICY.items()
        if p.get('feedback_level') == level
    ]


def get_layer_signal(error_code: str) -> tuple:
    """(layer, sub_layer, signal_name) 三元组; 用于 analysis_layer_feedback.

    signal_name 固定为 error_code 本身 (便于按码查询).
    """
    policy = get_policy(error_code)
    if not policy:
        return ('unknown', 'unknown', error_code)
    return (
        policy.get('layer', 'unknown'),
        policy.get('sub_layer', 'unknown'),
        error_code,
    )


def healthcheck() -> Dict[str, Any]:
    """健康检查: 返回策略表统计."""
    total = len(DEFAULT_FEEDBACK_POLICY)
    enabled = get_enabled_codes()
    by_level: Dict[str, int] = {}
    for c in enabled:
        lvl = DEFAULT_FEEDBACK_POLICY[c].get('feedback_level', 'none')
        by_level[lvl] = by_level.get(lvl, 0) + 1
    return {
        'ok': True,
        'module': 'reflection_feedback_config',
        'version': 'v204',
        'total_codes': total,
        'enabled_codes': len(enabled),
        'enabled_list': enabled,
        'by_level': by_level,
    }


if __name__ == "__main__":
    import sys, json
    sys.path.insert(0, '.')
    print(json.dumps(healthcheck(), ensure_ascii=False, indent=2))

