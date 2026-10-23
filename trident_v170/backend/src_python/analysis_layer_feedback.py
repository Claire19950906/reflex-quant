#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Reflection system -> Analysis Layer (L2.2)

v204+: 反馈层泛化, 支持所有启用错误码.
某错误码触发 → 写 decision_layer_signals 表
  layer=策略表 layer, sub_layer=策略表 sub_layer, signal_name={CODE}

不同错误码的 layer/sub_layer 由 reflection_feedback_config.get_layer_signal(code) 提供:
  E020 → ('data', 'sample_integrity', 'E020')
  E014 → ('event_logic', 'reasoning_broken', 'E014')
  E016 → ('event_logic', 'cross_asset_wrong', 'E016')
  ...

这是 v202+ 反思系统主动反馈给 analysis_layer 的能力.
不动 analysis_layer schema, 沿用现有决策层信号表.

Usage (新):
    from analysis_layer_feedback import record_error_code_to_layer_signals
    n = record_error_code_to_layer_signals('E014', conn=conn)

Usage (旧, backward compat):
    from analysis_layer_feedback import record_e020_to_layer_signals
    n = record_e020_to_layer_signals(conn=conn)  # = record_error_code_to_layer_signals('E020')
"""
from __future__ import annotations
import sqlite3
from typing import Any, Dict, List, Optional

import reflection_checkers as rc
import reflection_feedback_config as rfc
import db


# backward compat: 老模块仍在引用此常量
E020_LAYER = "data"
E020_SUB_LAYER = "sample_integrity"
E020_SIGNAL_NAME = "E020"


def _fetch_e020_decisions(limit: int = 500, conn=None) -> List[int]:
    """DEPRECATED wrapper: use _fetch_error_code_decisions('E020', limit, conn)."""
    return _fetch_error_code_decisions('E020', limit=limit, conn=conn)


def _fetch_error_code_decisions(error_code: str, limit: int = 500,
                                 conn=None) -> List[int]:
    """查 decision_errors 表, 取最近 N 条 {error_code} 触发的 decision_id"""
    own = conn is None
    if own:
        conn = db.get_connection()
    try:
        rows = conn.execute("""
          SELECT DISTINCT decision_id FROM decision_errors
          WHERE error_code = ? ORDER BY decision_id DESC LIMIT ?
        """, (error_code, limit)).fetchall()
        return [r[0] for r in rows]
    finally:
        # v189-8: 自建连接必须自闭。
        if own:
            conn.close()


def _fetch_decision_info(decision_id: int, conn=None) -> Optional[Dict[str, Any]]:
    """查 ai_decisions 取 asset/action"""
    own = conn is None
    if own:
        conn = db.get_connection()
    try:
        row = conn.execute("""
          SELECT id, target_asset, suggested_action FROM ai_decisions WHERE id = ?
        """, (decision_id,)).fetchone()
        if row is None:
            return None
        return {
            "decision_id": row[0],
            "asset": row[1],
            "action": row[2],
        }
    finally:
        # v189-8: 自建连接必须自闭。row is None 的提前 return 也走 finally。
        if own:
            conn.close()


def _upsert_layer_signal_for_code(error_code: str, asset: str, action: str,
                                   decision_id: int, conn=None) -> int:
    """upsert 到 decision_layer_signals 表 (按 layer+sub_layer+signal_name+asset+action 五元组 unique).

    layer/sub_layer/signal_name 从策略表取.

    Returns: 该行的 n_total
    """
    own = conn is None
    if own:
        conn = db.get_connection()
    try:
        layer, sub_layer, signal_name = rfc.get_layer_signal(error_code)

        existing = conn.execute("""
          SELECT id, n_total, n_loss, n_win FROM decision_layer_signals
          WHERE layer = ? AND sub_layer = ? AND signal_name = ?
            AND asset = ? AND action = ?
        """, (layer, sub_layer, signal_name, asset, action)).fetchone()

        if existing:
            # UPDATE: n_total += 1, last_decision_id 更新
            new_total = existing[1] + 1
            conn.execute("""
              UPDATE decision_layer_signals
              SET n_total = ?, last_seen_ts = strftime('%s','now'),
                  last_decision_id = ?, updated_at = strftime('%Y-%m-%dT%H:%M:%fZ','now')
              WHERE id = ?
            """, (new_total, decision_id, existing[0]))
            # 仅自建连接时提交: 调用方传入的 conn 由调用方控制事务边界。
            # 自建连接若不提交, close() 会回滚导致写入丢失。
            if own:
                conn.commit()
            return new_total
        else:
            # INSERT: 新行
            conn.execute("""
              INSERT INTO decision_layer_signals
                (layer, sub_layer, signal_name, asset, action,
                 n_total, n_loss, n_win, last_seen_ts, last_decision_id, updated_at)
              VALUES
                (?, ?, ?, ?, ?, 1, 0, 0, strftime('%s','now'), ?, strftime('%Y-%m-%dT%H:%M:%fZ','now'))
            """, (layer, sub_layer, signal_name, asset, action, decision_id))
            if own:
                conn.commit()
            return 1
    finally:
        # v189-8: 自建连接必须自闭。
        if own:
            conn.close()


def _upsert_layer_signal(asset: str, action: str, decision_id: int,
                         conn=None) -> int:
    """DEPRECATED wrapper: use _upsert_layer_signal_for_code('E020', asset, action, decision_id, conn)."""
    return _upsert_layer_signal_for_code('E020', asset, action, decision_id, conn=conn)


def record_e020_to_layer_signals(limit: int = 500, conn=None) -> Dict[str, Any]:
    """DEPRECATED wrapper: use record_error_code_to_layer_signals('E020', limit, conn)."""
    return record_error_code_to_layer_signals('E020', limit=limit, conn=conn)


def record_error_code_to_layer_signals(error_code: str, limit: int = 500,
                                        conn=None) -> Dict[str, Any]:
    """主入口 (v204+ 泛化): 把最近 {error_code} 触发的 decision 写到 decision_layer_signals 表.

    Returns:
        {
            'error_code': str,
            'layer': str,
            'sub_layer': str,
            'signal_name': str,
            'decisions_found': int,
            'signals_upserted': int,
            'by_asset_action': { 'XAU_BUY': 5, ... },
            'max_n_total': int,
        }
    """
    layer, sub_layer, signal_name = rfc.get_layer_signal(error_code)
    decision_ids = _fetch_error_code_decisions(error_code, limit=limit, conn=conn)
    by_aa: Dict[str, int] = {}
    max_total = 0
    upserts = 0

    for did in decision_ids:
        info = _fetch_decision_info(did, conn=conn)
        if info is None:
            continue
        asset = info["asset"] or "UNKNOWN"
        action = info["action"] or "UNKNOWN"
        key = "{}_{}".format(asset, action)
        n = _upsert_layer_signal_for_code(error_code, asset, action, did, conn=conn)
        by_aa[key] = by_aa.get(key, 0) + 1
        max_total = max(max_total, n)
        upserts += 1

    if conn is not None:
        conn.commit()

    return {
        "error_code": error_code,
        "layer": layer,
        "sub_layer": sub_layer,
        "signal_name": signal_name,
        "decisions_found": len(decision_ids),
        "signals_upserted": upserts,
        "by_asset_action": by_aa,
        "max_n_total": max_total,
    }


def print_feedback_report(result: Dict[str, Any]) -> None:
    """打印反馈报告 (兼容 v202 老格式 + v204 新格式)."""
    print('=' * 80)
    code = result.get('error_code') or 'E020'
    print('[L2.2 analysis_layer_feedback] {} → decision_layer_signals'.format(code))
    print('=' * 80)
    print('layer={} sub_layer={} signal_name={}'.format(
        result.get('layer', E020_LAYER),
        result.get('sub_layer', E020_SUB_LAYER),
        result.get('signal_name', E020_SIGNAL_NAME),
    ))
    found = result.get('decisions_found', result.get('e020_decisions_found', 0))
    print('{} decisions found: {}'.format(code, found))
    print('signals upserted: {}'.format(result.get('signals_upserted', 0)))
    if result.get('by_asset_action'):
        print('by asset_action:')
        for k, v in sorted(result['by_asset_action'].items()):
            print('  {}: {}'.format(k, v))
    print('max n_total: {}'.format(result.get('max_n_total', 0)))
    print('=' * 80)


if __name__ == "__main__":
    import sys
    sys.path.insert(0, '.')
    conn = db.get_connection()
    result = record_e020_to_layer_signals(limit=500, conn=conn)
    print_feedback_report(result)