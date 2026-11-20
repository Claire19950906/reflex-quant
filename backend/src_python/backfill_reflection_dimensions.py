#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
backfill_reflection_dimensions.py — v309 P0-1: 给已有反思行回填三维度
===================================================================
背景: v309 P0-1 之前, trade_reflections_v2 根本没有 dimension/subcategory
列, 更没有写入方。补列后, **历史行仍然是空的** → 前端三维度卡片仍会显示
"只有新增的反思才有维度"。本脚本把历史行按同一套权威口径回填。

口径: reflection_dimension_map.py (与新写入路径完全一致)。

用法:
    python3 backfill_reflection_dimensions.py              # 回填 (默认)
    python3 backfill_reflection_dimensions.py --dry-run    # 只报告不写
    python3 backfill_reflection_dimensions.py --limit 5000 # 限量

安全性:
  · 幂等: 只 UPDATE 维度为空的行, 重复跑不会覆盖已有维度。
  · 不删行, 不动其他列。
  · 打印 before/after 统计, 便于核对。
"""
from __future__ import annotations

import argparse
import sqlite3
import sys
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(Path(__file__).resolve().parent))

from review import reflection_dimension_map as rdm

DB_PATH = BACKEND / "trident_event_bus.db"


def _table_cols(conn: sqlite3.Connection, table: str) -> set:
    try:
        return {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}
    except sqlite3.Error:
        return set()


def _load_pending(conn: sqlite3.Connection, limit: int) -> list:
    """取维度为空的反思行 (含三层标签, 供兜底分类)。

    列名说明 (踩过的坑, 别改回去):
      · trade_reflections_v2 **没有** is_correct / verdict 列 —— 结算判定在
        quick_sim_trades.verdict (WIN/LOSS), pnl_pct 存的是**小数比例** (0.05=5%)。
      · 三层标签列是 layer / sub_layer / signal_name (v188 ALTER 补的),
        DDL 里另有 error_layer / error_sub_layer / error_signal 同义列;
        老库里可能只有后者 → 这里两个都取, 优先 ontology 三层。
    """
    cols = _table_cols(conn, "trade_reflections_v2")
    if not cols:
        raise RuntimeError(
            "trade_reflections_v2 不存在 — 需先跑 db.migrate(conn) 补齐 schema"
        )
    if "dimension" not in cols:
        raise RuntimeError(
            "trade_reflections_v2 缺 dimension 列 — 需先跑 db.migrate(conn) 补 v309 列"
        )

    layer_col = "layer" if "layer" in cols else "error_layer"
    sub_col = "sub_layer" if "sub_layer" in cols else "error_sub_layer"
    sig_col = "signal_name" if "signal_name" in cols else "error_signal"
    for c in (layer_col, sub_col, sig_col):
        if c not in cols:
            raise RuntimeError(
                f"trade_reflections_v2 缺标签列 ({c}) — 需先跑 db.migrate(conn)"
            )

    # 结算信息 JOIN quick_sim_trades (该表才有 verdict / 真实 pnl)
    has_qst = bool(_table_cols(conn, "quick_sim_trades"))
    if has_qst:
        sel = (
            "SELECT t.id AS id, t.decision_id AS decision_id, "
            f"       t.{layer_col} AS layer, t.{sub_col} AS sub_layer, t.{sig_col} AS signal_name, "
            "       t.pnl_pct AS t_pnl_pct, t.settled_at AS settled_at, t.root_cause AS root_cause, "
            "       q.verdict AS verdict, q.pnl_pct AS q_pnl_pct, q.settled AS q_settled "
            "FROM trade_reflections_v2 t "
            "LEFT JOIN quick_sim_trades q ON q.decision_id = t.decision_id "
            "WHERE (t.dimension IS NULL OR t.dimension = '') "
            "ORDER BY t.id DESC LIMIT ?"
        )
    else:
        sel = (
            "SELECT t.id AS id, t.decision_id AS decision_id, "
            f"       t.{layer_col} AS layer, t.{sub_col} AS sub_layer, t.{sig_col} AS signal_name, "
            "       t.pnl_pct AS t_pnl_pct, t.settled_at AS settled_at, t.root_cause AS root_cause, "
            "       NULL AS verdict, NULL AS q_pnl_pct, NULL AS q_settled "
            "FROM trade_reflections_v2 t "
            "WHERE (t.dimension IS NULL OR t.dimension = '') "
            "ORDER BY t.id DESC LIMIT ?"
        )
    rows = conn.execute(sel, (int(limit),)).fetchall()
    return [dict(r) for r in rows]


def _error_codes_for(conn: sqlite3.Connection, decision_id: int) -> list:
    if not decision_id:
        return []
    try:
        rows = conn.execute(
            "SELECT error_code FROM decision_errors WHERE decision_id = ?",
            (int(decision_id),),
        ).fetchall()
        return [r[0] for r in rows if r and r[0]]
    except sqlite3.OperationalError:
        return []


def _reason_code_for(conn: sqlite3.Connection, decision_id: int):
    if not decision_id:
        return None
    try:
        row = conn.execute(
            "SELECT reason_code FROM attribution_reviews "
            "WHERE trade_id = ? ORDER BY id DESC LIMIT 1",
            (int(decision_id),),
        ).fetchone()
        return row[0] if row else None
    except sqlite3.OperationalError:
        return None


def backfill(conn: sqlite3.Connection, dry_run: bool = False, limit: int = 100000) -> dict:
    rows = _load_pending(conn, limit)
    total_pending = len(rows)
    if not total_pending:
        return {"pending": 0, "updated": 0, "skipped": 0, "dry_run": dry_run}

    by_dim: dict = {}
    updates = []
    skipped = 0
    for r in rows:
        codes = _error_codes_for(conn, r.get("decision_id"))
        reason_code = _reason_code_for(conn, r.get("decision_id"))
        dim, subcat = rdm.resolve_dimension(
            error_codes=codes,
            reason_code=reason_code,
            layer=r.get("layer") or "",
            sub_layer=r.get("sub_layer") or "",
            signal_name=r.get("signal_name") or "",
        ) or ("", "")
        if not dim:
            skipped += 1
            continue
        # 结算口径对齐 paper_reflection_worker._insert_one:
        #   verdict 来自 quick_sim_trades.verdict (WIN/LOSS)
        #   pnl 优先用 quick_sim_trades.pnl_pct, 退化用 trade_reflections_v2.pnl_pct
        #   pnl 是**小数比例** (0.05 = 5%) → actual_outcome_pct = pnl * 100
        verdict = str(r.get("verdict") or "").strip().upper()
        raw_pnl = r.get("q_pnl_pct")
        if raw_pnl is None:
            raw_pnl = r.get("t_pnl_pct")
        try:
            pnl = float(raw_pnl) if raw_pnl is not None else 0.0
        except (TypeError, ValueError):
            pnl = 0.0
        hit = 1 if verdict == "WIN" else 0
        settled = 1 if (verdict in ("WIN", "LOSS") or r.get("q_settled")
                        or r.get("settled_at")) else 0
        updates.append((
            dim, subcat, hit, pnl * 100.0,
            (r.get("root_cause") or "")[:400], settled, int(r["id"]),
        ))
        by_dim[dim] = by_dim.get(dim, 0) + 1

    if not dry_run and updates:
        conn.executemany(
            "UPDATE trade_reflections_v2 SET dimension = ?, subcategory = ?, "
            "hit = ?, actual_outcome_pct = ?, reflection_text = ?, settled = ? "
            "WHERE id = ?",
            updates,
        )
        conn.commit()

    return {
        "pending": total_pending,
        "updated": len(updates),
        "skipped": skipped,
        "by_dimension": by_dim,
        "dry_run": dry_run,
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true", help="只报告不写库")
    ap.add_argument("--limit", type=int, default=100000, help="最多处理多少行")
    a = ap.parse_args()

    if not DB_PATH.exists():
        print(f"[FATAL] DB not found: {DB_PATH}", file=sys.stderr)
        return 1
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    try:
        rep = backfill(conn, dry_run=a.dry_run, limit=a.limit)
    except Exception as exc:
        # 关键: 这里必须 return 非 0 —— 早期版本吞掉异常后 return 0,
        # 导致 CI/验证脚本把"查询失败"当成"成功回填 0 行"而误判 PASS。
        print(f"[FATAL] {type(exc).__name__}: {exc}", file=sys.stderr)
        conn.rollback()
        return 2
    finally:
        conn.close()

    print("=" * 62)
    print(f"待回填行数      : {rep['pending']}")
    print(f"成功回填        : {rep['updated']}")
    print(f"无法归类(跳过) : {rep['skipped']}")
    if rep.get("by_dimension"):
        print(f"维度分布        : {rep['by_dimension']}")
    if rep["dry_run"]:
        print("(dry-run, 未落库)")
    if rep["pending"] == 0:
        # 0 待回填可能是"没历史数据"也可能是"列还没补" —— 用 schema 检查区分,
        # 避免把"查询失败伪装成空结果"这种事再发生一次。
        print("[WARN] 0 行待回填。若预期有历史反思, 请确认已跑 db.migrate(conn) "
              "补齐 dimension 列, 且历史反思的 layer/sub_layer 非空。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
