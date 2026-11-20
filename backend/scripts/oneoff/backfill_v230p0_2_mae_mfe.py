"""v230.2 P0.2: backfill mae_pct/mfe_pct in quick_sim_trades & ai_decisions.

Bug history:
    _settle_sl used abs(sgn*(cur_price-entry)/entry*100) for mae,
    _settle_tp2 used abs(sgn*(cur_price-entry)/entry*100) for mfe.
    Both ignored update_open_extremes-tracked max_price/min_price, so they
    underestimated real excursion magnitudes (esp. breakeven SL = mae 0%).

Note: settle_trades path was already correct (uses max/min). Re-deriving
    here should leave its rows unchanged.

    python3 scripts/backfill_v230p0_2_mae_mfe.py --dry-run
    python3 scripts/backfill_v230p0_2_mae_mfe.py            # run
    python3 scripts/backfill_v230p0_2_mae_mfe.py --revert   # restore from .bak
"""
from __future__ import annotations

import argparse
import shutil
import sqlite3
import sys
import time
from pathlib import Path

BACKEND_DIR = Path(__file__).resolve().parent.parent
DB_PATH = BACKEND_DIR / "trident_event_bus.db"
BACKUP_PATH = BACKEND_DIR / f"trident_event_bus.db.v230p0_2_bak_{int(time.time())}"


def _compute_extremes_pct(action, entry, max_price, min_price):
    """Return (mfe_pct, mae_pct). If entry<=0 return (0, 0)."""
    if entry <= 0:
        return 0.0, 0.0
    if action == "BUY":
        mfe = (max_price - entry) / entry * 100
        mae = (entry - min_price) / entry * 100
    else:
        mfe = (entry - min_price) / entry * 100
        mae = (max_price - entry) / entry * 100
    return round(max(0.0, mfe), 4), round(max(0.0, mae), 4)


def _classify_quick_sim(cur):
    rows = cur.execute(
        """
        SELECT id, asset, action, entry_price, max_price, min_price,
               mfe_pct, mae_pct, decision_id
          FROM quick_sim_trades
         WHERE settled = 1
           AND entry_price IS NOT NULL AND entry_price > 0
           AND max_price IS NOT NULL
           AND min_price IS NOT NULL
         ORDER BY id
        """
    ).fetchall()
    result = []
    for r in rows:
        old_mfe = float(r[6]) if r[6] is not None else 0.0
        old_mae = float(r[7]) if r[7] is not None else 0.0
        mfe, mae = _compute_extremes_pct(str(r[2]), float(r[3]),
                                          float(r[4]), float(r[5]))
        if abs(mfe - old_mfe) > 0.01 or abs(mae - old_mae) > 0.01:
            result.append((int(r[0]), str(r[1]), str(r[2]), float(r[3]),
                          float(r[4]), float(r[5]),
                          old_mfe, old_mae, mfe, mae,
                          int(r[8]) if r[8] is not None else None))
    return result
def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true",
                        help="print changes without applying")
    parser.add_argument("--revert", action="store_true",
                        help="restore from v230p0_2 .bak file")
    args = parser.parse_args()

    if args.revert:
        if not BACKUP_PATH.exists():
            print(f"[ERR] backup missing: {BACKUP_PATH}", file=sys.stderr)
            return 1
        shutil.copy2(BACKUP_PATH, DB_PATH)
        print(f"[OK] restored from {BACKUP_PATH.name}")
        return 0

    if not DB_PATH.exists():
        print(f"[ERR] DB missing: {DB_PATH}", file=sys.stderr)
        return 1

    if not args.dry_run and not BACKUP_PATH.exists():
        shutil.copy2(DB_PATH, BACKUP_PATH)
        print(f"[BAK] saved -> {BACKUP_PATH.name}")

    conn = sqlite3.connect(str(DB_PATH), timeout=30.0)
    conn.row_factory = sqlite3.Row
    try:
        records = _classify_quick_sim(conn)
        records.sort(key=lambda r: max(abs(r[6] - r[8]), abs(r[7] - r[9])),
                     reverse=True)

        print(f"\n=== quick_sim_trades to fix: {len(records)} ===")
        for (tid, asset, action, entry, mp, minp,
             old_mfe, old_mae, new_mfe, new_mae, did) in records:
            print(
                f"  id={tid:4d} {asset:6s} {action:4s} "
                f"entry={entry:.2f} max={mp:.2f} min={minp:.2f} | "
                f"mfe {old_mfe:.2f}->{new_mfe:.2f}  "
                f"mae {old_mae:.2f}->{new_mae:.2f}  "
                f"decision_id={did}"
            )

        if args.dry_run:
            print("\n[DRY-RUN] no writes; re-run without --dry-run to apply.")
            return 0

        if not records:
            print("\n[OK] no changes needed.")
            return 0

        for (tid, _a, _ac, _e, _mp, _mn,
             _om, _oa, nfe, nae, _did) in records:
            conn.execute(
                "UPDATE quick_sim_trades SET mfe_pct = ?, mae_pct = ? "
                "WHERE id = ?",
                (nfe, nae, tid),
            )

        ai_ids = [d[10] for d in records if d[10] is not None]
        ai_changed = 0
        if ai_ids:
            placeholders = ",".join("?" * len(ai_ids))
            ai_rows = conn.execute(
                f"SELECT a.id, a.entry_price, a.max_price, a.min_price, "
                f"a.mfe_pct, a.mae_pct, t.action "
                f"FROM ai_decisions a "
                f"LEFT JOIN quick_sim_trades t ON t.decision_id = a.id "
                f"WHERE a.id IN ({placeholders})",
                ai_ids,
            ).fetchall()
            for r in ai_rows:
                did = int(r[0])
                entry = float(r[1])
                if entry <= 0:
                    continue
                action = str(r[6]) if r[6] is not None else None
                if action not in ("BUY", "SELL"):
                    continue
                mp = float(r[2]) if r[2] is not None else None
                minp = float(r[3]) if r[3] is not None else None
                if mp is None or minp is None:
                    qs = conn.execute(
                        "SELECT max_price, min_price FROM quick_sim_trades "
                        "WHERE decision_id = ?",
                        (did,),
                    ).fetchone()
                    if qs is None:
                        continue
                    mp = float(qs[0]) if qs[0] is not None else entry
                    minp = float(qs[1]) if qs[1] is not None else entry
                mfe, mae = _compute_extremes_pct(action, entry, mp, minp)
                conn.execute(
                    "UPDATE ai_decisions "
                    "SET mfe_pct = ?, mae_pct = ? "
                    "WHERE id = ?",
                    (mfe, mae, did),
                )
                ai_changed += 1

        conn.commit()

        print(f"\n[OK] fixed: quick_sim_trades x {len(records)}, "
              f"ai_decisions x {ai_changed}")
        print(f"[BAK] {BACKUP_PATH}")
        print(f"[REVERT] python3 scripts/backfill_v230p0_2_mae_mfe.py --revert")
        return 0
    finally:
        conn.close()


if __name__ == "__main__":
    sys.exit(main())