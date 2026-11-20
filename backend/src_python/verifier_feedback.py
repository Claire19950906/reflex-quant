#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Reflection system -> Verifier (L2.1)

v204+: 反馈层泛化, 支持所有启用错误码 (默认 E004/5/14/15/16/17/18/20).
触发某 error_code → 找关联的 review_insight → 标记 note='{CODE}_CONTAMINATED' + active=0 (veto)

这是 v202+ 反思系统主动反馈给 verifier 的能力.
不动 verifier 逻辑, 沿用现有 review_insights 表的 active=0 (verifier 默认 active_only=True 会跳过).

工作流程:
  1. 扫 decision_errors 表所有 {error_code} triggered decision
  2. 对每个 decision 找关联 insight: asset 一致 AND created_at 距离 decision 时间 ±24h 内
  3. 给关联 insight 写 note + (veto 时) 设 active=0
  4. verifier 跑的时候自然跳过 active=0 insight (现有逻辑)

策略 (哪个码要 veto, 哪个只标 note) 见 reflection_feedback_config.DEFAULT_FEEDBACK_POLICY.

Usage (新):
    from verifier_feedback import apply_error_code_to_insights
    n = apply_error_code_to_insights('E014', conn=conn)

Usage (旧, backward compat):
    from verifier_feedback import apply_e020_to_insights
    n = apply_e020_to_insights(conn=conn)  # 等价于 apply_error_code_to_insights('E020')
"""
from __future__ import annotations
import sqlite3
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional, Set

import db
from review import reflection_feedback_config as rfc


# backward compat: 老模块仍在引用此常量
E020_NOTE_PREFIX = "E020_CONTAMINATED: "
DECISION_TO_INSIGHT_HOURS = 24  # decision 时间 ±24h 内的 insight 算关联


def _note_prefix(error_code: str) -> str:
    """错误码对应的 note 前缀. e.g. E020_CONTAMINATED: """
    return "{}CONTAMINATED: ".format(error_code + "_")


def _fetch_e020_decisions_with_ts(limit: int = 500, conn=None) -> List[Dict[str, Any]]:
    """DEPRECATED wrapper: use _fetch_error_code_decisions_with_ts('E020', limit, conn)."""
    return _fetch_error_code_decisions_with_ts('E020', limit=limit, conn=conn)


def _fetch_error_code_decisions_with_ts(error_code: str, limit: int = 500,
                                         conn=None) -> List[Dict[str, Any]]:
    """查决策_errors 表, 取 {error_code} 触发的 decision_id + 对应的 decision 创建时间 + asset"""
    own = conn is None
    if own:
        conn = db.get_connection()
    try:
        try:
            conn.row_factory = sqlite3.Row
        except Exception:
            pass
        rows = conn.execute("""
          SELECT d.id AS decision_id, d.created_at AS decision_ts,
                 d.target_asset AS asset, d.suggested_action AS action,
                 e.error_code AS error_code
          FROM decision_errors e
          JOIN ai_decisions d ON d.id = e.decision_id
          WHERE e.error_code = ?
          GROUP BY d.id
          ORDER BY d.id DESC
          LIMIT ?
        """, (error_code, limit)).fetchall()
        out = []
        for r in rows:
            if isinstance(r, sqlite3.Row):
                out.append({k: r[k] for k in r.keys()})
            elif isinstance(r, dict):
                out.append(r)
            else:
                out.append({'decision_id': r[0], 'decision_ts': r[1], 'asset': r[2],
                            'action': r[3], 'error_code': r[4]})
        return out
    finally:
        # v189-8: 自建连接必须自闭, 否则每次调用泄漏一个 fd。
        if own:
            conn.close()


def _parse_ts(ts_str: str) -> Optional[datetime]:
    """解析 ai_decisions.created_at 时间字符串 (支持 +08:00 等时区格式)"""
    if not ts_str:
        return None
    # 处理 +08:00 时区
    try:
        from datetime import datetime as _dt
        # strip timezone 部分, 直接当 naive datetime 用 (后续对比用 naive)
        if '+' in ts_str[10:] or (ts_str.count('-') > 2):
            # 找 '+' 或最后一个 '-'
            for sep in ('+', '-'):
                # 在 10 之后找第一个时区分隔符
                idx = ts_str[10:].find(sep)
                if idx > 0:
                    ts_str = ts_str[:10 + idx]
                    break
        for fmt in ("%Y-%m-%dT%H:%M:%S.%f", "%Y-%m-%dT%H:%M:%S",
                    "%Y-%m-%d %H:%M:%S.%f", "%Y-%m-%d %H:%M:%S"):
            try:
                return _dt.strptime(ts_str, fmt)
            except ValueError:
                continue
    except Exception:
        pass
    return None


def _find_related_insights(decision_id: int, asset: str, decision_ts: datetime,
                            conn=None) -> List[int]:
    """找与 E020 decision 关联的 review_insight id

    关联规则 (放宽, 减少漏匹配):
      - asset 一致 OR asset 为空 (空 asset 也算全局)
      - insight.created_at 在 decision_ts ±48h 内
    """
    own = conn is None
    if own:
        conn = db.get_connection()
    try:
        if decision_ts is None:
            return []
        window = 48  # 之前 48h 内入库、且此刻仍生效的洞察，才可能是它带偏了这条决策
        lo = (decision_ts - timedelta(hours=window)).isoformat()
        hi = decision_ts.isoformat()  # v409.6: 上界就是决策时刻（因果单向）
        # v310 修: **必须用 datetime() 做时间比较, 不能直接拿字符串比大小**。
        # review_insights.created_at 是 SQLite 的 datetime('now','localtime') →
        # 'YYYY-MM-DD HH:MM:SS' (空格分隔); 而 lo/hi 来自 datetime.isoformat() →
        # 'YYYY-MM-DDTHH:MM:SS' (T 分隔)。字符串字典序里 ' ' (0x20) < 'T' (0x54),
        # 于是**同一天的 insight 永远小于 lo** → 一条都匹配不上 →
        # insights_marked 长期为 0, v204 反馈层在生产里形同虚设。
        # 实测生产库: review_insights.created_at 全是空格格式。
        # v409.6: 因果方向必须单向 —— 只有「洞察先存在、决策后犯错」才可能是它带偏了决策。
        # 原实现 hi = decision_ts + 48h，把「决策之后才出生的洞察」也算关联，
        # 于是每条新洞察一入库就撞上历史事故被 veto + active=0（实测 96/97 条带 CONTAMINATED 标记）。
        rows = conn.execute("""
          SELECT id FROM review_insights
          WHERE (asset = ? OR asset = '' OR asset IS NULL)
            AND datetime(created_at) >= datetime(?)
            AND datetime(created_at) <= datetime(?)   -- 上界 = 决策时刻，不再 +48h
            AND active = 1
        """, (asset, lo, hi)).fetchall()
        return [r[0] for r in rows]
    finally:
        # v189-8: 自建连接必须自闭。decision_ts 为 None 的提前 return 也走 finally。
        if own:
            conn.close()


def _mark_insight_contaminated(insight_id: int, e020_decision_ids: Set[int],
                                conn=None) -> bool:
    """DEPRECATED wrapper: use _mark_insight_with_code(insight_id, 'E020', ids, veto=True, conn)."""
    return _mark_insight_with_code(insight_id, 'E020', e020_decision_ids, veto=True, conn=conn)


def _mark_insight_with_code(insight_id: int, error_code: str,
                            decision_ids: Set[int],
                            veto: bool = True, conn=None) -> bool:
    """给 insight 写 counter_factual 标记 + (veto 时) 设 active=0.

    Returns: 是否真的改了什么 (False 表示已经被标记过).

    同一错误码标记过的不会重复 (note prefix 含 {error_code}_CONTAMINATED:).
    """
    own = conn is None
    if own:
        conn = db.get_connection()
    try:
        row = conn.execute("""
          SELECT counter_factual, active FROM review_insights WHERE id = ?
        """, (insight_id,)).fetchone()
        if row is None:
            return False
        current_note = row[0] or ""
        prefix = _note_prefix(error_code)
        if prefix in current_note:
            return False  # 已标记
        new_note = (current_note + "；" if current_note.strip() else "") \
            + prefix + "基于 {} 个 {} 事故样本".format(len(decision_ids), error_code)
        # v453: 反例是给人（和模型）看的一句话，不是日志堆叠 —— 限长，避免越拼越长
        new_note = new_note[:200]
        if veto:
            # v482: 只停用**软启发**。硬铁律（kind=0）是人写下的强制约束，
            # 「不参与胜率停用」是既有口径（见 build_active_insights_section 注释）；
            # 漏掉 kind=1 会让反例回写把硬铁律一起停掉 —— 实测 11-19 五条硬铁律
            # 全部 active=0，prompt 里「硬铁律」区直接消失，且没有 manual_deactivated 标记，
            # 排查时看起来像"没人动过"。
            conn.execute("""
              UPDATE review_insights
              SET counter_factual = ?, active = 0
              WHERE id = ? AND kind = 1
            """, (new_note, insight_id))
        else:
            conn.execute("""
              UPDATE review_insights
              SET counter_factual = ?
              WHERE id = ?
            """, (new_note, insight_id))
        # 仅自建连接时提交: 调用方 conn 由调用方控制事务边界;
        # 自建连接若不提交, close() 会回滚导致 UPDATE 丢失。
        if own:
            conn.commit()
        return True
    finally:
        # v189-8: 自建连接必须自闭。row is None / 已标记 的提前 return 也走 finally。
        if own:
            conn.close()


def apply_e020_to_insights(limit: int = 500, conn=None) -> Dict[str, Any]:
    """DEPRECATED wrapper: use apply_error_code_to_insights('E020', limit, conn)."""
    return apply_error_code_to_insights('E020', limit=limit, conn=conn)


def apply_error_code_to_insights(error_code: str, limit: int = 500,
                                  veto: Optional[bool] = None,
                                  conn=None) -> Dict[str, Any]:
    """主入口 (v204+ 泛化): 把 {error_code} 触发反馈到 review_insights.

    Args:
        error_code: 错误码, 如 'E020' / 'E014' / 'E015' / 'E016'
        limit: 最多扫多少条决策错误 (默认 500)
        veto: 是否设 active=0. None = 从 reflection_feedback_config 查策略.
              True = 设 active=0 (硬 veto), False = 只标 note (软 veto).
        conn: 数据库连接.

    Returns:
        {
            'error_code': str,
            'decisions_found': int,
            'insights_marked': int,
            'insights_already_marked': int,
            'veto_applied': bool,
            'asset_action_distribution': dict,
        }
    """
    if veto is None:
        policy = rfc.get_policy(error_code)
        veto = policy.get('veto_insights', True) if policy else True

    decisions = _fetch_error_code_decisions_with_ts(error_code, limit=limit, conn=conn)
    by_aa: Dict[str, int] = {}
    marked = 0
    already = 0

    # 收集每个 insight 关联的 decision ids
    insight_to_decisions: Dict[int, Set[int]] = {}

    for ed in decisions:
        asset = ed.get('asset') or 'UNKNOWN'
        action = ed.get('action') or 'UNKNOWN'
        key = '{}_{}'.format(asset, action)
        by_aa[key] = by_aa.get(key, 0) + 1

        ts = _parse_ts(ed.get('decision_ts') or '')
        related = _find_related_insights(ed['decision_id'], asset, ts, conn=conn)
        for ins_id in related:
            insight_to_decisions.setdefault(ins_id, set()).add(ed['decision_id'])

    # 写入
    for ins_id, did_set in insight_to_decisions.items():
        changed = _mark_insight_with_code(ins_id, error_code, did_set,
                                           veto=veto, conn=conn)
        if changed:
            marked += 1
        else:
            already += 1

    if conn is not None:
        conn.commit()

    return {
        'error_code': error_code,
        'decisions_found': len(decisions),
        'insights_marked': marked,
        'insights_already_marked': already,
        'veto_applied': veto,
        'asset_action_distribution': by_aa,
    }


def print_feedback_report(result: Dict[str, Any]) -> None:
    """打印反馈报告 (兼容 v202 老格式 + v204 新格式)."""
    print('=' * 80)
    code = result.get('error_code') or result.get('e020_code') or 'E020'
    print('[L2.1 verifier_feedback] {} → review_insights'.format(code))
    print('=' * 80)
    found = result.get('decisions_found', result.get('e020_decisions_found', 0))
    print('{} decisions found: {}'.format(code, found))
    print('veto applied:', result.get('veto_applied', True))
    print('insights_marked (新): {}'.format(result.get('insights_marked', 0)))
    print('insights_already_marked (已有): {}'.format(result.get('insights_already_marked', 0)))
    if result.get('asset_action_distribution'):
        print('asset_action distribution:')
        for k, v in sorted(result['asset_action_distribution'].items()):
            print('  {}: {}'.format(k, v))
    print('=' * 80)


if __name__ == "__main__":
    import sys
    sys.path.insert(0, '.')
    conn = db.get_connection()
    result = apply_e020_to_insights(limit=500, conn=conn)
    print_feedback_report(result)