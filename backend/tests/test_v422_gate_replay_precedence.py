#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""v422：门卫回放的 backlog 统计被 SQL 优先级吞掉 → 配额被无意义烧光。

## 现场（2026-11-17 10:4x）

`[gate-replay] 自动回放 33 条，剩余 11141（ok=33 frozen=1 err=0 remaining=11141）`

但同一时刻真查库：

| 口径 | 条数 |
|---|---|
| `gate_unavailable=1 AND status='PENDING'` | **1** |
| `(gate_unavailable=1 OR 未判定) AND status='PENDING'`（正确口径） | **3** |
| 脚本报的 remaining | **11,141** |

## 根因

`count_backlog()` 把 `OR (gate_verdict IS NULL ...)` 直接拼在 WHERE 末尾、**没加括号**：

```sql
WHERE COALESCE(gate_unavailable,0)=1 OR (gate_verdict IS NULL OR gate_verdict IN ('','未判定'))
```

SQL 里 AND 优先于 OR ⇒ status 条件被整个 OR 掉 ⇒ 统计口径变成"全库未判定新闻"，
包含早已 FILTERED / DONE 的历史行。

## 后果链

`if not pending: return stat` 这个短路**永远不触发** ⇒ 每 300s 照跑一轮回放
（50 条/轮、每条 6–13s）⇒ 今日 gate 调用 2,688 次 vs 主分析 312 次（8.6:1）
⇒ 配额熔断 ⇒ 可交易新闻排不上分析 ⇒ 不成交。
"""
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src_python"))

from engine.gate_replay_guard import count_backlog


def _make_db():
    conn = sqlite3.connect(":memory:")
    # v444：count_backlog 的 SQL 带断档水位过滤（ts IS NOT NULL AND ts >= ?），
    # 这里必须给 ts 列，否则直接 OperationalError: no such column: ts。
    conn.execute(
        "CREATE TABLE raw_news ("
        " id INTEGER PRIMARY KEY, status TEXT, gate_unavailable INTEGER,"
        " gate_verdict TEXT, ts INTEGER)"
    )
    _TS = 2_000_000_000  # 远高于任何断档水位，保证全部行都参与统计
    rows = [
        # 真·待回放（应计入 pending）
        (1, "PENDING", 1, "", _TS),
        (2, "PENDING", 0, "", _TS),        # PENDING 且未判定 → 也应计入
        (3, "PENDING", 1, "未判定", _TS),
        # 历史行：不是 PENDING，不该计入 pending（bug 会把这些算进来）
        (4, "FILTERED", 0, "", _TS),
        (5, "FILTERED", 0, None, _TS),
        (6, "DONE", 0, "", _TS),
        (7, "FAILED", 0, "", _TS),
        (8, "DONE", 1, "", _TS),
        # 已判定完成的行：不该计入
        (9, "PENDING", 0, "排除", _TS),
        (10, "DONE", 1, "可交易", _TS),
    ]
    conn.executemany("INSERT INTO raw_news VALUES (?,?,?,?,?)", rows)
    return conn


def test_pending_counts_only_pending_rows():
    """pending 必须只统计真·PENDING 行（历史 FILTERED/DONE 不能算进来）。"""
    conn = _make_db()
    total, pending = count_backlog(conn)
    assert pending == 3, f"待回放应只有 3 条真 PENDING，实际算出 {pending}"


def test_total_not_dominated_by_history():
    """total 也只应统计 gate_unavailable=1 或未判定的行，而不是全库。"""
    conn = _make_db()
    total, _ = count_backlog(conn)
    # gate_unavailable=1（1,3,8,10）∪ 未判定（1,2,3,4,5,6,7,8）= {1..8,10} = 9 条
    # （id=10 虽是"已判定"，但 gate_unavailable=1，仍属 backlog 总口径）
    assert total == 9, f"total 应 9 条，实际 {total}"


def test_no_backlog_when_only_history_remains():
    """只剩历史行时 pending 必须是 0 —— 否则 `if not pending` 短路永远不触发。"""
    conn = sqlite3.connect(":memory:")
    conn.execute(
        "CREATE TABLE raw_news ("
        " id INTEGER PRIMARY KEY, status TEXT, gate_unavailable INTEGER,"
        " gate_verdict TEXT, ts INTEGER)"
    )
    conn.executemany(
        "INSERT INTO raw_news VALUES (?,?,?,?,?)",
        [(i, "FILTERED", 0, "", 2_000_000_000) for i in range(1, 6)]
        + [(6, "DONE", 0, None, 2_000_000_000)],
    )
    _, pending = count_backlog(conn)
    assert pending == 0, f"历史行不该让 pending 非零，实际 {pending}"
