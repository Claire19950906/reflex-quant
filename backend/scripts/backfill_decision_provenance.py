# -*- coding: utf-8 -*-
"""v399: 回填 ai_decisions 的两处来源留痕（asset_route / hard_data_status）。

判据全部在 `decision_provenance.py`（纯函数，只描述事实，不补造数据）。
默认 dry-run，`--commit` 才写库；只处理两列为空的行，可重复跑。

用法：
  python3 scripts/backfill_decision_provenance.py
  python3 scripts/backfill_decision_provenance.py --commit
"""
from __future__ import annotations

import argparse
import collections
import os
import sqlite3
import sys

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(BASE_DIR, "src_python"))
DB_PATH = os.path.join(BASE_DIR, "trident_event_bus.db")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--commit", action="store_true")
    args = ap.parse_args()

    import db as _db
    from signals import decision_provenance as dp

    conn = sqlite3.connect(DB_PATH, timeout=30)
    conn.row_factory = sqlite3.Row
    try:
        _db._ensure_columns(conn, "ai_decisions", _db._AI_DECISIONS_COLUMNS)
        conn.commit()
    except sqlite3.Error as exc:
        print(f"迁移失败: {exc}")
        return 1

    rows = conn.execute(
        "SELECT id, target_asset, COALESCE(hard_data_evidence,'') ev, "
        "COALESCE(asset_route,'') r, COALESCE(hard_data_status,'') h "
        "FROM ai_decisions WHERE COALESCE(asset_route,'')='' "
        "   OR COALESCE(hard_data_status,'')=''"
    ).fetchall()

    routes: collections.Counter = collections.Counter()
    hds: collections.Counter = collections.Counter()
    changed = 0
    for r in rows:
        route, hd = dp.tags(r["target_asset"], r["ev"])
        routes[route] += 1
        hds[hd] += 1
        if not r["r"] or not r["h"]:
            changed += 1
            if args.commit:
                conn.execute(
                    "UPDATE ai_decisions SET asset_route=?, hard_data_status=? WHERE id=?",
                    (route, hd, int(r["id"])),
                )
    if args.commit:
        conn.commit()

    print(f"待回填 {len(rows)} 行 / 实际改动 {changed} 行")
    print("asset_route:", dict(routes.most_common(6)))
    print("hard_data_status:", dict(hds.most_common(8)))
    print("[commit] 已写库" if args.commit else "[dry-run] 未写库，确认后加 --commit")
    conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
