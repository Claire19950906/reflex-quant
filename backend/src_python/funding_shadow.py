#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""资金费率影子账本（v425）：只向前记账，不回头补历史。

背景
----
v424 的检验结论：ETH「极端高资金费率 → 做空 1 天」扣费后 +1.616%/笔、t=3.00、
时机置换 p=0.006，但只有 18 笔 —— 样本不够，不能上实盘。

唯一能把它变成结论的办法：从现在开始记。所以这个任务做三件很小的事：
  1. 第一次运行只写一个 `activated_at`（= 此刻最新的费率点），**不把停机期/历史极端点补进来**；
  2. 之后每次运行，只在 activated_at 之后出现极端费率时落一条 pending 记录；
  3. 满 1 天后自动结算（用真实 K 线 + cost_model 的统一成本）。

幂等：`UNIQUE(asset, signal_ts)` + INSERT OR IGNORE，重复跑不会写重。
只读判断 + 只写本表，不碰决策链、不碰生产交易。

用法
----
  python3 funding_shadow.py --once        # 跑一次：收信号 + 结算到期
  python3 funding_shadow.py --status      # 只看账本现状
  python3 funding_shadow.py --dry-run     # 只打印将要做什么，不写库
"""
from __future__ import annotations

import argparse
import sqlite3
import sys
import time
from typing import Any, Dict, List, Optional, Sequence, Tuple

import cost_horizon_eval as ce
import db
import structural_edge as se

ASSETS = ("BTC", "ETH", "SOL")
HORIZON_S = 86_400          # 1 天
KLINE_TF = "4h"
KLINE_INTERVAL = 14_400
Q_HIGH = 0.8
MIN_HISTORY = 40

DDL = """
CREATE TABLE IF NOT EXISTS funding_shadow (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    asset TEXT NOT NULL,
    direction TEXT NOT NULL,
    signal_ts INTEGER NOT NULL,
    funding_value REAL,
    threshold REAL,
    entry_ts INTEGER,
    entry_price REAL,
    horizon_s INTEGER DEFAULT 86400,
    exit_ts INTEGER,
    exit_price REAL,
    gross_pct REAL,
    net_pct REAL,
    cost_bps REAL,
    hit INTEGER,
    settled INTEGER DEFAULT 0,
    created_at INTEGER,
    UNIQUE(asset, signal_ts)
);
"""
META_DDL = """
CREATE TABLE IF NOT EXISTS funding_shadow_meta (
    key TEXT PRIMARY KEY,
    value TEXT
);
"""


# ── 纯函数（单测直接打这些，不碰库）──────────────────────────────────────

def pick_signals(series: Sequence[Tuple[int, float]], activated_at: int,
                 q_high: float = Q_HIGH, min_history: int = MIN_HISTORY,
                 limit_side: str = "high") -> List[Dict[str, Any]]:
    """只返回"激活之后"的极端点。

    - 阈值用前 min_history 个点（含激活前的历史，那是校准，不是补记信号）；
    - 信号本身必须 ts >= activated_at，避免把停机期堆出来的极端点当成新机会。
    """
    out: List[Dict[str, Any]] = []
    vals: List[float] = []
    for i, (ts, v) in enumerate(series):
        if i < min_history:
            vals.append(v)
            continue
        hi = se._quantile(vals, q_high)
        lo = se._quantile(vals, 1.0 - q_high)
        if ts >= activated_at:
            if limit_side in ("high", "both") and v >= hi and hi - lo > 1e-12:
                out.append({"ts": ts, "value": v, "direction": "SELL",
                            "threshold": hi, "side": "high"})
            elif limit_side in ("low", "both") and v <= lo and hi - lo > 1e-12:
                out.append({"ts": ts, "value": v, "direction": "BUY",
                            "threshold": lo, "side": "low"})
        vals.append(v)
    return out


def settle_pnl(direction: str, entry: float, exit_: float,
               cost_bps: float) -> Optional[Dict[str, Any]]:
    """毛/净收益 + 是否命中。成本走 cost_model 的同一个数，不在这里自造。"""
    g = ce.gross_pct(direction, entry, exit_)
    if g is None:
        return None
    net = g - cost_bps / 100.0
    return {"gross": round(g, 4), "net": round(net, 4), "hit": 1 if net > 0 else 0}


# ── 库操作 ────────────────────────────────────────────────────────────────

def _conn() -> sqlite3.Connection:
    conn = db.get_connection()
    conn.execute(DDL)
    conn.execute(META_DDL)
    conn.commit()
    return conn


def _get_meta(conn: sqlite3.Connection, key: str) -> Optional[str]:
    row = conn.execute("SELECT value FROM funding_shadow_meta WHERE key=?", (key,)).fetchone()
    return row[0] if row else None


def _set_meta(conn: sqlite3.Connection, key: str, value: str) -> None:
    conn.execute(
        "INSERT INTO funding_shadow_meta(key, value) VALUES(?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, value),
    )
    conn.commit()


def _latest_funding_ts(conn: sqlite3.Connection) -> int:
    row = conn.execute(
        "SELECT MAX(ts) FROM flow_snapshots WHERE kind='funding' AND asset IN (%s)"
        % ",".join("?" * len(ASSETS)), ASSETS,
    ).fetchone()
    return int(row[0] or 0)


def _bars(conn: sqlite3.Connection, asset: str) -> List[Tuple[int, float]]:
    return ce.load_bars(conn, asset, KLINE_TF, int(time.time()) - 120 * 86_400)


def run_once(dry_run: bool = False) -> Dict[str, Any]:
    conn = _conn()
    try:
        activated = _get_meta(conn, "activated_at")
        if activated is None:
            # 首次运行：只定起点，不补历史（用户口径：中断后以恢复时刻为新起点）
            # 起点 = 此刻（而不是"库里最新那条费率"）：用户口径是"中断后以恢复时刻为新起点"，
            # 这样停机期间堆出来的、恢复后才入库的费率点一律不补记。
            start = int(time.time())
            if not dry_run:
                _set_meta(conn, "activated_at", str(start))
            return {"first_run": True, "activated_at": start, "created": 0, "settled": 0,
                    "note": "首次运行只记录起点；历史极端点不补记，从下一个费率点开始。"}

        act_ts = int(activated)
        created = 0
        for asset in ASSETS:
            series = se.load_series(conn, "funding", asset, act_ts - 40 * 8 * 3_600)
            sigs = pick_signals(series, act_ts)
            if not sigs:
                continue
            bars = _bars(conn, asset)
            for s in sigs:
                entry_ts = s["ts"]
                entry = ce.exit_at(bars, entry_ts, KLINE_INTERVAL)
                if entry is None:
                    continue
                if dry_run:
                    created += 1
                    continue
                cur = conn.execute(
                    """INSERT OR IGNORE INTO funding_shadow
                       (asset, direction, signal_ts, funding_value, threshold,
                        entry_ts, entry_price, horizon_s, cost_bps, created_at)
                       VALUES (?,?,?,?,?,?,?,?,?,?)""",
                    (asset, s["direction"], s["ts"], s["value"], s["threshold"],
                     entry_ts, entry, HORIZON_S, se.asset_cost_bps(asset), int(time.time())),
                )
                created += cur.rowcount
        if not dry_run:
            conn.commit()

        settled = _settle(conn, dry_run)
        return {"first_run": False, "activated_at": act_ts, "created": created,
                "settled": settled.get("settled", 0), "settle_skipped": settled.get("skipped", 0)}
    finally:
        conn.close()


def _settle(conn: sqlite3.Connection, dry_run: bool) -> Dict[str, int]:
    rows = conn.execute(
        "SELECT id, asset, direction, entry_ts, entry_price, horizon_s, cost_bps "
        "FROM funding_shadow WHERE settled=0"
    ).fetchall()
    done = skipped = 0
    for rid, asset, direction, entry_ts, entry, horizon, cost_bps in rows:
        bars = _bars(conn, asset)
        target = int(entry_ts) + int(horizon or HORIZON_S)
        px = ce.exit_at(bars, target, KLINE_INTERVAL)
        if px is None:
            skipped += 1
            continue
        st = settle_pnl(direction, float(entry), px, float(cost_bps or 30.0))
        if st is None:
            skipped += 1
            continue
        if dry_run:
            done += 1
            continue
        conn.execute(
            """UPDATE funding_shadow SET exit_ts=?, exit_price=?, gross_pct=?, net_pct=?,
               hit=?, settled=1 WHERE id=?""",
            (target, px, st["gross"], st["net"], st["hit"], rid),
        )
        done += 1
    if not dry_run:
        conn.commit()
    return {"settled": done, "skipped": skipped}


def status() -> Dict[str, Any]:
    conn = _conn()
    try:
        act = _get_meta(conn, "activated_at")
        total = conn.execute("SELECT COUNT(*) FROM funding_shadow").fetchone()[0]
        settled = conn.execute("SELECT COUNT(*) FROM funding_shadow WHERE settled=1").fetchone()[0]
        hits = conn.execute("SELECT COALESCE(SUM(hit),0) FROM funding_shadow WHERE settled=1").fetchone()[0]
        avg = conn.execute("SELECT AVG(net_pct) FROM funding_shadow WHERE settled=1").fetchone()[0]
        return {"activated_at": int(act) if act else None, "total": total,
                "settled": settled, "hits": hits,
                "avg_net_pct": round(avg, 4) if avg is not None else None,
                "need_samples": max(0, 30 - settled)}
    finally:
        conn.close()


def main(argv: Optional[Sequence[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="资金费率影子账本（只向前记账）")
    ap.add_argument("--once", action="store_true", help="跑一次：收信号 + 结算到期")
    ap.add_argument("--status", action="store_true", help="只看账本")
    ap.add_argument("--dry-run", action="store_true", help="不写库，只打印")
    a = ap.parse_args(argv)
    if a.status:
        print(status())
        return 0
    print(run_once(dry_run=a.dry_run))
    return 0


if __name__ == "__main__":
    sys.exit(main())
