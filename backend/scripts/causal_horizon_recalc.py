#!/usr/bin/env python3
"""按分档窗口重算存量样本（v432）。

只重算"窗口真的要变"的链（脉冲型：原 T+5 → 新 T+1；趋势型窗口不变，跳过）。
做法：把该 outcome 置回 PENDING、截止时间调到过去，然后调用评估器自身的
`evaluate_one(force=True)` —— 保证新分数与在线评估逐条同源，不另写一套逻辑。

审计 CSV 记下重算前的 status / direction_hit / actual_pct_5d，可逐条回滚。
"""
import argparse
import csv
import os
import sqlite3
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "src_python"))
import causal_chain_evaluator as cce

DEFAULT_DB = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "trident_event_bus.db")
OUT_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "runtime", "horizon_recalc")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--db", default=DEFAULT_DB)
    args = ap.parse_args()
    conn = sqlite3.connect(args.db, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout=30000")

    rows = conn.execute(
        """SELECT o.id, o.chain_id, o.status, o.direction_hit, o.actual_pct_5d, o.trigger_ts
           FROM causal_chain_outcomes o JOIN causal_chains c ON c.id=o.chain_id
           WHERE c.enabled=1 AND c.origin!='deprecated_meta'
             AND o.status IN ('HIT','HIT_WEAK','MISS','NEUTRAL')
           ORDER BY o.id""").fetchall()

    buckets = {}
    todo = []
    for r in rows:
        hb = cce.chain_horizon_bars(conn, r["chain_id"])
        buckets[hb] = buckets.get(hb, 0) + 1
        if hb == cce._HORIZON_PULSE_BARS:
            todo.append(r)

    print("[horizon] 已结算 %d 条，窗口分布 %s，需重算（脉冲型）%d 条"
          % (len(rows), buckets, len(todo)))

    os.makedirs(OUT_DIR, exist_ok=True)
    path = os.path.join(OUT_DIR, "horizon_recalc_%s.csv" % time.strftime("%Y%m%d_%H%M%S"))
    with open(path, "w", newline="", encoding="utf-8-sig") as fh:
        w = csv.writer(fh)
        w.writerow(["outcome_id", "chain_id", "status_before", "hit_before",
                    "pct_before_5d", "status_after", "hit_after", "pct_after",
                    "bars"])
        if not args.apply:
            for r in todo:
                w.writerow([r["id"], r["chain_id"], r["status"], r["direction_hit"],
                            r["actual_pct_5d"], "", "", "", cce._HORIZON_PULSE_BARS])
            print("[horizon] dry-run 明细: %s（加 --apply 执行）" % path)
            return

        done = ok = 0
        fails = []
        for r in todo:
            try:
                conn.execute("UPDATE causal_chain_outcomes SET status='PENDING', "
                             "eval_deadline_ts=1 WHERE id=?", (r["id"],))
                conn.commit()
                # evaluate_one 收"outcomes 整行"（列序固定），不收 outcome id。
                # 昨晚这里传 id 导致 771 条全部 TypeError，样本被冲成 PENDING ——
                # 修成按 evaluate_pending 的同一列序取整行再传。
                row_ev = conn.execute(
                    """SELECT id, chain_id, trigger_ts, trigger_source, trigger_ref_id,
                              trigger_headline, trigger_keywords_matched, predicted_direction,
                              NULL, NULL, predicted_direction, predicted_mag_lo, predicted_mag_hi,
                              eval_deadline_ts
                       FROM causal_chain_outcomes WHERE id=?""", (r["id"],)).fetchone()
                res = cce.evaluate_one(conn, row_ev, force=True)
                done += 1
                if res.get("status") in ("HIT", "HIT_WEAK", "MISS", "NEUTRAL"):
                    ok += 1
                after = conn.execute(
                    "SELECT status, direction_hit, actual_pct_5d FROM causal_chain_outcomes "
                    "WHERE id=?", (r["id"],)).fetchone()
                w.writerow([r["id"], r["chain_id"], r["status"], r["direction_hit"],
                            r["actual_pct_5d"], after[0], after[1], after[2],
                            cce._HORIZON_PULSE_BARS])
            except Exception as exc:
                fails.append((r["id"], "%s: %s" % (type(exc).__name__, exc)))
            if done % 200 == 0 and done:
                print("  ...已重算 %d" % done)
        print("[horizon] 重算完成 %d 条，成功 %d，失败 %d" % (done, ok, len(fails)))
        for f in fails[:5]:
            print("   !! #%s %s" % f)
        row = conn.execute(
            """SELECT COUNT(*) n, SUM(o.direction_hit) k FROM causal_chain_outcomes o
               JOIN causal_chains c ON c.id=o.chain_id
               WHERE c.enabled=1 AND c.origin!='deprecated_meta'
                 AND o.status IN ('HIT','HIT_WEAK','MISS') AND o.direction_hit IS NOT NULL"""
        ).fetchone()
        print("[horizon] 新口径方向命中率: %d/%d = %.1f%%"
              % (row["k"] or 0, row["n"] or 0,
                 100.0 * (row["k"] or 0) / max(1, row["n"] or 0)))
        print("[horizon] 明细: %s" % path)
    conn.close()


if __name__ == "__main__":
    main()
