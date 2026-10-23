#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""v193 #1: 回填 event_atom.direction='unknown' 的历史行。

历史问题：dim_event_type.direction_bias 之前是空的（13/13 default '{}'），
导致 extract_and_persist() 写入的 event_atom.direction 全部 'unknown'。
修复后：seed_event_dim.seed_all() 触发 get_or_create_event_type 的 UPDATE 分支，
24/27 行已填 bias。剩下 3 个空的是规则不抢答（market_event/oil_inventory/cpi_release）。

本脚本：把 event_atom 里 direction='unknown' 的行，对 (event_type_code, asset_code)
查 dim_event_type.direction_bias，能查到的 UPDATE direction='bullish/bearish'。
查不到（asset 不在 bias dict / event_type 是中性规则）的保持 unknown。

特点：
- 只动 source='rule_bias' 或未指定的方向源，不覆盖 LLM/AI 已经填的方向
- 默认 direction_confidence=0.7（与 extract_and_persist 一致）
- 报告 before/after 统计 + direction_source 分布
"""
from __future__ import annotations
import json
import sqlite3
import sys
import os

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "src_python"))

import db
import dim_tables

CN_TO_DB = {"利多": "bullish", "利空": "bearish", "中性": "neutral",
            "bullish": "bullish", "bearish": "bearish", "neutral": "neutral"}


def main():
    conn = db.get_connection()
    conn.row_factory = sqlite3.Row

    # ── before stats ──
    before = conn.execute("""
        SELECT direction, COUNT(*) AS n
        FROM event_atom
        GROUP BY direction
    """).fetchall()
    print("[before] event_atom.direction 分布:")
    for r in before:
        print(f"  {r['direction']:<10} {r['n']:>4}")

    # ── build (event_type_id -> direction_bias_dict) -> (asset_code -> direction) ──
    bias_map = {}
    for r in conn.execute(
        "SELECT id, code, direction_bias FROM dim_event_type"
    ).fetchall():
        try:
            bias_map[r["id"]] = {
                "code": r["code"],
                "bias": json.loads(r["direction_bias"] or "{}"),
            }
        except Exception:
            bias_map[r["id"]] = {"code": r["code"], "bias": {}}

    # ── UPDATE 那些 direction='unknown' 且 source 是 'rule_bias' 或空的 ──
    # event_atom 用 event_type_id (FK) + asset_id (FK)，所以 JOIN dim_event_type/dim_asset
    rows = conn.execute("""
        SELECT ea.id, ea.event_type_id, ea.asset_id, ea.direction_source,
               det.code AS et_code, a.code AS asset_code
        FROM event_atom ea
        JOIN dim_event_type det ON det.id = ea.event_type_id
        LEFT JOIN dim_asset a ON a.id = ea.asset_id
        WHERE ea.direction = 'unknown'
          AND (ea.direction_source IS NULL
               OR ea.direction_source = ''
               OR ea.direction_source = 'rule_bias')
    """).fetchall()

    filled = 0
    skipped_no_bias = 0
    skipped_wrong_source = 0
    for r in rows:
        et_info = bias_map.get(r["event_type_id"], {})
        bias = et_info.get("bias", {})
        asset = (r["asset_code"] or "").upper()
        if asset in bias:
            new_dir = CN_TO_DB.get(bias[asset], "unknown")
            if new_dir != "unknown":
                conn.execute("""
                    UPDATE event_atom
                    SET direction = ?, direction_confidence = 0.7,
                        direction_source = 'rule_bias'
                    WHERE id = ?
                """, (new_dir, r["id"]))
                filled += 1
                continue
        skipped_no_bias += 1

    conn.commit()

    # ── after stats ──
    after = conn.execute("""
        SELECT direction, COUNT(*) AS n
        FROM event_atom
        GROUP BY direction
    """).fetchall()
    print()
    print("[after] event_atom.direction 分布:")
    total = 0
    unknown_n = 0
    for r in after:
        print(f"  {r['direction']:<10} {r['n']:>4}")
        total += r["n"]
        if r["direction"] == "unknown":
            unknown_n = r["n"]
    print()
    rate = (1 - unknown_n / max(total, 1)) * 100
    print(f"  填充率: {rate:.1f}% (期望 >= 80%)")
    print(f"  filled: {filled}")
    print(f"  skipped_no_bias: {skipped_no_bias}")
    conn.close()


if __name__ == "__main__":
    main()