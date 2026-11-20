#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
dedupe_signals.py —— 清理 abnormal_signals 里的重复签名

为什么做：
  `multi_factor` / `sector` 这两类签名原本每根 3 分钟 K 线就写一条，
  去重键带 anchor_ts（锚随 K 线变），所以同一标的同一方向能在一天里写上百遍。
  实测 59,652 条 multi_factor 按"资产+方向+日"聚合后只剩 461 条 —— **99.2% 是冗余**。
  它不会让判断变准，只会把信号流和后续统计淹掉。

做法（保守，可逆）：
  - 默认处理**所有** open 类型（原来只清 multi_factor / sector，其他类型照样堆）；
  - 每个 (asset, signal_kind, direction, 自然日) 保留**最新一条**，
    其余状态改成 `deduped`（不是删除，随时可查、可回滚）；
  - 保留原 ts / detail，不修不改，只改 status。

用法：
  python3 dedupe_signals.py --dry-run
  python3 dedupe_signals.py
"""
import argparse
import sqlite3
import sys
import time
from typing import List, Optional

import db

KINDS = ("multi_factor", "sector")
_STATE_KINDS = KINDS   # 状态型签名：会原地刷新，过期窗口给长一点


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    # 默认改为"库里所有 open 类型"：原来只清 multi_factor / sector，
    # 结果 anomaly_* 这些照样一天写十几条，信号板照样被淹。
    ap.add_argument("--expire-hours", type=float, default=48.0,
                    help="普通签名 open 超过这么多小时即视为过期")
    ap.add_argument("--state-expire-hours", type=float, default=168.0,
                    help="状态型签名（multi_factor/sector）过期窗口，默认 7 天")
    ap.add_argument("--no-expire", action="store_true")
    ap.add_argument("--kinds", default="ALL",
                    help="逗号分隔的类型；ALL=库里所有 open 类型")
    args, _ = ap.parse_known_args(argv)

    kinds = ([k.strip() for k in args.kinds.split(",") if k.strip()]
             if args.kinds != "ALL" else None)
    conn = sqlite3.connect(db.get_db_path(), timeout=30, isolation_level=None)
    conn.execute("PRAGMA busy_timeout=30000")
    if kinds is None:
        kinds = [r[0] for r in conn.execute(
            """SELECT signal_kind, count(*) c FROM abnormal_signals
               WHERE status='open' GROUP BY 1 ORDER BY c DESC""").fetchall()]

    total_marked = 0
    for kind in kinds:
        rows = conn.execute(
            """SELECT id, asset, direction, ts FROM abnormal_signals
               WHERE status='open' AND signal_kind=? ORDER BY ts""", (kind,)).fetchall()
        # 找出每个 (asset, direction, day) 里的最新一条 id
        keep: dict = {}
        groups: dict = {}
        for rid, asset, direction, ts in rows:
            day = int(ts) // 86400 if ts else 0
            key = (asset, direction, day)
            groups.setdefault(key, []).append(rid)
            keep[key] = rid                    # ORDER BY ts，最后一条就是最新
        to_mark = [rid for key, ids in groups.items()
                   for rid in ids if rid != keep[key]]
        print(f"  {kind:<14} open {len(rows):>6}  组 {len(groups):>4}  可标 deduped {len(to_mark):>6}")
        if args.dry_run or not to_mark:
            continue
        # 分批更新，避免一次性锁太久
        for i in range(0, len(to_mark), 500):
            chunk = to_mark[i:i + 500]
            conn.execute(
                f"UPDATE abnormal_signals SET status='deduped' WHERE id IN ({','.join('?' * len(chunk))})",
                chunk)
        total_marked += len(to_mark)

    # —— 过期收敛 ——
    # 只改状态不改数：open 挂几个月不关，前端默认只显示 open，
    # 结果看板上一直挂着 10-08 的异动，看着像"现在还在发生"。
    expired = 0
    if not args.no_expire:
        now = int(time.time())
        for label, kinds_set, hours in (
                ("状态型", set(_STATE_KINDS), args.state_expire_hours),
                ("普通", None, args.expire_hours)):
            cutoff = now - int(hours * 3600)
            sql = "SELECT id FROM abnormal_signals WHERE status='open' AND ts < ?"
            params: list = [cutoff]
            if kinds_set is not None:
                sql += f" AND signal_kind IN ({','.join('?' * len(kinds_set))})"
                params += list(kinds_set)
            else:
                # 普通签名要排除状态型，否则同一批行会被两类统计各算一遍
                sql += f" AND signal_kind NOT IN ({','.join('?' * len(_STATE_KINDS))})"
                params += list(_STATE_KINDS)
            ids = [r[0] for r in conn.execute(sql, params).fetchall()]
            print(f"  {label} open 超 {hours:g}h：{len(ids)} 条可标 expired")
            if args.dry_run or not ids:
                continue
            for i in range(0, len(ids), 500):
                chunk = ids[i:i + 500]
                conn.execute(
                    f"UPDATE abnormal_signals SET status='expired' WHERE id IN ({','.join('?' * len(chunk))})",
                    chunk)
            expired += len(ids)

    if not args.dry_run:
        conn.commit()
        print(f"  已标记 {total_marked} 条为 deduped、{expired} 条为 expired（未删除，可回滚）")
    conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
