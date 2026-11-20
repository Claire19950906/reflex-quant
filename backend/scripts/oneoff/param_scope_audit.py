#!/usr/bin/env python3
"""参数作用域审计：每个可调参数到底有没有进入决策路径。

为什么需要它
------------
v339 排查时曾用 `grep -rn "buy_min_score" ... | head -12` 判断某个参数"没有消费方"，
结果 `head` 把 `decision_guard.py` 的命中整段截掉，得出**完全相反的结论** ——
`buy_min_score` 其实就是 BUY/SELL 方向的判定门槛。

所以这个脚本的判定口径刻意写成：
  1. 先取命中总数，再谈消费点（不截断）；
  2. 把"存取 / 别名 / 折算 / 测试"这些 plumbing 命中单列一栏，与真正的消费点区分；
  3. 只报文件名和行号，不预判结论 —— 结论由人看行内容定。

用法
----
    cd backend/src_python && python3 ../scripts/param_scope_audit.py
    cd backend/src_python && python3 ../scripts/param_scope_audit.py --detail buy_min_score

退出码恒为 0（这是只读诊断，不是断言）。
"""
from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

SRC = Path(__file__).resolve().parent.parent / "src_python"

KEYS = [
    # paper_trading_settings 运行态
    "buy_min_score", "sell_min_score", "tp1_mult", "tp2_mult",
    "tp1_ratio", "tp2_ratio", "tp_enabled", "gate_enabled",
    "news_trade_max_age_seconds", "market_structure_enabled",
    # strategy_versions 下的 signal_params
    "signal_threshold", "notional_usdt", "leverage", "trailing_callback_rate",
    "holding_horizon_minutes", "min_event_strength",
    "require_direct_catalyst", "asset_filter",
]

# 只做存取 / 登记 / 折算的地方 —— 出现在这些文件里不代表"被消费"
PLUMBING = {
    "paper_trading.py",
    "param_key_aliases.py",
    "param_adjust_apply.py",
    "expire_unlandable_adjustments.py",
    "review_agent.py",                 # _V182_PARAM_KEYS 白名单
    "weekly_insight_apply.py",
    "engine/strategy_reflection.py",
}


def scan(key: str, with_tests: bool = False):
    """返回 (消费命中[(file, lineno, text)], plumbing 命中数, 总命中数)。"""
    proc = subprocess.run(
        ["grep", "-rn", "--include=*.py", key, str(SRC)],
        capture_output=True, text=True,
    )
    lines = [ln for ln in proc.stdout.splitlines() if ln.strip()]
    live, plumbing = [], 0
    for ln in lines:
        rel = ln.split(str(SRC) + "/", 1)[-1]
        path, _, rest = rel.partition(":")
        lineno, _, text = rest.partition(":")
        if path.startswith("tests/"):
            if with_tests:
                live.append((path + " (test)", lineno, text.strip()))
            continue
        if path in PLUMBING:
            plumbing += 1
            continue
        live.append((path, lineno, text.strip()))
    return live, plumbing, len(lines)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="参数作用域审计（只读）")
    ap.add_argument("--detail", metavar="PARAM", help="展开某个参数的全部消费点")
    ap.add_argument("--with-tests", action="store_true", help="把测试里的出现也算进来")
    args = ap.parse_args(argv)

    if not SRC.is_dir():
        print(f"找不到源码目录: {SRC}", file=sys.stderr)
        return 2

    if args.detail:
        live, plumbing, total = scan(args.detail, args.with_tests)
        print(f"{args.detail}: 总命中 {total} / plumbing {plumbing} / 消费 {len(live)}")
        for path, lineno, text in live:
            print(f"  {path}:{lineno}  {text[:110]}")
        if not live:
            print("  （没有消费点 —— 但它可能被动态键访问，仍需人工确认）")
        return 0

    print(f"{'param':28s}{'消费':>5s}{'plumbing':>10s}{'总命中':>7s}   涉及文件")
    print("-" * 104)
    dead = []
    for key in KEYS:
        live, plumbing, total = scan(key, args.with_tests)
        files = sorted({p for p, _, _ in live})
        print(f"{key:28s}{len(live):>5d}{plumbing:>10d}{total:>7d}   "
              f"{', '.join(files) if files else '—'}")
        if not live:
            dead.append(key)

    print()
    if dead:
        print("没有找到消费点的参数（需要人工确认是否走动态键）:")
        for k in dead:
            print(f"  - {k}")
    else:
        print("全部参数都有 plumbing 以外的命中。注意：命中 ≠ 被判断使用，看行内容再下结论。")
    print("\n提示：本脚本只统计命中位置。判定「是否影响行为」必须打开对应行看是不是比较/判断语句。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
