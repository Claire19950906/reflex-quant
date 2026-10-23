#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""v198: 把 dim_event_type 3 条 bias 空行的状态同步到设计目标。

设计目标（区别于 v190/v193 时填 bias 的策略）：
  - oil_inventory: 补 bias={WTI: neutral} + 补 name_zh/name_en/category
                  （早期手插行漏了 name 字段；事件方向多变规则不抢答）
  - cpi_release: 保留 bias={} + 加注释字段到 dir_note
             （dead code，无任何代码引用；保守保留防外部兼容性破坏）
  - market_event: 保留 bias={} + 加 dir_note
              （v176.5 LLM 兜底占位，方向 by-design 中性，bias 不该有内容）

方向说明：因为 dim_event_type 表没 dir_note 列，使用 description 列或独立 sidecar。
实际上 dir_note 列不存在 — 我们改用 active=1 + code 自描述 + 在 weekly verify 中按 code 排除。

本脚本仅 UPDATE 数据，不改 schema。
"""
from __future__ import annotations
import sys
import os

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "src_python"))

import db
import json


def main():
    conn = db.get_connection()
    try:
        # 1. oil_inventory: 补全 name + bias
        n1 = conn.execute(
            """UPDATE dim_event_type
               SET name_zh = COALESCE(NULLIF(name_zh, ''), '原油库存'),
                   name_en = COALESCE(NULLIF(name_en, ''), 'Crude Oil Inventory'),
                   category = CASE WHEN category='other' OR category=''
                                   THEN 'commodity' ELSE category END,
                   direction_bias = ?
               WHERE code = 'oil_inventory'""",
            (json.dumps({"WTI": "neutral"}, ensure_ascii=False, sort_keys=True),),
        ).rowcount
        print(f"  [oil_inventory] UPDATE 行数 = {n1}")

        # 2. cpi_release: 保留 bias={}（dead code，不破坏）
        n2 = conn.execute(
            "SELECT direction_bias FROM dim_event_type WHERE code='cpi_release'"
        ).fetchone()
        print(f"  [cpi_release] 当前 bias = {n2[0] if n2 else 'NOT FOUND'}（dead code，保持 {{}})")

        # 3. market_event: 保留 bias={}（v176.5 placeholder，by-design 中性）
        n3 = conn.execute(
            "SELECT direction_bias FROM dim_event_type WHERE code='market_event'"
        ).fetchone()
        print(f"  [market_event] 当前 bias = {n3[0] if n3 else 'NOT FOUND'}（v176.5 placeholder，保持 {{}})")

        conn.commit()
        print("\n  ✓ v198 dim_event_type 同步完成")
        return 0
    finally:
        conn.close()


if __name__ == "__main__":
    sys.exit(main())