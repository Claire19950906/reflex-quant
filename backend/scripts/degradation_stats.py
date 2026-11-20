#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""降级治理 / 洞察可执行率 / 抢救成功率 —— 只读统计（不改库、不改代码）。

用法：
    cd backend && python3 scripts/degradation_stats.py

为什么单独做这个：S16/S19 是「占比」口径，看不到**为什么**降级、**为什么**
观察、抢救**成没成**。这张表把三件事拆开，用于判断下一步该改哪儿。
"""
from __future__ import annotations

import json
import os
import sqlite3
import sys
from collections import Counter

_BACKEND = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_DB = os.path.join(_BACKEND, "trident_event_bus.db")
_RESCUE = os.path.join(_BACKEND, "runtime", "rescue_log.jsonl")


def _db() -> sqlite3.Connection:
    conn = sqlite3.connect(f"file:{_DB}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def degraded(conn: sqlite3.Connection) -> None:
    print("\n[1] 降级原因分布（decision_kind=analysis，全库）")
    rows = conn.execute(
        "SELECT coalesce(analysis_degraded_reason,'(无)') AS why,"
        " count(*) AS n FROM ai_decisions"
        " WHERE decision_kind='analysis' GROUP BY why ORDER BY n DESC"
    ).fetchall()
    total = sum(r["n"] for r in rows) or 1
    for r in rows:
        print(f"    {r['why']:<22} {r['n']:>5}  {100.0*r['n']/total:>5.1f}%")


def insights(conn: sqlite3.Connection) -> None:
    print("\n[2] 洞察可执行率（review_insights）")
    row = conn.execute(
        # v409.5: 只算 active=1（停用的失败洞察不该继续影响可执行率口径）
        "SELECT count(*) AS n, sum(observe_only=1) AS obs,"
        " sum(observe_only=0) AS act FROM review_insights WHERE active=1"
    ).fetchone()
    n = row["n"] or 1
    print(f"    总 {row['n']}  观察型 {row['obs']} ({100.0*(row['obs'] or 0)/n:.1f}%)"
          f"  可执行 {row['act']}")
    print("    观察原因 Top：")
    for r in conn.execute(
        "SELECT coalesce(observe_reason,'(空)') AS why, count(*) AS n"
        " FROM review_insights WHERE active=1 AND observe_only=1"
        " GROUP BY why ORDER BY n DESC LIMIT 6"
    ).fetchall():
        print(f"      {str(r['why'])[:46]:<48} {r['n']:>4}")


def rescue() -> None:
    print("\n[3] 抢救成功率（runtime/rescue_log.jsonl）")
    if not os.path.exists(_RESCUE):
        print("    尚未产生抢救记录")
        return
    rows = []
    with open(_RESCUE, encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            if rec.get("why") == "selftest":
                continue
            rows.append(rec)
    if not rows:
        print("    只有自检记录，没有真实抢救")
        return
    by_why = Counter()
    ok_by_why = Counter()
    for r in rows:
        w = r.get("why") or "(空)"
        by_why[w] += 1
        if r.get("ok") == 1 and not r.get("skip"):
            ok_by_why[w] += 1
    print(f"    真实记录 {len(rows)} 条")
    for w, n in by_why.most_common():
        print(f"      why={w:<18} 共 {n:>2}  成功 {ok_by_why[w]:>2}"
              f"  ({100.0*ok_by_why[w]/n:.0f}%)")
    total_ok = sum(ok_by_why.values())
    print(f"    合计成功 {total_ok}/{len(rows)} = {100.0*total_ok/len(rows):.0f}%")
    skips = Counter((r.get("skip") or "none") for r in rows)
    print("    skip 分布：" + "、".join(f"{k} {v}" for k, v in skips.most_common()))


def main() -> int:
    if not os.path.exists(_DB):
        print(f"库不存在：{_DB}")
        return 2
    conn = _db()
    try:
        degraded(conn)
        insights(conn)
    finally:
        conn.close()
    rescue()
    return 0


if __name__ == "__main__":
    sys.exit(main())
