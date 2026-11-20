#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""decision_reject_audit.py — 拒单质量审计（真·复盘"为什么不开仓"）(v338)

回答的问题
----------
#1 能力验收只说"真实开仓命中 52.6%"，但 3,187 条拒绝**从没被检查过对错**。
如果规则拒掉的多是好机会，那系统不是"谨慎"，是"瞎"。

本脚本不新增字段、不改库 —— trade_decisions 的 3,187 条 REJECT 行
**全部**有 entry_price + actual_outcome_pct，可以直接回算:
  intended_action=BUY 且 outcome>0  → 错拒（错过上涨）
  intended_action=SELL 且 outcome<0 → 错拒（错过下跌）
其余 → 拒对

按 deny_reason 归一成人类可读类别，输出每类的错拒率与平均错过幅度。

用法:
    python3 decision_reject_audit.py
    python3 decision_reject_audit.py --json
"""
from __future__ import annotations

import argparse
import json
import sqlite3
from collections import defaultdict
from pathlib import Path
from typing import Any, Dict

BACKEND_DIR = Path(__file__).resolve().parents[2]
DB_PATH = BACKEND_DIR / "trident_event_bus.db"


def classify(reason: str) -> str:
    r = str(reason or "")
    if r.startswith("asset_"):
        return "额度/集中度上限"
    if r == "high_strength":
        return "强度闸门"
    if r.startswith("R8"):
        return "同批次连坐"
    if r:
        return "其他规则"
    return "未记录"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default=str(DB_PATH))
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()

    conn = sqlite3.connect("file:%s?mode=ro" % args.db, uri=True)
    rows = conn.execute(
        """SELECT intended_action, final_action, deny_reason,
                  actual_outcome_pct
             FROM trade_decisions
            WHERE final_action = 'REJECTED'"""
    ).fetchall()

    agg: Dict[str, Dict[str, Any]] = defaultdict(
        lambda: {"n": 0, "wrong": 0, "sum_missed": 0.0, "max_missed": 0.0})
    for action, _final, reason, outcome in rows:
        if outcome is None:
            continue
        cat = classify(reason)
        pct = float(outcome)
        d = agg[cat]
        d["n"] += 1
        missed = pct if (action or "").upper() == "BUY" else -pct
        if missed > 0:
            d["wrong"] += 1
            d["sum_missed"] += missed
            d["max_missed"] = max(d["max_missed"], missed)

    total_n = sum(d["n"] for d in agg.values())
    total_wrong = sum(d["wrong"] for d in agg.values())
    out = {
        "total_rejects": total_n,
        "wrong_rejects": total_wrong,
        "wrong_pct": round(total_wrong / total_n * 100, 2) if total_n else 0.0,
        "by_category": {
            k: {"n": v["n"], "wrong": v["wrong"],
                "wrong_pct": round(v["wrong"] / v["n"] * 100, 2) if v["n"] else 0.0,
                "avg_missed_pct": round(v["sum_missed"] / v["wrong"], 2) if v["wrong"] else 0.0,
                "max_missed_pct": round(v["max_missed"], 2)}
            for k, v in sorted(agg.items(), key=lambda x: -x[1]["n"])},
    }

    if args.json:
        print(json.dumps(out, ensure_ascii=False, indent=2))
        return 0

    print("拒单审计 · %d 条 · 错拒 %d 条 (%.2f%%)" % (
        total_n, total_wrong, out["wrong_pct"]))
    print("%-16s %6s %6s %8s %10s %10s" % ("拒因", "n", "错拒", "错拒率", "均错过%", "最大%"))
    for k, v in out["by_category"].items():
        print("%-16s %6d %6d %7.1f%% %9.2f%% %9.2f%%" % (
            k, v["n"], v["wrong"], v["wrong_pct"],
            v["avg_missed_pct"], v["max_missed_pct"]))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
