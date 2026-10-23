#!/usr/bin/env python3
"""本地事件看板 — 盯 raw_news 新进卡片的 cross_net 全网筛查标记。

用法：
    python3 scripts/watch_cross_net.py                  # TUI 模式，每 5s 刷新
    python3 scripts/watch_cross_net.py --once           # 单次快照
    python3 scripts/watch_cross_net.py --json --since 5898   # JSONL 输出
    python3 scripts/watch_cross_net.py --interval 10 --limit 20

看板列：news_id | status | 入库 Δt | cross_net_first_seen_ts | source
末尾展示 24h 累计统计（filled / AMBIGUOUS / empty 三档填充率）。

依赖：纯标准库。零风险（只读）。
"""
from __future__ import annotations

import argparse
import datetime
import json
import os
import sqlite3
import sys
import time

DB_PATH = os.environ.get(
    "TRIDENT_DB",
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "trident_event_bus.db"),
)
DEFAULT_INTERVAL = 5
DEFAULT_LIMIT = 15


def _connect(db_path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path, timeout=5)
    conn.row_factory = sqlite3.Row
    return conn


def _fetch_recent(conn: sqlite3.Connection, limit: int, since_id: int) -> list:
    return conn.execute(
        """
        SELECT id, content, timestamp, ts, status, relevance_score,
               cross_net_first_seen_ts, cross_net_source
        FROM raw_news
        WHERE id > ?
        ORDER BY id DESC
        LIMIT ?
        """,
        (since_id, limit),
    ).fetchall()


def _fetch_stats_24h(conn: sqlite3.Connection) -> dict:
    cutoff = (datetime.datetime.now() - datetime.timedelta(hours=24)).isoformat(timespec="seconds")
    row = conn.execute(
        """
        SELECT
          COUNT(*)                                                              AS total,
          SUM(CASE WHEN cross_net_first_seen_ts IS NOT NULL
                       AND cross_net_first_seen_ts != ''                       THEN 1 ELSE 0 END) AS filled,
          SUM(CASE WHEN cross_net_first_seen_ts = 'AMBIGUOUS'                  THEN 1 ELSE 0 END) AS ambig,
          SUM(CASE WHEN cross_net_first_seen_ts IS NULL
                       OR cross_net_first_seen_ts = ''                         THEN 1 ELSE 0 END) AS empty
        FROM raw_news
        WHERE status = 'DONE' AND timestamp >= ?
        """,
        (cutoff,),
    ).fetchone()
    return dict(row)


def _ts_delta_label(db_ts, now_ts: int) -> str:
    if not db_ts:
        return "   ?"
    delta = max(0, now_ts - int(db_ts))
    if delta < 3600:
        return f"{delta // 60:>4}m"
    if delta < 86400:
        return f"{delta // 3600:>3}h{delta % 3600 // 60:02d}m"
    return f"{delta // 86400:>2}d{delta % 86400 // 3600:02d}h"


def _pct(num, den):
    return f"{num * 100 // den}%" if den else "n/a"


def _render_tui(rows: list, stats: dict, since_id: int) -> str:
    now_ts = int(time.time())
    lines = []
    lines.append(
        f"=== Trident 事件看板 · cross_net 全网筛查 · since_id={since_id} · "
        f"{datetime.datetime.now():%H:%M:%S} ==="
    )
    lines.append(f"{'id':>5}  {'status':6}  {'入库Δt':6}  {'cross_net_first_seen_ts':<25}  {'source':<22}  content")
    lines.append("-" * 120)
    for r in rows:
        cn = (r["cross_net_first_seen_ts"] or "")[:24]
        src = (r["cross_net_source"] or "")[:22]
        content = (r["content"] or "")[:60].replace("\n", " ")
        lines.append(
            f"{r['id']:>5}  {r['status']:6}  {_ts_delta_label(r['ts'], now_ts):6}  "
            f"{cn:<25}  {src:<22}  {content}"
        )
    lines.append("")
    lines.append(
        f"=== 24h DONE 累计 === total={stats['total']:>3}  "
        f"filled={stats['filled']:>3}({_pct(stats['filled'], stats['total'])})  "
        f"AMBIGUOUS={stats['ambig']:>3}({_pct(stats['ambig'], stats['total'])})  "
        f"empty={stats['empty']:>3}({_pct(stats['empty'], stats['total'])})"
    )
    return "\n".join(lines)


def _emit_jsonl(rows: list, stats: dict) -> None:
    out = {
        "kind": "snapshot",
        "ts": int(time.time()),
        "rows": [
            {
                "id": r["id"],
                "status": r["status"],
                "relevance": r["relevance_score"],
                "content": (r["content"] or "")[:200],
                "cross_net_first_seen_ts": r["cross_net_first_seen_ts"],
                "cross_net_source": r["cross_net_source"],
            }
            for r in rows
        ],
        "stats_24h": stats,
    }
    print(json.dumps(out, ensure_ascii=False))


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0] if __doc__ else "watch")
    p.add_argument("--once", action="store_true", help="只输出一次快照后退出")
    p.add_argument("--json", action="store_true", help="输出 JSON Lines（供前端 / log 抓取）")
    p.add_argument("--interval", type=int, default=DEFAULT_INTERVAL, help=f"刷新间隔秒（默认 {DEFAULT_INTERVAL}）")
    p.add_argument("--limit", type=int, default=DEFAULT_LIMIT, help=f"显示最近 N 条（默认 {DEFAULT_LIMIT}）")
    p.add_argument("--since", type=int, default=0, help="只看 id > since 的新闻（默认 0，看表内最新 N 条）")
    p.add_argument("--db", default=DB_PATH, help="DB 路径")
    args = p.parse_args()

    conn = _connect(args.db)
    since_id = args.since

    try:
        while True:
            rows = _fetch_recent(conn, args.limit, since_id)
            stats = _fetch_stats_24h(conn)
            if args.json:
                _emit_jsonl(rows, stats)
            else:
                # TUI 模式：清屏（仅在交互终端）
                if not args.once and sys.stdout.isatty():
                    sys.stdout.write("\033[2J\033[H")
                print(_render_tui(rows, stats, since_id))

            if args.once:
                break

            time.sleep(args.interval)
            # 推进 since_id 到已显示的最新一条
            if rows:
                since_id = max(since_id, rows[0]["id"])
    finally:
        conn.close()

    return 0


if __name__ == "__main__":
    sys.exit(main())