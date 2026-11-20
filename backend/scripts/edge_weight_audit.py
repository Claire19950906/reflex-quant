#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""edge_weight_audit.py — v410: 图谱边权重的量程体检

背景（2026-11-17 实测）
----------------------
`asset_driver_edges.weight` 在设计上被当成 0–10 的"强度"：`causal_graph._upsert_edge`
累加上限写死 10.0，`update_cross_asset_weights` 也是 `共现率 * 10`。
但库里有 281 条边超过 10（supports max 40 / contradicts max 41）——因为
supports / contradicts 这两类边记的是**票数**（命中/反驳累计次数），不是强度。

于是同一列里混了两种语义：
  causes / conditions / correlates_with : 0–10 强度
  supports / contradicts               : 无上限票数

直接"全表归一 0–1"会把票数语义抹掉（41:10 的差别在归一会变成 0.9:0.1，可用，
但 41 和 5 谁多谁少得靠别的方式保留）。所以本脚本只做两件事：
  1. 体检：按 kind 打印量程、超量程条数；
  2. 可选归一：`--normalize --apply` 才把**超量程**的 kind 按组内 max 压到 0–10，
     并把原值写进 `weight_raw` 列（新增，可回滚），不动 causes/conditions。

用法
    python3 scripts/edge_weight_audit.py
    python3 scripts/edge_weight_audit.py --normalize            # 看方案，不写库
    python3 scripts/edge_weight_audit.py --normalize --apply    # 真要写
"""
from __future__ import annotations

import argparse
import sqlite3
import sys
import time
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[1]
DB_PATH = BACKEND / "trident_event_bus.db"
AUDIT_DIR = BACKEND / "runtime" / "chain_cleanup"
sys.path.insert(0, str(BACKEND / "src_python"))

# 语义分组：这几类是"强度"（0–10），其余按票数
INTENSITY_KINDS = {"causes", "conditions", "correlates_with", "precedes", "affects"}
CAP = 10.0


def _report(conn: sqlite3.Connection) -> list:
    rows = conn.execute(
        "SELECT kind, COUNT(*), MIN(weight), AVG(weight), MAX(weight), "
        "       SUM(CASE WHEN weight > ? THEN 1 ELSE 0 END) "
        "FROM asset_driver_edges GROUP BY kind ORDER BY 2 DESC",
        (CAP,),
    ).fetchall()
    print("%-16s %6s %7s %7s %7s %8s  %s" % ("kind", "count", "min", "avg", "max", ">10", "语义"))
    for kind, n, mn, avg, mx, over in rows:
        sem = "强度 0–10" if kind in INTENSITY_KINDS else "票数（无上限）"
        flag = "⚠" if over else " "
        print("%-16s %6d %7.2f %7.2f %7.2f %8s%s %s" % (kind, n, mn, avg, mx, over, flag, sem))
    return rows


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--normalize", action="store_true", help="给出超量程 kind 的压缩方案")
    ap.add_argument("--apply", action="store_true", help="真写库（需同时给 --normalize）")
    args = ap.parse_args()

    conn = sqlite3.connect(str(DB_PATH), timeout=60.0)
    conn.row_factory = sqlite3.Row
    print("库: %s\n" % DB_PATH)
    rows = _report(conn)

    over_kinds = [r[0] for r in rows if r[5] and r[0] not in INTENSITY_KINDS]
    if not over_kinds:
        print("\n无超量程边，无需处理。")
        return 0
    print("\n超量程的 kind: %s（票数语义，不能按强度解读）" % ", ".join(over_kinds))
    print("建议：读取端按 kind 分开用；若要统一展示，可压到 0–10 并保留原值 weight_raw。")

    if not args.normalize:
        return 0

    cols = {r[1] for r in conn.execute("PRAGMA table_info(asset_driver_edges)")}
    if "weight_raw" not in cols:
        print("新增列 weight_raw（原始值，可回滚）" + ("…" if args.apply else "（dry-run 不执行）"))
        if args.apply:
            conn.execute("ALTER TABLE asset_driver_edges ADD COLUMN weight_raw REAL")
            conn.commit()

    now = time.strftime("%Y-%m-%d %H:%M:%S")
    plan = []
    for kind in over_kinds:
        mx = conn.execute(
            "SELECT MAX(weight) FROM asset_driver_edges WHERE kind=?", (kind,)
        ).fetchone()[0] or 1.0
        n = conn.execute(
            "SELECT COUNT(*) FROM asset_driver_edges WHERE kind=? AND weight > ?",
            (kind, CAP)).fetchone()[0]
        scale = CAP / mx
        plan.append((kind, n, mx, scale))
        print("  %s: %d 条 >10，组内 max=%.1f → 缩放系数 %.3f" % (kind, n, mx, scale))
        if args.apply:
            conn.execute(
                "UPDATE asset_driver_edges SET weight_raw=COALESCE(weight_raw, weight), "
                "weight=ROUND(weight * ?, 2), updated_at=? "
                "WHERE kind=? AND weight > ?", (scale, now, kind, CAP))

    if args.apply:
        conn.commit()
        AUDIT_DIR.mkdir(parents=True, exist_ok=True)
        path = AUDIT_DIR / ("edges-%s.jsonl" % time.strftime("%Y%m%d-%H%M%S"))
        with path.open("w", encoding="utf-8") as fh:
            for kind, n, mx, scale in plan:
                fh.write('{"kind":"%s","rows":%d,"max":%.2f,"scale":%.4f}\n'
                         % (kind, n, mx, scale))
        print("\n已落库。审计: %s" % path)
    else:
        print("\n（dry-run：未写库。加 --apply 落库，原值会存进 weight_raw）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
