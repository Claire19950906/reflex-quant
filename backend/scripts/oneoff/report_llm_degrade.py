#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""report_llm_degrade — 决策降级分布：真实八段 vs 各种兜底。

背景
----
272 条决策里 `sentiment_score=0` 占 89%、BUY 只有 2 条。看上去像
"模型判断无方向"，实际上多数是**降级兜底**（模型没给可用输出，服务端按模板补八段）。
本脚本把"到底卡在哪一步"量出来：

    full_degrade_in_scope    完整降级 · 核心品种（LLM 空输出/截断）
    full_degrade_out_scope   完整降级 · 范围外（当时 target_asset 还是 NONE）
    reasoning_summary_only   只拿到 reasoning 摘要，八段没展开
    short_summary            只有简短摘要
    ok                       真实八段推演

输出两部分：
  ① 历史基线：直接归判 `ai_decisions.reasoning_path`（只读，立刻可用）
  ② 探针表：v342 之后新写入的决策（`llm_degrade_probe`）

用法：
    python3 scripts/report_llm_degrade.py
    python3 scripts/report_llm_degrade.py --json
退出码：0 = 正常；1 = 真实八段占比 < 50%（降级为主，需要查上游）
"""
from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "src_python")))

LABEL = {
    "ok": "真实八段",
    "full_degrade_in_scope": "完整降级·核心品种",
    "full_degrade_out_scope": "完整降级·范围外",
    "reasoning_summary_only": "只有 reasoning 摘要",
    "short_summary": "只有简短摘要",
}


def main(argv) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--days", type=int, default=30)
    args = ap.parse_args(argv)

    import db
    import llm_degrade_probe as ldp

    conn = db.get_connection()
    try:
        historical = ldp.classify_existing(conn)
        probe = ldp.stats(conn, days=args.days)
        total_hist = sum(historical.values())
        holds = conn.execute(
            "SELECT COUNT(*) FROM ai_decisions WHERE suggested_action='HOLD'").fetchone()[0]
        zero_score = conn.execute(
            "SELECT COUNT(*) FROM ai_decisions WHERE sentiment_score=0").fetchone()[0]
    finally:
        conn.close()

    payload = {"historical": historical, "historical_total": total_hist,
               "holds": int(holds), "zero_score": int(zero_score),
               "probe": probe}
    if args.json:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return 0 if total_hist and historical.get("ok", 0) >= total_hist * 0.5 else 1

    print("决策降级分布（历史基线：直接归判 ai_decisions.reasoning_path）")
    print("-" * 66)
    for arch in ("ok", "full_degrade_in_scope", "full_degrade_out_scope",
                 "reasoning_summary_only", "short_summary"):
        n = historical.get(arch, 0)
        pct = (100.0 * n / total_hist) if total_hist else 0.0
        print(f"{LABEL.get(arch, arch):<22}{n:>6}  {pct:>6.1f}%")
    print(f"{'合计':<22}{total_hist:>6}")
    print()
    ok_pct = (100.0 * historical.get("ok", 0) / total_hist) if total_hist else 0.0
    print(f"HOLD {holds} 条 · sentiment_score=0 {zero_score} 条 · 真实八段占比 {ok_pct:.1f}%")
    if ok_pct < 50:
        print("⚠️ 真实八段不足一半 → 这是「不给方向」的主因，先查上游 LLM 输出")
        print("   （不是模型的判断偏好，是输出为空/被截断后走了兜底）")
    print()
    p = probe or {}
    if p.get("total"):
        print(f"探针表近 {p.get('days')} 天 {p['total']} 条：")
        for arch, v in (p.get("by_archetype") or {}).items():
            print(f"  {LABEL.get(arch, arch):<22} n={v['n']:<5} "
                  f"平均原文 {v['avg_raw_len']} 字 / 八段 {v['avg_path_len']} 字 / HOLD {v['holds']}")
    else:
        print("探针表暂无数据（v342 之后的新决策才会写入）。")
    return 0 if ok_pct >= 50 else 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
