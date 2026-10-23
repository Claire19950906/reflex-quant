#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Reflection system -> META_REFLECTION.md 自更新 (L3.3)

每周日 03:00 自动跑:
  1. 扫本周 E020 累计 / 盲区变化 / 新规则
  2. 自动更新 backend/META_REFLECTION.md:
     - "当前盲区" 表 + 新增条目 (如果发现)
     - "版本" 表 + 新版本
  3. 不动 L1+L2 本身, 只更新文档

默认 dry-run 模式 (打印要写的改动, 不真改文件).
设置 --apply 端点真改.

Usage:
    from meta_reflection_updater import update_meta_reflection
    result = update_meta_reflection(dry_run=True)
"""
from __future__ import annotations
import os
import re
import json
import sqlite3
from datetime import datetime, date
from typing import Any, Dict, List, Optional, Tuple

import db
import kg_feedback as kgf
import reflection_alert as ral


META_REFLECTION_PATH = "META_REFLECTION.md"  # 默认 backend/ 下
WEEKLY_VERSION_PREFIX = "v202+"
TODAY = datetime.now().strftime("%Y-%m-%d")
TODAY_ISO = datetime.now().isoformat(timespec='seconds') + 'Z'


def _scan_weekly_blindspots(week_start: date, conn=None) -> Dict[str, Any]:
    """扫本周反思系统新发现的盲区"""
    if conn is None:
        conn = db.get_connection()
    week_start_iso = week_start.isoformat()
    summary = {
        'week_start': week_start_iso,
        'e020_total_week': 0,
        'new_kg_proposals': [],
        'unresolved_alerts': 0,
        'asset_action_hotspots': {},
    }

    row = conn.execute(
        "SELECT COUNT(DISTINCT decision_id) FROM decision_errors WHERE error_code = 'E020' AND created_at >= ?",
        (week_start_iso,)
    ).fetchone()
    summary['e020_total_week'] = row[0] if row else 0

    rows = conn.execute(
        "SELECT id, kind, target, reason, status FROM kg_improvements WHERE source = 'reflection_system_v202' AND first_seen_at >= ?",
        (week_start_iso,)
    ).fetchall()
    for r in rows:
        try:
            summary['new_kg_proposals'].append({
                'id': r['id'], 'kind': r['kind'], 'target': r['target'],
                'reason': r['reason'], 'status': r['status'],
            })
        except (TypeError, KeyError, IndexError):
            summary['new_kg_proposals'].append({
                'id': r[0], 'kind': r[1], 'target': r[2],
                'reason': r[3], 'status': r[4],
            })

    summary['unresolved_alerts'] = ral._count_e020_in_window(window_hours=24*7, conn=conn)

    rows = conn.execute("""
      SELECT d.target_asset, d.suggested_action, COUNT(DISTINCT d.id) AS n
      FROM decision_errors e JOIN ai_decisions d ON d.id = e.decision_id
      WHERE e.error_code = 'E020' AND e.created_at >= ?
      GROUP BY d.target_asset, d.suggested_action ORDER BY n DESC
    """, (week_start_iso,)).fetchall()
    for r in rows:
        try:
            asset, action, n = r['target_asset'] or 'UNKNOWN', r['suggested_action'] or 'UNKNOWN', r['n']
        except (TypeError, KeyError, IndexError):
            asset, action, n = r[0] or 'UNKNOWN', r[1] or 'UNKNOWN', r[2]
        summary['asset_action_hotspots'][asset + '_' + action] = n

    return summary


def _propose_new_blindspots(summary: Dict[str, Any]) -> List[Dict[str, Any]]:
    """基于本周 summary 推出新盲区 (heuristic 形式, 不调用 LLM)"""
    new = []
    if summary['e020_total_week'] >= 50:
        new.append({
            'blindspot': '本周 E020 累计 {} 笔, 事故样本比例持续偏高, gate / allowlist 修复未完成'.format(summary['e020_total_week']),
            'severity': 'high',
        })
    if summary['unresolved_alerts'] > 0:
        new.append({
            'blindspot': '本周仍有 {} 笔 E020 未解决, 自动 resolve 机制可能失效'.format(summary['unresolved_alerts']),
            'severity': 'medium',
        })
    hotspots = summary.get('asset_action_hotspots', {})
    if hotspots.get('XAU_BUY', 0) >= 10:
        new.append({
            'blindspot': 'XAU BUY 方向 E020 累计 {}, 是事故样本重灾区'.format(hotspots['XAU_BUY']),
            'severity': 'high',
        })
    if hotspots.get('WTI_BUY', 0) >= 5:
        new.append({
            'blindspot': 'WTI BUY 方向 E020 累计 {}, K 线 fallback 占位问题严重'.format(hotspots['WTI_BUY']),
            'severity': 'medium',
        })
    return new


def _next_version(current_version: str) -> str:
    """生成下一个版本号 (基于周次)"""
    iso_week = datetime.now().isocalendar()
    return '{}.w{}'.format(WEEKLY_VERSION_PREFIX, iso_week.week)


def _read_meta_file(path: str) -> Optional[str]:
    if not os.path.exists(path):
        return None
    with open(path, 'r', encoding='utf-8') as f:
        return f.read()


def _build_new_block(old_summary: Dict[str, Any], new_blindspots: List[Dict[str, Any]],
                      next_version: str, summary: Dict[str, Any]) -> str:
    """构造要追加到 META_REFLECTION.md 的内容块"""
    lines = ['', '---', '', '## 自动追加: {} ({})'.format(next_version, TODAY), '']
    lines.append('### 本周盲区扫描')
    lines.append('')
    lines.append('| 指标 | 值 |')
    lines.append('|---|---|')
    lines.append('| week_start | {} |'.format(summary['week_start']))
    lines.append('| E020 total | {} 笔 |'.format(summary['e020_total_week']))
    lines.append('| new KG proposals | {} 条 |'.format(len(summary['new_kg_proposals'])))
    lines.append('| unresolved alerts (7d) | {} |'.format(summary['unresolved_alerts']))
    hotspots = ', '.join('{}: {}'.format(k, v) for k, v in sorted(summary['asset_action_hotspots'].items()))
    lines.append('| hotspots | ' + hotspots + ' |')
    lines.append('')
    if new_blindspots:
        lines.append('### 新发现盲区 ({} 条)'.format(len(new_blindspots)))
        lines.append('')
        for b in new_blindspots:
            lines.append('- **[{}]**: {}'.format(b['severity'].upper(), b['blindspot']))
        lines.append('')
    return '\n'.join(lines)


def update_meta_reflection(meta_path: str = META_REFLECTION_PATH,
                            week_start: Optional[date] = None,
                            dry_run: bool = True) -> Dict[str, Any]:
    """主入口: 更新 META_REFLECTION.md

    Args:
        meta_path: META_REFLECTION.md 路径 (相对 backend/ 或绝对)
        week_start: 本周开始 (默认周一)
        dry_run: True = 只打印, False = 真改
    """
    if week_start is None:
        today = date.today()
        week_start = today.fromordinal(today.toordinal() - today.weekday())

    conn = db.get_connection()
    try:
        summary = _scan_weekly_blindspots(week_start, conn=conn)
        new_blindspots = _propose_new_blindspots(summary)
        next_version = _next_version('')

        appended = _build_new_block({}, new_blindspots, next_version, summary)

        if not os.path.isabs(meta_path):
            backend_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
            meta_path = os.path.join(backend_root, meta_path)
        old_content = _read_meta_file(meta_path)
        old_size = len(old_content) if old_content else 0
        new_content = (old_content or '') + appended
        new_size = len(new_content)

        applied = False
        if not dry_run and old_content is not None:
            with open(meta_path, 'w', encoding='utf-8') as f:
                f.write(new_content)
            applied = True
    finally:
        # v189-8: 自建连接必须自闭。
        conn.close()

    return {
        'old_summary': {},
        'new_blindspots': new_blindspots,
        'next_version': next_version,
        'meta_path': meta_path,
        'old_size': old_size,
        'new_size': new_size,
        'appended_block': appended,
        'dry_run': dry_run,
        'applied': applied,
    }


def print_update_report(result: Dict[str, Any]) -> None:
    """打印更新报告"""
    print('=' * 80)
    print('[L3.3 meta_reflection_updater]')
    print('=' * 80)
    print('next_version:', result['next_version'])
    print('meta_path:', result['meta_path'])
    print('old_size:', result['old_size'], 'bytes')
    print('new_size:', result['new_size'], 'bytes')
    print('dry_run:', result['dry_run'])
    print('applied:', result['applied'])
    if result['new_blindspots']:
        print('new blindspots:')
        for b in result['new_blindspots']:
            print(' ', '[' + b['severity'].upper() + ']', b['blindspot'])
    print('=' * 80)
    if result['dry_run']:
        print('--- append block preview (first 500 chars) ---')
        print(result['appended_block'][:500])
        print('--- end preview ---')


if __name__ == "__main__":
    import sys
    sys.path.insert(0, '.')
    apply_mode = '--apply' in sys.argv
    result = update_meta_reflection(dry_run=not apply_mode)
    print_update_report(result)