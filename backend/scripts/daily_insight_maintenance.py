#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""v409.7 洞察池每日维护（一条命令跑完四步，供定时任务调用）。

顺序有讲究：
  1) 决策级归因刷新 —— 先按"决策后 24h 真实价格"重算方向类洞察的结论；
  2) 验证刷新（成交口径）—— 有成交的洞察按 K 线/成交重算；
  3) 出池 —— 把 active=1 且 verdict=failed 的停用；
  4) 修复污染标记 —— 按单向窗口重算 E014/E020 关联，并恢复被误杀的。

全部改动都会保留 verification_* 与 counter_factual 原值，可审计。
用法：
  python3 scripts/daily_insight_maintenance.py            # 干跑
  python3 scripts/daily_insight_maintenance.py --commit   # 落库
"""
from __future__ import annotations

import argparse
import os
import subprocess
import sys

BACKEND = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPTS = os.path.join(BACKEND, "scripts")


def run(cmd) -> int:
    print(f"\n$ {' '.join(cmd)}")
    p = subprocess.run(cmd, cwd=BACKEND)
    if p.returncode != 0:
        print(f"[WARN] 上述步骤退出码 {p.returncode}，继续下一步")
    return p.returncode


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--commit", action="store_true")
    ap.add_argument("--min-group", type=int, default=5)
    args = ap.parse_args()
    py = sys.executable
    flag = ["--commit"] if args.commit else []
    codes = []
    codes.append(run([py, os.path.join(SCRIPTS, "verify_insights_by_decisions.py"),
                      "--min-group", str(args.min_group), *flag]))
    codes.append(run([py, os.path.join(SCRIPTS, "retire_failed_insights.py"), *flag]))
    codes.append(run([py, os.path.join(SCRIPTS, "repair_contaminated_marks.py"), *flag]))
    print(f"\n[DONE] 各步退出码={codes} 模式={'COMMIT' if args.commit else 'DRYRUN'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
