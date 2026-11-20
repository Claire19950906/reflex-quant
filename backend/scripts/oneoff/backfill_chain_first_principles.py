#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""backfill_chain_first_principles.py — 给 causal_chains 补"第一性原理三问"。

    python3 backfill_chain_first_principles.py --limit 50 --workers 4
    python3 backfill_chain_first_principles.py --all --workers 8 --no-llm

行为:
  · 只处理 mechanism / precondition / invalidation 有空缺的链 (可重复执行, 幂等);
  · 默认走 LLM (MiniMax-M3) 生成, LLM 失败自动降级到规则模板;
  · 并发执行 (每线程一个 sqlite 连接, 写库串行提交)。
"""
from __future__ import annotations

import argparse
import os
import sqlite3
import sys
from pathlib import Path
import time
from concurrent.futures import ThreadPoolExecutor

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src_python"))

import causal_chain_first_principles as fp  # noqa: E402

DB_PATH = os.environ.get("TRIDENT_DB_PATH") or os.path.join(
    str(Path(__file__).resolve().parents[2]), "trident_event_bus.db"
)

CHAIN_COLS = ("id, name, cause, effect, trigger_words, asset, expected_direction, "
              "magnitude_lo, magnitude_hi, confidence, mechanism, precondition, invalidation")


def fetch_chains(conn, only_empty: bool, limit: int):
    sql = "SELECT %s FROM causal_chains" % CHAIN_COLS
    if only_empty:
        sql += (" WHERE COALESCE(TRIM(mechanism),'')='' "
                "   OR COALESCE(TRIM(precondition),'')='' "
                "   OR COALESCE(TRIM(invalidation),'')=''")
    sql += " ORDER BY id"
    if limit > 0:
        sql += " LIMIT %d" % int(limit)
    rows = conn.execute(sql).fetchall()
    return [dict(r) for r in rows]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default=DB_PATH)
    ap.add_argument("--limit", type=int, default=50)
    ap.add_argument("--all", action="store_true")
    ap.add_argument("--workers", type=int, default=4)
    ap.add_argument("--no-llm", dest="use_llm", action="store_false",
                    help="只用规则模板 (不调 LLM)")
    ap.add_argument("--include-filled", action="store_true",
                    help="连已填好的链一起重写")
    ap.set_defaults(use_llm=True)
    args = ap.parse_args()

    conn = sqlite3.connect(args.db, timeout=60)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    chains = fetch_chains(conn, not args.include_filled, 0 if args.all else args.limit)
    print("[INFO] 待处理链: %d (use_llm=%s, workers=%d)"
          % (len(chains), args.use_llm, args.workers))
    if not chains:
        conn.close()
        return 0

    t0 = time.time()
    results = {}

    def work(chain):
        try:
            out = fp.first_principles_for_chain(chain, use_llm=args.use_llm)
            return chain["id"], out
        except Exception as exc:  # noqa: BLE001
            return chain["id"], {"error": str(exc)}

    with ThreadPoolExecutor(max_workers=max(1, args.workers)) as pool:
        for cid, out in pool.map(work, chains):
            results[cid] = out
            done = len(results)
            if done % 25 == 0:
                print("[PROGRESS] %d/%d (%.0fs)" % (done, len(chains), time.time() - t0))

    ok = 0
    llm_n = 0
    for chain in chains:
        out = results.get(chain["id"]) or {}
        if out.get("error") or not out.get("mechanism"):
            continue
        conn.execute(
            "UPDATE causal_chains SET mechanism=?, precondition=?, invalidation=?, "
            "updated_at=datetime('now','localtime') WHERE id=?",
            (out["mechanism"][:1000], out["precondition"][:600],
             out["invalidation"][:600], int(chain["id"])),
        )
        ok += 1
        if out.get("source") == "llm":
            llm_n += 1
    conn.commit()
    print("[OK] 写入 %d 条 (LLM %d / 规则 %d), 用时 %.0fs"
          % (ok, llm_n, ok - llm_n, time.time() - t0))
    row = conn.execute(
        "SELECT COUNT(*) FROM causal_chains WHERE COALESCE(TRIM(mechanism),'')<>'' "
        "AND COALESCE(TRIM(precondition),'')<>'' AND COALESCE(TRIM(invalidation),'')<>''"
    ).fetchone()
    tot = conn.execute("SELECT COUNT(*) FROM causal_chains").fetchone()
    print("[OK] 三问齐全: %s / %s" % (row[0], tot[0]))
    conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
