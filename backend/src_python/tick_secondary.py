# -*- coding: utf-8 -*-
"""二路行情源补录 + 源间背离检测（v479）。

背景（实测）：
  · `market_ticks` 里 XAU / WTI 的 tick **只有 sina 一个来源**（14 万 / 15 万行），
    也就是说这两个品种在界面上根本没有"交叉验证"，只有一个源在说话。
  · 但库里的 `external_indicators` 明明存着金十的报价序列
    （`asset in (XAU,WTI,XAG)`、`indicator='jq_price'`），从来没被写进 tick 表。
  · 更值得警惕：金十最近一条 XAU 报 3873.47，而 sina tick 报 3733 左右，
    两者差 ~3.7%。这既可能是品种口径不同（现货 vs 期货），也可能是其中一个源错。

本脚本做两件事，都不猜、不补数：
  1) **补二路源**：把 `jq_price` 新鲜（默认 15 分钟内）的报价写入 `market_ticks`，
     `source='jin10'`。这样 ≥2 个独立源时才真的 ≥2 个。过期数据不写入（宁缺勿假）。
  2) **背离检测**：同一品种近 30 分钟内若存在 ≥2 个独立源且价差 > 阈值（默认 1%），
     写一行 `source_divergence` 审计记录，并在 stdout 打印 —— 让"源打架"这件事可见，
     而不是用中位数把它抹平。
"""
from __future__ import annotations

import os
import sqlite3
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Tuple

BACKEND = Path(__file__).resolve().parents[1]
DB_PATH = os.environ.get("TRIDENT_DB_PATH") or str(BACKEND / "trident_event_bus.db")

FRESH_S = int(os.getenv("TICK_SECONDARY_FRESH_S", "900"))       # 15 分钟内算新鲜
DIVERGE_PCT = float(os.getenv("TICK_SOURCE_DIVERGE_PCT", "1.0"))  # 源间价差阈值 %
ASSETS = ("XAU", "WTI", "XAG", "BTC", "ETH", "SOL")

DERIVED = ("median", "snapshot", "snapshot-sync", "agg", "derived")


def _init_divergence_table(conn: sqlite3.Connection) -> None:
    conn.execute("""
        CREATE TABLE IF NOT EXISTS source_divergence (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            symbol      TEXT NOT NULL,
            ts          INTEGER NOT NULL,
            source_a    TEXT NOT NULL,
            price_a     REAL NOT NULL,
            source_b    TEXT NOT NULL,
            price_b     REAL NOT NULL,
            diff_pct    REAL NOT NULL,
            note        TEXT,
            UNIQUE (symbol, source_a, source_b, ts)
        )""")


def ingest_secondary(conn: sqlite3.Connection, now: int) -> List[Dict[str, Any]]:
    """把金十 `jq_price` 的新鲜报价补进 tick 表（source='jin10'）。"""
    written: List[Dict[str, Any]] = []
    for asset in ASSETS:
        row = conn.execute(
            "SELECT value, ts FROM external_indicators "
            "WHERE asset=? AND indicator='jq_price' AND value>0 ORDER BY ts DESC LIMIT 1",
            (asset,),
        ).fetchone()
        if not row:
            continue
        price, ts = float(row[0]), int(row[1])
        age = now - ts
        if age > FRESH_S:
            print(f"[TICK-2SRC] {asset}: 金十报价已过期 {age}s (>{FRESH_S}s)，不写入（宁缺勿假）")
            continue
        try:
            cur = conn.execute(
                "INSERT INTO market_ticks (symbol, ts, price, change24h, source, source_count) "
                "VALUES (?,?,?,?,?,?) ON CONFLICT (symbol, ts, source) DO NOTHING",
                (asset, ts, price, 0.0, "jin10", 0),
            )
            if cur.rowcount:
                written.append({"asset": asset, "price": price, "ts": ts})
                print(f"[TICK-2SRC] {asset}: 补入 jin10 报价 {price} @ {ts} (age={age}s)")
        except sqlite3.OperationalError as exc:
            print(f"[TICK-2SRC] {asset} 写入失败: {exc}")
    return written


def detect_divergence(conn: sqlite3.Connection, now: int, window: int = 1800) -> List[Dict[str, Any]]:
    """同品种近 window 秒内多源价差超阈值 → 记审计 + 打印。"""
    out: List[Dict[str, Any]] = []
    placeholders = ",".join("?" for _ in DERIVED)
    for asset in ASSETS:
        rows = conn.execute(
            f"SELECT source, price, ts FROM market_ticks "
            f"WHERE symbol=? AND ts>=? AND price>0 AND lower(source) NOT IN ({placeholders}) "
            f"ORDER BY ts DESC",
            (asset, now - window, *DERIVED),
        ).fetchall()
        if not rows:
            continue
        latest: Dict[str, Tuple[float, int]] = {}
        for src, price, ts in rows:
            latest.setdefault(str(src), (float(price), int(ts)))   # 每源取最新一条
        if len(latest) < 2:
            continue
        items = sorted(latest.items(), key=lambda kv: kv[1][1], reverse=True)
        (sa, (pa, ta)), (sb, (pb, tb)) = items[0], items[1]
        diff = abs(pa - pb) / min(pa, pb) * 100.0
        if diff < DIVERGE_PCT:
            continue
        note = (f"{sa}={pa} @{ta} vs {sb}={pb} @{tb}，相差 {diff:.2f}%"
                f"（阈值 {DIVERGE_PCT}%）—— 别用中位数抹平，先查口径")
        conn.execute(
            "INSERT OR IGNORE INTO source_divergence "
            "(symbol, ts, source_a, price_a, source_b, price_b, diff_pct, note) "
            "VALUES (?,?,?,?,?,?,?,?)",
            (asset, now, sa, pa, sb, pb, round(diff, 4), note),
        )
        out.append({"asset": asset, "diff_pct": round(diff, 3),
                    "a": sa, "b": sb, "note": note})
        print(f"[TICK-2SRC] ⚠ 源间背离 {asset}: {note}")
    return out


def main() -> None:
    conn = sqlite3.connect(DB_PATH, timeout=60)
    conn.execute("PRAGMA busy_timeout=60000")
    now = int(time.time())
    _init_divergence_table(conn)
    written = ingest_secondary(conn, now)
    diverged = detect_divergence(conn, now)
    conn.commit()
    print(f"[TICK-2SRC] 完成：补入 {len(written)} 条 / 背离 {len(diverged)} 条")
    conn.close()


if __name__ == "__main__":
    main()
