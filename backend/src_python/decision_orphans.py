# -*- coding: utf-8 -*-
"""v482: 决策派生表的孤儿清理 —— 重置/换代之后不再留下悬空指针。

为什么需要
----------
`_clear_trading_traces` 只删 `ai_decisions` 与几张显式清单表，但库里有 50+ 张表
用 `decision_id` / `ai_decision_id` 指回决策。实测 11-19 那次重置后：
  · `decision_errors` 有 231 条指向已被删除的决策（占全表 29.6%）；
  · 错因统计的分母因此混入幽灵行，`E001a/E001b` 占比被系统性拉高；
  · 这类残留每次重置都会累积，且没有任何孤儿监控。

关键约束（为什么不敢"全表扫"）
----------------------------
库里存在**三个决策编号空间**（见 `signals/decision_id_space.py`）：
  · ai_decisions.id        提案空间
  · trade_decisions.id     最终决策空间（decision_errors / 多周期读数用它）
  · 其他历史空间（news / 权重 / 质检表各写各的）
因此"不在一张表里"≠"孤儿"。本模块只处理**白名单内、且两个权威空间都不存在**
的行；白名单之外的表只盘点、永不删除。

设计边界
--------
· 默认 dry-run；apply 时先把待删行导出 JSON（可回溯）再删；
· 单表删除超过 30% 且总量 >1000 行时**拒删**（防口径写错造成大范围误删）；
· 幂等：重复跑不会重复删；不碰 `ai_decisions` / `trade_decisions` 本体与归档表。
"""
from __future__ import annotations

import json
import os
import sqlite3
import time
from typing import Any, Dict, List, Optional

# 这些表**不参与**孤儿清理：
# · ai_decisions 是判定基准；ai_decisions_history / decision_ref_archive 是有意归档；
# · trade_decisions 在重置时保留本体、只把上游指针置 NULL（既有口径，不改）。
EXCLUDE_TABLES = {
    "ai_decisions",
    "ai_decisions_history",
    "decision_ref_archive",
    "trade_decisions",
    "quick_sim_trades",
}

# 只有这些表的 decision_id 语义被确认是"指回决策"（任一空间），才允许删除。
# 不在名单里的表（如 quality_rejections / news_events / hermes_observations）
# 只盘点不删 —— 已实测它们的 decision_id 指向别的编号空间。
PURGE_ALLOW = {
    "decision_errors",
    "decision_evidence",
    "decision_narrative",
    "decision_actions",
    "decision_threshold_snapshot",
    "decision_fundamental_snapshot",
    "decision_outcomes_horizon",
    "decision_horizon_reflection",
    "structured_reflections",
    "shadow_reflections",
    "shadow_degraded",
    "signal_weight_history",
}

MAX_DELETE_RATIO = 0.30   # 单表删除比例上限
MAX_DELETE_MIN_ROWS = 1000  # 只有超过这个量级才做比例保护

_ID_COLUMNS = ("decision_id", "ai_decision_id")


def _valid_id_sql(conn: sqlite3.Connection) -> str:
    """两个权威空间 + 归档的并集（NULL 安全）。"""
    parts = ["SELECT id FROM ai_decisions", "SELECT id FROM trade_decisions"]
    for t, c in (("ai_decisions_history", "id"),
                 ("decision_ref_archive", "decision_id")):
        try:
            conn.execute(f"SELECT 1 FROM {t} LIMIT 1")
            parts.append(f"SELECT {c} FROM {t} WHERE {c} IS NOT NULL")
        except sqlite3.OperationalError:
            continue
    return " UNION ".join(parts)


def decision_tables(conn: sqlite3.Connection) -> List[str]:
    names = [r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")]
    out: List[str] = []
    for t in names:
        if t in EXCLUDE_TABLES:
            continue
        cols = {r[1] for r in conn.execute(f"PRAGMA table_info('{t}')")}
        if any(c in cols for c in _ID_COLUMNS):
            out.append(t)
    return out


def _where_orphan(cols: List[str], valid_sql: str) -> str:
    parts = [f"({c} IS NOT NULL AND {c} NOT IN ({valid_sql}))" for c in cols]
    return " OR ".join(parts) if parts else "0"


def audit(conn: sqlite3.Connection, *, all_tables: bool = False) -> List[Dict[str, Any]]:
    valid = _valid_id_sql(conn)
    rows: List[Dict[str, Any]] = []
    for t in decision_tables(conn):
        if not all_tables and t not in PURGE_ALLOW:
            continue
        cols = [r[1] for r in conn.execute(f"PRAGMA table_info('{t}')")]
        use = [c for c in _ID_COLUMNS if c in cols]
        where = _where_orphan(use, valid)
        try:
            total = conn.execute(f"SELECT COUNT(*) FROM '{t}'").fetchone()[0]
            orphan = conn.execute(f"SELECT COUNT(*) FROM '{t}' WHERE {where}").fetchone()[0]
        except sqlite3.OperationalError:
            continue
        if orphan:
            rows.append({"table": t, "columns": use, "total": int(total),
                         "orphan": int(orphan),
                         "ratio": round(orphan / total, 3) if total else 0.0,
                         "purgeable": t in PURGE_ALLOW})
    rows.sort(key=lambda r: r["orphan"], reverse=True)
    return rows


def purge(conn: sqlite3.Connection, *, dry_run: bool = True,
          dump_dir: Optional[str] = None) -> Dict[str, Any]:
    """清理白名单表的孤儿行。dry_run=True 只报数。"""
    summary: Dict[str, Any] = {"dry_run": bool(dry_run), "tables": [],
                               "total_orphans": 0, "deleted": 0,
                               "refused": [], "dump": ""}
    valid = _valid_id_sql(conn)
    dump_path = ""
    fh = None
    first = True
    if not dry_run:
        dump_dir = dump_dir or os.path.join(
            os.path.expanduser("~"), ".trident", "backups")
        os.makedirs(dump_dir, exist_ok=True)
        dump_path = os.path.join(
            dump_dir, f"decision_orphans_{time.strftime('%Y%m%d_%H%M%S')}.json")
        fh = open(dump_path, "w", encoding="utf-8")
        fh.write("[")
    try:
        for t in decision_tables(conn):
            if t not in PURGE_ALLOW:
                continue
            cols = [r[1] for r in conn.execute(f"PRAGMA table_info('{t}')")]
            use = [c for c in _ID_COLUMNS if c in cols]
            where = _where_orphan(use, valid)
            n = conn.execute(f"SELECT COUNT(*) FROM '{t}' WHERE {where}").fetchone()[0]
            if not n:
                continue
            total = conn.execute(f"SELECT COUNT(*) FROM '{t}'").fetchone()[0]
            # 安全阀：大比例删除一定是口径写错了，宁可不删
            if total > MAX_DELETE_MIN_ROWS and n / total > MAX_DELETE_RATIO:
                summary["refused"].append({"table": t, "orphan": int(n),
                                           "total": int(total),
                                           "reason": "占比超过安全阀 30%"})
                continue
            if not dry_run:
                info = [c[1] for c in conn.execute(f"PRAGMA table_info('{t}')")]
                rows = conn.execute(
                    f"SELECT * FROM '{t}' WHERE {where} LIMIT 5000").fetchall()
                for r in rows:
                    rec = {"table": t, "row": dict(zip(info, r))}
                    if not first:
                        fh.write(",\n")
                    fh.write(json.dumps(rec, ensure_ascii=False, default=str))
                    first = False
                conn.execute(f"DELETE FROM '{t}' WHERE {where}")
                summary["deleted"] += int(n)
            summary["tables"].append({"table": t, "orphan": int(n),
                                      "total": int(total),
                                      "deleted": 0 if dry_run else int(n)})
            summary["total_orphans"] += int(n)
    finally:
        if fh:
            fh.write("]")
            fh.close()
            summary["dump"] = dump_path
    return summary


def main(argv: Optional[List[str]] = None) -> int:
    import argparse

    p = argparse.ArgumentParser(description="决策派生表孤儿清理（白名单）")
    p.add_argument("--apply", action="store_true", help="真正删除（默认只盘点）")
    p.add_argument("--all-tables", action="store_true",
                   help="盘点所有带 decision_id 的表（仅报告，永不删除）")
    args = p.parse_args(argv)
    from db import get_connection

    conn = get_connection()
    try:
        if not args.apply:
            rows = audit(conn, all_tables=args.all_tables)
            print(json.dumps({"orphan_tables": rows,
                              "total": sum(r["orphan"] for r in rows)},
                             ensure_ascii=False, indent=2))
            return 0
        res = purge(conn, dry_run=False)
        conn.commit()
        print(json.dumps(res, ensure_ascii=False, indent=2))
        return 0
    finally:
        conn.close()


if __name__ == "__main__":
    raise SystemExit(main())
