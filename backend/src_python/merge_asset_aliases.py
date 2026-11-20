# -*- coding: utf-8 -*-
"""把"同一品种的两套名字"合并到规范名下（v341）。

实测（2026-11-15）：同一个标的在库里被写成两个名字，样本被拆成两份：
    ADA 2480 行 / ADA-USD 9 行（9 行全重叠，纯重复）
    BNB 2470 / BNB-USD 31（31 全重叠）
    DOGE 2319 / DOGE-USD 10（10 全重叠）
    SOL 2254 / SOL-USD 31（31 全重叠）
    XRP 2505 / XRP-USD 7（7 全重叠）
    NVDA 6326 / NVIDIA 0（daily 无，但 external_indicators 里有 NVIDIA）

合并规则（保守）：
  1. 别名行 → 改写成规范名；
  2. 若规范名下已有同一 ts 的行：不覆盖、不比较"谁更准"，
     只把别名行丢掉（它就是重复）；值不一致的单独列出来人工看，不自动改；
  3. SKHYNIX 与 000660.KS 这类**两边都有独立数据**的，默认不动，
     只在报告里列出（合并它们等于选边站，得人来定）。

用法：
  python3 src_python/merge_asset_aliases.py --dry-run
  python3 src_python/merge_asset_aliases.py --apply
"""
from __future__ import annotations

import argparse
import json
import os
import sqlite3
import time

import asset_alias as aa

DB = os.path.join(os.path.dirname(__file__), "..", "trident_event_bus.db")
BACKUP_DIR = os.path.join(os.path.dirname(__file__), "runtime", "backup")

# (表, 名字列)
TARGETS = [
    ("ohlcv_daily", "asset"),
    ("ohlcv_intraday", "asset"),
    ("kline_bars", "symbol"),
    ("external_indicators", "asset"),
]

# 明确"两边各有独立数据"的组合：不自动合并
AMBIGUOUS = {
    # 两边各有独立数据，合并等于选边站
    ("SKHYNIX", "000660.KS"),
    # NVIDIA 名下只有 mexc_funding_rate（把个股当合约对上的味道），
    # 直接并进 NVDA 等于给一只股票塞进合约资金费，语义不对，先挂着
    ("NVDA", "NVIDIA"),
}


def _cols(conn, table):
    return [r[1] for r in conn.execute(f"PRAGMA table_info({table})")]


def plan_table(conn, table, col) -> list[dict]:
    """列出该表里所有"别名行"，并判定可否安全合并。"""
    try:
        names = [r[0] for r in conn.execute(
            f"SELECT DISTINCT {col} FROM {table} WHERE {col} IS NOT NULL")]
    except sqlite3.Error as e:
        return [{"table": table, "error": f"{type(e).__name__}: {e}"}]
    cols = _cols(conn, table)
    has_ts = "ts" in cols
    out = []
    for n in names:
        if not aa.is_alias(n):
            continue
        canon = aa.canonical(n)
        if (canon, n) in AMBIGUOUS or (n, canon) in AMBIGUOUS:
            out.append({"table": table, "alias": n, "canon": canon,
                        "rows": 0, "verdict": "ambiguous_skip",
                        "note": "两边都有独立数据，需人工定"})
            continue
        n_alias = conn.execute(f"SELECT COUNT(*) FROM {table} WHERE {col}=?", (n,)).fetchone()[0]
        n_canon = conn.execute(f"SELECT COUNT(*) FROM {table} WHERE {col}=?", (canon,)).fetchone()[0]
        overlap = 0
        mismatched = 0
        if has_ts:
            overlap = conn.execute(
                f"SELECT COUNT(*) FROM {table} a JOIN {table} b "
                f"ON a.ts=b.ts WHERE a.{col}=? AND b.{col}=?", (n, canon)).fetchone()[0]
            if "close" in cols:
                mismatched = conn.execute(
                    f"SELECT COUNT(*) FROM {table} a JOIN {table} b "
                    f"ON a.ts=b.ts WHERE a.{col}=? AND b.{col}=? AND a.close<>b.close",
                    (n, canon)).fetchone()[0]
        verdict = "merge" if n_canon else "rename_only"
        out.append({"table": table, "alias": n, "canon": canon,
                    "alias_rows": n_alias, "canon_rows": n_canon,
                    "overlap": overlap, "mismatched": mismatched,
                    "verdict": verdict})
    return out


def apply_table(conn, table, col, item, backup: dict) -> tuple[int, int]:
    """返回 (改名条数, 删重条数)。"""
    alias, canon = item["alias"], item["canon"]
    cols = _cols(conn, table)
    rows = conn.execute(f"SELECT * FROM {table} WHERE {col}=?", (alias,)).fetchall()
    backup.setdefault(table, []).extend([dict(zip(cols, r)) for r in rows])
    renamed = dropped = 0
    if "ts" in cols:
        existing = {r[0] for r in conn.execute(
            f"SELECT ts FROM {table} WHERE {col}=?", (canon,))}
        for r in rows:
            rec = dict(zip(cols, r))
            if rec["ts"] in existing:
                conn.execute(f"DELETE FROM {table} WHERE {col}=? AND ts=?",
                             (alias, rec["ts"]))
                dropped += 1
            else:
                conn.execute(
                    f"UPDATE {table} SET {col}=? WHERE {col}=? AND ts=?",
                    (canon, alias, rec["ts"]))
                existing.add(rec["ts"])
                renamed += 1
    else:
        conn.execute(f"UPDATE {table} SET {col}=? WHERE {col}=?", (canon, alias))
        renamed = len(rows)
    return renamed, dropped


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true", help="实际写库（默认只报告）")
    ap.add_argument("--db", default=DB)
    args = ap.parse_args()

    conn = sqlite3.connect(args.db, timeout=120)
    plans = []
    for table, col in TARGETS:
        plans.extend(plan_table(conn, table, col))

    print("=== 别名扫描 ===")
    todo = []
    for p in plans:
        if "error" in p:
            print(f"  [ERR ] {p['table']}: {p['error']}")
            continue
        if p["verdict"] == "ambiguous_skip":
            print(f"  [HOLD] {p['table']:20s} {p['alias']} ↔ {p['canon']} — {p['note']}")
            continue
        print(f"  [{p['verdict'].upper():11s}] {p['table']:20s} {p['alias']} → {p['canon']}"
              f"  别名 {p['alias_rows']} 行 / 规范名下已有 {p['canon_rows']} 行"
              f" / 重叠 {p['overlap']} / 值不一致 {p['mismatched']}")
        todo.append(p)

    if not todo:
        print("[DONE] 没有可安全合并的别名")
        return 0

    total = sum(p["alias_rows"] for p in todo)
    print(f"\n合计待处理 {len(todo)} 组、{total} 行")
    if not args.apply:
        print("[DRY-RUN] 未改动。确认后加 --apply（先备份被改动的行到 runtime/backup）")
        return 0

    os.makedirs(BACKUP_DIR, exist_ok=True)
    backup = {}
    renamed = dropped = 0
    with conn:
        for p in todo:
            r, d = apply_table(conn, p["table"], ("symbol" if p["table"] == "kline_bars"
                                                  else "asset"), p, backup)
            renamed += r
            dropped += d
    path = os.path.join(BACKUP_DIR, f"alias_merge_{time.strftime('%Y%m%d_%H%M%S')}.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump(backup, f, ensure_ascii=False)
    print(f"[APPLY] 改名 {renamed} 行 / 删除重复 {dropped} 行")
    print(f"[BACKUP] 被改动的原始行已存 {path}")

    left = [p for p in (plan_table(conn, t, c) for t, c in TARGETS) for p in p]
    left = [p for p in left if "error" not in p and p["verdict"] != "ambiguous_skip"]
    print(f"[VERIFY] 复扫剩余可合并别名 {len(left)} 组" + ("（已干净）" if not left else ""))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
