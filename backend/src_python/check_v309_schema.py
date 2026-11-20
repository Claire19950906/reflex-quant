#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
check_v309_schema.py — v309 P0-1: 真实库 schema 检查 (只读)

为什么需要:
  之前 backfill_reflection_dimensions.py 查了**不存在的列**
  (is_correct / layer / sub_layer / signal_name), 报
  "no such column: is_correct", 但脚本把异常吞掉后 return 0 →
  验证脚本把"查询失败"判成了 PASS, 白跑一场。

  这个脚本只读地列出真实 schema, 并明确回答:
    1. trade_reflections_v2 存在吗?
    2. v309 的 7 个新列在不在?
    3. 回填脚本要用的标签列 (layer/sub_layer/signal_name) 在不在?
    4. 结算判定在哪张表 (quick_sim_trades.verdict / ai_decisions.is_correct)?
    5. 现有多少行待回填?

退出码: 0 = 一切就绪; 1 = 有缺失 (需先跑 db.migrate())
"""
from __future__ import annotations

import os
import sqlite3
import sys

SRC = os.path.dirname(os.path.abspath(__file__))
BACKEND = os.path.dirname(SRC)
sys.path.insert(0, SRC)

DB_PATH = os.path.join(BACKEND, "trident_event_bus.db")

V309_COLUMNS = [
    "dimension", "subcategory", "hit", "actual_outcome_pct",
    "confidence_at_time", "reflection_text", "settled",
]
# 回填脚本要用的三层标签列 (两组语义等价, 任一组在即可)
LABEL_GROUPS = [
    ("layer", "sub_layer", "signal_name"),
    ("error_layer", "error_sub_layer", "error_signal"),
]

_failures: list[str] = []


def _check(name: str, cond: bool, detail: str = "") -> None:
    if cond:
        print(f"  PASS  {name}")
    else:
        print(f"  FAIL  {name}" + (f" — {detail}" if detail else ""))
        _failures.append(name)


def main() -> int:
    print("=" * 62)
    print("v309 P0-1 真实库 schema 检查")
    print(f"  DB: {DB_PATH}")
    print("=" * 62)

    if not os.path.exists(DB_PATH):
        print(f"  FAIL  DB 文件不存在 — {DB_PATH}")
        return 1

    conn = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        tables = {r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")}

        _check("trade_reflections_v2 表存在", "trade_reflections_v2" in tables)
        if "trade_reflections_v2" not in tables:
            return 1

        cols = {r[1] for r in conn.execute("PRAGMA table_info(trade_reflections_v2)")}
        missing = [c for c in V309_COLUMNS if c not in cols]
        _check(f"v309 七列齐备 ({len(V309_COLUMNS)} 个)", not missing,
               f"缺: {missing} — 需先跑 db.migrate(conn) 补齐")

        # 标签列: 两组任一可用即可
        label_ok = False
        for grp in LABEL_GROUPS:
            if all(c in cols for c in grp):
                label_ok = True
                print(f"  INFO  标签列用: {grp}")
                break
        if not label_ok:
            print("  INFO  标签列: 两组都缺 (layer/sub_layer/signal_name 与 "
                  "error_layer/error_sub_layer/error_signal)")
        _check("回填所需标签列存在", label_ok,
               "两组标签列都不存在 — 需先跑 db.migrate(conn)")

        # 结算判定来源
        if "quick_sim_trades" in tables:
            qcols = {r[1] for r in conn.execute("PRAGMA table_info(quick_sim_trades)")}
            _check("quick_sim_trades.verdict 存在 (结算判定来源)",
                   "verdict" in qcols, f"实际列: {sorted(qcols)[:20]}")
            has_pnl = "pnl_pct" in qcols
            print(f"  INFO  quick_sim_trades.pnl_pct: {'有' if has_pnl else '无'}")
        else:
            _check("quick_sim_trades 表存在", False, "回填无法取结算信息")

        if "ai_decisions" in tables:
            acols = {r[1] for r in conn.execute("PRAGMA table_info(ai_decisions)")}
            print(f"  INFO  ai_decisions.is_correct: "
                  f"{'有' if 'is_correct' in acols else '无'}")

        # 待回填行数
        if not missing:
            n = conn.execute(
                "SELECT COUNT(*) FROM trade_reflections_v2 "
                "WHERE dimension IS NULL OR dimension = ''").fetchone()[0]
            print(f"  INFO  待回填行数: {n}")
            total = conn.execute(
                "SELECT COUNT(*) FROM trade_reflections_v2").fetchone()[0]
            print(f"  INFO  反思总行数: {total}")
            if total and n == total:
                print("  WARN  全部行都还没有维度 (符合预期: v309 之前从无写入方)")
    finally:
        conn.close()

    print("-" * 62)
    if _failures:
        print(f"FAILED ({len(_failures)}): {', '.join(_failures)}")
        # 提示必须给**能直接跑**的命令: db.migrate(conn) 需要一个连接,
        # 光写 db.migrate() 会抛 TypeError (缺 conn 参数)。
        print('提示: cd backend/src_python && python3 -c "import db; '
              'c = db.get_connection(); db.migrate(c); c.close()"')
        return 1
    print("SCHEMA OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
