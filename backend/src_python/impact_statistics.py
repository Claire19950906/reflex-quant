# -*- coding: utf-8 -*-
"""impact_statistics.py — 影响力统计作业 (需求 #12 / G04)。

背景
----
event_impact_stat 表结构在, 但**从来没有作业去写**(实测 0 行), 于是"影响力
到底有多大"这件事没有任何统计口径: 前端看不到日维度的影响汇总, 也没人知道
曲线回填率是多少。

粒度: (日期, 资产, 观察窗口) —— 5m/30m/2h/1d 四个窗口分别统计:
  · samples              该格子的曲线点数
  · real_filled          其中有多少点拿到了真实价格 (price not null)
  · avg_impact_pct       平均带符号涨跌幅 (相对事件时刻基准价)
  · avg_abs_impact_pct   平均绝对涨跌幅 (影响力强度)
  · direction_hit_rate   实际方向与事件预判方向一致的占比 (事件的 direction 字段)
  · avg_window_minutes   窗口分钟数 (5/30/120/1440)
"""
from __future__ import annotations

import os
import sqlite3
import time
from typing import Any, Dict, List, Optional

WINDOW_MINUTES = {"5m": 5, "20m": 20, "30m": 30, "2h": 120, "4h": 240, "1d": 1440}

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS event_impact_stat (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    date_local          TEXT    NOT NULL,
    date_epoch_start    INTEGER NOT NULL,
    asset               TEXT    NOT NULL DEFAULT '',
    window_label        TEXT    NOT NULL DEFAULT '',
    impact_curve_filled INTEGER NOT NULL DEFAULT 0,
    event_atoms_total   INTEGER NOT NULL DEFAULT 0,
    fallback_used       INTEGER NOT NULL DEFAULT 0,
    avg_window_minutes  REAL    NOT NULL DEFAULT 0,
    samples             INTEGER NOT NULL DEFAULT 0,
    real_filled         INTEGER NOT NULL DEFAULT 0,
    avg_impact_pct      REAL    NOT NULL DEFAULT 0,
    avg_abs_impact_pct  REAL    NOT NULL DEFAULT 0,
    direction_hit_rate  REAL    NOT NULL DEFAULT 0,
    clock_jumps         INTEGER NOT NULL DEFAULT 0,
    created_at          INTEGER NOT NULL,
    UNIQUE (date_local, asset, window_label)
);
CREATE INDEX IF NOT EXISTS idx_event_impact_stat_local ON event_impact_stat(date_local);
"""

# 老表结构 (UNIQUE(date_local)) 需要迁移成 (date_local, asset, window_label)
OLD_TABLE_SQL = """
CREATE TABLE event_impact_stat (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    date_local          TEXT    NOT NULL,
    date_epoch_start    INTEGER NOT NULL,
    impact_curve_filled INTEGER NOT NULL DEFAULT 0,
    event_atoms_total   INTEGER NOT NULL DEFAULT 0,
    fallback_used       INTEGER NOT NULL DEFAULT 0,
    avg_window_minutes  REAL    NOT NULL DEFAULT 0,
    clock_jumps         INTEGER NOT NULL DEFAULT 0,
    created_at          INTEGER NOT NULL,
    UNIQUE (date_local)
);
"""


def _columns(conn: sqlite3.Connection, table: str) -> List[str]:
    try:
        return [r[1] for r in conn.execute("PRAGMA table_info(%s)" % table).fetchall()]
    except sqlite3.Error:
        return []


def ensure_schema(conn: sqlite3.Connection) -> bool:
    """确保 event_impact_stat 是新粒度。返回 True 表示做过重建迁移。"""
    cols = _columns(conn, "event_impact_stat")
    if not cols:
        conn.executescript(SCHEMA_SQL)
        conn.commit()
        return False
    if "asset" in cols and "window_label" in cols:
        conn.executescript(SCHEMA_SQL)
        conn.commit()
        return False
    # 老表 → 新表 (保留旧数据, 资产/窗口留空表示"当日汇总")
    conn.execute("ALTER TABLE event_impact_stat RENAME TO event_impact_stat_old")
    conn.executescript(SCHEMA_SQL)
    keep = ["date_local", "date_epoch_start", "impact_curve_filled", "event_atoms_total",
            "fallback_used", "avg_window_minutes", "clock_jumps", "created_at"]
    conn.execute(
        "INSERT OR IGNORE INTO event_impact_stat (%s) SELECT %s FROM event_impact_stat_old"
        % (", ".join(keep), ", ".join(keep))
    )
    conn.execute("DROP TABLE event_impact_stat_old")
    conn.commit()
    return True



def _asset_codes(conn: sqlite3.Connection) -> Dict[int, str]:
    out: Dict[int, str] = {}
    try:
        for r in conn.execute("SELECT id, code FROM dim_asset").fetchall():
            if r[1]:
                out[int(r[0])] = str(r[1]).upper()
    except sqlite3.Error:
        pass
    return out


def compute_and_store(conn: sqlite3.Connection, days: int = 120) -> Dict[str, Any]:
    """按 (日期, 资产, 窗口) 聚合影响力统计并写库。"""
    ensure_schema(conn)
    codes = _asset_codes(conn)
    cutoff = int(time.time()) - days * 86400
    rows = conn.execute(
        """SELECT c.id, c.asset_id, c.window_label, c.sample_ts, c.price, c.delta_pct,
                  c.volume_ratio, e.event_time, e.direction, e.fallback_used, e.raw_news_id
           FROM event_impact_curve c
           LEFT JOIN event_atom e ON e.id = c.event_atom_id
           WHERE e.event_time IS NOT NULL AND e.event_time >= ?""",
        (cutoff,),
    ).fetchall()

    buckets: Dict[Any, Dict[str, Any]] = {}
    for r in rows:
        ev_ts = int(r["event_time"] or 0)
        date_local = time.strftime("%Y-%m-%d", time.gmtime(ev_ts))
        asset = codes.get(int(r["asset_id"] or 0), "")
        window = str(r["window_label"] or "")
        key = (date_local, asset, window)
        b = buckets.setdefault(key, {
            "samples": 0, "real_filled": 0, "sum_delta": 0.0, "sum_abs": 0.0,
            "atoms": set(), "fallback": 0, "hits": 0, "hits_n": 0,
            "epoch": ev_ts - (ev_ts % 86400),
        })
        b["samples"] += 1
        b["atoms"].add(int(r["id"]))
        if r["fallback_used"]:
            b["fallback"] += 1
        delta = r["delta_pct"]
        if delta is not None and r["price"] is not None:
            b["real_filled"] += 1
            d = float(delta)
            b["sum_delta"] += d
            b["sum_abs"] += abs(d)
            direction = str(r["direction"] or "").lower()
            if direction in ("bullish", "bearish"):
                b["hits_n"] += 1
                if (direction == "bullish" and d > 0) or (direction == "bearish" and d < 0):
                    b["hits"] += 1

    written = 0
    now = int(time.time())
    for (date_local, asset, window), b in buckets.items():
        n_real = max(1, b["real_filled"])
        conn.execute(
            """INSERT INTO event_impact_stat
                 (date_local, date_epoch_start, asset, window_label,
                  impact_curve_filled, event_atoms_total, fallback_used,
                  avg_window_minutes, samples, real_filled,
                  avg_impact_pct, avg_abs_impact_pct, direction_hit_rate,
                  clock_jumps, created_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,0,?)
               ON CONFLICT(date_local, asset, window_label) DO UPDATE SET
                  date_epoch_start=excluded.date_epoch_start,
                  impact_curve_filled=excluded.impact_curve_filled,
                  event_atoms_total=excluded.event_atoms_total,
                  fallback_used=excluded.fallback_used,
                  avg_window_minutes=excluded.avg_window_minutes,
                  samples=excluded.samples,
                  real_filled=excluded.real_filled,
                  avg_impact_pct=excluded.avg_impact_pct,
                  avg_abs_impact_pct=excluded.avg_abs_impact_pct,
                  direction_hit_rate=excluded.direction_hit_rate,
                  created_at=excluded.created_at""",
            (date_local, b["epoch"], asset, window,
             b["real_filled"], len(b["atoms"]), b["fallback"],
             float(WINDOW_MINUTES.get(window, 0)), b["samples"], b["real_filled"],
             round(b["sum_delta"] / n_real, 4), round(b["sum_abs"] / n_real, 4),
             round(b["hits"] / b["hits_n"], 4) if b["hits_n"] else 0.0,
             now),
        )
        written += 1
    conn.commit()
    total = conn.execute("SELECT COUNT(*) FROM event_impact_stat").fetchone()[0]
    return {"buckets": len(buckets), "written": written, "table_rows": total}


def daily_job(conn: Optional[sqlite3.Connection] = None) -> Dict[str, Any]:
    """统计作业入口 (cron 调用)。"""
    own = conn is None
    if own:
        conn = sqlite3.connect(
            os.environ.get("TRIDENT_DB_PATH")
            or os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                            "trident_event_bus.db"),
            timeout=120,
        )
        conn.row_factory = sqlite3.Row
    try:
        conn.execute("PRAGMA journal_mode=WAL")
        res = compute_and_store(conn, days=120)
        return res
    finally:
        if own:
            conn.close()


if __name__ == "__main__":
    import json
    print(json.dumps(daily_job(), ensure_ascii=False, indent=2))
