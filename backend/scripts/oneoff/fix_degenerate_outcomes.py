#!/usr/bin/env python3
"""清理"同一个常数被当成结果"的伪结算行。

背景（v343）：
    missed_signal_taxonomy.compute_outcome 原来允许取 ts+5d ±2 天内的"最近一根"。
    纪元初期行情只有 1 根日线，这个兜底就退化成"拿相邻几小时的两次收盘相减"，
    于是**几十个不同事件算出完全相同的数** —— 实测 XAU 有 18 行、跨 6 个不同
    时间点，actual_outcome_pct 全是 1.461，并据此判成 hit=1。

判定口径（保守，只动明显是常数的行）：
    同一资产同一 dimension 下，某个 actual_outcome_pct 值出现在 >=3 个**不同**
    时间点上 —— 真实行情的 5 日涨跌不可能反复出现同一个四位小数。

处理：
    用修好的 compute_outcome 重算；重算仍拿不到（说明窗口根本没走完）→
    把 actual_outcome_pct 置 NULL、hit 置 NULL、settled 置 0，并在文本后追加
    "【结算作废：当时行情不足以覆盖 5 日窗口】"。
    不改 created_at、不动别的列。

用法：
    python3 fix_degenerate_outcomes.py            # 试运行
    python3 fix_degenerate_outcomes.py --apply
"""
from __future__ import annotations

import argparse
import sqlite3
import sys
from datetime import datetime, timezone, timedelta
from collections import defaultdict

import sys as _sys
from pathlib import Path as _Path
_sys.path.insert(0, str(_Path(__file__).resolve().parents[2] / "src_python"))
import db
import db_retry
import missed_signal_taxonomy as mst

BJ = timezone(timedelta(hours=8))


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--min-points", type=int, default=3,
                    help="同一数值出现在多少个不同时间点以上算'可疑常数'")
    args = ap.parse_args()

    conn = sqlite3.connect(db.get_db_path(), timeout=30, isolation_level=None)
    conn.execute("PRAGMA busy_timeout=30000")

    # 只看预测类维度（A/B）且结果不为 0 的行：
    # C 维的 pct=0.0 是"未测到"的正常占位，混进来会把 500 多条正常行当常数处理。
    rows = conn.execute(
        "SELECT id, asset, dimension, created_at, actual_outcome_pct, hit, settled "
        "FROM trade_reflections_v2 WHERE actual_outcome_pct IS NOT NULL "
        "AND dimension IN ('A','B') AND ABS(actual_outcome_pct) > 0.0001"
    ).fetchall()

    # 同一个 (asset, dimension, 数值) 覆盖了多少个不同时间点
    spread: dict[tuple, set] = defaultdict(set)
    for _id, asset, dim, created, pct, _hit, _st in rows:
        spread[(asset, dim, round(float(pct), 4))].add(str(created))

    suspects = {k for k, v in spread.items() if len(v) >= args.min_points}
    print(f"扫描 {len(rows)} 条有结果的反思 · 可疑常数 {len(suspects)} 组")
    for k in sorted(suspects, key=lambda x: -len(spread[x])):
        print(f"  {k[0]:<8} {k[1]} pct={k[2]:<8} 覆盖 {len(spread[k])} 个不同时间点")

    fixed = kept = 0
    for _id, asset, dim, created, pct, hit, settled in rows:
        if (asset, dim, round(float(pct), 4)) not in suspects:
            continue
        try:
            sec = int(datetime.fromisoformat(str(created)).replace(tzinfo=BJ).timestamp())
        except ValueError:
            continue
        real = mst.compute_outcome(conn, asset, sec, 5)
        if real is not None:
            kept += 1
            continue
        fixed += 1
        if args.apply:
            # 引擎在写、这里也在写 —— 裸 UPDATE 会直接撞 SQLITE_BUSY（实测一次）
            db_retry.run_write(conn, lambda c: c.execute(
                "UPDATE trade_reflections_v2 SET actual_outcome_pct=NULL, hit=NULL, "
                "settled=0, reflection_text=COALESCE(reflection_text,'') || ? "
                "WHERE id=?",
                ("【结算作废：当时行情不足以覆盖 5 日窗口，原值疑为相邻两根相减】", _id)))

    print(f"\n结论：作废 {fixed} 条 · 重算仍有真结果保留 {kept} 条")
    if not args.apply:
        print("(试运行，未写库 — 加 --apply 生效)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
