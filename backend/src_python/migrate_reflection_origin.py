#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""migrate_reflection_origin.py — v413 反思来源分账与证据回填

为什么需要
----------
`trade_reflections_v2` 里混着三类完全不同的东西，共用一个 `depth_score`：

    来源        条数    特征
    template    143     action=HOLD，lesson 只有 4 种文本，depth_score 恒为 4
    llm          13     depth_score 8~10，有真实的归因文本
    shadow        0     影子结算反思（v413 新建，见 shadow_reflections.py）

结果是所有"反思覆盖率 / 反思深度"报表都被模板行拉平：156 条看起来都有反思，
实际能提供信息的只有 13 条。不把来源分开，后面任何优化都无法验收。

本脚本做两件事：
  1) 幂等补 `origin` / `evidence` 两列（老库 ALTER，新库由 db_schema 建全）；
  2) 回填历史 156 行的 origin，并为模板行写入**诚实的空证据**
     ——模板行的 `attribution` 明确写 'none'，表示"这条没有归因依据"，
     而不是让它看起来像一条有效反思。

判定口径（写死在这里，别改历史原因）
    action='HOLD' and depth_score<8   → template
    depth_score>=8                    → llm
    其余                              → unknown

用法
    python3 migrate_reflection_origin.py --dry-run
    python3 migrate_reflection_origin.py --commit
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

import db

_COLUMNS = [("origin", "TEXT NOT NULL DEFAULT ''"),
            ("evidence", "TEXT NOT NULL DEFAULT ''")]

# missed_signal_reflection.OUTCOME_WINDOW_DAYS 的口径；模板行的涨跌幅就是这个窗口算的
_TEMPLATE_WINDOW_DAYS = 5


def _ensure_columns(conn: sqlite3.Connection) -> None:
    existing = {r[1] for r in conn.execute("PRAGMA table_info(trade_reflections_v2)")}
    for col, col_def in _COLUMNS:
        if col not in existing:
            conn.execute(f"ALTER TABLE trade_reflections_v2 ADD COLUMN {col} {col_def};")
            print(f"[MIGRATE] add column {col}", flush=True)


def _classify(action: str, depth_score: int) -> str:
    if (action or "").upper() == "HOLD" and int(depth_score or 0) < 8:
        return "template"
    if int(depth_score or 0) >= 8:
        return "llm"
    return "unknown"


def _template_evidence(asset: str, move_pct, hit) -> str:
    """模板行没有归因，只能给出"当时算出来的涨跌"这一条可复核事实。"""
    return json.dumps(
        {
            "asset": asset or "",
            "move_pct": move_pct,
            "window_days": _TEMPLATE_WINDOW_DAYS,
            "price_source": "forward_move",
            "attribution": "none",      # 关键：模板行没有归因依据
            "note": "模板生成，无根因、无入场/出场时刻，不可用于验收",
        },
        ensure_ascii=False,
    )


def run(commit: bool = False) -> dict:
    conn = db.get_connection()
    conn.execute("PRAGMA busy_timeout=30000")
    out: dict = {}
    try:
        _ensure_columns(conn)
        rows = conn.execute(
            "SELECT id, action, depth_score, asset, actual_outcome_pct, hit"
            "  FROM trade_reflections_v2 WHERE COALESCE(origin,'')=''"
        ).fetchall()
        out["pending_before"] = len(rows)
        buckets: dict = {}
        for r in rows:
            rid, action, depth, asset, move, hit = (
                int(r[0]), str(r[1] or ""), int(r[2] or 0),
                str(r[3] or ""), r[4], int(r[5] or 0))
            origin = _classify(action, depth)
            buckets[origin] = buckets.get(origin, 0) + 1
            evidence = ""
            if origin == "template":
                evidence = _template_evidence(asset, move, hit)
            elif origin == "llm":
                evidence = json.dumps(
                    {"asset": asset or "", "pnl_pct": move,
                     "attribution": "llm",
                     "price_source": "pnl_or_forward_move"},
                    ensure_ascii=False,
                )
            if commit:
                conn.execute(
                    "UPDATE trade_reflections_v2 SET origin=?, evidence=?"
                    " WHERE id=? AND COALESCE(origin,'')=''",
                    (origin, evidence, rid))
        if commit:
            conn.commit()
        out["classified"] = buckets
        # 模板行的 error_layer 之前留空 —— 空值会被所有 GROUP BY 当成"没有错误"。
        # 这些行本来就没有归因依据，必须写成显式的 unattributed（可判定的缺失）。
        if commit:
            fixed = conn.execute(
                "UPDATE trade_reflections_v2 SET error_layer='unattributed'"
                " WHERE COALESCE(origin,'')='template'"
                "   AND COALESCE(error_layer,'')=''"
            ).rowcount
            conn.commit()
            out["error_layer_filled"] = int(fixed)
        after = conn.execute(
            "SELECT COALESCE(NULLIF(origin,''),'(空)') o, COUNT(*)"
            "  FROM trade_reflections_v2 GROUP BY o ORDER BY 2 DESC"
        ).fetchall()
        out["after"] = {str(a[0]): int(a[1]) for a in after}
        out["committed"] = bool(commit)
        return out
    finally:
        conn.close()


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--commit", action="store_true")
    args = ap.parse_args()
    if not args.commit:
        args.dry_run = True
    res = run(commit=args.commit and not args.dry_run)
    print(json.dumps(res, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
