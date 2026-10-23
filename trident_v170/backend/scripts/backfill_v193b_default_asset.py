#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""v193 #2: 给 asset=NULL 的 event_atom 填默认资产（兜底）。

v193 #1 修复后，剩下 23 个 direction=unknown：
- 17 个 market_event + asset=NULL （catch-all 类别，没 bias 是设计，**保留 unknown**）
- 6 个 geopolitical_conflict + asset=NULL （news 没标 asset）

修这 6 个：geopolitical_conflict 默认填 **XAU**（黄金避险，第一关联资产），
direction 就能从 unknown → bullish（与 bias['XAU']='bullish' 对齐）。

实现：
- 创建 "XAU" 默认 asset_id 记录
- 对所有 direction='unknown' AND asset_id=0 AND event_type_code IN
  {'geopolitical_conflict', 'geopolitical_easing'} 的 row：
  - UPDATE asset_id = XAU 的 id
  - UPDATE direction = bias[XAU]
  - UPDATE direction_source='rule_bias_default_asset'
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

# 哪些 event_type 适用 default asset + 默认 asset
DEFAULT_ASSET_RULES = {
    "geopolitical_conflict": ("XAU", "黄金避险第一关联"),
    "geopolitical_easing": ("XAU", "黄金避险缓解"),
}


def main():
    c = db.get_connection()
    c.row_factory = sqlite3.Row

    # ── before ──
    before = c.execute("""
        SELECT direction, COUNT(*) AS n
        FROM event_atom
        GROUP BY direction
    """).fetchall()
    n_before_unknown = sum(r["n"] for r in before if r["direction"] == "unknown")
    print(f"[before] direction=unknown 总数: {n_before_unknown}")

    # ── resolve asset XAU id ──
    xau_row = c.execute(
        "SELECT id FROM dim_asset WHERE code='XAU' LIMIT 1"
    ).fetchone()
    if not xau_row:
        # XAU 不在 dim_asset 里，先创建
        import dim_tables
        xau_id = dim_tables.get_or_create_asset(
            "XAU", name="Gold", kind="commodity", venue="spot",
            connection=c,
        )
        print(f"  [create] dim_asset XAU id={xau_id}")
    else:
        xau_id = xau_row["id"]
        print(f"  [reuse] dim_asset XAU id={xau_id}")

    # ── build (event_type_code -> bias) ──
    bias_map = {}
    for r in c.execute("SELECT code, direction_bias FROM dim_event_type").fetchall():
        try:
            bias_map[r["code"]] = json.loads(r["direction_bias"] or "{}")
        except Exception:
            bias_map[r["code"]] = {}

    # ── scan + UPDATE ──
    filled = 0
    skipped = 0
    for et_code, (asset, reason) in DEFAULT_ASSET_RULES.items():
        rows = c.execute("""
            SELECT ea.id, ea.event_type_id, ea.asset_id
            FROM event_atom ea
            JOIN dim_event_type det ON det.id = ea.event_type_id
            WHERE det.code = ?
              AND ea.direction = 'unknown'
              AND (ea.asset_id = 0 OR ea.asset_id IS NULL)
        """, (et_code,)).fetchall()
        for r in rows:
            bias = bias_map.get(et_code, {})
            if asset in bias:
                new_dir = CN_TO_DB.get(bias[asset], "unknown")
                if new_dir != "unknown":
                    # 检查是否会触发 UNIQUE(raw_news_id, event_type_id, event_time, asset_id)
                    dup = c.execute("""
                        SELECT id FROM event_atom
                        WHERE raw_news_id = (
                                SELECT raw_news_id FROM event_atom WHERE id = ?
                              )
                          AND event_type_id = ?
                          AND event_time = (
                                SELECT event_time FROM event_atom WHERE id = ?
                              )
                          AND asset_id = ?
                          AND id != ?
                    """, (r["id"], r["event_type_id"], r["id"], xau_id, r["id"])).fetchone()
                    if dup:
                        # 冲突：同 (news, type, time) 已有 XAU 行 — 跳过 UPDATE
                        skipped += 1
                        print(f"  [skip-dup] id={r['id']} {et_code} 同 news 已存在 XAU 行 id={dup['id']}")
                        continue
                    c.execute("""
                        UPDATE event_atom
                        SET asset_id = ?, direction = ?,
                            direction_confidence = 0.5,
                            direction_source = 'rule_bias_default_asset'
                        WHERE id = ?
                    """, (xau_id, new_dir, r["id"]))
                    filled += 1
                    print(f"  [OK] id={r['id']} {et_code} → {asset}={new_dir}")
                else:
                    skipped += 1
            else:
                skipped += 1
                print(f"  [skip] id={r['id']} {et_code} no bias for {asset}")

    c.commit()
    print(f"\n  filled: {filled}, skipped: {skipped}")

    # ── after ──
    after = c.execute("""
        SELECT direction, COUNT(*) AS n
        FROM event_atom
        GROUP BY direction
    """).fetchall()
    n_after_unknown = sum(r["n"] for r in after if r["direction"] == "unknown")
    print(f"[after]  direction=unknown 总数: {n_after_unknown}")
    print(f"  回收: {n_before_unknown - n_after_unknown}")
    c.close()


if __name__ == "__main__":
    main()