#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""kg_injection_report.py — v423 注入埋点的只读报表。

看什么：一段时间内**每个品种被注入了什么**（注入了哪些规范因子、走的是 batch/l2/attribute 哪个口）。
不看什么：不评估"注入有没有让决策更准"——那需要决策结果对照，样本不足前不做因果结论。

用法：
    python3 scripts/kg_injection_report.py                 # 最近 7 天
    python3 scripts/kg_injection_report.py --days 30
    python3 scripts/kg_injection_report.py --asset XAU
    python3 scripts/kg_injection_report.py --json
"""
from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
from collections import Counter, defaultdict

HERE = os.path.dirname(os.path.abspath(__file__))
SRC = os.path.normpath(os.path.join(HERE, "..", "src_python"))
if SRC not in sys.path:
    sys.path.insert(0, SRC)

import db


def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(f"file:{db.get_db_path()}?mode=ro", uri=True, timeout=10)
    conn.row_factory = sqlite3.Row
    return conn


def report(days: int = 7, asset: str = "") -> dict:
    since_sql = f"datetime('now', '-{int(days)} day')"
    where = f"ts >= {since_sql}"
    params: list = []
    if asset:
        where += " AND asset=?"
        params.append(asset.upper())

    conn = _connect()
    try:
        try:
            rows = [dict(r) for r in conn.execute(
                f"SELECT ts, asset, mode, block_len, factors FROM causal_injection_log "
                f"WHERE {where} ORDER BY id", params).fetchall()]
        except sqlite3.OperationalError:
            rows = []  # 表还没建（服务没跑过新代码）→ 空报表，不报错
    finally:
        conn.close()

    by_asset: dict[str, list] = defaultdict(list)
    for r in rows:
        by_asset[r["asset"]].append(r)

    out = {"days": days, "asset": asset or "ALL", "rows": len(rows), "assets": {}}
    for a, rs in sorted(by_asset.items()):
        modes = Counter(r["mode"] or "-" for r in rs)
        facs = Counter()
        for r in rs:
            for f in (r["factors"] or "").split(","):
                if f:
                    facs[f] += 1
        out["assets"][a] = {
            "injections": len(rs),
            "modes": dict(modes),
            "top_factors": facs.most_common(6),
            "last_ts": rs[-1]["ts"],
        }
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=7)
    ap.add_argument("--asset", default="")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()

    rep = report(args.days, args.asset)
    if args.json:
        print(json.dumps(rep, ensure_ascii=False, indent=2))
        return 0

    print(f"注入埋点报表 · 最近 {rep['days']} 天 · 共 {rep['rows']} 次注入")
    if not rep["rows"]:
        print("（还没有记录：埋点只对重启后的新决策生效，历史不回填）")
        return 0
    for a, v in rep["assets"].items():
        print(f"\n[{a}] 注入 {v['injections']} 次 · 最后 {v['last_ts']}")
        print("  口:", ", ".join(f"{k}={n}" for k, n in v["modes"].items()))
        print("  因子:", ", ".join(f"{k}={n}" for k, n in v["top_factors"]) or "-")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
