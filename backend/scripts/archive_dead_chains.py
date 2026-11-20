#!/usr/bin/env python3
"""v419 · 存量死链归档（默认 dry-run，`--apply` 才写库）

基于 `audit_dead_chains.py` 的分类结果，把**确定不是市场机制**的死链从
`causal_chains` 挪到 `causal_chains_archived`（整行原样保留，可一键还原）。

默认只打印计划，不写库；`--apply` 做的事：
  1) 建 `causal_chains_archived`（若无），把 A 类整行拷过去，附归档原因与时间；
  2) 从 `causal_chains` 删除这些行（**已先整行备份**，还原见 `--restore`）；
  3) 复核：主表总数、死链数、`causal_chain_outcomes` 里被判死链的引用数应为 0。

只归档 A 类（note 已判"系统体检日志/非市场机制"）。B/C/D/E 类一律不动 ——
它们要么能救、要么该转规则库，都不属于"垃圾清理"。

用法：
    python3 scripts/archive_dead_chains.py                  # 只看
    python3 scripts/archive_dead_chains.py --apply           # 归档 A 类
    python3 scripts/archive_dead_chains.py --restore         # 还原回去
"""
from __future__ import annotations

import argparse
import os
import sqlite3
import sys
import time

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _HERE)

from audit_dead_chains import DEAD_ASSETS, classify, _insight

BACKEND = os.path.dirname(_HERE)
DB = os.path.join(BACKEND, "trident_event_bus.db")
ARCHIVE = "causal_chains_archived"


def _conn(write: bool) -> sqlite3.Connection:
    uri = f"file:{DB}" if write else f"file:{DB}?mode=ro"
    conn = sqlite3.connect(uri, uri=not write, timeout=15)
    conn.row_factory = sqlite3.Row
    return conn


def _dead_rows(conn) -> list[dict]:
    return [
        dict(r) for r in conn.execute(
            """SELECT * FROM causal_chains
               WHERE asset IS NULL OR asset = '' OR UPPER(asset) IN ('UNKNOWN','NONE','MACRO')
                  OR COALESCE(trigger_words,'') = ''"""
        ).fetchall()
    ]


def plan(conn) -> list[dict]:
    out = []
    for r in _dead_rows(conn):
        cls, why = classify(r, _insight(conn, r.get("note") or ""))
        if cls == "A":
            r["_why"] = why
            out.append(r)
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--restore", action="store_true")
    args = ap.parse_args()

    if args.restore:
        conn = _conn(True)
        try:
            if not conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (ARCHIVE,)
            ).fetchone():
                print("没有归档表，无需还原")
                return 0
            cols = [r[1] for r in conn.execute(f"PRAGMA table_info({ARCHIVE})")]
            keep = [c for c in cols if c != "archived_at"]
            cols_sql = ", ".join(keep)
            n = conn.execute(f"SELECT COUNT(*) FROM {ARCHIVE}").fetchone()[0]
            conn.execute(
                f"INSERT OR IGNORE INTO causal_chains ({cols_sql}) SELECT {cols_sql} FROM {ARCHIVE}"
            )
            conn.execute(f"DELETE FROM {ARCHIVE}")
            conn.commit()
            print(f"已还原 {n} 条到 causal_chains（归档表已清空）")
        finally:
            conn.close()
        return 0

    conn = _conn(args.apply)
    try:
        todo = plan(conn)
        total = len(_dead_rows(conn))
        live_total = conn.execute("SELECT COUNT(*) FROM causal_chains").fetchone()[0]
        print(f"主表 {live_total} 条，死链 {total} 条，其中 A 类（非市场机制）{len(todo)} 条")
        if not todo:
            print("无需归档")
            return 0
        for r in todo[:5]:
            print(f"  #{r['id']} {str(r['name'])[:40]} | {r['_why']}")
        if not args.apply:
            print("\n[dry-run] 未写库。确认后加 --apply；还原用 --restore。")
            return 0

        cols = [r[1] for r in conn.execute("PRAGMA table_info(causal_chains)")]
        cols_sql = ", ".join(cols)
        conn.execute(
            f"CREATE TABLE IF NOT EXISTS {ARCHIVE} ({cols_sql}, archived_at TEXT, archived_reason TEXT)"
        )
        now = time.strftime("%Y-%m-%d %H:%M:%S")
        for r in todo:
            conn.execute(
                f"INSERT INTO {ARCHIVE} ({cols_sql}, archived_at, archived_reason) VALUES "
                f"({', '.join('?' * len(cols))}, ?, ?)",
                tuple(r.get(c) for c in cols) + (now, r["_why"]),
            )
        ids = [int(r["id"]) for r in todo]
        conn.executemany("DELETE FROM causal_chains WHERE id=?", [(i,) for i in ids])
        conn.commit()

        # 复核
        after = conn.execute("SELECT COUNT(*) FROM causal_chains").fetchone()[0]
        dead_after = len(_dead_rows(conn))
        refs = conn.execute(
            "SELECT COUNT(*) FROM causal_chain_outcomes WHERE CAST(chain_id AS INTEGER) IN "
            f"({','.join('?' * len(ids))})", ids
        ).fetchone()[0] if ids else 0
        arch = conn.execute(f"SELECT COUNT(*) FROM {ARCHIVE}").fetchone()[0]
        print(f"\n已归档 {len(ids)} 条 → 主表 {live_total} → {after}，死链 {total} → {dead_after}")
        print(f"归档表 {arch} 条；这些链的历史评估记录 = {refs}（>0 说明还需单独处理引用）")
        return 0
    finally:
        conn.close()


if __name__ == "__main__":
    sys.exit(main())
