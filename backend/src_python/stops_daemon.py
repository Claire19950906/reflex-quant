#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""v477 止损独立守护 + 账户对账告警。

为什么需要它：
    1. `stops.enforce` 原先只在 `trade_executor.tick()` 里调用。引擎一挂、一暂停
       （2026-11-19 16:34 就因为 DATA_PAUSED 停过），持仓的止损就完全停摆。
       本进程独立 30s 扫一次，只做“触发止损 → 平仓”，不开新仓。
    2. `account_ledger.reconcile()` 的 block（R1/R2/R3/R4/R6/R7）只有人手动跑才看得到。
       本进程每 5 轮跑一次，把 block 写进 `t2_alerts`（同 code 24h 去重，恢复时自动 resolved）。

跑法：
    python3 stops_daemon.py --interval 30
    python3 stops_daemon.py --once --json
"""
from __future__ import annotations

import argparse
import fcntl
import json
import os
import signal
import sqlite3
import sys
import time
from typing import Any, Dict, List, Optional

import account_ledger as al
import stops

FEATURE = "account_reconcile"
RUNNING = True

_ALERT_DDL = """
CREATE TABLE IF NOT EXISTS t2_alerts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    feature_id TEXT NOT NULL,
    level TEXT NOT NULL,
    dedupe_key TEXT NOT NULL,
    title TEXT,
    detail_json TEXT,
    created_at INTEGER NOT NULL,
    resolved_at INTEGER
)
"""


def _log(msg: str) -> None:
    print(f"[STOPS-GUARD] {time.strftime('%H:%M:%S')} {msg}", flush=True)


def sync_alerts(conn: sqlite3.Connection, issues: List[Dict[str, Any]],
                now: Optional[int] = None) -> Dict[str, int]:
    """把对账 block 写进 t2_alerts：新问题插入、老问题恢复时关闭。"""
    now = int(now or time.time())
    conn.execute(_ALERT_DDL)
    blocks = [i for i in issues if i.get("severity") == "block"]
    open_keys = {f"{FEATURE}:{i['code']}" for i in blocks}
    inserted = resolved = 0
    for i in blocks:
        key = f"{FEATURE}:{i['code']}"
        exists = conn.execute(
            "SELECT id FROM t2_alerts WHERE feature_id=? AND dedupe_key=? "
            "AND resolved_at IS NULL AND created_at>=? ORDER BY id DESC LIMIT 1",
            (FEATURE, key, now - 86400),
        ).fetchone()
        if exists:
            continue
        conn.execute(
            "INSERT INTO t2_alerts (feature_id, level, dedupe_key, title, detail_json, created_at)"
            " VALUES (?,?,?,?,?,?)",
            (FEATURE, "block", key, f"对账 {i['code']}", json.dumps(i, ensure_ascii=False), now),
        )
        inserted += 1
    for row in conn.execute(
        "SELECT id, dedupe_key FROM t2_alerts WHERE feature_id=? AND resolved_at IS NULL",
        (FEATURE,),
    ).fetchall():
        if row["dedupe_key"] not in open_keys:
            conn.execute("UPDATE t2_alerts SET resolved_at=? WHERE id=?", (now, int(row["id"])))
            resolved += 1
    conn.commit()
    return {"block": len(blocks), "inserted": inserted, "resolved": resolved}


def write_health(conn: sqlite3.Connection, db: str) -> Dict[str, Any]:
    """把交易段健康度写成 JSON，供前端/巡检读取（不写库）。"""
    c = al.canonical(conn)
    blocks = [i["code"] for i in al.reconcile(conn) if i.get("severity") == "block"]
    last_tick = conn.execute("SELECT MAX(ts) FROM market_ticks").fetchone()[0]
    alerts_open = conn.execute(
        "SELECT COUNT(*) FROM t2_alerts WHERE resolved_at IS NULL").fetchone()[0]
    payload = {
        "ts": int(time.time()),
        "db": db,
        "cash": round(float(c["cash"]), 4),
        "equity": round(float(c["equity"]), 4),
        "realized_pnl": round(float(c["realized"]), 4),
        "unrealized": round(float(c["unrealized"]), 4),
        "gross_exposure": round(float(c["gross_exposure"]), 2),
        "leverage_pct": round(float(c["leverage_pct"]), 2),
        "n_open": int(c["n_open"]),
        "blocks": blocks,
        "alerts_open": int(alerts_open or 0),
        "last_tick_age_s": int(time.time() - int(last_tick)) if last_tick else None,
    }
    path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                        "runtime", "trading_health.json")
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False)
        os.replace(tmp, path)
    except Exception as exc:
        payload["write_error"] = str(exc)[:120]
    return payload


def run_once(db: str, reconcile_every: int, tick: int,
             snapshot_every: int = 60) -> Dict[str, Any]:
    conn = sqlite3.connect(db, timeout=20.0)
    conn.row_factory = sqlite3.Row
    out: Dict[str, Any] = {"tick": tick, "closed": [], "alerts": None,
                           "snapshot": None, "error": None}
    try:
        out["closed"] = stops.enforce(conn)
        if snapshot_every > 0 and tick % snapshot_every == 0:
            try:
                out["snapshot"] = al.snapshot(conn, apply=True)
            except Exception as exc:
                out["snapshot"] = {"error": str(exc)[:120]}
        if reconcile_every > 0 and tick % reconcile_every == 0:
            out["alerts"] = sync_alerts(conn, al.reconcile(conn))
            write_health(conn, db)
    except Exception as exc:
        out["error"] = f"{type(exc).__name__}: {str(exc)[:200]}"
    finally:
        conn.close()
    return out


def _daemon_lock(db: str):
    path = os.getenv("STOPS_GUARD_LOCK_FILE", "").strip() or (db + ".stopsguard.lock")
    fh = open(path, "a+")
    try:
        fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except (BlockingIOError, OSError):
        fh.close()
        return None
    return fh


def _stop(signum, frame):
    global RUNNING
    RUNNING = False
    _log(f"收到信号 {signum}，本轮结束后退出")


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="止损独立守护 + 对账告警")
    ap.add_argument("--db", default=os.getenv("TRIDENT_DB_PATH", ""))
    ap.add_argument("--interval", type=float, default=30.0)
    ap.add_argument("--reconcile-every", type=int, default=5,
                    help="每 N 轮做一次账户对账与健康度快照，0=不做")
    ap.add_argument("--snapshot-every", type=int, default=60,
                    help="每 N 轮写一条权益快照（30s 轮询下 60 轮=30 分钟），0=不写")
    ap.add_argument("--once", action="store_true")
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args(argv)

    db = a.db.strip()
    if not db:
        # 与引擎同口径：backend/trident_event_bus.db
        here = os.path.dirname(os.path.abspath(__file__))
        db = os.path.join(os.path.dirname(here), "trident_event_bus.db")
    if not os.path.exists(db):
        print(f"库不存在：{db}", file=sys.stderr)
        return 2

    if a.once:
        out = run_once(db, 1, tick=1, snapshot_every=1)
        print(json.dumps(out, ensure_ascii=False, default=str) if a.json
              else f"平仓 {len(out['closed'])} 笔 · 告警 {out['alerts']} · error={out['error']}")
        return 0 if not out["error"] else 1

    lock = _daemon_lock(db)
    if lock is None:
        print("已有守护进程在跑，退出", file=sys.stderr)
        return 0
    signal.signal(signal.SIGTERM, _stop)
    signal.signal(signal.SIGINT, _stop)
    _log(f"启动 db={db} interval={a.interval:g}s reconcile_every={a.reconcile_every}")

    tick = 0
    while RUNNING:
        tick += 1
        out = run_once(db, a.reconcile_every, tick, a.snapshot_every)
        if out["closed"]:
            for c in out["closed"]:
                _log(f"止损平仓 {c['asset']}#{c['position_id']} {c['side']} "
                     f"exit={c['exit']} pnl={c['realized_pnl']}")
        if out["error"]:
            _log(f"本轮异常（继续）：{out['error']}")
        if out["alerts"] and (out["alerts"]["inserted"] or out["alerts"]["resolved"]):
            _log(f"对账告警 block={out['alerts']['block']} "
                 f"新增={out['alerts']['inserted']} 恢复={out['alerts']['resolved']}")
        if tick % 10 == 0:
            _log("心跳")
        for _ in range(int(max(1, a.interval))):
            if not RUNNING:
                break
            time.sleep(1)
    _log("已退出")
    return 0


if __name__ == "__main__":
    sys.exit(main())
