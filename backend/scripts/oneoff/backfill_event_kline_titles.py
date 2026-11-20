#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""backfill_event_kline_titles.py — 回填 event_kline_impact.event_title (v338)

背景
----
实测 5,561 行 event_kline_impact 的 event_title **全部**是空串或以
``[hash:xxxxxxxx]`` 开头 —— 因为写入端取的是 raw_news.content，而 content
入库时带上了跨源去重指纹。结果归因页只能显示哈希号，用户看不到"到底
是什么事件把价格打下去/拉上去"，第 6 条需求（事件→涨跌可解释）在展示层断裂。

本脚本做两件事:
  1. 对已有行按 news_events.id = event_kline_impact.event_id 回查
     news_events.headline（优先）/ raw_news.content（兜底）
  2. 复用 worker 的 _clean_headline 剥 hash 前缀 + 取首行 + 截断

只改 event_title 一列，不碰 impact_pct / direction / 时间窗。

用法:
    python3 backfill_event_kline_titles.py --dry-run   # 只统计能救回多少
    python3 backfill_event_kline_titles.py             # 实际写入
"""
from __future__ import annotations

import argparse
import re
import sqlite3
from pathlib import Path
from typing import Any

# 与 engine/event_kline_impact_worker.py::_clean_headline 同口径。
# 这里内联一份而不是 import: worker 会 import db → config → dotenv，
# 回填脚本必须零外部依赖才能在任何解释器上直接跑。
_HASH_PREFIX_RE = re.compile(r"^\s*\[hash:[0-9a-fA-F]+\]\s*")


def _clean_headline(raw: Any, limit: int = 120) -> str:
    s = str(raw or "").strip()
    s = _HASH_PREFIX_RE.sub("", s)
    if not s:
        return ""
    s = s.splitlines()[0].strip()
    s = re.sub(r"\s+", " ", s)
    return s[:limit]

BACKEND_DIR = Path(__file__).resolve().parents[2]
DB_PATH = BACKEND_DIR / "trident_event_bus.db"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default=str(DB_PATH))
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    conn = sqlite3.connect(args.db)
    conn.row_factory = sqlite3.Row

    rows = conn.execute(
        """SELECT eki.id, eki.event_source, eki.event_id, eki.event_title,
                  COALESCE(NULLIF(ne.headline, ''), rn.content) AS raw_headline
             FROM event_kline_impact eki
             LEFT JOIN news_events ne
                    ON CAST(ne.id AS TEXT) = eki.event_id
             LEFT JOIN raw_news rn ON rn.id = ne.news_id"""
    ).fetchall()

    total = len(rows)
    updates = []
    hashed = 0
    for r in rows:
        cur = str(r["event_title"] or "")
        if cur.startswith("[hash:"):
            hashed += 1
        new = _clean_headline(r["raw_headline"])
        if not new:
            continue  # join 不到正文的行保持原样，不写空
        if new == cur:
            continue
        updates.append((new, int(r["id"])))

    print(f"总行 {total} · 带 hash 前缀 {hashed} · 可回填 {len(updates)} "
          f"({len(updates) / total * 100:.1f}%)")

    if args.dry_run:
        for t, i in updates[:5]:
            print(f"  id={i} → {t}")
        print("dry-run: 未写入")
        return 0

    with conn:
        conn.executemany(
            "UPDATE event_kline_impact SET event_title = ? WHERE id = ?", updates)
    print(f"已写入 {len(updates)} 行")
    left = conn.execute(
        """SELECT COUNT(*) FROM event_kline_impact
            WHERE event_title IS NULL OR event_title = ''
               OR event_title LIKE '[hash:%'"""
    ).fetchone()[0]
    print(f"回填后仍为空/hash: {left}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
