#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""v193 #3: 列出所有 source='llm' 且 direction 与 dim_event_type bias 冲突的 event_atom。

设计意图：
- 后端规则是 ground truth (dim_event_type.direction_bias 是人工/官方定义)
- LLM 偶尔会反 bias（如 rate_hike/BTC 写成 bullish）
- 这些是数据质量问题，需要人工 review 后再决定：
  - 接受（修正 bias 表）
  - 拒绝（删 / 标 audit）
  - 标记（留 source='llm_audit'）

输出：按 (event_type, asset) 分桶的 conflict 报告
"""
from __future__ import annotations
import sys
import os
import json

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "src_python"))

import db
import sqlite3

CN_TO_DB = {"利多": "bullish", "利空": "bearish", "中性": "neutral",
            "bullish": "bullish", "bearish": "bearish", "neutral": "neutral"}


def main():
    c = db.get_connection()
    c.row_factory = sqlite3.Row

    print("=" * 70)
    print("event_atom 反 bias 报告 (source IN ('llm','llm_audit'))")
    print("=" * 70)

    rows = c.execute("""
        SELECT ea.id, det.code AS et_code, a.code AS asset_code,
               ea.direction, ea.direction_confidence, ea.direction_source,
               ea.raw_news_id, ea.actor, ea.reasoning,
               det.direction_bias AS bias_json
        FROM event_atom ea
        JOIN dim_event_type det ON det.id = ea.event_type_id
        LEFT JOIN dim_asset a ON a.id = ea.asset_id
        WHERE ea.direction != 'unknown'
          AND ea.direction_source IN ('llm', 'llm_audit', 'ai',
                                       'platform_published_at')
          AND a.code IS NOT NULL
    """).fetchall()

    # 按 (et_code, asset_code) 分桶 + 比较 bias
    conflicts_by_pair = {}
    ok_count = 0
    for r in rows:
        try:
            bias = json.loads(r["bias_json"] or "{}")
        except Exception:
            bias = {}
        asset = (r["asset_code"] or "").upper()
        bias_value = bias.get(asset)
        if not bias_value:
            continue  # 没有 bias 不算冲突
        bias_dir = CN_TO_DB.get(bias_value, "unknown")
        if bias_dir == "unknown":
            continue
        pair_key = (r["et_code"], asset)
        if pair_key not in conflicts_by_pair:
            conflicts_by_pair[pair_key] = {
                "et_code": r["et_code"],
                "asset": asset,
                "bias_dir": bias_dir,
                "rows": [],
            }
        is_conflict = (r["direction"] != bias_dir)
        if is_conflict:
            conflicts_by_pair[pair_key]["rows"].append(dict(r))
        else:
            ok_count += 1

    # 排序：冲突多的在前
    sorted_pairs = sorted(
        conflicts_by_pair.values(),
        key=lambda x: len(x["rows"]),
        reverse=True,
    )

    n_conflict_pairs = 0
    n_conflict_rows = 0
    for pair in sorted_pairs:
        if not pair["rows"]:
            continue
        n_conflict_pairs += 1
        n_conflict_rows += len(pair["rows"])
        print(f"\n  [CONFLICT] {pair['et_code']:<25} asset={pair['asset']:<10} "
              f"bias={pair['bias_dir']:<10} n_conflict={len(pair['rows'])}")
        for r in pair["rows"][:5]:  # 只展示前 5 行
            print(f"     id={r['id']:<5} src={r['direction_source']:<10} "
                  f"dir={r['direction']:<8} conf={r['direction_confidence']:.2f} "
                  f"news_id={r['raw_news_id']:<5} actor={r['actor'][:20]}")
        if len(pair["rows"]) > 5:
            print(f"     ... and {len(pair['rows']) - 5} more")

    print()
    print("=" * 70)
    print("总结")
    print("=" * 70)
    total_checked = ok_count + n_conflict_rows
    print(f"  总检查 (source=llm/ai 且有 bias): {total_checked}")
    print(f"  一致: {ok_count}")
    print(f"  冲突: {n_conflict_rows} (分布在 {n_conflict_pairs} 个 (et, asset) 对)")
    print()
    print("下一步建议：")
    print("  - 对每个冲突对 (et, asset)，人工 audit 后选:")
    print("    1) DELETE: 错了，删 row")
    print("    2) UPDATE bias: 改 dim_event_type.direction_bias 反映新共识")
    print("    3) MARK audit: UPDATE event_atom SET direction_source='llm_audit'")

    # 列出 audit SQL 模板
    print()
    print("=" * 70)
    print("audit 操作 SQL 模板")
    print("=" * 70)
    for pair in sorted_pairs[:3]:
        if not pair["rows"]:
            continue
        ids = [str(r["id"]) for r in pair["rows"]]
        print(f"\n  # 标记 {pair['et_code']}/{pair['asset']} 为 audit:")
        print(f"  UPDATE event_atom SET direction_source = 'llm_audit'")
        print(f"   WHERE id IN ({', '.join(ids[:5])});")
        if len(ids) > 5:
            print(f"   -- 还有 {len(ids) - 5} 个 id 未列出")
    c.close()


if __name__ == "__main__":
    main()