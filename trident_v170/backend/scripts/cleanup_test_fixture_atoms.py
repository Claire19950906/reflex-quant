#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""清理 fixture: 删除 actor='测试 actor' 的 event_atom。

背景:
- id=1332, 1333 是历史测试 fixture (raw_news_id=60/61, 但 raw_news 已被清理过)
- actor='测试 actor' 在 v193 #3 audit 中被发现为反 bias 数据源
- 删除后 v193 #3 audit 应通过 (无 fixture 反 bias)

注意:
- event_impact_curve 通过 event_atom_id 关联 → 一并清理
- review_insight id=183 ("测试新闻" 经验规则) 留着让 review_agent 自然降权
- raw_news 60/61 已经不存在，无需处理
"""
from __future__ import annotations
import sys
import os
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "src_python"))
import db
import sqlite3


def main():
    conn = db.get_connection()
    conn.row_factory = sqlite3.Row
    cur = conn.cursor()

    # ── 1. 删除前快照 ──
    print("=" * 70)
    print("删除前快照")
    print("=" * 70)
    n_before_atom = cur.execute(
        "SELECT COUNT(*) AS n FROM event_atom WHERE actor='测试 actor'"
    ).fetchone()["n"]
    n_before_curve = cur.execute(
        "SELECT COUNT(*) AS n FROM event_impact_curve"
        " WHERE event_atom_id IN (SELECT id FROM event_atom WHERE actor='测试 actor')"
    ).fetchone()["n"]
    n_before_unknown = cur.execute(
        "SELECT COUNT(*) AS n FROM event_atom WHERE direction='unknown'"
    ).fetchone()["n"]
    print(f"  event_atom (actor='测试 actor'): {n_before_atom}")
    print(f"  event_impact_curve (关联): {n_before_curve}")
    print(f"  event_atom direction='unknown' (全表): {n_before_unknown}")

    # ── 2. 列出待删 ──
    rows = cur.execute("""
        SELECT id, raw_news_id, event_type_id, asset_id,
               direction, direction_confidence, direction_source
        FROM event_atom WHERE actor='测试 actor'
    """).fetchall()
    print()
    print(f"待删 {len(rows)} 行:")
    for r in rows:
        print(f"  id={r['id']:>4} raw_news={r['raw_news_id']:>4} "
              f"dir={r['direction']:<8} src={r['direction_source']}")

    # ── 3. DELETE ──
    print()
    print("执行 DELETE ...")
    # 先删 impact_curve (外键)
    n_del_curve = cur.execute(
        "DELETE FROM event_impact_curve"
        " WHERE event_atom_id IN (SELECT id FROM event_atom WHERE actor='测试 actor')"
    ).rowcount
    print(f"  删除 event_impact_curve: {n_del_curve}")
    # 再删 event_atom
    n_del_atom = cur.execute(
        "DELETE FROM event_atom WHERE actor='测试 actor'"
    ).rowcount
    print(f"  删除 event_atom: {n_del_atom}")
    conn.commit()

    # ── 4. 删后验证 ──
    print()
    print("=" * 70)
    print("删除后验证")
    print("=" * 70)
    n_after_atom = cur.execute(
        "SELECT COUNT(*) AS n FROM event_atom WHERE actor='测试 actor'"
    ).fetchone()["n"]
    n_after_unknown = cur.execute(
        "SELECT COUNT(*) AS n FROM event_atom WHERE direction='unknown'"
    ).fetchone()["n"]
    print(f"  event_atom (actor='测试 actor') 残留: {n_after_atom}")
    print(f"  event_atom direction='unknown' (全表): {n_after_unknown} "
          f"(Δ={n_before_unknown - n_after_unknown})")

    # ── 总结 ──
    print()
    print("=" * 70)
    print("总结")
    print("=" * 70)
    expected = len(rows)
    if n_del_atom == expected and n_after_atom == 0:
        print(f"  PASS  fixture 完全清理 ({n_del_atom}/{expected})")
        print(f"  PASS  direction='unknown' 总数 {n_before_unknown} → {n_after_unknown}")
        ok = True
    else:
        print(f"  FAIL  删除不一致: 期望 {expected}, 实际 {n_del_atom}, 残留 {n_after_atom}")
        ok = False
    conn.close()
    return ok


if __name__ == "__main__":
    ok = main()
    sys.exit(0 if ok else 1)