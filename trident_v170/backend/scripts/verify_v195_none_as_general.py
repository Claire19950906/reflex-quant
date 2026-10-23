#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""v195: 验证 asset='NONE' 被当作通则处理 (不再作为 asset-specific)。

验收目标：
1. ai_prompts.py 把 asset='NONE' 归类为通则 (排序与 asset='' 同级)
2. asset_tag 不输出 [NONE] 前缀
3. asset-specific 计数不受 'NONE' 影响 (XAU/BTC/etc 仍是专属规则)
"""
from __future__ import annotations
import sys
import os

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "src_python"))

import ai_prompts
import db
import sqlite3


def main():
    results = []
    print("=" * 70)
    print("1. ai_prompts.py ORDER BY 把 NONE 归类为通则")
    print("=" * 70)
    ap_path = os.path.join(HERE, "..", "src_python", "ai_prompts.py")
    with open(ap_path, "r", encoding="utf-8") as f:
        src = f.read()
    has_none_in_general = "IN ('', 'NONE')" in src
    has_none_tag_guard = 'not in ("", "NONE")' in src
    print(f"  ORDER BY 含 IN ('', 'NONE'): {has_none_in_general}")
    print(f"  asset_tag 含 NONE 跳过: {has_none_tag_guard}")
    ok1 = has_none_in_general and has_none_tag_guard
    results.append(("ai_prompts.py 把 'NONE' 视为通则",
                    ok1, f"order={has_none_in_general} tag={has_none_tag_guard}"))

    print()
    print("=" * 70)
    print("2. load_recent_insights_block 实测 (XAU target)")
    print("=" * 70)
    block_xau = ai_prompts.load_recent_insights_block(
        target_asset="XAU", ttl=0
    )
    # 检查 [NONE] 是否泄露
    none_leak = "[NONE]" in block_xau
    print(f"  block 含 [NONE] 前缀 (应 False): {none_leak}")
    print(f"  block 长度: {len(block_xau)}")
    if block_xau:
        first_xau_idx = -1
        first_general_idx = -1
        for idx, line in enumerate(block_xau.split("\n")):
            s = line.strip()
            if not s.startswith("•"):
                continue
            if s.startswith("• [XAU]"):
                if first_xau_idx == -1:
                    first_xau_idx = idx
            elif s.startswith("• [NONE]"):
                # 如果出现 [NONE] 是 leak
                pass
            elif s.startswith("• "):
                if first_general_idx == -1:
                    first_general_idx = idx
        print(f"  首条 [XAU] idx: {first_xau_idx}")
        print(f"  首条 通则 idx: {first_general_idx}")
    ok2 = not none_leak
    results.append(("block 不泄露 [NONE] 前缀", ok2, f"leak={none_leak}"))

    print()
    print("=" * 70)
    print("3. 排序验证 (NONE 与 '' 同级 → XAU target 时 NONE 在通则列)")
    print("=" * 70)
    conn = db.get_connection()
    conn.row_factory = sqlite3.Row
    # 复现 ORDER BY 逻辑
    rows = conn.execute("""
        SELECT id, asset, substr(rule, 1, 50) AS rule
        FROM review_insights
        WHERE active=1 AND manual_deactivated=0 AND kind=1
          AND COALESCE(source_samples, 0) >= ?
        ORDER BY CASE WHEN ? <> '' AND upper(COALESCE(asset,'')) = ? THEN 0
                      WHEN upper(COALESCE(asset,'')) IN ('', 'NONE') THEN 1
                      ELSE 2 END,
                 COALESCE(source_samples, 0) DESC, id DESC
        LIMIT ?
    """, (5, "XAU", "XAU", 8)).fetchall()
    print(f"  target=XAU 排序后:")
    for r in rows:
        cat = ("专属" if r["asset"] == "XAU"
               else "通则" if r["asset"] in ("", "NONE")
               else "其他")
        print(f"    [{cat:<4}] id={r['id']:>4} asset={r['asset']:<5} "
              f"rule={r['rule'][:30]}")
    # 检查 NONE 是否在通则组（idx 1+）且在专属 XAU 之后
    none_idx = -1
    xau_idx = -1
    for idx, r in enumerate(rows):
        if r["asset"] == "NONE" and none_idx == -1:
            none_idx = idx
        if r["asset"] == "XAU" and xau_idx == -1:
            xau_idx = idx
    none_in_general = none_idx > xau_idx if xau_idx >= 0 else (none_idx >= 0)
    print(f"  XAU idx: {xau_idx}, NONE idx: {none_idx} → NONE 在通则区: {none_in_general}")
    ok3 = none_in_general or none_idx == -1  # 如果 NONE 不在前 8 条也算 OK
    results.append(("'NONE' 排在 XAU 之后 (通则组)",
                    ok3, f"xau={xau_idx} none={none_idx}"))

    print()
    print("=" * 70)
    print("4. 各类 asset 比例 (NONE 不计入 asset-specific)")
    print("=" * 70)
    asset_stats = conn.execute("""
        SELECT COALESCE(NULLIF(asset, ''), '(空)') AS a, COUNT(*) AS n
        FROM review_insights WHERE active=1 AND kind=1
        GROUP BY a ORDER BY n DESC
    """).fetchall()
    for r in asset_stats:
        print(f"  {r[0]:<10} {r[1]} 条")
    n_specific = sum(r["n"] for r in asset_stats
                     if r["a"] not in ("(空)", "NONE"))
    n_general = sum(r["n"] for r in asset_stats
                    if r["a"] in ("(空)", "NONE"))
    n_total = sum(r["n"] for r in asset_stats)
    print(f"  总: {n_total} | 专属: {n_specific} | 通则: {n_general} "
          f"({n_specific}/{n_total}={100*n_specific/n_total:.1f}% 专属)")
    ok4 = n_specific == 4  # XAU/WTI/BTC/ETH (NONE 现在归通则)
    results.append(("asset-specific 计数 = 4 (NONE 归入通则)",
                    ok4, f"specific={n_specific} general={n_general}"))

    print()
    print("=" * 70)
    print("总结")
    print("=" * 70)
    n_pass = sum(1 for _, ok, _ in results if ok)
    n_total_all = len(results)
    for label, ok, val in results:
        print(f"  {'PASS' if ok else 'FAIL'}  {label}: {val}")
    print(f"\n  总计: {n_pass}/{n_total_all} 通过")
    conn.close()
    return n_pass == n_total_all


if __name__ == "__main__":
    ok = main()
    sys.exit(0 if ok else 1)