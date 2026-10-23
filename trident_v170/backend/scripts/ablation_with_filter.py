#!/usr/bin/env python3
"""Ablation Wrapper: 统一接入 sample_quality_filter 的入口

L1.1: 任何 ablation 跑之前必须先调 audit_trades(),
      清洁样本 < 30 时拒绝出 alpha 结论 (柔性: print 警告, 仍跑但不计入 alpha 评估)

Usage:
    from ablation_with_filter import run_audit
    audit = run_audit(asset='XAU', action='BUY')
    clean_trade_ids = audit['clean_trade_ids']
    n_valid = audit['n_valid']
    if audit['alpha_allowed']:
        # 算 alpha...
    else:
        # 拒绝出结论
"""
import os, sys
THIS_DIR = os.path.dirname(os.path.abspath(__file__))
BACKEND = os.path.dirname(THIS_DIR)
sys.path.insert(0, os.path.join(BACKEND, 'src_python'))

import sample_quality_filter as sqf


def run_audit(asset=None, action=None, min_required=30, conn=None,
              allowed_assets=sqf.DEFAULT_SIM_ALLOWED_ASSETS,
              min_confidence=sqf.DEFAULT_MIN_CONFIDENCE) -> dict:
    """统一入口: 跑 audit + 检查硬门槛

    Returns:
        {
            'total': int, 'valid': int, 'invalid': int,
            'clean_trade_ids': [int],
            'invalid_trade_ids': [int],
            'by_reason': dict, 'by_flag': dict,
            'gate': {'allowed': bool, 'reason': str, 'recommendation': str},
            'alpha_allowed': bool,
        }
    """
    audit = sqf.audit_trades(
        asset=asset, action=action,
        allowed_assets=allowed_assets,
        min_confidence=min_confidence,
        conn=conn,
    )
    gate = sqf.require_min_clean_samples(audit['valid'], min_required=min_required)
    clean_ids = [s['trade_id'] for s in audit['samples'] if s['is_valid_for_alpha']]
    invalid_ids = [s['trade_id'] for s in audit['samples'] if not s['is_valid_for_alpha']]

    return {
        'total': audit['total'],
        'valid': audit['valid'],
        'invalid': audit['invalid'],
        'clean_trade_ids': clean_ids,
        'invalid_trade_ids': invalid_ids,
        'by_reason': audit['by_reason'],
        'by_flag': audit['by_flag'],
        'gate': gate,
        'alpha_allowed': gate['allowed'],
    }


def print_audit_report(audit_result: dict, label: str = '') -> None:
    """打印 audit 报告 (任何 ablation 跑之前先 print)"""
    a = audit_result
    print('=' * 80)
    print('[Ablation Wrapper] 样本质量 audit {}'.format(label))
    print('=' * 80)
    print('total: {} trades'.format(a['total']))
    print('valid: {} (5 条硬规则全过)'.format(a['valid']))
    print('invalid: {}'.format(a['invalid']))
    if a['by_reason']:
        print('by_reason:')
        for k in sorted(a['by_reason'].keys()):
            v = a['by_reason'][k]
            rule_desc = {
                'R1': 'gate_passed=0', 'R2': 'evidence=HOLD',
                'R3': 'pnl=0', 'R4': 'not_in_allowlist',
                'R7': 'confidence<40',
            }.get(k, k)
            print('  {} ({}): {}'.format(k, rule_desc, v))
    print('gate: allowed={}'.format(a['gate']['allowed']))
    print('  reason: {}'.format(a['gate']['reason']))
    if not a['gate']['allowed']:
        print('  ⚠️ alpha 评估被硬门槛拒绝')
        print('  recommendation: {}'.format(a['gate']['recommendation']))
    print('=' * 80)