# -*- coding: utf-8 -*-
"""analysis/ —— 分析链的阶段化实现（strangler 迁移中）

设计原则（见 docs/archive/分析链优化与结构重建方案-2026-11-16.md）：

  1. 只做判定，不写库、不抓数据、不打分 → 全部纯函数，可离线单测；
  2. 每个阶段独立成模块，主循环只调编排，不再把逻辑堆在 ai_worker 里；
  3. 五终态（complete / out_of_scope / data_unavailable / llm_failed /
     parse_failed）取代「一个布尔装五种原因」；
  4. HOLD 必须带 no_trade_reason，否则「没分析」和「分析后判中性」无法分开统计。

当前已落地：
  contract.py   终态派生 + HOLD 枚举 + 长度预算
  route.py      分析前范围路由（核心 / 邻近 / 越界）
  readiness.py  证据就绪检查（快照年龄 / 本地源 / 周期覆盖）

尚未迁移（仍由 engine/ai_worker.py 承担）：
  context / prompt / pool / call / parse / normalize / confidence / persist / pipeline
"""

from . import contract, readiness, route

__all__ = ["contract", "readiness", "route"]
