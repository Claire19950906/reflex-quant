#!/usr/bin/env python3
"""预路由干跑（只读）：统计"会被判池外"的新闻量与样例。

用法：
    cd backend && PYTHONPATH=src_python python3 ../scripts/news_pool_router_dryrun.py
"""
from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend" / "src_python"))

import db
import news_router


def main() -> int:
    conn = sqlite3.connect(str(ROOT / "backend" / "trident_event_bus.db"), timeout=20)
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        "SELECT id, source, content, status FROM raw_news "
        "WHERE datetime(ts, 'unixepoch', 'localtime') >= datetime('now','localtime','-2 days') ORDER BY id DESC LIMIT 4000"
    ).fetchall()
    total = len(rows)
    hits = []
    for r in rows:
        content = (r["content"] or "").replace("\n", " ")
        reason = news_router.pool_out_reason(content[:120], content)
        if reason:
            hits.append((r["id"], r["source"], reason, content[:80]))

    print(f"近 2 天新闻 {total} 条，预路由判池外 {len(hits)} 条 "
          f"({len(hits)*100/total:.1f}%)" if total else "无数据")
    by_src = {}
    for _, src, _, _ in hits:
        by_src[src or "?"] = by_src.get(src or "?", 0) + 1
    print("按来源：", ", ".join(f"{k}={v}" for k, v in sorted(by_src.items(), key=lambda x: -x[1])[:6]))
    print("\n样例（前 12 条）：")
    for hid, src, reason, text in hits[:12]:
        print(f"  #{hid} [{src}] {reason} | {text}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
