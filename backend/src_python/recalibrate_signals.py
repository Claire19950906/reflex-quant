"""
recalibrate_signals.py — 2026-10-19 v1
========================================
替换 signal_calibration 表中的 PAV (Placeholder/Anti-Vacancy) 占位数据。

PAV 历史 (审计发现):
  - 24 行全部 hit_rate=100% (数学不可能)
  - recommendation 全部 'boost' (无 boost/dampen/hold 分布)

新数据来源 (chat backend 真实):
  - causal_chain_outcomes (22 rows)
  - trade_reflections_v2 (521 settled rows)

重算:
  1. per asset, 从 causal_chain_outcomes 聚合 (n_signals, n_hit, hit_rate)
  2. avg_confidence 从 trade_reflections_v2 同 asset 真实反思取
  3. confidence_calibration = hit_rate - avg_confidence
  4. recommendation: calib_gap > 0.15 → dampen, < -0.15 → boost, else hold
  5. pattern_type = 'chain_outcome_aggregate'

用法:
  python3 src_python/recalibrate_signals.py --dry-run
  python3 src_python/recalibrate_signals.py --commit
"""

import argparse
import sqlite3
import sys
from pathlib import Path

BACKEND_DIR = Path(__file__).resolve().parents[1]
DB_PATH = BACKEND_DIR / "trident_event_bus.db"


def get_db():
    conn = sqlite3.connect(str(DB_PATH), timeout=10)
    conn.row_factory = sqlite3.Row
    return conn


def detect_pav_rows(conn):
    """PAV 启发式: hit_rate=1.0 AND recommendation='boost'."""
    return conn.execute("""
        SELECT id, week_start, asset, pattern_type, n_signals, n_hit, hit_rate,
               avg_confidence, confidence_calibration, recommendation, notes
        FROM signal_calibration
        WHERE hit_rate >= 0.999 AND recommendation = 'boost'
        ORDER BY asset, pattern_type
    """).fetchall()


def compute_new_rows(conn):
    """从 causal_chain_outcomes + trade_reflections_v2 聚合真实校准数据."""
    chain_agg = conn.execute("""
        SELECT cc.asset AS asset,
               COUNT(co.id) AS n_signals,
               SUM(CASE WHEN co.status='HIT' OR co.status='HIT_WEAK' THEN 1 ELSE 0 END) AS n_hit
        FROM causal_chain_outcomes co
        JOIN causal_chains cc ON cc.chain_id = co.chain_id
        GROUP BY cc.asset
    """).fetchall()
    conf_agg = conn.execute("""
        SELECT asset, AVG(confidence_at_time) AS avg_conf,
               AVG(actual_outcome_pct) AS avg_edge
        FROM trade_reflections_v2
        WHERE settled = 1 AND asset IS NOT NULL AND asset != ''
        GROUP BY asset
    """).fetchall()
    conf_map = {r["asset"]: r for r in conf_agg}

    rows = []
    week_start = "2026-10-W42"
    for r in chain_agg:
        asset = r["asset"]
        n_sig = r["n_signals"]
        n_hit = r["n_hit"] or 0
        hit_rate = round(n_hit / max(1, n_sig), 4)
        conf_row = conf_map.get(asset)
        avg_conf = round(conf_row["avg_conf"], 4) if conf_row else 0.0
        avg_edge = round(conf_row["avg_edge"] or 0.0, 4) if conf_row else 0.0
        calib_gap = round(hit_rate - avg_conf, 4)
        if calib_gap > 0.15:
            rec = "dampen"
        elif calib_gap < -0.15:
            rec = "boost"
        else:
            rec = "hold"
        rows.append({
            "week_start": week_start,
            "asset": asset,
            "pattern_type": "chain_outcome_aggregate",
            "n_signals": n_sig,
            "n_hit": n_hit,
            "hit_rate": hit_rate,
            "avg_edge_5d": avg_edge,
            "avg_confidence": avg_conf,
            "confidence_calibration": calib_gap,
            "recommendation": rec,
            "notes": "RECAL_2026-10-19_v1 — from causal_chain_outcomes + trade_reflections_v2",
        })
    return rows


def dry_run(pav_rows, new_rows):
    print("=" * 70)
    print("DRY RUN — recalibrate_signals.py (no writes)")
    print("=" * 70)
    print(f"\nPAV 占位行待删除: {len(pav_rows)} 行")
    pav_per_asset = {}
    for r in pav_rows:
        pav_per_asset[r["asset"]] = pav_per_asset.get(r["asset"], 0) + 1
    for a, n in sorted(pav_per_asset.items()):
        print(f"    {a}: {n}")
    print(f"\n新行将写入: {len(new_rows)} 行 (基于真实数据)")
    for r in new_rows:
        print(f"    {r['asset']:<8} n={r['n_signals']:<3} hit={r['n_hit']:<2} "
              f"rate={r['hit_rate']:.2%} conf={r['avg_confidence']:.2%} "
              f"gap={r['confidence_calibration']:+.2f} rec={r['recommendation']}")
    rec_dist = {}
    for r in new_rows:
        rec_dist[r["recommendation"]] = rec_dist.get(r["recommendation"], 0) + 1
    print(f"\n新行 recommendation 分布: {rec_dist}")
    print("\n确认后用 --commit 真写入.")


def commit(pav_rows, new_rows):
    conn = get_db()
    try:
        pav_ids = [r["id"] for r in pav_rows]
        if pav_ids:
            placeholders = ",".join("?" * len(pav_ids))
            conn.execute(
                f"DELETE FROM signal_calibration WHERE id IN ({placeholders})",
                pav_ids,
            )
            print(f"[commit] 删除 {len(pav_ids)} 行 PAV 占位")
        for r in new_rows:
            conn.execute("""
                INSERT INTO signal_calibration
                  (week_start, asset, pattern_type, n_signals, n_hit, hit_rate,
                   avg_edge_5d, avg_confidence, confidence_calibration, recommendation, notes)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                r["week_start"], r["asset"], r["pattern_type"], r["n_signals"], r["n_hit"],
                r["hit_rate"], r["avg_edge_5d"], r["avg_confidence"],
                r["confidence_calibration"], r["recommendation"], r["notes"],
            ))
        conn.commit()
        print(f"[commit] 写入 {len(new_rows)} 行真实校准")
        try:
            from datetime import datetime, timezone
            now = int(datetime.now(timezone.utc).timestamp())
            conn.execute("""
                INSERT INTO edit_audit_log (ts, action, target_table, notes)
                VALUES (?, ?, ?, ?)
            """, (now, "RECALIBRATE", "signal_calibration",
                  f"PAV purge n={len(pav_ids)} + RECAL_2026-10-19_v1 n={len(new_rows)}"))
            conn.commit()
            print(f"[commit] audit_log 记一笔")
        except Exception as e:
            print(f"[commit] audit_log 跳过: {e}")
    finally:
        conn.close()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true", help="仅打印 (默认)")
    parser.add_argument("--commit", action="store_true", help="真写入")
    args = parser.parse_args()

    conn = get_db()
    try:
        pav_rows = detect_pav_rows(conn)
        new_rows = compute_new_rows(conn)
    finally:
        conn.close()

    if args.commit:
        if not pav_rows and not new_rows:
            print("无 PAV 行需清除, 无新行可写 — 退出.")
            return
        rec_dist = {r["recommendation"]: 0 for r in new_rows}
        for r in new_rows:
            rec_dist[r["recommendation"]] = rec_dist.get(r["recommendation"], 0) + 1
        if len(new_rows) > 3 and rec_dist.get("boost", 0) == len(new_rows):
            print("ABORT: 新行全部 boost — 阻止写入")
            sys.exit(1)
        commit(pav_rows, new_rows)
    else:
        dry_run(pav_rows, new_rows)


if __name__ == "__main__":
    main()


