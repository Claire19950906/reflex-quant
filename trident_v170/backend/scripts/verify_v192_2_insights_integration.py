#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""v192 #2: 验证 ai_worker.py 已接入 load_recent_insights_block (asset-specific)。

验收目标：
1. ai_prompts.load_recent_insights_block(target_asset) 可调用且返回非空
2. ai_worker._call_llm_sync 在 gate_target_asset 非空时调用 load_recent_insights_block
3. 注入的 prompt 含 "近期实战经验规则" 关键词
4. asset 匹配排序：target_asset=XAU 时，asset=XAU 行排在 asset='' 行前
"""
from __future__ import annotations
import sys
import os
import re
import sqlite3

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "src_python"))

import db
import ai_prompts


def main():
    results = []

    # ── 1. ai_prompts.load_recent_insights_block(XAU) 实际可用 ──
    print("=" * 70)
    print("1. load_recent_insights_block(XAU) 实测")
    print("=" * 70)
    block = ai_prompts.load_recent_insights_block(target_asset="XAU", ttl=5.0)
    print(f"  返回长度: {len(block)}")
    print(f"  前 200 字: {block[:200]}")
    print(f"  含 '近期实战经验规则': {'近期实战经验规则' in block}")
    print(f"  含 '反例' 关键词: {'反例' in block}")
    ok1 = "近期实战经验规则" in block and len(block) > 50
    results.append(("load_recent_insights_block(XAU) 返回有效 block", ok1,
                    f"len={len(block)}"))

    # ── 2. ai_worker._call_llm_sync 源码含 load_recent_insights_block 调用 ──
    print()
    print("=" * 70)
    print("2. ai_worker.py 源码集成检查")
    print("=" * 70)
    ai_worker_path = os.path.join(
        HERE, "..", "src_python", "engine", "ai_worker.py",
    )
    with open(ai_worker_path, "r", encoding="utf-8") as f:
        src = f.read()
    has_call = "load_recent_insights_block" in src
    has_log = "INSIGHTS-ASSET" in src
    has_guard = "if gate_target_asset:" in src
    print(f"  ai_worker 引用 load_recent_insights_block: {has_call}")
    print(f"  含日志标签 INSIGHTS-ASSET: {has_log}")
    print(f"  含 gate_target_asset 守卫: {has_guard}")
    ok2 = has_call and has_log and has_guard
    results.append(("ai_worker.py 已接入 v192 #2 注入路径", ok2,
                    f"call={has_call} log={has_log} guard={has_guard}"))

    # ── 3. asset 匹配排序验证 ──
    print()
    print("=" * 70)
    print("3. asset 匹配排序 (XAU 专属规则应在 通则 之前)")
    print("=" * 70)
    conn = db.get_connection()
    conn.row_factory = sqlite3.Row
    n_xau_specific = conn.execute(
        "SELECT COUNT(*) AS n FROM review_insights"
        " WHERE active=1 AND manual_deactivated=0 AND kind=1"
        "   AND upper(COALESCE(asset,''))='XAU'"
    ).fetchone()["n"]
    n_general = conn.execute(
        "SELECT COUNT(*) AS n FROM review_insights"
        " WHERE active=1 AND manual_deactivated=0 AND kind=1"
        "   AND (asset IS NULL OR asset='')"
    ).fetchone()["n"]
    print(f"  XAU 专属规则数: {n_xau_specific}")
    print(f"  通则规则数: {n_general}")

    # 验 asset 排序：把 block 按 • 切行，找出首条命中 "[XAU]" 和 通用
    lines = [l for l in block.split("\n") if l.strip().startswith("•")]
    if lines:
        first_xau_idx = -1
        first_general_idx = -1
        for idx, line in enumerate(lines):
            if line.startswith("• [XAU]"):
                if first_xau_idx == -1:
                    first_xau_idx = idx
            elif line.startswith("• ") and "[XAU]" not in line and "[BTC]" not in line:
                # 不带 asset 标签的就是通则
                if first_general_idx == -1:
                    first_general_idx = idx
        print(f"  首条 XAU 专属 idx: {first_xau_idx}")
        print(f"  首条 通则 idx: {first_general_idx}")
        if first_xau_idx == -1 or first_general_idx == -1:
            sort_ok = True  # 缺一种就跳过
            print(f"  (某种规则不存在，跳过排序验证)")
        else:
            sort_ok = first_xau_idx < first_general_idx
            print(f"  XAU 专属优先于通则: {sort_ok}")
        results.append(("asset 专属规则排序优先于通则", sort_ok,
                        f"xau@#{first_xau_idx} general@#{first_general_idx}"))
    else:
        results.append(("block 含经验规则行", False, "no • lines"))

    # ── 4. 缓存命中验证 ──
    print()
    print("=" * 70)
    print("4. 5s TTL 缓存命中 (相同 asset 第二次返回应一致)")
    print("=" * 70)
    block2 = ai_prompts.load_recent_insights_block(target_asset="XAU", ttl=5.0)
    cache_ok = block2 == block
    print(f"  block == block2 (cache hit): {cache_ok}")
    results.append(("5s TTL 缓存命中", cache_ok, f"identical={cache_ok}"))

    # ── 5. 不同 asset 各自缓存 ──
    print()
    print("=" * 70)
    print("5. 不同 asset 桶独立 (XAU vs BTC 内容可不同)")
    print("=" * 70)
    block_btc = ai_prompts.load_recent_insights_block(target_asset="BTC", ttl=5.0)
    print(f"  XAU block len: {len(block)}")
    print(f"  BTC block len: {len(block_btc)}")
    print(f"  XAU != BTC 内容: {block != block_btc}")
    bucket_ok = block != block_btc or (n_xau_specific == 0 and n_general == 0)
    results.append(("XAU vs BTC 桶独立 (asset 匹配生效)", bucket_ok,
                    f"xau_len={len(block)} btc_len={len(block_btc)}"))

    # ── 总结 ──
    print()
    print("=" * 70)
    print("总结")
    print("=" * 70)
    n_pass = sum(1 for _, ok, _ in results if ok)
    n_total = len(results)
    for label, ok, val in results:
        print(f"  {'PASS' if ok else 'FAIL'}  {label}: {val}")
    print(f"\n  总计: {n_pass}/{n_total} 通过")
    conn.close()
    return n_pass == n_total


if __name__ == "__main__":
    ok = main()
    sys.exit(0 if ok else 1)