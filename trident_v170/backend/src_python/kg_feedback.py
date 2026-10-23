#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Reflection system -> KG Improvements (L2.3)

v204+: 反馈层泛化, 支持所有启用错误码 (默认 E004/5/14/15/16/17/18/20).

某错误码累计超阈值 (按策略表) → 自动提交 kg_improvements proposal:
  kind='add_rule', target='reflection_checkers',
  reason='{CODE} 事故样本累计超阈值',
  payload='{"rule_code": "{CODE}", "applies_to": [...], ...}',
  status='pending' → 人工审批后由 reconcile worker 应用

这是 v202+ 反思系统主动反馈给 KG 的能力.
不动 kg_improvements schema, 沿用现有 KG proposal API.

策略 (各码的阈值/applies_to) 见 reflection_feedback_config.DEFAULT_FEEDBACK_POLICY.

Usage (新):
    from kg_feedback import check_and_submit_error_code_proposal
    result = check_and_submit_error_code_proposal('E014', conn=conn)

Usage (旧, backward compat):
    from kg_feedback import check_and_submit_e020_proposal
    result = check_and_submit_e020_proposal(threshold=5, window_hours=1, conn=conn)
"""
from __future__ import annotations
import sqlite3
import json
from datetime import datetime
from typing import Any, Dict, List, Optional

import db
import reflection_feedback_config as rfc


# backward compat: 老模块仍在引用此常量
E020_RULE_CODE = "E020"
DEFAULT_THRESHOLD = 5        # 默认阈值: 1 小时 ≥ 5 笔
DEFAULT_WINDOW_HOURS = 1    # 默认窗口: 1 小时


def _count_e020_in_window(window_hours: int, conn=None) -> int:
    """DEPRECATED wrapper: use _count_error_code_in_window('E020', window_hours, conn)."""
    return _count_error_code_in_window('E020', window_hours, conn=conn)


def _count_error_code_in_window(error_code: str, window_hours: int,
                                conn=None) -> int:
    """查 decision_errors 表, 统计最近 window_hours 小时内 {error_code} 触发的决策数"""
    own = conn is None
    if own:
        conn = db.get_connection()
    try:
        row = conn.execute("""
          SELECT COUNT(DISTINCT decision_id) FROM decision_errors
          WHERE error_code = ?
            AND datetime(created_at) >= datetime('now', '-' || ? || ' hours')
        """, (error_code, window_hours)).fetchone()
        return row[0] if row else 0
    finally:
        # v189-8: 自建连接必须自闭, 否则每次调用泄漏一个 fd。
        if own:
            conn.close()


def _has_pending_proposal(rule_code: str, conn=None) -> bool:
    """查 kg_improvements 是否已有同 rule_code 的未处理 proposal (status=pending)"""
    own = conn is None
    if own:
        conn = db.get_connection()
    try:
        # payload 是 JSON 字符串, LIKE '%E020%' 简单判定 (避免 json_extract 兼容性问题)
        pattern = '%"rule_code": "{}"%'.format(rule_code)
        row = conn.execute("""
          SELECT id FROM kg_improvements
          WHERE kind = 'add_rule'
            AND status = 'pending'
            AND payload LIKE ?
        """, (pattern,)).fetchone()
        return row is not None
    finally:
        if own:
            conn.close()


def _build_proposal_payload(error_code: str, sample_size: int,
                            window_hours: int) -> Dict[str, Any]:
    """根据策略表构造 KG proposal payload. 不同错误码的 applies_to 不同."""
    policy = rfc.get_policy(error_code) or {}
    description = policy.get('description', error_code) + \
        " 累计超阈值, 建议在对应模块加固防御"
    applies_to = policy.get('applies_to', ["reflection_checkers:" + error_code])
    suggested_action = "在 {} 触发点加防御, 减少同类事故".format(error_code)
    if error_code == 'E020':
        # 向后兼容: 保留 v202 措辞
        description = "E020 事故样本累计超阈值, 建议在 analysis_layer / verifier / executor 加 E020 防御"
        suggested_action = "在 trade 入口加 E020 防御, 拒绝 gate fail / HOLD / pnl=0 / allowlist 外的 trade 进入 alpha 评估"
    return {
        "rule_code": error_code,
        "description": description,
        "applies_to": applies_to,
        "evidence_window_hours": window_hours,
        "evidence_sample_size": sample_size,
        "suggested_action": suggested_action,
        "feedback_level": policy.get('feedback_level', 'unknown'),
    }


def submit_e020_proposal(sample_size: int, window_hours: int,
                         conn=None) -> Dict[str, Any]:
    """DEPRECATED wrapper: use submit_error_code_proposal('E020', sample_size, window_hours, conn)."""
    return submit_error_code_proposal('E020', sample_size, window_hours, conn=conn)


def submit_error_code_proposal(error_code: str, sample_size: int,
                                window_hours: int,
                                conn=None) -> Dict[str, Any]:
    """提交 {error_code} add_rule proposal 到 kg_improvements (status=pending).

    Returns: { 'submitted': bool, 'id': int, 'reason': str }
    """
    own = conn is None
    if own:
        conn = db.get_connection()
    try:
        payload = _build_proposal_payload(error_code, sample_size, window_hours)
        now_iso = datetime.now().isoformat(timespec='seconds') + 'Z'

        cur = conn.execute("""
          INSERT INTO kg_improvements
            (kind, target, reason, confidence, sample_size, impact, payload,
             status, source, first_seen_at, last_seen_at)
          VALUES
            (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            'add_rule',
            'reflection_checkers',
            '{} 事故样本累计 {} 笔 ({}h 窗口)'.format(error_code, sample_size, window_hours),
            0.85,
            sample_size,
            'medium',
            json.dumps(payload),
            'pending',
            'reflection_system_v204',
            now_iso,
            now_iso,
        ))
        conn.commit()
        return {
            "submitted": True,
            "id": cur.lastrowid,
            "reason": "OK",
        }
    finally:
        if own:
            conn.close()


def check_and_submit_e020_proposal(threshold: int = DEFAULT_THRESHOLD,
                                   window_hours: int = DEFAULT_WINDOW_HOURS,
                                   conn=None) -> Dict[str, Any]:
    """DEPRECATED wrapper: use check_and_submit_error_code_proposal('E020', ...)."""
    return check_and_submit_error_code_proposal(
        'E020', threshold=threshold, window_hours=window_hours, conn=conn)


def check_and_submit_error_code_proposal(error_code: str,
                                          threshold: Optional[int] = None,
                                          window_hours: Optional[int] = None,
                                          conn=None) -> Dict[str, Any]:
    """主入口 (v204+ 泛化): 检查 {error_code} 累计, 超过阈值则提交 KG proposal.

    Args:
        error_code: 错误码 (e.g. 'E014')
        threshold: KG 触发阈值 (None = 从策略表读 kg_threshold)
        window_hours: 时间窗口 (None = 从策略表读 kg_window_hours)
        conn: 数据库连接

    Returns:
        {
            'error_code': str,
            'count_in_window': int,
            'threshold': int,
            'window_hours': int,
            'has_pending_proposal': bool,
            'submitted': bool,
            'proposal_id': int or None,
            'reason': str,
        }
    """
    policy = rfc.get_policy(error_code) or {}
    if threshold is None:
        threshold = policy.get('kg_threshold', DEFAULT_THRESHOLD)
    if window_hours is None:
        window_hours = policy.get('kg_window_hours', DEFAULT_WINDOW_HOURS)

    n = _count_error_code_in_window(error_code, window_hours, conn=conn)
    has_pending = _has_pending_proposal(error_code, conn=conn)

    result = {
        "error_code": error_code,
        "count_in_window": n,
        "threshold": threshold,
        "window_hours": window_hours,
        "has_pending_proposal": has_pending,
        "submitted": False,
        "proposal_id": None,
        "reason": "",
    }

    if n < threshold:
        result["reason"] = "未达阈值 ({} < {})".format(n, threshold)
        return result

    if has_pending:
        result["reason"] = "已有 pending proposal, 不重复提交"
        return result

    submit = submit_error_code_proposal(error_code,
                                         sample_size=n,
                                         window_hours=window_hours,
                                         conn=conn)
    result.update(submit)
    return result


def print_feedback_report(result: Dict[str, Any]) -> None:
    """打印反馈报告 (兼容 v202 老格式 + v204 新格式)."""
    print('=' * 80)
    code = result.get('error_code') or 'E020'
    print('[L2.3 kg_feedback] {} → kg_improvements'.format(code))
    print('=' * 80)
    print('threshold: {} 笔 / {}h 窗口'.format(result.get('threshold', 0),
                                              result.get('window_hours', 0)))
    count = result.get('count_in_window', result.get('e020_count_in_window', 0))
    print('{} in window: {}'.format(code, count))
    print('has_pending_proposal: {}'.format(result.get('has_pending_proposal', False)))
    print('submitted: {}'.format(result.get('submitted', False)))
    if result.get('submitted'):
        print('proposal_id: {}'.format(result.get('proposal_id')))
    print('reason: {}'.format(result.get('reason', '')))
    print('=' * 80)


if __name__ == "__main__":
    import sys
    sys.path.insert(0, '.')
    conn = db.get_connection()
    result = check_and_submit_e020_proposal(threshold=5, window_hours=1,
                                            conn=conn)
    print_feedback_report(result)