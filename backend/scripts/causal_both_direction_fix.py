#!/usr/bin/env python3
"""方向中性（BOTH）链的假命中清洗（v429）。

问题：`causal_chain_outcomes.direction_hit` 对 BOTH 链被定义成
"abs_pct >= mag_lo"，只要波动够大就记成"方向命中"。实测全库 611 条
（404 HIT + 207 HIT_WEAK）是这种"恒真命中"，它们混进链级准确率与
real_hit_rate，把"验证通过的机制"比例抬成假象（真实方向命中率仅 49.8%）。

处置：
  1) BOTH 行的 direction_hit 置 NULL（方向命中列只表达"方向判断对不对"，
     没判断就没有命中，所有聚合自动跳过）；
  2) 状态按幅度重新分档：magnitude_hit=1 → HIT；abs_pct>=mag_lo → HIT_WEAK；
     否则 NEUTRAL（方向中性链只能核"事件有没有推动价格"）。

用法：
    python3 scripts/causal_both_direction_fix.py            # 只看
    python3 scripts/causal_both_direction_fix.py --apply    # 写库
"""
import argparse
import csv
import os
import sqlite3
import sys
import time

BACKEND = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def plan(conn):
    return conn.execute(
        """SELECT id, chain_id, predicted_direction, status, direction_hit,
                  magnitude_hit, actual_pct_5d, predicted_mag_lo, predicted_mag_hi
           FROM causal_chain_outcomes
           WHERE status IN ('HIT','HIT_WEAK','MISS','NEUTRAL')
             AND (predicted_direction IN ('BOTH','') OR predicted_direction IS NULL)
             AND direction_hit IS NOT NULL
           ORDER BY id"""
    ).fetchall()


def new_status(row):
    mag_hit = row["magnitude_hit"] or 0
    abs_pct = abs(float(row["actual_pct_5d"] or 0.0))
    mag_lo = float(row["predicted_mag_lo"] or 0.0)
    if mag_hit:
        return "HIT"
    if abs_pct >= mag_lo:
        return "HIT_WEAK"
    return "NEUTRAL"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--db", default=os.environ.get("TRIDENT_DB_PATH")
                    or os.path.join(BACKEND, "trident_event_bus.db"))
    args = ap.parse_args()

    conn = sqlite3.connect(args.db, timeout=30)
    conn.row_factory = sqlite3.Row
    rows = plan(conn)
    print("[both-fix] 待处理 BOTH 假命中 %d 条" % len(rows))

    changes = []
    for r in rows:
        ns = new_status(r)
        if r["status"] != ns or r["direction_hit"] is not None:
            changes.append((r["id"], r["chain_id"], r["status"], ns,
                            r["direction_hit"], r["actual_pct_5d"]))

    from collections import Counter
    print("[both-fix] 状态迁移:", dict(Counter("%s→%s" % (c[2], c[3]) for c in changes)))

    out_dir = os.path.join(BACKEND, "runtime", "outcome_fix")
    os.makedirs(out_dir, exist_ok=True)
    path = os.path.join(out_dir, "both_direction_fix_%s.csv"
                        % time.strftime("%Y%m%d_%H%M%S"))
    with open(path, "w", newline="", encoding="utf-8-sig") as fh:
        w = csv.writer(fh)
        w.writerow(["outcome_id", "chain_id", "status_before", "status_after",
                    "direction_hit_before", "actual_pct_5d"])
        w.writerows(changes)
    print("[both-fix] 明细: %s" % path)

    if not args.apply:
        print("[both-fix] dry-run，未写库（加 --apply 执行）")
        return

    done = 0
    for r in rows:
        ns = new_status(r)
        for attempt in range(30):
            try:
                conn.execute(
                    "UPDATE causal_chain_outcomes SET status=?, direction_hit=NULL "
                    "WHERE id=?", (ns, r["id"]))
                done += 1
                break
            except sqlite3.OperationalError as exc:
                if "locked" not in str(exc):
                    raise
                time.sleep(0.5 * (attempt + 1))
        else:
            print("  [warn] outcome %s 写入仍被锁，跳过" % r["id"])
        if done % 100 == 0:
            conn.commit()
    conn.commit()
    print("[both-fix] 已更新 %d 条" % done)

    # 处理后的真实方向命中率
    row = conn.execute(
        """SELECT COUNT(*) n, SUM(direction_hit) k FROM causal_chain_outcomes
           WHERE status IN ('HIT','HIT_WEAK','MISS') AND direction_hit IS NOT NULL"""
    ).fetchone()
    print("[both-fix] 方向命中率（仅 UP/DOWN）: %d/%d = %.1f%%"
          % (row["k"] or 0, row["n"] or 0, 100.0 * (row["k"] or 0) / max(1, row["n"] or 0)))
    conn.close()


if __name__ == "__main__":
    main()
