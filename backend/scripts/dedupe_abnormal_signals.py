#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""abnormal_signals 历史重复清理（v497）。

背景
----
`abnormal_detector.record_signal()` 是全表唯一写入口，原设计把去重交给调用方，
但回填脚本按 (资产 × 周期 × 指标) 逐轮调用，同一条 K 线同一指标被写 41 次：
实测 6340 行里只有 656 个不同的 (asset, ts, signal_kind)，
24h 窗口 5899 行 → 去重后 476 个 —— 面板"异动数"虚高 12 倍。

本脚本做的事
------------
1. 先把 abnormal_signals 整表 dump 到 backup 目录（可回滚）；
2. 按 (asset, ts, signal_kind) 只保留**最大 id**那一行（最新一次检测的 detail 最全）；
3. 被 `abnormal_attributions.signal_id` 引用的行一律不删，避免归因断链；
4. 默认 dry-run，只打印将删除多少行；加 --apply 才真的删。

用法
----
    python3 backend/scripts/dedupe_abnormal_signals.py            # 预演
    python3 backend/scripts/dedupe_abnormal_signals.py --apply    # 执行

回滚
----
    sqlite3 <db> < backup/abnormal_signals_YYYYMMDD_HHMMSS.sql
（备份是 INSERT 语句，直接把删掉的行插回即可，id 不变。）
"""
from __future__ import annotations

import argparse
import os
import sqlite3
import sys
from datetime import datetime

HERE = os.path.dirname(os.path.abspath(__file__))
BACKEND = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(BACKEND, "src_python"))

DUP_SQL = """
SELECT id FROM abnormal_signals
 WHERE id NOT IN (SELECT MAX(id) FROM abnormal_signals GROUP BY asset, ts, signal_kind)
   AND id NOT IN (SELECT signal_id FROM abnormal_attributions)
"""


def _db_path() -> str:
    try:
        import db as db_mod  # noqa: WPS433
        return str(db_mod.get_db_path())
    except Exception:
        return os.path.join(BACKEND, "trident_event_bus.db")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true", help="真的删除（默认只预演）")
    ap.add_argument("--db", default="", help="覆盖数据库路径")
    args = ap.parse_args()

    path = args.db or _db_path()
    if not os.path.exists(path):
        print(f"[dedupe] 数据库不存在: {path}")
        return 1

    conn = sqlite3.connect(path, timeout=30)
    try:
        total = conn.execute("SELECT COUNT(*) FROM abnormal_signals").fetchone()[0]
        unique = conn.execute(
            "SELECT COUNT(*) FROM (SELECT 1 FROM abnormal_signals "
            "GROUP BY asset, ts, signal_kind)"
        ).fetchone()[0]
        dup_ids = [int(r[0]) for r in conn.execute(DUP_SQL).fetchall()]
        protected = conn.execute(
            "SELECT COUNT(*) FROM abnormal_attributions WHERE signal_id IN "
            "(SELECT signal_id FROM abnormal_attributions)"
        ).fetchone()[0]

        print(f"[dedupe] 库: {path}")
        print(f"[dedupe] 现有 {total} 行，唯一键(asset,ts,kind) {unique} 个，"
              f"可删重复 {len(dup_ids)} 行，归因引用保护 {protected} 行")
        if not dup_ids:
            print("[dedupe] 没有需要清理的重复行")
            return 0
        if not args.apply:
            print("[dedupe] 预演模式，未改动。加 --apply 执行。")
            return 0

        backup_dir = os.path.join(BACKEND, "backup")
        os.makedirs(backup_dir, exist_ok=True)
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        backup = os.path.join(backup_dir, f"abnormal_signals_{stamp}.sql")
        with open(backup, "w", encoding="utf-8") as fh:
            for row in conn.execute(
                "SELECT id, ts, asset, signal_kind, direction, anchor_ts, "
                "detail_json, hint, status, created_at FROM abnormal_signals "
                "WHERE id IN (%s)" % ",".join(str(i) for i in dup_ids)
            ):
                vals = ",".join(
                    "NULL" if v is None else
                    str(int(v)) if isinstance(v, (int, float)) and not isinstance(v, bool) else
                    "'" + str(v).replace("'", "''") + "'"
                    for v in row
                )
                fh.write(
                    "INSERT INTO abnormal_signals (id, ts, asset, signal_kind, direction, "
                    "anchor_ts, detail_json, hint, status, created_at) VALUES (%s);\n" % vals
                )
        print(f"[dedupe] 已备份 {len(dup_ids)} 行 → {backup}")

        deleted = conn.execute(
            "DELETE FROM abnormal_signals WHERE id IN (%s)" % ",".join(str(i) for i in dup_ids)
        ).rowcount
        conn.commit()
        left = conn.execute("SELECT COUNT(*) FROM abnormal_signals").fetchone()[0]
        print(f"[dedupe] 已删除 {deleted} 行，剩余 {left} 行")
        return 0
    finally:
        conn.close()


if __name__ == "__main__":
    raise SystemExit(main())
