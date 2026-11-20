#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""运维工具：查看 / 清理 LLM 通道的熔断状态。

为什么需要这个（2026-11-05 事故）：

熔断状态 v322 起**跨进程落盘**在 backend/runtime/breakers/*.json。
这是对的（一个进程发现通道坏了，其它进程一起冷却），
但带来一个运维陷阱：**状态不会自己过期**。

  · auth（401/403）熔断冷却 24 小时 —— 换 key 之后旧状态仍然生效
    → 通道明明已经能用了，引擎却仍判定「全部通道熔断」
    → 主循环跳过认领 → 前端「待分析」只涨不跌。
  · 门卫（gate）通道的 auth 熔断卡住 → 每条新闻都落成 gate_unavailable=1
    → 主分析的认领 SQL 排除这些行 → 队列整体卡死。

v322 已加「凭据指纹」机制：换 key 后旧状态自动作废。
但**同一把 key** 从 429 恢复、或手工误触发的熔断，仍需手动清理。

用法：
    python3 scripts/reset_llm_breakers.py            # 只看状态（默认，不改任何东西）
    python3 scripts/reset_llm_breakers.py --reset    # 清空全部熔断状态
    python3 scripts/reset_llm_breakers.py --reset llm_main_auth:minimax1   # 只清指定通道
"""
from __future__ import annotations

import argparse
import os
import sys

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SRC = os.path.join(BASE_DIR, "src_python")
if SRC not in sys.path:
    sys.path.insert(0, SRC)


def main() -> int:
    ap = argparse.ArgumentParser(description="查看/清理 LLM 通道熔断状态")
    ap.add_argument("--reset", nargs="?", const="__ALL__", default=None,
                    help="清理指定熔断器（不给值=清空全部）")
    args = ap.parse_args()

    import circuit_breaker

    if args.reset is None:
        # 只读视图
        state = circuit_breaker.report_all()
        if not state:
            print("当前无熔断器记录（全部 closed）")
            return 0
        print("熔断器状态：")
        for name in sorted(state):
            s = state[name]
            flag = "  <-- 熔断中" if s.get("state") == "open" else ""
            print("  %-44s state=%-9s fail=%s/%s cooldown=%ss%s" % (
                name, s.get("state"), s.get("fail_count"),
                s.get("fail_threshold"), s.get("open_seconds"), flag))
        epoch = circuit_breaker.credential_epoch()
        print("\n当前凭据指纹: %s" % (epoch or "(无 LLM key)"))
        if epoch:
            print("提示：换 key 后旧状态会自动作废（指纹不符即忽略）。")
            print("      若同一把 key 恢复后仍显示熔断，用 --reset 手动清理。")
        return 0

    if args.reset == "__ALL__":
        circuit_breaker.purge_all()
        print("✅ 已清空全部熔断状态（内存 + 磁盘）")
        print("   下次引擎循环会立即重新体检并恢复认领。")
    else:
        circuit_breaker.reset(args.reset)
        print("✅ 已复位熔断器: %s" % args.reset)
    return 0


if __name__ == "__main__":
    sys.exit(main())
