#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""v322: 查账 —— 今天烧了多少 LLM 额度，剩多少，闸门状态。

用法：
    python3 scripts/budget_status.py           # 今天
    python3 scripts/budget_status.py --days 7  # 最近 7 天
    python3 scripts/budget_status.py --by-channel   # 按通道拆分

事故背景（2026-11-04）：用户反馈"额度被一口气烧光"。
本脚本是那之后加的 —— 以后**每天先看一眼这个**，再决定要不要放开跑。
"""
from __future__ import annotations

import argparse
import os
import sys

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SRC = os.path.join(BASE, "src_python")
if SRC not in sys.path:
    sys.path.insert(0, SRC)


def _fmt(n: float) -> str:
    n = float(n or 0)
    if n >= 1_000_000:
        return f"{n / 1_000_000:.2f}M"
    if n >= 1_000:
        return f"{n / 1_000:.1f}K"
    return f"{int(n)}"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=1, help="统计最近 N 天（默认 1）")
    ap.add_argument("--by-channel", action="store_true", help="按通道拆分")
    args = ap.parse_args()

    import llm_budget

    st = llm_budget.state()
    print("=" * 62)
    print(f"  LLM 额度状态  ·  {st['day']}")
    print("=" * 62)

    def row(name: str, used: float, cap: float) -> str:
        if cap and cap > 0:
            pct = used / cap * 100
            left = max(0.0, cap - used)
            return (f"  {name:<12} {_fmt(used):>10} / {_fmt(cap):<10}"
                    f"  ({pct:5.1f}%)  剩 {_fmt(left)}")
        return f"  {name:<12} {_fmt(used):>10} / {'不限':<10}  (——)"

    print(row("token", st["tokens_used"], st["token_budget"]))
    print(row("请求数", st["requests_used"], st["request_budget"]))
    print(row("信号条数", st["news_used"], st["news_budget"]))
    print("-" * 62)
    if llm_budget.kill_switch_on():
        print("  🔴 全局急停开关：已打开（所有 LLM 调用都不会发出）")
    elif st["ok"]:
        print("  🟢 闸门状态：放行（额度充足）")
    else:
        print(f"  🔴 闸门状态：拦截中 —— {st['reason']}")
    print("=" * 62)

    if args.by_channel:
        import time

        import db
        day_ts = int(time.mktime(
            time.localtime()[:3] + (0, 0, 0, 0, 0, -1)))
        rows = []
        try:
            conn = db.get_connection()
            cur = conn.cursor()
            cur.execute(
                "SELECT key_alias, COUNT(*), "
                "COALESCE(SUM(tokens_in+tokens_out),0) "
                "FROM ai_call_stats WHERE ts >= ? "
                "GROUP BY key_alias ORDER BY 3 DESC", (day_ts,))
            rows = cur.fetchall()
            conn.close()
        except Exception as exc:
            print(f"  [按通道查询失败: {type(exc).__name__}: {exc}]")
        print("  今日各通道消耗：")
        print(f"  {'通道':<24}{'次数':>8}{'token':>12}")
        print("  " + "-" * 44)
        remote = local = 0
        for r in rows:
            alias = str(r[0])
            mark = ""
            if alias == "local":
                mark = "  ← 本地，不计费"
                local += 1
            else:
                remote += 1
            print(f"  {alias[:23]:<24}{int(r[1]):>8}{_fmt(r[2]):>12}{mark}")
        print(f"\n  付费通道 {remote} 个 / 本地通道 {local} 个"
              f"（本地不计入预算）")
        print("=" * 62)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
