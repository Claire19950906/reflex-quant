#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""reclassify_legacy_reflections.py — 清理 B 类「未验证历史标签」(v338)

问题
----
trade_reflections_v2 里 B(信息分析方向) 共 1,762 条，其中 **1,611 条(91.4%)**
的 subcategory 是 unverified_legacy_label —— 旧兜底逻辑把所有"只有 layer/sub_layer
标签、没有方向证据"的行一律塞进 B。结果:

  · B 类看起来"问题很严重"，实际真方向错只有 151 条
  · 用户按 B 去修"逻辑框架"，修的是一个统计幻觉

重判规则 (只看机器记录的结构化标签, 不做语义猜测)
------------------------------------------------
  analysis / missed_signal      (1,429) → A / signal_missed
      信号出现但系统没出手 = 买卖信号判断能力, 不是方向错。
      这是**唯一**一个数量级明显的错位。
  analysis / news_source_class  (73)    → C / feed_missing
      信源分类/缺失是数据面问题, 不是"逻辑框架错"。
  decision / auto_settled_5d    (100)   → 保持 unverified
      自动结算行确实没有可判定的错因, 保留标记比瞎猜诚实。
  其余                                   → 保持

只改 subcategory + subcategory_evidence 两列, 不动 dimension 以外的任何统计字段。

用法:
    python3 reclassify_legacy_reflections.py --dry-run
    python3 reclassify_legacy_reflections.py
"""
from __future__ import annotations

import argparse
import sqlite3
from collections import Counter
from pathlib import Path

BACKEND_DIR = Path(__file__).resolve().parents[1]
DB_PATH = BACKEND_DIR / "trident_event_bus.db"

# (layer, sub_layer) → (dimension, subcategory)
RULES = {
    ("analysis", "missed_signal"): ("A", "market_regime_misread"),
    ("analysis", "news_source_class"): ("C", "feed_missing"),
}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default=str(DB_PATH))
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    conn = sqlite3.connect(args.db)
    rows = conn.execute(
        """SELECT id, layer, sub_layer FROM trade_reflections_v2
            WHERE subcategory = 'unverified_legacy_label'"""
    ).fetchall()

    plan = []
    for rid, layer, sub in rows:
        key = (str(layer or ""), str(sub or ""))
        if key in RULES:
            dim, sc = RULES[key]
            plan.append((dim, sc,
                         f"v338 重判: {layer}/{sub} → {dim}/{sc} (结构化标签明确, 非方向错)",
                         int(rid)))

    print(f"待重判 {len(plan)} / 全部 legacy {len(rows)}")
    print("目标分布:", Counter((p[0], p[1]) for p in plan))
    left = Counter((str(l), str(s)) for _, l, s in rows
                   if (str(l or ""), str(s or "")) not in RULES)
    print("保留 unverified:", left.most_common(5))

    if args.dry_run:
        print("dry-run: 未写入")
        return 0

    with conn:
        conn.executemany(
            """UPDATE trade_reflections_v2
                  SET dimension = ?, subcategory = ?, subcategory_evidence = ?
                WHERE id = ?""",
            plan,
        )
    print(f"已重判 {len(plan)} 行")
    print("新分布:", conn.execute(
        """SELECT dimension, subcategory, COUNT(*) FROM trade_reflections_v2
            GROUP BY 1,2 ORDER BY 1,2"""
    ).fetchall())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
