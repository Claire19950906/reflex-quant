# -*- coding: utf-8 -*-
"""给 external_indicators 每行打坐标，落成 series_registry 表（v341）。

只新增一张注册表，不动原数据：
  series_registry(asset, indicator, namespace, series_key, kind, rows, updated_at)
下游要按“品种”聚合时查它，就不会再把 '*' 抽屉里的 ETF 价格、
CRYPTO 的市值、STABLE 的存量当成某个品种的行情。

用法：
  python3 src_python/build_series_registry.py            # 只报告
  python3 src_python/build_series_registry.py --apply    # 写表
"""
from __future__ import annotations

import argparse
import collections
import datetime as dt
import os
import sqlite3
import time

import series_namespace as sn

DB = os.path.join(os.path.dirname(__file__), "..", "trident_event_bus.db")


def collect(conn) -> list[dict]:
    rows = conn.execute(
        "SELECT asset, indicator, COUNT(*) FROM external_indicators "
        "GROUP BY asset, indicator").fetchall()
    out = []
    for asset, indicator, n in rows:
        s = sn.resolve(asset, indicator)
        out.append({"asset": asset, "indicator": indicator, "rows": n,
                    "namespace": s.namespace, "series_key": s.series, "kind": s.kind})
    return out


SCHEMA = (
    "CREATE TABLE IF NOT EXISTS series_registry ("
    " asset TEXT NOT NULL, indicator TEXT NOT NULL, namespace TEXT NOT NULL,"
    " series_key TEXT NOT NULL, kind TEXT NOT NULL, rows INTEGER NOT NULL,"
    " updated_at INTEGER NOT NULL, UNIQUE(asset, indicator))")


def refresh(conn) -> tuple[int, int]:
    """重建注册表（幂等）。→ (写入行数, 新增/变更行数)"""
    items = collect(conn)
    conn.execute(SCHEMA)
    before = conn.execute("SELECT COUNT(*) FROM series_registry").fetchone()[0]
    now = int(time.time())
    with conn:
        conn.executemany(
            "INSERT INTO series_registry "
            "(asset,indicator,namespace,series_key,kind,rows,updated_at) "
            "VALUES (?,?,?,?,?,?,?) "
            "ON CONFLICT(asset,indicator) DO UPDATE SET "
            "namespace=excluded.namespace, series_key=excluded.series_key,"
            "kind=excluded.kind, rows=excluded.rows, updated_at=excluded.updated_at",
            [(i["asset"], i["indicator"], i["namespace"], i["series_key"],
              i["kind"], i["rows"], now) for i in items])
    after = conn.execute("SELECT COUNT(*) FROM series_registry").fetchone()[0]
    return after, after - before


def freshness(conn, now: int | None = None) -> dict:
    """注册表 vs 数据的时差。

    stale_hours   = 最新一条指标数据比"现在"旧了多少小时（数据源停更？）
    missing       = 有指标但注册表里没有的组合数（说明注册表没跟上）
    """
    now = int(now or time.time())
    out = {"rows": 0, "stale_hours": None, "missing": 0, "updated_at": None}
    try:
        out["rows"] = int(conn.execute(
            "SELECT COUNT(*) FROM series_registry").fetchone()[0] or 0)
        out["updated_at"] = conn.execute(
            "SELECT MAX(updated_at) FROM series_registry").fetchone()[0]
    except sqlite3.Error as exc:
        # v399：允许失败，但不许不留痕（v322 审计门禁要求）
        print(f"[SERIES-REG] series_registry 读取失败（注册表可能缺失）: {type(exc).__name__}: {str(exc)[:80]}", flush=True)
    try:
        newest = conn.execute("SELECT MAX(ts) FROM external_indicators").fetchone()[0]
        if newest:
            out["stale_hours"] = round((now - int(newest)) / 3600.0, 1)
    except sqlite3.Error:
        pass
    try:
        live = conn.execute(
            "SELECT COUNT(*) FROM (SELECT DISTINCT asset, indicator FROM external_indicators)").fetchone()[0]
        out["missing"] = max(0, int(live) - out["rows"])
    except sqlite3.Error:
        pass
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--db", default=DB)
    args = ap.parse_args()

    conn = sqlite3.connect(args.db, timeout=120)
    items = collect(conn)

    print(f"=== (asset, indicator) 组合 {len(items)} 个 ===")
    agg = collections.Counter()
    for it in items:
        agg[(it["namespace"], it["kind"])] += it["rows"]
    total = sum(agg.values())
    for (ns, kind), n in sorted(agg.items(), key=lambda x: -x[1]):
        print(f"  {ns:8s} / {kind:9s}  {n:7d} 行  ({n / total * 100:5.1f}%)")

    print("\n=== 抽屉里的“其实是某个品种”的样本 ===")
    for it in items:
        if it["asset"] in ("*", "FX") and it["namespace"] == "asset":
            print(f"  {it['asset']:4s} + {it['indicator']:20s} → 品种 {it['series_key']:8s}"
                  f" ({it['rows']} 行)")
            if it["rows"] < 300:
                break

    print("\n=== 被误当“品种”的聚合量（前 8）===")
    for it in sorted(items, key=lambda x: -x["rows"]):
        if it["namespace"] != "asset" and it["rows"] >= 200:
            print(f"  {it['asset']:14s} + {it['indicator']:26s} → {it['namespace']}/{it['kind']}"
                  f"  {it['rows']} 行")

    if not args.apply:
        print("\n[DRY-RUN] 加 --apply 写 series_registry")
        return 0

    n, added = refresh(conn)
    fr = freshness(conn)
    print(f"[APPLY] series_registry {n} 行（新增/变更 {added}）  "
          f"({dt.datetime.now():%Y-%m-%d %H:%M:%S})")
    print(f"[FRESH] 最新指标数据落后 {fr['stale_hours']}h / 未登记组合 {fr['missing']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
