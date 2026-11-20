"""normalize_external_ts.py —— 把 external_indicators 的历史毫秒时间戳统一到秒

背景（2026-11-14）：
  该表 52746 行是秒、27383 行是毫秒（coinmetrics / derived_ratio / derived_crypto
  直接拿 K 线 ts_ms 落库）。下游一律按秒做窗口：
    · `now - ts` → 毫秒行的 age 是负的 ≈ -1.6e12 → 永远判"新鲜"（新鲜度审计失明）
    · `ts >= cutoff` / `BETWEEN lo AND hi` → 毫秒行要么全进要么全出

  写入端已在 `external_data.save_records` 归一（`_to_sec_ts`），这里处理存量。

用法：
  python3 normalize_external_ts.py            # dry-run
  python3 normalize_external_ts.py --commit
  python3 normalize_external_ts.py --rollback
"""
from __future__ import annotations

import argparse
import sqlite3
import time
from pathlib import Path
from typing import List

DB = Path(__file__).resolve().parents[1] / "trident_event_bus.db"
RB_TABLE = "external_indicators_ts_backfill"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--commit", action="store_true")
    ap.add_argument("--rollback", action="store_true")
    args = ap.parse_args()

    conn = sqlite3.connect(str(DB), timeout=30)
    conn.row_factory = sqlite3.Row

    if args.rollback:
        if RB_TABLE not in {r[0] for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'")}:
            print(f"没有 {RB_TABLE}，无法回滚")
            return 1
        n = conn.execute(f"SELECT COUNT(*) FROM {RB_TABLE}").fetchone()[0]
        conn.execute(
            f"UPDATE external_indicators SET ts = ts * 1000 "
            f"WHERE id IN (SELECT id FROM {RB_TABLE})")
        conn.commit()
        print(f"已回滚 {n} 行（恢复成毫秒）")
        return 0

    ids: List[int] = [r[0] for r in conn.execute(
        "SELECT id FROM external_indicators WHERE ts >= 100000000000")]
    src = conn.execute(
        "SELECT source, COUNT(*) n FROM external_indicators WHERE ts >= 100000000000 "
        "GROUP BY source ORDER BY n DESC").fetchall()
    total = conn.execute("SELECT COUNT(*) FROM external_indicators").fetchone()[0]
    print(f"总行 {total} · 待归一 {len(ids)} 行（{len(ids)/max(1,total):.1%}）")
    for r in src:
        print(f"  {r['source']}: {r['n']}")

    if not args.commit:
        print("\n[dry-run] 未写入。加 --commit 执行（会先建回滚表）")
        return 0

    now = int(time.time())
    conn.execute(
        f"CREATE TABLE IF NOT EXISTS {RB_TABLE} ("
        f" id INTEGER PRIMARY KEY, backed_up_at INTEGER NOT NULL)")
    conn.executemany(f"INSERT OR REPLACE INTO {RB_TABLE} VALUES (?,?)",
                     [(i, now) for i in ids])
    conn.execute("UPDATE external_indicators SET ts = ts / 1000 WHERE ts >= 100000000000")
    conn.commit()

    left = conn.execute(
        "SELECT COUNT(*) FROM external_indicators WHERE ts >= 100000000000").fetchone()[0]
    fresh = conn.execute(
        "SELECT COUNT(*) FROM external_indicators WHERE ts >= ?",
        (now - 7 * 86400,)).fetchone()[0]
    print(f"[commit] 已归一 {len(ids)} 行 · 残留毫秒 {left} 行 · 近 7 天 {fresh} 行")
    print("撤销: python3 normalize_external_ts.py --rollback")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
