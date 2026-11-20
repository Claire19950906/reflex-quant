#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""cron_experience_adaptive.py — 经验自适应的调度入口 (v337)

为什么需要这个模块
------------------
`experience_reflector.adaptive_review_and_apply()` 早就写好了：
按"应用了该经验的决策"的真实胜率调整 weight/priority、算 control 组 lift。
但它和两条反思富化链路一样，**没有任何调度** —— 函数存在、没人按表跑。

结果就是经验库只进不出：
  · 10 条经验 apply_count 全 0（连"被引用过"都没记录）；
  · `experience_applications` / `experience_application_daily` 两张表空；
  · 没有任何经验因为有效被提权、或因为无效被降权。

本脚本每 6 小时跑一次：回写应用/命中计数 → 按胜率调权 → 算 lift。
窗口默认 720 小时（30 天）：24 小时窗口在低频交易下几乎永远 n<3，
调权逻辑等于空转。

用法:
    python3 cron_experience_adaptive.py                 # 默认 30 天窗口
    python3 cron_experience_adaptive.py --window 168     # 7 天
    python3 cron_experience_adaptive.py --json
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

import experience_reflector as er


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--window", type=int, default=720, help="胜率统计窗口(小时)")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()
    t0 = time.time()
    try:
        stats = er.adaptive_review_and_apply(window_hours=args.window)
    except Exception as exc:
        print(f"[exp-adaptive] FAIL {type(exc).__name__}: {exc}")
        return 1
    stats["at"] = time.strftime("%Y-%m-%d %H:%M:%S")
    stats["window_hours"] = args.window
    stats["elapsed_sec"] = round(time.time() - t0, 1)
    if args.json:
        print(json.dumps(stats, ensure_ascii=False, default=str))
    else:
        print(f"[exp-adaptive] scanned={stats.get('n_scanned')} "
              f"counts_backfilled={stats.get('n_counts_backfilled', 0)} "
              f"weight_up={stats.get('n_weight_up')} weight_down={stats.get('n_weight_down')} "
              f"lift={stats.get('n_lift_computed')} · {stats['elapsed_sec']}s")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
