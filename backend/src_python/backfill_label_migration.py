#!/usr/bin/env python3
"""backfill_label_migration.py — 修正 tech_signals 里历史 MACD 交叉标签。

背景
----
`tech_detector.py` 旧实现把 MACD 柱交叉的命名贴反了：
  · 柱由正转负（空头交叉）→ 旧标签 `macd_top_div`
  · 柱由负转正（多头交叉）→ 旧标签 `macd_bot_div`

台账复核时这两条被记成「反向」，一部分原因就是标签方向贴反。本脚本把历史
`signal_json` 里的旧 key 按纠正后口径重映射：
  `macd_top_div` → `macd_bear_cross`
  `macd_bot_div` → `macd_bull_cross`

注意：这是**标签修正**，不重算指标、不改动方向以外的数值；`ema_golden` /
`ema_death` 不在这里改名（它们口径没贴反，只是台账判定为噪声，已在桥接层
HOLD 处理）。

幂等：只替换还带旧 key 的行；重复跑不会二次改写。

用法:
    python3 backfill_label_migration.py --dry-run   # 只统计，不写库
    python3 backfill_label_migration.py             # 实写
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from typing import Any
import time
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(Path(__file__).resolve().parent))

DB_PATH = BACKEND / "trident_event_bus.db"

# 旧标签 → 纠正后标签
LABEL_MAP = {
    "macd_top_div": "macd_bear_cross",
    "macd_bot_div": "macd_bull_cross",
}


def remap_items(items: list[dict]) -> tuple[list[dict], int]:
    """按 LABEL_MAP 重写每条命中的 k，返回 (新列表, 改写条数)。"""
    changed = 0
    for it in items:
        k = str(it.get("k") or "")
        if k in LABEL_MAP:
            it["k"] = LABEL_MAP[k]
            changed += 1
    return items, changed


def migrate_row(conn: sqlite3.Connection, row_id: int, signal_json: str) -> int:
    try:
        items = json.loads(signal_json or "[]")
    except (TypeError, ValueError):
        return 0
    if not isinstance(items, list):
        return 0
    items, changed = remap_items(items)
    if changed == 0:
        return 0
    conn.execute(
        "UPDATE tech_signals SET signal_json=? WHERE id=?",
        (json.dumps(items, ensure_ascii=False), row_id),
    )
    return changed


def migrate_abnormal_detail(conn: sqlite3.Connection) -> int:
    """abnormal_signals.detail_json 里若带旧因子 key，同步重映射。"""
    changed_rows = 0
    rows = conn.execute(
        "SELECT id, detail_json FROM abnormal_signals "
        "WHERE detail_json LIKE '%macd_top_div%' OR detail_json LIKE '%macd_bot_div%'"
    ).fetchall()
    for r in rows:
        try:
            detail = json.loads(r["detail_json"] or "{}")
        except (TypeError, ValueError):
            continue
        touched = False
        # detail 里因子可能在 factors 列表或顶层 divergence/rsi 字段，统一递归找 k
        def walk(node: Any) -> None:
            nonlocal touched
            if isinstance(node, dict):
                if str(node.get("k") or "") in LABEL_MAP:
                    node["k"] = LABEL_MAP[node["k"]]
                    touched = True
                for v in node.values():
                    walk(v)
            elif isinstance(node, list):
                for v in node:
                    walk(v)
        walk(detail)
        if touched:
            conn.execute(
                "UPDATE abnormal_signals SET detail_json=? WHERE id=?",
                (json.dumps(detail, ensure_ascii=False), r["id"]),
            )
            changed_rows += 1
    return changed_rows


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--db", default=str(DB_PATH))
    args = ap.parse_args()

    if not Path(args.db).exists():
        print(f"[FATAL] DB not found: {args.db}", file=sys.stderr)
        return 1

    conn = sqlite3.connect(args.db, timeout=30)
    conn.row_factory = sqlite3.Row
    try:
        like = " OR ".join("signal_json LIKE ?" for _ in LABEL_MAP)
        params = [f"%{k}%" for k in LABEL_MAP]
        rows = conn.execute(
            f"SELECT id, signal_json FROM tech_signals WHERE {like}", params
        ).fetchall()

        total_rows = len(rows)
        total_keys = 0
        written = 0
        for r in rows:
            before = r["signal_json"] or ""
            items_before = json.loads(before or "[]") if before else []
            keys_before = [str(it.get("k") or "") for it in items_before]
            n_old = sum(1 for k in keys_before if k in LABEL_MAP)
            if n_old == 0:
                continue
            changed = migrate_row(conn, r["id"], before)
            if changed:
                total_keys += changed
                written += 1

        if not args.dry_run:
            conn.commit()

        abnormal_n = 0 if args.dry_run else migrate_abnormal_detail(conn)
        if not args.dry_run:
            conn.commit()

        print(f"[{'DRY-RUN' if args.dry_run else 'OK'}] 命中含旧标签的行 {total_rows}；"
              f"{'待改写' if args.dry_run else '已改写'} {written} 行，"
              f"重映射标签 {total_keys} 处；abnormal_signals 改写 {abnormal_n} 行")
        for old, new in LABEL_MAP.items():
            print(f"  {old} → {new}")
        return 0
    finally:
        conn.close()


if __name__ == "__main__":
    raise SystemExit(main())
