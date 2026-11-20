#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""H4 查询工具: 读 quality_rejections 表, 用于 C1 假阳性 A/B 决策.

用法:
  python3 scripts/test_quality_rejections_query.py                 # 全部, 按 rule 分组
  python3 scripts/test_quality_rejections_query.py --rule R4       # 单个 rule
  python3 scripts/test_quality_rejections_query.py --last 24h      # 时间窗口
  python3 scripts/test_quality_rejections_query.py --decision 123 # 单个 decision 的所有 reason
  python3 scripts/test_quality_rejections_query.py --limit 50     # 限行
"""
import sys
import os
import argparse
import sqlite3
import json

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'src_python'))
import db as _db  # noqa: E402


def get_db_path() -> str:
    """从环境变量或 config 拿 DB path."""
    p = os.environ.get('DB_PATH')
    if p and os.path.exists(p):
        return p
    # fallback: config.DB_PATH
    try:
        import config
        if os.path.exists(config.DB_PATH):
            return config.DB_PATH
    except Exception:
        pass
    return ':memory:'


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--rule', help='过滤 rule (R1/R2/R4/R7/R8/R9)')
    ap.add_argument('--last', help='时间窗口, e.g. 24h, 7d, 30m')
    ap.add_argument('--decision', type=int, help='只看单个 decision')
    ap.add_argument('--limit', type=int, default=100)
    ap.add_argument('--group', action='store_true', help='按 rule 分组聚合')
    ap.add_argument('--db', help='指定 db path (默认 DB_PATH env 或 config.DB_PATH)')
    args = ap.parse_args()

    db_path = args.db or get_db_path()
    if db_path == ':memory:':
        print('[WARN] 没找到 DB_PATH, 用 :memory: (空数据)', file=sys.stderr)

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    _db.migrate(conn)  # idempotent, 老库自动 ALTER 补表

    if args.group:
        rows = conn.execute("""
            SELECT rule, COUNT(*) AS n,
                   COUNT(DISTINCT decision_id) AS unique_decisions,
                   MIN(created_at) AS first_seen,
                   MAX(created_at) AS last_seen
            FROM quality_rejections
            GROUP BY rule
            ORDER BY n DESC
        """).fetchall()
        print(f'\n按 rule 聚合 (total {sum(r["n"] for r in rows)} 行):')
        print(f'{"rule":6s} {"count":>7s} {"unique":>7s}  first_seen           last_seen')
        for r in rows:
            print(f'{r["rule"]:6s} {r["n"]:>7d} {r["unique_decisions"]:>7d}  '
                  f'{r["first_seen"]:20s} {r["last_seen"]}')
        return 0

    where = []
    params = []
    if args.rule:
        where.append('rule = ?')
        params.append(args.rule)
    if args.decision:
        where.append('decision_id = ?')
        params.append(args.decision)
    if args.last:
        # '24h' -> 24 hours
        unit = args.last[-1]
        try:
            n = int(args.last[:-1])
        except ValueError:
            print(f'--last 格式: 24h / 7d / 30m', file=sys.stderr)
            return 1
        if unit == 'h':
            where.append(f"datetime(created_at, 'localtime') > datetime('now', 'localtime', '-{n} hours')")
        elif unit == 'd':
            where.append(f"datetime(created_at, 'localtime') > datetime('now', 'localtime', '-{n} days')")
        elif unit == 'm':
            where.append(f"datetime(created_at, 'localtime') > datetime('now', 'localtime', '-{n} minutes')")
        else:
            print(f'--last 格式: 24h / 7d / 30m', file=sys.stderr)
            return 1

    where_sql = ('WHERE ' + ' AND '.join(where)) if where else ''
    sql = f"""
        SELECT id, decision_id, rule, reason, payload_json, filter_version, created_at
        FROM quality_rejections
        {where_sql}
        ORDER BY created_at DESC
        LIMIT ?
    """
    params.append(args.limit)
    rows = conn.execute(sql, params).fetchall()

    print(f'\nquality_rejections ({len(rows)} 行):')
    for r in rows:
        pj = json.loads(r['payload_json']) if r['payload_json'] else {}
        asset = pj.get('target_asset', '?')
        conf = pj.get('evidence_confidence', '?')
        action = pj.get('suggested_action', '?')
        status = pj.get('status', '?')
        print(f'  #{r["id"]:>4d}  decision={r["decision_id"]:>4d}  {r["rule"]:3s}  '
              f'{action:4s} {asset:4s} conf={conf!s:>5s}  status={status:13s}  '
              f'at {r["created_at"]}')
        print(f'         reason: {r["reason"]}')
    print(f'\n(共 {len(rows)} 行, 用 --group 看按 rule 聚合)')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
