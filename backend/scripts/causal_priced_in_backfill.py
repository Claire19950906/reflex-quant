#!/usr/bin/env python3
"""已计价样本回填（v431）。

口径：触发前 5 个自然日价格已朝"链的预期方向"走过 ≥ PRICED_IN_PCT（1%）的样本，
**不计入方向命中率**——消息落地时这段行情大概率已走完，判对判错都不代表机制预测力
（实测：已计价样本命中率 0.480，未计价 0.568）。

处置：direction_hit 置 NULL；status 按幅度重新分档（HIT / HIT_WEAK / NEUTRAL）；
notes 里补 priced_in=1、dir_hit_raw（原始方向判定）、prior_pct_5d，原始信息不丢。

默认 dry-run；`--apply` 写库并输出审计 CSV。
"""
import argparse
import csv
import json
import os
import sqlite3
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "src_python"))
import causal_chain_evaluator as cce

DEFAULT_DB = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "trident_event_bus.db")
OUT_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "runtime", "outcome_fix")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--db", default=DEFAULT_DB)
    ap.add_argument("--asset-scope", default="enabled",
                    help="enabled=只处理启用链（默认）；all=全部链")
    args = ap.parse_args()

    conn = sqlite3.connect(args.db, timeout=30)
    conn.row_factory = sqlite3.Row
    where = "" if args.asset_scope == "all" else \
        "AND c.enabled=1 AND c.origin!='deprecated_meta'"
    rows = conn.execute(
        f"""SELECT o.id, o.chain_id, o.predicted_direction d, o.direction_hit,
                   o.status, o.trigger_ts, o.actual_pct_5d, o.magnitude_hit,
                   o.predicted_mag_lo, o.notes, c.asset
            FROM causal_chain_outcomes o JOIN causal_chains c ON c.id=o.chain_id
            WHERE o.status IN ('HIT','HIT_WEAK','MISS')
              AND o.direction_hit IS NOT NULL
              AND o.predicted_direction IN ('UP','DOWN') {where}
            ORDER BY o.id"""
    ).fetchall()

    series_cache = {}
    changes = []
    for r in rows:
        asset = r["asset"]
        if not asset:
            continue
        if asset not in series_cache:
            series_cache[asset] = cce._cached_series(conn, asset)
        s = series_cache[asset]
        t0 = int(r["trigger_ts"])
        c0 = cce._close_at_or_before(s, t0)
        cp = cce._close_at_or_before(s, t0 - 5 * 86400)
        if not c0 or not cp or cp <= 0:
            continue
        prior = (c0 - cp) / cp * 100.0
        d = (r["d"] or "").upper()
        priced = (d == "UP" and prior >= cce.PRICED_IN_PCT) or \
                 (d == "DOWN" and prior <= -cce.PRICED_IN_PCT)
        if not priced:
            continue
        mag_hit = r["magnitude_hit"] or 0
        if mag_hit:
            ns = "HIT"
        elif abs(float(r["actual_pct_5d"] or 0)) >= float(r["predicted_mag_lo"] or 0):
            ns = "HIT_WEAK"
        else:
            ns = "NEUTRAL"
        changes.append((r["id"], r["chain_id"], asset, d, r["status"], ns,
                        r["direction_hit"], round(prior, 3), r["notes"]))

    print("[priced-in] 已计价样本 %d 条（%s）" % (len(changes), args.asset_scope))
    from collections import Counter
    print("  状态迁移:", dict(Counter("%s→%s" % (c[4], c[5]) for c in changes)))

    os.makedirs(OUT_DIR, exist_ok=True)
    path = os.path.join(OUT_DIR, "priced_in_%s.csv" % time.strftime("%Y%m%d_%H%M%S"))
    with open(path, "w", newline="", encoding="utf-8-sig") as fh:
        w = csv.writer(fh)
        w.writerow(["outcome_id", "chain_id", "asset", "pred_dir", "status_before",
                    "status_after", "direction_hit_before", "prior_pct_5d"])
        for c in changes:
            w.writerow(c[:8])
    print("  明细: %s" % path)

    if not args.apply:
        print("（dry-run，加 --apply 执行）")
        return

    done = 0
    for (oid, _cid, _asset, d, _sb, ns, hitb, prior, notes_raw) in changes:
        try:
            notes = json.loads(notes_raw) if notes_raw else {}
            if not isinstance(notes, dict):
                notes = {}
        except Exception:
            notes = {}
        notes.update({"priced_in": 1, "prior_pct_5d": prior, "dir_hit_raw": hitb,
                      "priced_in_note": "触发前 5 日已同向≥1%，不计入方向命中率"})
        for attempt in range(30):
            try:
                conn.execute(
                    "UPDATE causal_chain_outcomes SET direction_hit=NULL, status=?, notes=? "
                    "WHERE id=?", (ns, json.dumps(notes, ensure_ascii=False), oid))
                done += 1
                break
            except sqlite3.OperationalError as exc:
                if "locked" not in str(exc):
                    raise
                time.sleep(0.5 * (attempt + 1))
        if done % 100 == 0:
            conn.commit()
    conn.commit()
    print("[priced-in] 已更新 %d 条" % done)

    row = conn.execute(
        """SELECT COUNT(*) n, SUM(direction_hit) k FROM causal_chain_outcomes
           WHERE status IN ('HIT','HIT_WEAK','MISS') AND direction_hit IS NOT NULL"""
    ).fetchone()
    print("[priced-in] 未计价口径方向命中率: %d/%d = %.1f%%"
          % (row["k"] or 0, row["n"] or 0, 100.0 * (row["k"] or 0) / max(1, row["n"] or 0)))
    conn.close()


if __name__ == "__main__":
    main()
