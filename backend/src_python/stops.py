# -*- coding: utf-8 -*-
"""开仓止损执行器（v463）。

`trade_executor` 开仓时会写 `t2_positions.stop_price`，但系统此前**没有任何代码
在价格触及时平仓**——止损字段只是展示。本模块补上这一步：

  - `enforce(conn)`：每轮扫描 open 持仓，取 `market_ticks` 最新价；
    多头 mark <= stop / 空头 mark >= stop 时调用 `store.close_position`，
    写 `realized_pnl`、更新 `t2_account`，并记一条 `t2_interventions`。
  - `backfill(conn, pct)`：给存量无止损持仓补挂“入场价 ± pct%”的硬止损（人工执行一次）。

只做纸面平仓，不碰真实交易所；真实模式要接交易所止损单，本模块不冒充。
"""
from __future__ import annotations

import fcntl
import hashlib
import os
import sqlite3
import tempfile
import time
from typing import Any, Dict, List, Optional

from trident2 import store as t2store


def _db_path(conn: sqlite3.Connection) -> str:
    try:
        for row in conn.execute("PRAGMA database_list"):
            if row[1] == "main":
                return str(row[2] or "")
    except Exception:
        pass
    return ""


def _lock_file(conn: sqlite3.Connection) -> Optional[str]:
    explicit = os.getenv("STOPS_LOCK_FILE", "").strip()
    if explicit:
        return explicit
    path = _db_path(conn)
    if not path:
        return None  # 内存库：单进程内没有并发平仓问题
    h = hashlib.sha1(path.encode("utf-8")).hexdigest()[:10]
    return os.path.join(tempfile.gettempdir(), f"trident_stops_{h}.lock")


class _EnforceLock:
    """跨进程互斥：引擎 tick 与独立止损守护不能同时平同一笔仓。

    非阻塞；拿不到说明另一进程正在扫，直接跳过本轮（下一轮 5~30s 后就接上）。
    """

    def __init__(self, conn: sqlite3.Connection) -> None:
        self.path = _lock_file(conn)
        self.fh = None

    def __enter__(self) -> bool:
        if not self.path:
            return True
        try:
            self.fh = open(self.path, "a+")
            fcntl.flock(self.fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            return True
        except (BlockingIOError, OSError):
            if self.fh:
                self.fh.close()
                self.fh = None
            return False

    def __exit__(self, *exc) -> None:
        if self.fh:
            try:
                fcntl.flock(self.fh.fileno(), fcntl.LOCK_UN)
            finally:
                self.fh.close()
                self.fh = None


def _latest_price(conn: sqlite3.Connection, asset: str) -> Optional[float]:
    row = conn.execute(
        "SELECT price FROM market_ticks WHERE symbol=? ORDER BY ts DESC, rowid DESC LIMIT 1",
        (str(asset).upper(),),
    ).fetchone()
    if row and row[0]:
        return float(row[0])
    for tf in ("1m", "5m"):
        row = conn.execute(
            "SELECT close FROM kline_bars WHERE symbol=? AND timeframe=? ORDER BY ts DESC, rowid DESC LIMIT 1",
            (str(asset).upper(), tf),
        ).fetchone()
        if row and row[0]:
            return float(row[0])
    return None


def _breached(side: str, mark: float, stop: float) -> bool:
    return mark <= stop if str(side).lower() in ("long", "buy") else mark >= stop


def enforce(conn: sqlite3.Connection) -> List[Dict[str, Any]]:
    with _EnforceLock(conn) as got:
        if not got:
            return []
        return _enforce_locked(conn)


def _best_price(conn: sqlite3.Connection, asset: str, since_ts: int, mode: str) -> Optional[float]:
    """开仓以来的最优价（多头取最高、空头取最低），用于移动止损。"""
    rows = conn.execute(
        "SELECT price FROM market_ticks WHERE symbol=? AND ts>=? AND price>0",
        (asset, int(since_ts or 0)),
    ).fetchall()
    prices = [float(r[0]) for r in rows if r[0]]
    mark = _latest_price(conn, asset)
    if mark:
        prices.append(float(mark))
    if not prices:
        return None
    return max(prices) if mode == "max" else min(prices)


def _enforce_locked(conn: sqlite3.Connection) -> List[Dict[str, Any]]:
    rows = conn.execute(
        "SELECT id, asset, side, qty, entry_price, entry_ts, stop_price, trailing_bp, time_stop_min "
        "FROM t2_positions WHERE status='open' ORDER BY id"
    ).fetchall()
    now = int(time.time())
    closed: List[Dict[str, Any]] = []
    for r in rows:
        pid, asset, side = int(r[0]), r[1], str(r[2]).lower()
        qty, entry = float(r[3]), float(r[4])
        entry_ts = int(r[5] or 0)
        stop = float(r[6]) if r[6] not in (None, "") else None
        trail_bp = float(r[7] or 0.0)
        # v479：time_stop 字段是每仓手填的，实测 4 个存量持仓全是 0/NULL
        # → 时间止损整条规则形同不存在（XAU/BTC/WTI 已持有 220-296 分钟无人管）。
        # 这里补一层"系统默认最長持有"：字段没填就吃 STOPS_DEFAULT_TIME_STOP_MIN。
        tstop_min = int(r[8] or 0)
        if tstop_min <= 0:
            try:
                tstop_min = int(os.getenv("STOPS_DEFAULT_TIME_STOP_MIN", "0") or 0)
            except ValueError:
                tstop_min = 0
        if stop is None and trail_bp <= 0 and tstop_min <= 0:
            continue
        mark = _latest_price(conn, asset)
        if mark is None:
            continue

        trigger: Optional[str] = None
        eff_stop: Optional[float] = None
        note = ""

        if trail_bp > 0:
            best = _best_price(conn, asset, entry_ts, "max" if side in ("long", "buy") else "min")
            if best:
                if side in ("long", "buy"):
                    eff_stop = best * (1.0 - trail_bp / 10_000.0)
                    if stop is not None:
                        eff_stop = max(stop, eff_stop)
                else:
                    eff_stop = best * (1.0 + trail_bp / 10_000.0)
                    if stop is not None:
                        eff_stop = min(stop, eff_stop)

        if eff_stop is not None and _breached(side, mark, eff_stop):
            trigger = "trailing_stop"
            note = f"trailing_stop hit: {side} mark={mark} trail={eff_stop:.4f} bp={trail_bp:g}"
        elif stop is not None and _breached(side, mark, stop):
            trigger = "auto_stop"
            eff_stop = stop
            note = f"auto_stop hit: {side} mark={mark} stop={stop}"
        elif tstop_min > 0 and entry_ts and (now - entry_ts) >= tstop_min * 60:
            trigger = "time_stop"
            held = (now - entry_ts) / 60.0
            note = f"time_stop: held={held:.0f}min limit={tstop_min}min mark={mark}"

        if not trigger:
            continue
        try:
            res = t2store.close_position(conn, pid, exit_price=mark, note=note)
            try:
                t2store.insert_intervention(
                    conn, kind=trigger, target=f"{asset}#{pid}",
                    note=f"{side} {qty} entry={entry} exit={mark}",
                    before_json={"status": "open", "mark": mark,
                                 "stop": eff_stop, "trailing_bp": trail_bp},
                    after_json={"status": "closed", "realized_pnl": res.get("realized_pnl")},
                )
            except Exception:
                pass
            closed.append({"position_id": pid, "asset": asset, "side": side,
                           "qty": qty, "entry": entry, "stop": eff_stop, "exit": mark,
                           "trigger": trigger, "realized_pnl": res.get("realized_pnl")})
        except Exception:
            continue
    return closed


def backfill(conn: sqlite3.Connection, pct: Optional[float] = None) -> List[Dict[str, Any]]:
    """给没有 stop_price 的 open 持仓补挂止损；已有止损不动。"""
    pct = float(os.getenv("AUTO_STOP_PCT", "2.0")) if pct is None else float(pct)
    rows = conn.execute(
        "SELECT id, asset, side, entry_price FROM t2_positions "
        "WHERE status='open' AND stop_price IS NULL ORDER BY id"
    ).fetchall()
    out: List[Dict[str, Any]] = []
    for pid, asset, side, entry in rows:
        entry = float(entry or 0)
        if entry <= 0:
            continue
        stop = round(entry * (1 + pct / 100.0), 6) if str(side).lower() in ("short", "sell") \
            else round(entry * (1 - pct / 100.0), 6)
        conn.execute("UPDATE t2_positions SET stop_price=? WHERE id=? AND stop_price IS NULL",
                     (stop, int(pid)))
        out.append({"position_id": int(pid), "asset": asset, "side": side,
                    "entry": entry, "stop": stop})
    conn.commit()
    return out


def _main() -> int:
    import argparse
    from pathlib import Path
    ap = argparse.ArgumentParser(description="开仓止损执行 / 存量补挂")
    ap.add_argument("--db", default=str(Path(__file__).resolve().parents[1] / "trident_event_bus.db"))
    ap.add_argument("--backfill", action="store_true", help="给存量无止损持仓补挂止损")
    ap.add_argument("--pct", type=float, default=None)
    ap.add_argument("--once", action="store_true", help="只扫一轮并平掉已触发的持仓")
    a = ap.parse_args()
    conn = sqlite3.connect(a.db, timeout=10.0)
    try:
        if a.backfill:
            for x in backfill(conn, a.pct):
                print(f"补挂 {x['asset']}#{x['position_id']} {x['side']} "
                      f"entry={x['entry']} stop={x['stop']}")
        closed = enforce(conn)
        for x in closed:
            print(f"止损平仓 {x['asset']}#{x['position_id']} {x['side']} "
                  f"exit={x['exit']} pnl={x['realized_pnl']}")
        print(f"本轮平仓 {len(closed)} 笔")
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
