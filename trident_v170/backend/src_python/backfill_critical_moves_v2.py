"""
backfill_critical_moves_v2.py — v302+ P0: 重分类 fast_* / slow_*
============================================================
背景 bug (v301 之前):
  原 backfill_critical_moves.py / backfill_critical_moves_late.py 写死:
      kind = "slow_rise" if pct > 0 else "slow_fall"
  即便 k_days=1 (1天窗口检测到的大幅涨跌) 也被标 slow_*,
  用户在归因页看不到"快涨/快跌"分类.
  实际数据: critical_price_moves 中 duration_min<=1440 的 2132 条本应是 fast_*.

修复逻辑 (v302+):
  - duration_min <= 1440 (1天窗口)  → fast_rise / fast_fall
  - duration_min >  1440 (3-5天累计) → slow_rise / slow_fall
  - algo_score 重新校准:
      fast: 0.5 + |pct|/20 (封顶 0.9)  — 短期剧烈,信号更紧急
      slow: 0.5 + |pct|/30 (封顶 0.9)  — 中期趋势,信号更稳

操作:
  1) UPDATE 已有 critical_price_moves 行 (按 duration_min 重分类)
  2) event_move_attribution.move_kind 同步 (通过 move_id)
  3) 输出 fast_* / slow_* 分布统计

idempotent: 重复运行无副作用.
"""
from __future__ import annotations
import json
import sqlite3
import sys
from pathlib import Path

BACKEND_DIR = Path(__file__).resolve().parents[1]
DB_PATH = BACKEND_DIR / "trident_event_bus.db"
FAST_DURATION_MAX_MIN = 1440


def _score_fast(pct: float) -> float:
    return min(0.9, 0.5 + abs(pct) / 20.0)


def _score_slow(pct: float) -> float:
    return min(0.9, 0.5 + abs(pct) / 30.0)


def reclassify_moves(conn):
    rows = conn.execute(
        "SELECT id, move_pct, duration_min, move_kind, algo_score, context_json "
        "FROM critical_price_moves"
    ).fetchall()
    counts = {"fast_rise": 0, "fast_fall": 0, "slow_rise": 0, "slow_fall": 0, "unchanged": 0}
    updates = []
    for r in rows:
        rid, pct, dur_min, old_kind, old_score, ctx_raw = r[0], r[1], r[2], r[3], r[4], r[5]
        if dur_min is not None and dur_min <= FAST_DURATION_MAX_MIN:
            new_kind = "fast_rise" if pct > 0 else "fast_fall"
            new_score = round(_score_fast(pct), 3)
        else:
            new_kind = "slow_rise" if pct > 0 else "slow_fall"
            new_score = round(_score_slow(pct), 3)
        if old_kind == new_kind and abs((old_score or 0) - new_score) < 0.005:
            counts["unchanged"] += 1
            continue
        counts[new_kind] += 1
        try:
            ctx = json.loads(ctx_raw or "{}")
        except Exception:
            ctx = {}
        ctx["fast_v2_reclassified"] = True
        ctx["old_move_kind"] = old_kind
        ctx["new_move_kind"] = new_kind
        updates.append((new_kind, new_score, json.dumps(ctx, ensure_ascii=False), rid))
    if updates:
        conn.executemany(
            "UPDATE critical_price_moves SET move_kind=?, algo_score=?, context_json=? WHERE id=?",
            updates,
        )
    conn.commit()
    return {"rows_scanned": len(rows), "rows_updated": len(updates), "counts_after": counts}


def sync_event_move_attribution(conn):
    cols = [r[1] for r in conn.execute("PRAGMA table_info(event_move_attribution)").fetchall()]
    if "move_id" not in cols:
        return {"updated": 0, "note": "event_move_attribution 无 move_id 列"}
    if "move_kind" not in cols:
        return {"updated": 0, "note": "event_move_attribution 无 move_kind 列"}
    rows = conn.execute(
        "SELECT a.id, c.move_kind, c.algo_score "
        "FROM event_move_attribution a "
        "INNER JOIN critical_price_moves c ON a.move_id=c.id "
        "WHERE a.move_id IS NOT NULL"
    ).fetchall()
    updates = []
    for r in rows:
        if r[1] is None:
            continue
        updates.append((r[1], r[2], r[0]))
    if updates:
        conn.executemany(
            "UPDATE event_move_attribution SET move_kind=?, algo_score=? WHERE id=?",
            updates,
        )
    conn.commit()
    return {"rows_scanned": len(rows), "rows_updated": len(updates)}


def distribution_report(conn):
    rows = conn.execute(
        "SELECT move_kind, COUNT(*) n, ROUND(AVG(move_pct), 2) avg_pct, "
        "ROUND(AVG(algo_score), 3) avg_algo, ROUND(AVG(duration_min)) avg_dur "
        "FROM critical_price_moves GROUP BY move_kind ORDER BY move_kind"
    ).fetchall()
    return {
        r[0]: {"n": r[1], "avg_pct": r[2], "avg_algo": r[3], "avg_dur": r[4]}
        for r in rows
    }


def main():
    if not DB_PATH.exists():
        print(f"[FATAL] DB not found: {DB_PATH}", file=sys.stderr)
        return 1
    print("=" * 70)
    print("backfill_critical_moves_v2.py · v302+ P0 fast/slow 重新分类")
    print("=" * 70)
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    try:
        conn.execute("PRAGMA journal_mode=WAL")
        before = distribution_report(conn)
        print("\n📊 修复前 move_kind 分布:")
        for k, v in before.items():
            print(f"  {k:12} | n={v['n']:4} | avg_pct={v['avg_pct']}% | avg_dur={v['avg_dur']}min")
        result = reclassify_moves(conn)
        print(f"\n✅ critical_price_moves 重分类:")
        print(f"   rows_scanned={result['rows_scanned']} | rows_updated={result['rows_updated']}")
        print(f"   new_counts={result['counts_after']}")
        sync_result = sync_event_move_attribution(conn)
        print(f"\n✅ event_move_attribution 同步:")
        print(f"   rows_scanned={sync_result['rows_scanned']} | rows_updated={sync_result['rows_updated']}")
        after = distribution_report(conn)
        print("\n📊 修复后 move_kind 分布:")
        for k, v in after.items():
            print(
                f"  {k:12} | n={v['n']:4} | avg_pct={v['avg_pct']}% | "
                f"avg_dur={v['avg_dur']}min | avg_algo={v['avg_algo']}"
            )
        all_4 = all(
            after.get(k, {}).get("n", 0) > 0
            for k in ("fast_rise", "fast_fall", "slow_rise", "slow_fall")
        )
        if all_4:
            print("\n🎉 fast_rise/fast_fall/slow_rise/slow_fall 四类齐全!")
        else:
            miss = [
                k for k in ("fast_rise", "fast_fall", "slow_rise", "slow_fall")
                if after.get(k, {}).get("n", 0) == 0
            ]
            print(f"\n⚠️ 缺类: {miss}")
        return 0
    except Exception as e:
        print(f"[ERROR] {e}", file=sys.stderr)
        conn.rollback()
        return 2
    finally:
        conn.close()


if __name__ == "__main__":
    sys.exit(main())