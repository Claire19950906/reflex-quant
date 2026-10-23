#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""v201 #2: 事件 -> K线影响 (Event->Kline Impact) worker。

设计目标
--------
把宏观/重大新闻事件 (news_events + raw_news) 与对应资产后续 4h K线 (market_ticks) 关联,
计算该事件引发的实际价格波动幅度。供前端 MacroKlineLink 组件展示"这条新闻带来多大行情"。

数据源
------
- news_events: id, news_id, ts, source, asset, score, status
- raw_news:    title (content 前 80 字), gate_strength (1-5 重要性代理), gate_main_asset
- market_ticks: symbol, ts, price (每分钟)

输出 event_kline_impact (v201 schema in db.py):
  pre_event_close, max_high, min_low, impact_pct, direction, impact_severity

使用
----
    python3 src_python/engine/event_kline_impact_worker.py --once
    python3 src_python/engine/event_kline_impact_worker.py --daemon
"""
from __future__ import annotations

import argparse
import sqlite3
import time
from typing import List, Optional, Tuple

import db

_WINDOW_BEFORE_SEC = 60
_WINDOW_AFTER_SEC = 4 * 3600
_SEVERITY_THRESHOLDS = (0.003, 0.01)   # 0.3% / 1% → medium / high
_DAEMON_INTERVAL_SEC = 15 * 60
_LOOKBACK_DAYS = 7


def _classify_severity(impact_pct: float) -> str:
    abs_pct = abs(impact_pct)
    if abs_pct >= _SEVERITY_THRESHOLDS[1]:
        return "high"
    if abs_pct >= _SEVERITY_THRESHOLDS[0]:
        return "medium"
    return "low"


def _classify_direction(pre: float, hi: float, lo: float) -> str:
    if pre <= 0:
        return "neutral"
    up_move = (hi - pre) / pre
    dn_move = (pre - lo) / pre
    if up_move > dn_move + 0.001:
        return "up"
    if dn_move > up_move + 0.001:
        return "down"
    return "neutral"


def _fetch_pending_events(conn: sqlite3.Connection,
                          lookback_days: int) -> List[sqlite3.Row]:
    """未入库 event_kline_impact 的事件（UNIQUE=source+id+asset）。"""
    cutoff = int(time.time()) - lookback_days * 86400
    return conn.execute(
        """SELECT ne.id AS event_id,
                  ne.source AS event_source,
                  ne.news_id,
                  ne.ts AS event_time,
                  ne.asset,
                  ne.score,
                  ne.status,
                  rn.content AS title,
                  COALESCE(rn.gate_strength, 0) AS importance_score
             FROM news_events ne
             LEFT JOIN raw_news rn ON rn.id = ne.news_id
        LEFT JOIN event_kline_impact eki
               ON eki.event_source = ne.source
              AND eki.event_id = CAST(ne.id AS TEXT)
              AND eki.asset = ne.asset
            WHERE ne.status IN ('PENDING', 'DONE')
              AND ne.ts >= ?
              AND ne.asset != 'NONE'
              AND eki.id IS NULL
            ORDER BY ne.ts DESC
            LIMIT 500""",
        (cutoff,),
    ).fetchall()
def _fetch_window_ticks(conn: sqlite3.Connection,
                         symbol: str,
                         t_start: int,
                         t_end: int) -> Tuple[List[Tuple[int, float]], float]:
    """返回 ([(ts, price), ...], pre_event_close)."""
    pre = conn.execute(
        """SELECT price FROM market_ticks
            WHERE symbol = ? AND ts < ?
            ORDER BY ts DESC LIMIT 1""",
        (symbol, t_start),
    ).fetchone()
    pre_close = float(pre["price"]) if pre else 0.0

    rows = conn.execute(
        """SELECT ts, price FROM market_ticks
            WHERE symbol = ? AND ts >= ? AND ts <= ?
            ORDER BY ts ASC""",
        (symbol, t_start, t_end),
    ).fetchall()
    return [(int(r["ts"]), float(r["price"])) for r in rows], pre_close


def process_one_event(conn: sqlite3.Connection,
                      row: sqlite3.Row) -> Optional[int]:
    """处理单个事件 -> 写 event_kline_impact。返回新插入 id；无足够 ticks 返回 None。"""
    symbol = (row["asset"] or "").upper()
    if not symbol:
        return None
    event_time = int(row["event_time"] or 0)
    if event_time <= 0:
        return None
    t_start = event_time - _WINDOW_BEFORE_SEC
    t_end = event_time + _WINDOW_AFTER_SEC

    ticks, pre_close = _fetch_window_ticks(conn, symbol, t_start, t_end)
    if not ticks or pre_close <= 0:
        return None

    prices = [p for _, p in ticks]
    max_high = max(prices)
    min_low = min(prices)
    up_move = (max_high - pre_close) / pre_close
    dn_move = (pre_close - min_low) / pre_close
    impact_pct = max(up_move, dn_move)
    direction = _classify_direction(pre_close, max_high, min_low)
    severity = _classify_severity(impact_pct)

    raw_imp = int(row["importance_score"] or 0)
    if raw_imp >= 4:
        importance = "high"
    elif raw_imp >= 2:
        importance = "medium"
    else:
        importance = "low"

    title = str(row["title"] or "")[:80]

    cur = conn.execute(
        """INSERT OR IGNORE INTO event_kline_impact
           (event_source, event_id, event_title, event_time, importance,
            asset, pre_event_close, max_high, min_low, impact_pct,
            impact_window_start, impact_window_end,
            impact_severity, direction)
def run_once(lookback_days: int = _LOOKBACK_DAYS,
             log: bool = True) -> dict:
    # docstring in English only to avoid SyntaxError on legacy encoders
    conn = db.get_connection()
    conn.row_factory = sqlite3.Row
    try:
        rows = _fetch_pending_events(conn, lookback_days)
        seen = len(rows)
        written = 0
        skipped = 0
        for row in rows:
            try:
                rid = process_one_event(conn, row)
                if rid:
                    written += 1
                else:
                    skipped += 1
            except Exception as exc:
                skipped += 1
                if log:
                    print(
                        f"[KLINE-IMPACT] event_id={row['event_id']} "
                        f"asset={row['asset']} failed: "
                        f"{type(exc).__name__}: {exc}", flush=True,
                    )
        if log:
            print(
                f"[KLINE-IMPACT] run_once: seen={seen} "
                f"written={written} skipped={skipped}",
                flush=True,
            )
        return {"events_seen": seen, "written": written, "skipped": skipped}
    finally:
        conn.close()


def run_daemon(interval_sec: int = _DAEMON_INTERVAL_SEC) -> None:
    print(f"[KLINE-IMPACT] daemon 启动: interval={interval_sec}s", flush=True)
    while True:
        try:
            run_once()
        except Exception as exc:
            print(
                f"[KLINE-IMPACT] daemon 失败: {type(exc).__name__}: {exc}",
                flush=True,
            )
        time.sleep(int(interval_sec))


def main() -> None:
    p = argparse.ArgumentParser(description="v201 #2 event_kline_impact worker")
    p.add_argument("--daemon", action="store_true", help="15min 循环")
    p.add_argument("--once", action="store_true", help="立即跑一次（默认）")
    p.add_argument("--lookback-days", type=int, default=_LOOKBACK_DAYS)
    p.add_argument("--interval-sec", type=int, default=_DAEMON_INTERVAL_SEC)
    args = p.parse_args()
    if args.daemon:
        run_daemon(interval_sec=args.interval_sec)
    else:
        run_once(lookback_days=args.lookback_days)


if __name__ == "__main__":
    main()
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (
            str(row["event_source"] or "")[:32],
            str(int(row["event_id"])),
            title,
            event_time,
            importance,
            symbol,
            pre_close,
            max_high,
            min_low,
            round(impact_pct, 6),
            t_start,
            t_end,
            severity,
            direction,
        ),
    )
    conn.commit()
    return int(cur.lastrowid or 0) if cur.rowcount > 0 else None