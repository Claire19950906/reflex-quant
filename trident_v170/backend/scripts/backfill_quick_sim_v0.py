"""P0 历史回填脚本：把 quick_sim_trades 5 笔结算数据回填到 ai_decisions + quick_sim_trades 新列。

前置条件：quick_sim_trades 已 ALTER 加 mfe_pct / mae_pct 列。
可重复运行（idempotent：用 COALESCE / 条件 UPDATE）。

用法：python backfill_quick_sim_v0.py [--dry-run] [--db PATH]
"""
from __future__ import annotations

import argparse
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

DEFAULT_DB = "/Users/claire/.cline/data/workspaces/chat/trident_v170/backend/trident_event_bus.db"


def compute_mfe_mae(action: str, entry: float, max_p: float | None, min_p: float | None) -> tuple[float, float]:
    """按方向计算 MFE% / MAE%。max_p/min_p 为 None 时 fallback 到 entry（=0）。"""
    if not entry or entry <= 0:
        return 0.0, 0.0
    hi = max_p if max_p is not None else entry
    lo = min_p if min_p is not None else entry
    if action.upper() == "BUY":
        mfe = (hi - entry) / entry * 100
        mae = (entry - lo) / entry * 100
    elif action.upper() == "SELL":
        mfe = (entry - lo) / entry * 100
        mae = (hi - entry) / entry * 100
    else:
        mfe, mae = 0.0, 0.0
    return max(0.0, round(mfe, 4)), max(0.0, round(mae, 4))


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--db", default=DEFAULT_DB)
    args = ap.parse_args()

    db_path = Path(args.db)
    if not db_path.exists():
        print(f"DB not found: {db_path}", file=sys.stderr)
        return 1

    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(
            """SELECT id, decision_id, asset, action, entry_price, exit_price,
                      max_price, min_price, pnl_pct, verdict, settled,
                      entry_ts, horizon_minutes
               FROM quick_sim_trades
               WHERE settled = 1
               ORDER BY id"""
        ).fetchall()
        print(f"[backfill] quick_sim_trades settled rows: {len(rows)}")

        if not rows:
            print("[backfill] nothing to do")
            return 0

        # 校验 quick_sim_trades 是否有 mfe/mae 列（防御性）
        cols = [r["name"] for r in conn.execute("PRAGMA table_info(quick_sim_trades)").fetchall()]
        if "mfe_pct" not in cols or "mae_pct" not in cols:
            print(f"[backfill] missing mfe_pct/mae_pct columns. PRAGMA said: {cols}", file=sys.stderr)
            return 2

        updated_qs = 0
        updated_ad = 0
        now_iso = datetime.now(timezone.utc).isoformat(timespec="seconds")

        for r in rows:
            mfe, mae = compute_mfe_mae(r["action"], r["entry_price"], r["max_price"], r["min_price"])
            if args.dry_run:
                print(
                    f"  [DRY] qs.id={r['id']} dcid={r['decision_id']} {r['action']} {r['asset']} "
                    f"mfe={mfe:+.4f} mae={mae:+.4f} verdict={r['verdict']}"
                )
                continue

            # 1) UPDATE quick_sim_trades 自身 mfe/mae 列
            cur = conn.execute(
                """UPDATE quick_sim_trades
                   SET mfe_pct = COALESCE(mfe_pct, ?),
                       mae_pct = COALESCE(mae_pct, ?)
                   WHERE id = ?""",
                (mfe, mae, r["id"]),
            )
            updated_qs += cur.rowcount

            # 2) 反向 UPDATE ai_decisions（保留 forward 兼容 + 补 entry_price/entry_time/holding_horizon_minutes）
            entry_iso = (
                datetime.fromtimestamp(int(r["entry_ts"]), tz=timezone.utc)
                .astimezone()
                .replace(tzinfo=None)
                .isoformat(timespec="seconds")
                if r["entry_ts"] else None
            )
            cur = conn.execute(
                """UPDATE ai_decisions
                   SET is_correct              = COALESCE(NULLIF(is_correct, ''), ?),
                       settled                 = 1,
                       forward_pnl             = COALESCE(forward_pnl, ?),
                       exit_price              = COALESCE(exit_price, ?),
                       mfe_pct                 = COALESCE(mfe_pct, ?),
                       mae_pct                 = COALESCE(mae_pct, ?),
                       entry_price             = COALESCE(entry_price, ?),
                       entry_time              = COALESCE(NULLIF(entry_time, ''), ?),
                       holding_horizon_minutes = COALESCE(holding_horizon_minutes, ?)
                   WHERE id = ?""",
                (r["verdict"], r["pnl_pct"], r["exit_price"], mfe, mae,
                 r["entry_price"], entry_iso, r["horizon_minutes"], r["decision_id"]),
            )
            updated_ad += cur.rowcount

        conn.commit()

        print(f"[backfill] updated quick_sim_trades rows: {updated_qs}")
        print(f"[backfill] updated ai_decisions   rows: {updated_ad}")
        print(f"[backfill] done at {now_iso}")

        # 验证：回填后 ai_decisions.is_correct 应与 quick_sim.verdict 一致
        print("\n[verify] LEFT JOIN quick_sim ⨝ ai_decisions (settled=1):")
        verify = conn.execute(
            """SELECT qs.decision_id, qs.verdict AS qs_verdict,
                      ad.is_correct AS ad_is_correct, ad.forward_pnl AS ad_pnl,
                      qs.pnl_pct AS qs_pnl
               FROM quick_sim_trades qs
               JOIN ai_decisions ad ON ad.id = qs.decision_id
               WHERE qs.settled = 1
               ORDER BY qs.id"""
        ).fetchall()
        for r in verify:
            ok = "✓" if r["qs_verdict"] == r["ad_is_correct"] else "✗"
            print(
                f"  {ok} dcid={r['decision_id']} qs.verdict={r['qs_verdict']:<10} "
                f"ad.is_correct={(r['ad_is_correct'] or ''):<10} "
                f"qs.pnl={r['qs_pnl']:+.4f} ad.pnl={(r['ad_pnl'] or 0):+.4f}"
            )

        return 0
    finally:
        conn.close()


if __name__ == "__main__":
    raise SystemExit(main())