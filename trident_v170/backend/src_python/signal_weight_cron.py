# -*- coding: utf-8 -*-
"""
v20251014 signal_weight_cron

Background task that re-evaluates signal_weight for all recent decisions every
5 minutes. Two main effects:

  1. reaction_mult update — picks up any new abnormal_signals triggered in
     the 5min window since the last cron tick. Down-weights "cold signals"
     (signal sent but market didn't move).

  2. time_mult decay — naturally decreases weight for older signals (handled
     inside compute_signal_weight, no special logic needed here).

  3. cold_signal flag — sets ai_decisions.cold_signal=1 for stale signals
     so frontend can gray them out (or paper trading can skip them).

Usage:
    # One-shot (manual / cron entry-point)
    python3 signal_weight_cron.py --once

    # Daemon mode (run every 5min, never exit)
    python3 signal_weight_cron.py --loop --interval 300

Cron entry (recommended):
    */5 * * * * cd /path/to/backend/src_python && python3 signal_weight_cron.py --once >> logs/signal_weight_cron.log 2>&1

Performance:
    ~10ms per decision on a 1k decision batch. Default 24h window = ~500-2000
    decisions = 5-20 seconds total. Safe to run every 5min.
"""
from __future__ import annotations

import argparse
import sqlite3
import sys
import time
import traceback
from typing import Any, Dict, Optional


# Add backend/src_python to sys.path so we can import db / signal_weight_evaluator
import os
_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)


def _connect(db_path=None):
    """Connect to DB. db_path is used for tests; default reads from config."""
    if db_path:
        return sqlite3.connect(db_path)
    try:
        import db as _db
        # db.py exposes get_connection() (with timeout, row_factory, isolation)
        # but historically also a connect() alias — try both for forward-compat.
        if hasattr(_db, "get_connection"):
            return _db.get_connection()
        if hasattr(_db, "connect"):
            return _db.connect()
    except Exception:
        pass
    # Last-resort fallback (only used if db module fails to import).
    return sqlite3.connect("ai.db")


def run_once(hours=24, db_path=None, trigger_kind="cron_react",
             backfill_source=True, backfill_limit=500):
    """Single pass: recompute all recent non-HOLD decisions.

    v20261023: 新增 source_stats 回写步骤。每轮结算后把已结算但尚未计入的
    决策增量写入命中率统计 —— 此前该闭环只有 HTTP 手动端点驱动，定时任务
    从不回写，命中率会随时间冻结。幂等性见 backfill_source_stats()。

    Returns:
        {"processed": N, "updated": M, "cold": K, "errors": E, "log_id": L,
         "source_stats": {...}}
    """
    started = int(time.time())
    conn = _connect(db_path)
    log_id = 0
    try:
        # Ensure schema
        try:
            import db as _db
            _db.migrate(conn)
        except Exception:
            pass
        # Log start
        try:
            cur = conn.execute(
                """INSERT INTO signal_weight_cron_log
                    (started_at, processed_n, updated_n, cold_n, error_msg)
                   VALUES (?, 0, 0, 0, '')""",
                (started,),
            )
            log_id = cur.lastrowid or 0
            conn.commit()
        except sqlite3.OperationalError:
            pass
        import signal_weight_evaluator as _swe
        # v20261023: 先回写命中率闭环，再重算权重 —— 保证本轮重算能读到
        # 本轮刚更新的 source_mult。顺序反过来会慢一轮才生效。
        src_stats = {"skipped": "disabled"}
        if backfill_source:
            try:
                src_stats = _swe.backfill_source_stats(
                    conn, limit=backfill_limit, now_ts=started,
                )
                conn.commit()
            except Exception as e:  # noqa: BLE001
                # 回写失败不得阻断权重重算 —— 权重正确性优先于统计新鲜度。
                conn.rollback()
                src_stats = {"error": f"{type(e).__name__}: {e}"}
                print(f"[signal_weight_cron] source_stats backfill failed: "
                      f"{src_stats['error']}", file=sys.stderr, flush=True)
        stats = _swe.batch_recompute(
            conn, hours=hours, trigger_kind=trigger_kind, now_ts=started,
        )
        conn.commit()
        if log_id:
            try:
                conn.execute(
                    """UPDATE signal_weight_cron_log SET
                        finished_at = ?, processed_n = ?, updated_n = ?,
                        cold_n = ?, error_msg = ''
                       WHERE id = ?""",
                    (int(time.time()), stats["processed"], stats["updated"],
                     stats["cold"], log_id),
                )
                conn.commit()
            except sqlite3.OperationalError:
                pass
        stats["log_id"] = log_id
        stats["source_stats"] = src_stats
        return stats
    except Exception as e:  # noqa: BLE001
        err = f"{type(e).__name__}: {e}\n{traceback.format_exc()[:500]}"
        if log_id:
            try:
                conn.execute(
                    """UPDATE signal_weight_cron_log SET
                        finished_at = ?, error_msg = ? WHERE id = ?""",
                    (int(time.time()), err[:1000], log_id),
                )
                conn.commit()
            except sqlite3.OperationalError:
                pass
        print(f"[signal_weight_cron] ERROR: {err}", file=sys.stderr, flush=True)
        return {"processed": 0, "updated": 0, "cold": 0, "errors": 1, "log_id": log_id}
    finally:
        try:
            conn.close()
        except Exception:
            pass


def run_loop(interval_sec=300, hours=24):
    """Daemon mode: run every interval_sec until killed."""
    print(f"[signal_weight_cron] starting loop mode (interval={interval_sec}s, "
          f"hours={hours})", flush=True)
    while True:
        try:
            stats = run_once(hours=hours)
            print(f"[signal_weight_cron] tick: {stats}", flush=True)
        except KeyboardInterrupt:
            print("[signal_weight_cron] interrupted, exiting", flush=True)
            return
        except Exception as e:  # noqa: BLE001
            print(f"[signal_weight_cron] tick error: {e}", file=sys.stderr, flush=True)
        time.sleep(interval_sec)


def main():
    p = argparse.ArgumentParser(description="signal_weight_cron - re-evaluate signal weights")
    p.add_argument("--once", action="store_true",
                   help="Single pass (default for cron entry-point)")
    p.add_argument("--loop", action="store_true",
                   help="Daemon mode: run forever every --interval seconds")
    p.add_argument("--interval", type=int, default=300,
                   help="Loop interval in seconds (default 300 = 5min)")
    p.add_argument("--hours", type=int, default=24,
                   help="Re-evaluate decisions in the last N hours (default 24)")
    p.add_argument("--db", type=str, default=None,
                   help="DB path override (default: use db.get_connection())")
    p.add_argument("--no-backfill", action="store_true",
                   help="跳过 source_stats 命中率回写（只重算权重）")
    p.add_argument("--backfill-limit", type=int, default=500,
                   help="单次回写最多处理多少条结算决策 (default 500)")
    args = p.parse_args()
    if args.loop:
        run_loop(interval_sec=args.interval, hours=args.hours)
        return 0
    stats = run_once(hours=args.hours, db_path=args.db,
                     backfill_source=not args.no_backfill,
                     backfill_limit=args.backfill_limit)
    print(f"[signal_weight_cron] {stats}", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())

