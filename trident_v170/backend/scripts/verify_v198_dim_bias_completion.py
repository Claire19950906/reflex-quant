#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""v198: 验证 dim_event_type.direction_bias 覆盖率补全。

设计目标：
- oil_inventory: bias={WTI: neutral} + name_zh/en/category 补全
- cpi_release: 保留 bias={}（dead code，无代码引用）
- market_event: 保留 bias={}（v176.5 LLM placeholder，by-design 中性）

期望：活跃 dim_event_type 行 25/27 有 bias（market_event + cpi_release 排除）。
"""
from __future__ import annotations
import sys
import os
import json

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "src_python"))

import db
import sqlite3


# v198 设计上不应该有 bias 的 code (by-design / dead code)
EXCLUDED_FROM_COVERAGE = {"market_event", "cpi_release"}


def main():
    results = []
    conn = db.get_connection()
    conn.row_factory = sqlite3.Row
    try:
            print("=" * 70)
            print("1. dim_event_type 全表统计")
            print("=" * 70)
            rows = conn.execute(
                "SELECT code, name_zh, name_en, category, direction_bias, active "
                "FROM dim_event_type ORDER BY code"
            ).fetchall()
            total = len(rows)
            active_rows = [r for r in rows if r["active"]]
            active_total = len(active_rows)
            print(f"  总行数 = {total} (active = {active_total})")
            ok1 = active_total == total
            results.append(("所有 dim_event_type 行 active=1", ok1,
                            f"active/total={active_total}/{total}"))
            print()

            print("=" * 70)
            print("2. oil_inventory 字段补全")
            print("=" * 70)
            r = conn.execute(
                "SELECT name_zh, name_en, category, direction_bias "
                "FROM dim_event_type WHERE code='oil_inventory'"
            ).fetchone()
            if not r:
                ok2 = False
                print(f"  oil_inventory 行不存在")
            else:
                bias = json.loads(r["direction_bias"] or "{}")
                print(f"  name_zh = {r['name_zh']}")
                print(f"  name_en = {r['name_en']}")
                print(f"  category = {r['category']}")
                print(f"  bias = {bias}")
                ok2 = (
                    r["name_zh"] == "原油库存"
                    and r["name_en"] == "Crude Oil Inventory"
                    and r["category"] == "commodity"
                    and bias == {"WTI": "neutral"}
                )
            results.append(("oil_inventory name + bias 完整",
                            ok2, f"ok={ok2}"))
            print()

            print("=" * 70)
            print("3. by-design/dead code 排除列表 (应=无 bias 且在排除集)")
            print("=" * 70)
            excluded_actual = {
                r["code"]: json.loads(r["direction_bias"] or "{}")
                for r in rows
                if r["code"] in EXCLUDED_FROM_COVERAGE
            }
            print(f"  EXCLUDED_FROM_COVERAGE = {EXCLUDED_FROM_COVERAGE}")
            print(f"  实际排除行 = {excluded_actual}")
            ok3 = all(
                code in excluded_actual and excluded_actual[code] == {}
                for code in EXCLUDED_FROM_COVERAGE
            )
            results.append(("market_event + cpi_release bias={} (by-design)",
                            ok3, f"excluded={excluded_actual}"))
            print()

            print("=" * 70)
            print("4. 覆盖率统计: (有 bias) / (总 - 排除集) ≥ 95%")
            print("=" * 70)
            excluded_set = set(EXCLUDED_FROM_COVERAGE)
            in_scope = [r for r in active_rows if r["code"] not in excluded_set]
            with_bias = [r for r in in_scope
                         if json.loads(r["direction_bias"] or "{}")]
            cov_pct = len(with_bias) / max(len(in_scope), 1)
            print(f"  在统计范围内: {len(in_scope)} 行")
            print(f"  有 bias: {len(with_bias)} 行 ({cov_pct*100:.1f}%)")
            missing = [r["code"] for r in in_scope
                       if not json.loads(r["direction_bias"] or "{}")]
            print(f"  缺失: {missing}")
            ok4 = cov_pct >= 0.95 and not missing
            results.append((
                f"覆盖率 {len(with_bias)}/{len(in_scope)} ≥ 95%",
                ok4, f"missing={missing}"))
            print()

            print("=" * 70)
            print("5. oil_inventory direction_for 实测 (WTI → 'neutral')")
            print("=" * 70)
            import dim_tables
            bias_cn = dim_tables.direction_for("oil_inventory", "WTI",
                                               connection=conn)
            print(f"  dim_tables.direction_for('oil_inventory','WTI') = {bias_cn}")
            ok5 = bias_cn == "neutral"
            results.append(("oil_inventory/WTI bias lookup = 'neutral'",
                            ok5, f"got={bias_cn}"))
            print()

    finally:
        conn.close()

    print("=" * 70)
    print("总结")
    print("=" * 70)
    n_pass = sum(1 for _, ok, _ in results if ok)
    n_total = len(results)
    for label, ok, val in results:
        print(f"  {'PASS' if ok else 'FAIL'}  {label}: {val}")
    print(f"\n  总计: {n_pass}/{n_total} 通过")
    return n_pass == n_total


if __name__ == "__main__":
    ok = main()
    sys.exit(0 if ok else 1)