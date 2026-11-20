#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""audit_asset_scope_text — 查「记录自称不在监控清单，实际是核心品种」的历史行。

背景（2026-11-16 实测）
----------------------
168 条核心品种（XAU/WTI/BTC/ETH/SOL）决策里，93 条（55%）的 reasoning_path
写着「目标资产 NONE … 该标的不在核心监控清单」。

根因：降级兜底先按 NONE 写文案，随后 ASSETFIX / MAINFIX 改了 target_asset，
文案没跟着改 → 记录自相矛盾。后果是**归因污染**：反思把「LLM 空输出」
学成「品种不在监控范围」，于是 E 码归因全错。

v342 已修：品种变更时重写文案（`rerender_degrade_text_for_asset`）。
本脚本只读，用来量化历史存量、决定是否需要回填。

用法：
    python3 scripts/audit_asset_scope_text.py            # 人读
    python3 scripts/audit_asset_scope_text.py --json
退出码：0 = 无矛盾行；1 = 有矛盾行（历史存量还在）
"""
from __future__ import annotations

import argparse
import json
import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "src_python")))

CORE = ("BTC", "XAU", "WTI", "ETH", "SOL")


def collect(conn) -> dict:
    rows = conn.execute(
        f"""SELECT id, UPPER(COALESCE(target_asset,'')) AS asset,
                   substr(reasoning, 1, 70) AS rsn
            FROM ai_decisions
            WHERE UPPER(COALESCE(target_asset,'')) IN ({",".join("'%s'" % a for a in CORE)})
              AND reasoning_path LIKE '%目标资产 NONE%'""").fetchall()
    by_asset = {}
    for r in rows:
        by_asset[r[1]] = by_asset.get(r[1], 0) + 1
    total_core = conn.execute(
        f"""SELECT COUNT(*) FROM ai_decisions
            WHERE UPPER(COALESCE(target_asset,'')) IN ({",".join("'%s'" % a for a in CORE)})"""
    ).fetchone()[0]
    return {"contradictory": len(rows), "core_rows": total_core,
            "by_asset": by_asset,
            "samples": [{"id": r[0], "asset": r[1], "reasoning_head": r[2]}
                        for r in rows[:5]]}


def main(argv) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args(argv)
    import db
    conn = db.get_connection()
    conn.row_factory = None
    try:
        data = collect(conn)
    finally:
        conn.close()
    if args.json:
        print(json.dumps(data, ensure_ascii=False, indent=2))
    else:
        pct = (100.0 * data["contradictory"] / data["core_rows"]) if data["core_rows"] else 0.0
        print("核心品种文案错位核查")
        print("-" * 60)
        print(f"核心品种决策 {data['core_rows']} 条，文案仍写「目标资产 NONE」的 "
              f"{data['contradictory']} 条（{pct:.1f}%）")
        for a, n in sorted(data["by_asset"].items(), key=lambda x: -x[1]):
            print(f"  {a:<6} {n}")
        print()
        print("说明：这些行的判定本身没被改过，只是文案与品种对不上。")
        print("v342 之后新入库的行不会再出现；历史行按下述两种处理之一：")
        print("  ① 只读忽略（反思已按 reasoning_path 里的「分析降级」区分）")
        print("  ② 回填重写文案（需要单独确认，脚本不自动写库）")
    return 0 if data["contradictory"] == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
