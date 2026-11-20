#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""v483：回填历史 quick_sim_trades 的“零成本/空毛值”行（幂等）。

背景（2026-11-19 快照实测）：
  8 笔已结算纸面单里，4 笔（3 WTI + 1 XAU）`cost_bps=0`、`gross_pnl_pct=NULL`、
  `mfe_pct=NULL`。这些行是 v482 统一结算入口之前留下的，导致：
    · 净收益 = 毛收益，少扣 16~30bps；
    · MFE 覆盖闸拿不到样本，成本闸一直冷启动；
    · 反思层把“零成本亏损”当真值，标签不可比。

本脚本只修历史行，不猜未来：
  · gross = 当前 pnl_pct（这些行当初就是毛值，因为 cost=0）
  · cost = cost_model.round_trip_cost_bps(asset)；仍为 0 则用 30bps 兜底
  · net = gross - cost/100
  · mfe/mae 为空时，用 entry/max_price/min_price 按方向补算
  · 重算 pnl_usdt / verdict

幂等：只处理 cost_bps=0 且 (gross_pnl_pct IS NULL 或 mfe_pct IS NULL) 的已结算行；
处理过后 cost_bps>0，不会重复扣费。

用法：
  python3 scripts/backfill_quick_sim_cost_v483.py --db runtime/eval/snapshot_copy.db --dry-run
  python3 scripts/backfill_quick_sim_cost_v483.py --db runtime/eval/snapshot_copy.db
"""
from __future__ import annotations

import argparse
import sqlite3
import sys
from pathlib import Path
from typing import Any, Dict, List

_SRC = Path(__file__).resolve().parent.parent / "src_python"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from cost_model import round_trip_cost_bps


def _f(v: Any, default: float = 0.0) -> float:
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


def compute_fix(row: Dict[str, Any]) -> Dict[str, Any]:
    """返回要写回的字段；纯函数，便于单测。"""
    asset = str(row.get("asset") or "").upper().strip()
    action = str(row.get("action") or "").upper().strip()
    entry = _f(row.get("entry_price"))
    exit_price = _f(row.get("exit_price"))
    max_price = _f(row.get("max_price"), entry)
    min_price = _f(row.get("min_price"), entry)
    gross = _f(row.get("gross_pnl_pct") if row.get("gross_pnl_pct") is not None else row.get("pnl_pct"))
    cost_bps = _f(round_trip_cost_bps(asset))
    if cost_bps <= 0:
        cost_bps = 30.0  # 未知品种兜底：绝不能零成本
    net = gross - cost_bps / 100.0
    verdict = "WIN" if net > 0 else "LOSS" if net < 0 else "BREAKEVEN"

    mfe = row.get("mfe_pct")
    mae = row.get("mae_pct")
    if entry > 0 and (mfe is None or mae is None):
        if action == "SELL":
            mfe_calc = max(0.0, (entry - min_price) / entry * 100.0)
            mae_calc = max(0.0, (max_price - entry) / entry * 100.0)
        else:
            mfe_calc = max(0.0, (max_price - entry) / entry * 100.0)
            mae_calc = max(0.0, (entry - min_price) / entry * 100.0)
        if mfe is None:
            mfe = mfe_calc
        if mae is None:
            mae = mae_calc

    notional = _f(row.get("notional_usdt"))
    return {
        "pnl_pct": round(net, 6),
        "pnl_usdt": round(notional * net / 100.0, 6),
        "verdict": verdict,
        "cost_bps": round(cost_bps, 2),
        "gross_pnl_pct": round(gross, 6),
        "mfe_pct": None if mfe is None else round(float(mfe), 6),
        "mae_pct": None if mae is None else round(float(mae), 6),
    }


def backfill(db_path: str, *, dry_run: bool = False) -> List[Dict[str, Any]]:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(
            """SELECT id, decision_id, asset, action, entry_price, exit_price,
                      max_price, min_price, notional_usdt, pnl_pct,
                      gross_pnl_pct, cost_bps, mfe_pct, mae_pct
               FROM quick_sim_trades
               WHERE settled = 1
                 AND COALESCE(cost_bps, 0) = 0
                 AND (gross_pnl_pct IS NULL OR mfe_pct IS NULL)
               ORDER BY id"""
        ).fetchall()
        fixed: List[Dict[str, Any]] = []
        for r in rows:
            d = dict(r)
            patch = compute_fix(d)
            fixed.append({**d, **patch})
            if not dry_run:
                conn.execute(
                    """UPDATE quick_sim_trades
                       SET pnl_pct=?, pnl_usdt=?, verdict=?, cost_bps=?,
                           gross_pnl_pct=?, mfe_pct=?, mae_pct=?
                       WHERE id=?""",
                    (patch["pnl_pct"], patch["pnl_usdt"], patch["verdict"],
                     patch["cost_bps"], patch["gross_pnl_pct"],
                     patch["mfe_pct"], patch["mae_pct"], d["id"]),
                )
        if not dry_run:
            conn.commit()
        return fixed
    finally:
        conn.close()


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", required=True, help="quick_sim_trades 所在 SQLite")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    fixed = backfill(args.db, dry_run=args.dry_run)
    print(f"[backfill] rows={len(fixed)} dry_run={args.dry_run}")
    for r in fixed:
        print(
            f"  id={r['id']} {r['asset']} {r['action']} "
            f"gross={r['gross_pnl_pct']} cost={r['cost_bps']}bps "
            f"net={r['pnl_pct']} mfe={r['mfe_pct']} mae={r['mae_pct']} "
            f"verdict={r['verdict']}"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
