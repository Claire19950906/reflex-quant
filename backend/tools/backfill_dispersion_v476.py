#!/usr/bin/env python3
"""v476 回填：把 decision_fundamental_snapshot 里历史行的分歧度按 v472 口径算出来。

背景：`dispersion` / `dispersion_level` / `confidence` 三列 v473 就建好了，但写入时
从 `components` 子字典取顶层键，永远取到 None —— 205 行全空。列建了却永远为空，
等于没建。这个脚本用与 `fundamental_shadow._dispersion_from_components` **同一公式**
把历史行的分量还原成三个数（外加多空权重），之后的新行由写入链路自己填。

幂等：只更新 dispersion IS NULL 的行；重复跑不会改动已回填的值。
用法：cd backend && python3 tools/backfill_dispersion_v476.py [--dry-run]
"""
from __future__ import annotations

import json
import os
import sqlite3
import sys

DB = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "trident_event_bus.db",
)


def dispersion(score, comps):
    vals = []
    for c in (comps or {}).values():
        if not isinstance(c, dict):
            continue
        s, w = c.get("score"), c.get("weight")
        if s is None or w is None:
            continue
        try:
            vals.append((float(s), float(w)))
        except (TypeError, ValueError):
            continue
    usable = [(s, w) for s, w in vals if w > 0]
    if len(usable) < 2:
        return None
    wsum = sum(w for _, w in usable)
    mean = sum(s * w for s, w in usable) / wsum
    disp = (sum(w * (s - mean) ** 2 for s, w in usable) / wsum) ** 0.5
    level = "高" if disp >= 0.60 else ("中" if disp >= 0.35 else "低")
    return (round(disp, 3), level, round(max(0.0, 1.0 - disp / 1.5), 3),
            round(sum(w for s, w in usable if s > 0.05), 2),
            round(sum(w for s, w in usable if s < -0.05), 2))


def main():
    dry = "--dry-run" in sys.argv
    conn = sqlite3.connect(DB, timeout=30)
    cols = {r[1] for r in conn.execute("PRAGMA table_info(decision_fundamental_snapshot)")}
    for col, typ in (("dispersion", "REAL"), ("dispersion_level", "TEXT"),
                     ("confidence", "REAL"), ("bull_weight", "REAL"),
                     ("bear_weight", "REAL")):
        if col not in cols:
            conn.execute(f"ALTER TABLE decision_fundamental_snapshot ADD COLUMN {col} {typ}")
    rows = conn.execute(
        "SELECT decision_id, fund_score, components FROM decision_fundamental_snapshot "
        "WHERE dispersion IS NULL AND components IS NOT NULL"
    ).fetchall()
    filled = skipped = 0
    for did, score, raw in rows:
        try:
            parsed = json.loads(raw or "{}")
        except Exception:
            parsed = {}
        d = dispersion(score, parsed.get("components") or {})
        if not d:
            skipped += 1
            continue
        filled += 1
        if dry:
            print(did, d)
        else:
            conn.execute(
                "UPDATE decision_fundamental_snapshot SET dispersion=?, dispersion_level=?, "
                "confidence=?, bull_weight=?, bear_weight=? WHERE decision_id=?",
                (*d, did))
    if not dry:
        conn.commit()
    total = conn.execute("SELECT COUNT(*), COUNT(dispersion) FROM decision_fundamental_snapshot").fetchone()
    print(f"待回填 {len(rows)} · 成功 {filled} · 分量不足跳过 {skipped} · "
          f"表内合计 {total[0]} 行 / 已有值 {total[1]} 行{'（dry-run，未写库）' if dry else ''}")
    conn.close()


if __name__ == "__main__":
    main()
