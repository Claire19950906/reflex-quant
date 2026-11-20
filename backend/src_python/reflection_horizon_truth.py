#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""reflection_horizon_truth.py — 给反思盖「4h/24h 真值」章（v482）

为什么需要
----------
2026-11-19 实测：`trade_reflections_v2.pnl_pct` 来自 20~40 分钟的纸上单
（`settle_source=quick_sim_trades`），而往返成本 0.16%~0.30%、同期毛幅中位
只有 0.09% —— 这个窗口先天负期望，拿它判"判断对不对"，标签本身就是噪声。
同时 4h/24h 的真值早就在 `shadow_reflections` 里（v413/v416），只是没回接到反思上。

本模块只做一件事：把影子账本的结论**盖章**到对应反思行，不改任何历史 pnl。
  truth_4h_verdict  direction_ok / direction_wrong / flat / insufficient / ''(无样本)
  truth_4h_net_pct  该方向在 4h 扣往返成本后的净收益（百分点）
  truth_24h_*       同上，24h 窗口（只回答"慢半拍但方向对"）
  truth_stamped_at  盖章时刻

口径（与 shadow_reflections._judge 一致，不另造一套）
  · verdict 直接用影子表的结论，不在这里重算方向；
  · 净收益 = 方向符号 × 实际涨跌 − round_trip_cost_bps/100；
  · 没有影子行 / 没有结算价 → 留空，**不写 0**（判不了 ≠ 判错）。

用法
    python3 reflection_horizon_truth.py --report     # 只看盖章情况
    python3 reflection_horizon_truth.py --dry-run    # 算但不写
    python3 reflection_horizon_truth.py --commit     # 写库（幂等）
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

import db

_COLUMNS = [
    ("truth_4h_verdict", "TEXT NOT NULL DEFAULT ''"),
    ("truth_4h_net_pct", "REAL"),
    ("truth_24h_verdict", "TEXT NOT NULL DEFAULT ''"),
    ("truth_24h_net_pct", "REAL"),
    ("truth_stamped_at", "TEXT NOT NULL DEFAULT ''"),
]


def _ensure_columns(conn: sqlite3.Connection) -> None:
    existing = {r[1] for r in conn.execute("PRAGMA table_info(trade_reflections_v2)")}
    for col, col_def in _COLUMNS:
        if col not in existing:
            conn.execute(f"ALTER TABLE trade_reflections_v2 ADD COLUMN {col} {col_def};")
            print(f"[HORIZON-TRUTH] add column {col}", flush=True)


def _sign(action: str) -> int:
    a = (action or "").strip().upper()
    if a in ("BUY", "LONG", "UP", "看多", "做多"):
        return 1
    if a in ("SELL", "SHORT", "DOWN", "看空", "做空"):
        return -1
    return 0


def _net_pct(move_pct: Optional[float], action: str, asset: str) -> Optional[float]:
    """方向净收益（百分点）：sign × 实际涨跌 − 往返成本。取不到就 None。"""
    if move_pct is None:
        return None
    s = _sign(action)
    if s == 0:
        return None
    try:
        from cost_model import apply_costs, round_trip_cost_bps
        gross = s * float(move_pct)
        return round(float(apply_costs(gross, asset, action)), 4)
    except Exception:
        return None


def _has_table(conn: sqlite3.Connection, name: str) -> bool:
    return bool(conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)
    ).fetchone())


def stamp(conn: sqlite3.Connection, commit: bool = False) -> Dict[str, Any]:
    """把 shadow_reflections 的 4h/24h 结论盖到非模板反思行上。

    幂等：每次都按当前影子行重算并覆盖；影子行不存在时**不动**已有值
    （避免一次查询失败把已盖的章擦掉）。
    """
    out: Dict[str, Any] = {"at": time.strftime("%Y-%m-%dT%H:%M:%S"), "commit": commit}
    if not _has_table(conn, "shadow_reflections"):
        out["skipped"] = "shadow_reflections 表不存在"
        return out
    _ensure_columns(conn)

    shadow_cols = {r[1] for r in conn.execute("PRAGMA table_info(shadow_reflections)")}
    has_24h = {"verdict_24h", "move_pct_24h"}.issubset(shadow_cols)

    select_24h = ("s.verdict_24h, s.move_pct_24h" if has_24h else "NULL, NULL")
    rows = conn.execute(
        f"""SELECT r.id, r.decision_id, r.asset, r.action,
                   s.claimed_direction, s.verdict, s.realized_move_pct,
                   {select_24h}
              FROM trade_reflections_v2 r
              JOIN shadow_reflections s ON s.decision_id = r.decision_id
             WHERE COALESCE(r.origin, '') <> 'template'
               AND r.decision_id > 0""",
    ).fetchall()

    out["matched"] = len(rows)
    buckets: Dict[str, int] = {}
    written = 0
    stamp_ts = time.strftime("%Y-%m-%dT%H:%M:%S")
    for r in rows:
        rid, did, asset, action, claimed, v4, move4, v24, move24 = r
        # 用影子表"当时声称的方向"算净收益（这才是被判决的对象）；缺失再退回反思 action
        direction = claimed or action
        net4 = _net_pct(move4, direction, asset)
        net24 = _net_pct(move24, direction, asset)
        key = str(v4 or "")
        buckets[key] = buckets.get(key, 0) + 1
        if not commit:
            continue
        conn.execute(
            """UPDATE trade_reflections_v2
                  SET truth_4h_verdict = ?,
                      truth_4h_net_pct = ?,
                      truth_24h_verdict = ?,
                      truth_24h_net_pct = ?,
                      truth_stamped_at = ?
                WHERE id = ?""",
            (str(v4 or ""), net4,
             str(v24 or ""), net24,
             stamp_ts, int(rid)),
        )
        written += 1
    if commit and written:
        conn.commit()
    out["written"] = written
    out["verdicts_4h"] = buckets

    # 盖章覆盖率（非模板反思里有多少拿到了 4h 真值）
    total_real = int(conn.execute(
        "SELECT COUNT(*) FROM trade_reflections_v2 WHERE COALESCE(origin,'') <> 'template'"
    ).fetchone()[0] or 0)
    stamped = int(conn.execute(
        "SELECT COUNT(*) FROM trade_reflections_v2"
        " WHERE COALESCE(origin,'') <> 'template' AND truth_4h_verdict <> ''"
    ).fetchone()[0] or 0)
    out["real_reflections"] = total_real
    out["stamped_4h"] = stamped
    out["coverage"] = round(stamped / total_real, 4) if total_real else None
    return out


def report(conn: sqlite3.Connection) -> Dict[str, Any]:
    total_real = int(conn.execute(
        "SELECT COUNT(*) FROM trade_reflections_v2 WHERE COALESCE(origin,'') <> 'template'"
    ).fetchone()[0] or 0)
    rows = conn.execute(
        """SELECT COALESCE(NULLIF(truth_4h_verdict,''),'(未盖章)') v, COUNT(*)
             FROM trade_reflections_v2
            WHERE COALESCE(origin,'') <> 'template'
            GROUP BY v ORDER BY 2 DESC"""
    ).fetchall()
    rows24 = conn.execute(
        """SELECT COALESCE(NULLIF(truth_24h_verdict,''),'(未盖章)') v, COUNT(*)
             FROM trade_reflections_v2
            WHERE COALESCE(origin,'') <> 'template'
            GROUP BY v ORDER BY 2 DESC"""
    ).fetchall()
    return {
        "real_reflections": total_real,
        "truth_4h": {str(a): int(b) for a, b in rows},
        "truth_24h": {str(a): int(b) for a, b in rows24},
    }


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--commit", action="store_true")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--report", action="store_true")
    args = ap.parse_args(argv)

    conn = db.get_connection()
    conn.execute("PRAGMA busy_timeout=30000")
    try:
        if args.report:
            print(json.dumps(report(conn), ensure_ascii=False, indent=2))
            return 0
        res = stamp(conn, commit=bool(args.commit and not args.dry_run))
        print(json.dumps(res, ensure_ascii=False, indent=2))
        return 0
    finally:
        conn.close()


if __name__ == "__main__":
    raise SystemExit(main())
