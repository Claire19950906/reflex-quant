#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Reflection system -> Alert (L3.2)

v204+: 反馈层泛化, 支持所有启用错误码.
某错误码 in window ≥ threshold (策略表) → 写 alert_log 表 + 触发告警.

表 schema:
  id, ts, level, source, message, detail, resolved
  level: WARN | CRITICAL
  source: reflection_system_v204
  resolved: 0 = 未解决, 1 = 已解决

防重复: 已 resolved=0 的同 source + message 不重复写.

Usage (新):
    from reflection_alert import check_and_alert_error_code
    result = check_and_alert_error_code('E014', conn=conn)

Usage (旧, backward compat):
    from reflection_alert import check_and_alert_e020
    result = check_and_alert_e020(threshold=5, window_hours=1, conn=conn)
"""
from __future__ import annotations
import sqlite3
import json
from datetime import datetime
from typing import Any, Dict, List, Optional

import db
import kg_feedback as kgf
import reflection_feedback_config as rfc


ALERT_TABLE = "reflection_alert_log"
DEFAULT_THRESHOLD = 5
DEFAULT_WINDOW_HOURS = 1


def ensure_alert_table(conn=None) -> None:
    """新建/确保 alert_log 表存在 (idempotent)"""
    own = conn is None
    if own:
        conn = db.get_connection()
    try:
        conn.execute("""
      CREATE TABLE IF NOT EXISTS """ + ALERT_TABLE + """ (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        ts TEXT NOT NULL,
        level TEXT NOT NULL,
        source TEXT NOT NULL,
        message TEXT NOT NULL,
        detail TEXT NOT NULL DEFAULT '{}',
        resolved INTEGER NOT NULL DEFAULT 0,
        resolved_at TEXT,
        resolved_note TEXT NOT NULL DEFAULT ''
      )
    """)
        conn.commit()
    finally:
        # v189-8: 自建连接必须自闭, 否则调用方不传 conn 时每次调用泄漏一个 fd。
        if own:
            conn.close()


def _count_e020_in_window(window_hours, conn=None):
    """DEPRECATED wrapper: use kgf._count_error_code_in_window('E020', window_hours, conn)."""
    return kgf._count_error_code_in_window('E020', window_hours, conn=conn)


def _has_unresolved_alert(message_prefix, conn=None):
    own = conn is None
    if own:
        conn = db.get_connection()
    try:
        row = conn.execute(
            "SELECT id FROM " + ALERT_TABLE + " WHERE message LIKE ? AND resolved = 0 ORDER BY id DESC LIMIT 1",
            (message_prefix + "%",)
        ).fetchone()
        return row is not None
    finally:
        if own:
            conn.close()


def _by_asset_action(error_code='E020', window_hours=1, conn=None):
    """统计 window_hours 小时内 {error_code} 触发的决策按 (asset, action) 分布."""
    own = conn is None
    if own:
        conn = db.get_connection()
    try:
        rows = conn.execute("""
      SELECT d.target_asset, d.suggested_action, COUNT(DISTINCT d.id) AS n
      FROM decision_errors e JOIN ai_decisions d ON d.id = e.decision_id
      WHERE e.error_code = ?
        AND datetime(e.created_at) >= datetime('now', '-' || ? || ' hours')
      GROUP BY d.target_asset, d.suggested_action ORDER BY n DESC
    """, (error_code, window_hours)).fetchall()
        out = {}
        for r in rows:
            try:
                asset = r['target_asset'] or 'UNKNOWN'
                action = r['suggested_action'] or 'UNKNOWN'
                n = r['n']
            except (TypeError, KeyError, IndexError):
                asset = r[0] or 'UNKNOWN'
                action = r[1] or 'UNKNOWN'
                n = r[2]
            out[asset + "_" + action] = n
        return out
    finally:
        if own:
            conn.close()


def _write_alert(level, message, detail, source='reflection_system_v204', conn=None):
    own = conn is None
    if own:
        conn = db.get_connection()
    try:
        cur = conn.execute(
            "INSERT INTO " + ALERT_TABLE + " (ts, level, source, message, detail, resolved) VALUES (?, ?, ?, ?, ?, 0)",
            (datetime.now().isoformat(timespec='seconds') + 'Z',
             level, source, message,
             json.dumps(detail, ensure_ascii=False))
        )
        conn.commit()
        return cur.lastrowid
    finally:
        if own:
            conn.close()


def _auto_resolve(message_prefix, note, conn=None):
    own = conn is None
    if own:
        conn = db.get_connection()
    try:
        cur = conn.execute(
            "UPDATE " + ALERT_TABLE + " SET resolved = 1, resolved_at = ?, resolved_note = ? WHERE message LIKE ? AND resolved = 0",
            (datetime.now().isoformat(timespec='seconds') + 'Z',
             note, message_prefix + '%')
        )
        conn.commit()
        return cur.rowcount
    finally:
        if own:
            conn.close()


def check_and_alert_e020(threshold=DEFAULT_THRESHOLD,
                         window_hours=DEFAULT_WINDOW_HOURS,
                         conn=None):
    """DEPRECATED wrapper: use check_and_alert_error_code('E020', ...)."""
    return check_and_alert_error_code(
        'E020', threshold=threshold, window_hours=window_hours, conn=conn)


def check_and_alert_error_code(error_code: str,
                                threshold: Optional[int] = None,
                                window_hours: Optional[int] = None,
                                conn=None) -> Dict[str, Any]:
    """主入口 (v204+ 泛化): 检查 {error_code} 累计, 超过阈值则写告警.

    Args:
        error_code: 错误码
        threshold: alert 阈值 (None = 从策略表读 alert_threshold)
        window_hours: 时间窗口 (None = 从策略表读 alert_window_hours)
        conn: 数据库连接
    """
    ensure_alert_table(conn=conn)
    policy = rfc.get_policy(error_code) or {}
    if threshold is None:
        threshold = policy.get('alert_threshold', DEFAULT_THRESHOLD)
    if window_hours is None:
        window_hours = policy.get('alert_window_hours', DEFAULT_WINDOW_HOURS)

    n = kgf._count_error_code_in_window(error_code, window_hours, conn=conn)
    message_prefix = '[' + error_code + ' 事故样本累计]'
    has_pending = _has_unresolved_alert(message_prefix, conn=conn)
    by_aa = _by_asset_action(error_code=error_code, window_hours=window_hours, conn=conn)
    result = {
        'error_code': error_code,
        'count_in_window': n,
        'threshold': threshold,
        'window_hours': window_hours,
        'has_unresolved_alert': has_pending,
        'alerted': False,
        'alert_id': None,
        'level': '',
        'reason': '',
        'asset_action_distribution': by_aa,
    }
    if n < threshold:
        result['reason'] = '未达阈值 (' + str(n) + ' < ' + str(threshold) + ')'
        if has_pending:
            _auto_resolve(message_prefix, error_code + ' 已回落到阈值以下', conn=conn)
            result['reason'] += ', 已自动 resolve 之前的告警'
        return result
    if has_pending:
        result['reason'] = '已有未解决告警, 不重复'
        return result
    level = 'CRITICAL' if n >= threshold * 2 else 'WARN'
    message = message_prefix + ' ' + str(n) + ' 笔 / ' + str(window_hours) + 'h 窗口. 分布: ' + str(by_aa)
    detail = {
        'error_code': error_code,
        'count_in_window': n,
        'window_hours': window_hours,
        'asset_action_distribution': by_aa,
        'kg_proposal_status': 'pending (auto-submitted by reflection_system_v204)',
    }
    alert_id = _write_alert(level, message, detail, conn=conn)
    result['alerted'] = True
    result['alert_id'] = alert_id
    result['level'] = level
    result['reason'] = '已写告警 id=' + str(alert_id)
    return result


def print_alert_report(result):
    """打印告警报告 (兼容 v202 老格式 + v204 新格式)."""
    print('=' * 80)
    code = result.get('error_code') or 'E020'
    print('[L3.2 reflection_alert] {} -> alert_log'.format(code))
    print('=' * 80)
    print('threshold:', result.get('threshold', 0), '笔 /', result.get('window_hours', 0), 'h 窗口')
    count = result.get('count_in_window', result.get('e020_count_in_window', 0))
    print('{} in window:'.format(code), count)
    print('has_unresolved_alert:', result.get('has_unresolved_alert', False))
    print('alerted:', result.get('alerted', False))
    if result.get('alerted'):
        print('alert_id:', result.get('alert_id'), 'level:', result.get('level'))
    print('reason:', result.get('reason', ''))
    if result.get('asset_action_distribution'):
        print('asset_action distribution:')
        for k, v in sorted(result['asset_action_distribution'].items()):
            print(' ', k + ':', v)
    print('=' * 80)


if __name__ == "__main__":
    import sys
    sys.path.insert(0, '.')
    conn = db.get_connection()
    result = check_and_alert_e020(threshold=5, window_hours=1, conn=conn)
    print_alert_report(result)