# -*- coding: utf-8 -*-
"""outcome_backfill_merge.py — 把在副本上算好的 outcome 一次性并进生产（v405 / P1）

为什么不在生产库里直接跑 backfill
---------------------------------
生产库 WAL 里同时有 30 多个 cron（`cron_atom_backfill --limit 3000`、
`signal_performance_settler --apply`、`market_flow --fetch --commit` …）每分钟在写，
`causal_chain_evaluator.backfill` 是**逐行 commit**：3,000+ 次提交，每一次都要抢写锁 →
实测跑 4 分钟直接被 `database is locked` 打死。

所以改成"先算、后并"：
  1. 在副本（`--source`，生产快照）上跑 backfill + evaluate_pending，算完不抢生产锁；
  2. 用一个事务把结果并进生产（`--apply`），提交前只 hold 锁几十毫秒。

这套做法对"历史重算"是安全的：副本与生产是同一份快照，OHLCV 与 news 完全一致，
同样的输入必然得到同样的判定。**新的实时触发仍走生产侧的 routine（每小时 cron）。**

用法
----
    python3 scripts/outcome_backfill_merge.py --source /tmp/fin10_outcome_lab.db           # 只预览
    python3 scripts/outcome_backfill_merge.py --source /tmp/fin10_outcome_lab.db --apply   # 写入
    python3 scripts/outcome_backfill_merge.py --source ... --apply --lock-timeout 300
"""
from __future__ import annotations

import argparse
import sqlite3
import sys
import time
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[1]
if str(BACKEND / "src_python") not in sys.path:
    sys.path.insert(0, str(BACKEND / "src_python"))

# 连同 actual_* 一起写 —— 只搬 status 不搬收盘价，会留下"HIT 但没有实际价"的残行
COLS_INS = ["chain_id", "trigger_ts", "trigger_source", "trigger_ref_id", "trigger_headline",
            "trigger_keywords_matched", "predicted_direction", "predicted_mag_lo",
            "predicted_mag_hi", "eval_deadline_ts", "actual_close_at_trigger",
            "actual_close_at_5d", "actual_pct_5d", "status", "direction_hit",
            "magnitude_hit", "notes", "evaluated_at", "created_at"]
COLS_UPD = ["actual_close_at_trigger", "actual_close_at_5d", "actual_pct_5d", "status",
            "direction_hit", "magnitude_hit", "notes", "evaluated_at"]


def _db_path() -> str:
    try:
        import db
        return db.get_db_path()
    except Exception:
        return str(BACKEND / "trident_event_bus.db")


def merge(source: str, target: str, apply: bool, lock_timeout: int) -> dict:
    src = sqlite3.connect("file:%s?mode=ro" % source, uri=True, timeout=30)
    src.row_factory = sqlite3.Row
    rows = [dict(r) for r in src.execute(
        "SELECT * FROM causal_chain_outcomes ORDER BY id")]
    src.close()

    tgt = sqlite3.connect(target, timeout=lock_timeout)
    tgt.row_factory = sqlite3.Row
    tgt.execute("PRAGMA busy_timeout=%d" % (lock_timeout * 1000))
    existing = {}
    for r in tgt.execute("SELECT id, chain_id, trigger_ts, trigger_ref_id, status FROM causal_chain_outcomes"):
        existing[(str(r["chain_id"]), int(r["trigger_ts"]), int(r["trigger_ref_id"] or 0))] = dict(r)

    to_insert, to_update = [], []
    for r in rows:
        key = (str(r["chain_id"]), int(r["trigger_ts"]), int(r["trigger_ref_id"] or 0))
        cur = existing.get(key)
        if cur is None:
            to_insert.append(r)
        elif r["status"] != "PENDING" and cur["status"] == "PENDING":
            to_update.append((r, cur["id"]))

    stats = {"source_rows": len(rows), "insert": len(to_insert), "evaluate": len(to_update)}
    if not apply:
        return stats

    t0 = time.time()
    tgt.execute("BEGIN IMMEDIATE")
    try:
        for r in to_insert:
            tgt.execute(
                "INSERT OR IGNORE INTO causal_chain_outcomes (%s) VALUES (%s)"
                % (",".join(COLS_INS), ",".join("?" * len(COLS_INS))),
                tuple(r[c] for c in COLS_INS),
            )
        for r, oid in to_update:
            tgt.execute(
                "UPDATE causal_chain_outcomes SET %s WHERE id=?"
                % ",".join("%s=?" % c for c in COLS_UPD),
                tuple(r[c] for c in COLS_UPD) + (oid,),
            )
        tgt.commit()
    except Exception:
        tgt.rollback()
        tgt.close()
        raise
    stats["elapsed_s"] = round(time.time() - t0, 2)
    stats["status_after"] = [tuple(x) for x in tgt.execute(
        "SELECT status, COUNT(*) FROM causal_chain_outcomes GROUP BY 1")]
    tgt.close()
    return stats


def main(argv=None):
    ap = argparse.ArgumentParser(description="把副本上算好的 outcome 并进生产（v405）")
    ap.add_argument("--source", required=True, help="算好的副本库")
    ap.add_argument("--target", default="", help="默认取 db.get_db_path()")
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--lock-timeout", type=int, default=180)
    a = ap.parse_args(argv)
    st = merge(a.source, a.target or _db_path(), a.apply, a.lock_timeout)
    print(("已写入" if a.apply else "预览（未写入）") + ": " + ", ".join(
        "%s=%s" % (k, v) for k, v in st.items() if k != "status_after"))
    if "status_after" in st:
        print("生产现状: %s" % (st["status_after"],))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
