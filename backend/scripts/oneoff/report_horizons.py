#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""report_horizons — 按周期看判断质量（15m / 1h / 4h / 1d / 5d）。

背景
----
主口径只结算 5 天（`HORIZON_S = 5*86400`），于是有一个问题一直答不上来：

    这套判断到底擅长 1 小时，还是擅长 5 天？

v342 加了 `decision_outcomes_horizon`（同一条决策在 5 个周期上分别记账），
本脚本就是它的读数入口，直接给出「每个周期的胜率 / 平均净收益 / 取不到价比例」。

口径
----
- 胜率分子分母只算 `status='settled'` —— `no_price`（历史库没有对应分钟的价）
  与 `not_executable`（T+1 品种在那个时点还不能平仓）单列，**不算判断错**。
- `pnl_pct` 是扣完成本后的净收益；`outcome_pct` 是原始涨跌幅。
- 样本 < 30 时在输出里标 ⚠️：这个胜率的 95% 区间很宽，别当结论。

用法
----
    python3 scripts/report_horizons.py                    # 近 30 天，全品种
    python3 scripts/report_horizons.py --days 90
    python3 scripts/report_horizons.py --asset BTC --json
退出码：0 = 正常；1 = 表不存在（尚未跑过 v342 结算）
"""
from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
import time

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "src_python")))

MIN_SAMPLE_NOTE = 30


def _conn() -> sqlite3.Connection:
    import db as _db
    conn = _db.get_connection()
    conn.row_factory = sqlite3.Row
    return conn


def collect(conn: sqlite3.Connection, days: int, asset: str | None) -> dict:
    cutoff = int(time.time()) - days * 86400
    has_table = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' "
        "AND name='decision_outcomes_horizon'").fetchone() is not None
    if not has_table:
        return {"ok": False, "error": "缺表 decision_outcomes_horizon（先跑一次结算）",
                "days": days, "asset": asset, "horizons": []}

    params: list = [cutoff]
    where = "decision_ts >= ?"
    if asset:
        where += " AND asset = ?"
        params.append(asset.strip().upper())
    rows = conn.execute(
        "SELECT horizon, horizon_sec, status, COUNT(*) AS n, "
        "       SUM(CASE WHEN hit=1 THEN 1 ELSE 0 END) AS wins, "
        "       AVG(CASE WHEN status='settled' THEN pnl_pct END) AS avg_pnl, "
        "       AVG(CASE WHEN status='settled' THEN outcome_pct END) AS avg_outcome "
        "FROM decision_outcomes_horizon WHERE " + where + " "
        "GROUP BY horizon, horizon_sec ORDER BY horizon_sec", tuple(params)).fetchall()

    merged: dict = {}
    totals: dict = {}
    for r in rows:
        status = r["status"] or "unknown"
        totals[status] = totals.get(status, 0) + int(r["n"])
        item = merged.setdefault(r["horizon"], {
            "horizon": r["horizon"], "horizon_sec": int(r["horizon_sec"]),
            "settled": 0, "wins": 0, "hit_rate": None,
            "avg_pnl_pct": None, "avg_outcome_pct": None,
            "no_price": 0, "not_executable": 0, "implausible": 0,
        })
        n = int(r["n"] or 0)
        if status == "settled":
            item["settled"] = n
            item["wins"] = int(r["wins"] or 0)
            item["avg_pnl_pct"] = round(float(r["avg_pnl"]), 3) if r["avg_pnl"] is not None else None
            item["avg_outcome_pct"] = round(float(r["avg_outcome"]), 3) if r["avg_outcome"] is not None else None
        elif status in ("no_price", "not_executable", "implausible"):
            item[status] = n
    for item in merged.values():
        if item["settled"]:
            item["hit_rate"] = round(item["wins"] / item["settled"], 4)
    ordered = sorted(merged.values(), key=lambda x: x["horizon_sec"])
    out = {"ok": True, "days": days, "asset": asset, "horizons": ordered,
           "status_totals": totals}
    try:
        import horizon_reflection as _hr
        out["reflection"] = _hr.read_stats(conn, days=days, asset=asset)
    except Exception as exc:  # noqa: BLE001
        out["reflection"] = {"total": 0, "note": f"判定不可用: {type(exc).__name__}"}
    return out


def render(data: dict) -> str:
    if not data["ok"]:
        return f"✗ {data['error']}"
    lines = [f"周期读数 · 近 {data['days']} 天 · 品种={data['asset'] or '全部'}"]
    lines.append("-" * 78)
    lines.append(f"{'周期':<6}{'已结算':>6}{'胜':>5}{'胜率':>9}{'平均净收益':>12}"
                 f"{'平均原始':>11}{'无价':>6}{'不可执行':>9}")
    for h in data["horizons"]:
        rate = f"{h['hit_rate']:.1%}" if h["hit_rate"] is not None else "—"
        avg = f"{h['avg_pnl_pct']:+.2f}%" if h["avg_pnl_pct"] is not None else "—"
        raw = f"{h['avg_outcome_pct']:+.2f}%" if h["avg_outcome_pct"] is not None else "—"
        flag = " ⚠️" if 0 < h["settled"] < MIN_SAMPLE_NOTE else ""
        lines.append(f"{h['horizon']:<6}{h['settled']:>6}{h['wins']:>5}{rate:>9}"
                     f"{avg:>12}{raw:>11}{h['no_price']:>6}{h['not_executable']:>9}{flag}")
    if not data["horizons"]:
        lines.append("（暂无读数：等下一趟结算）")
    else:
        best = max((h for h in data["horizons"] if h["hit_rate"] is not None),
                   key=lambda h: h["hit_rate"], default=None)
        if best:
            lines.append("-" * 78)
            lines.append(f"最好周期：{best['horizon']}  胜率 {best['hit_rate']:.1%}"
                         f"  平均净收益 {best['avg_pnl_pct']:+.2f}%")
    if any(0 < h["settled"] < MIN_SAMPLE_NOTE for h in data["horizons"]):
        lines.append(f"⚠️ = 样本少于 {MIN_SAMPLE_NOTE}，胜率区间很宽，先别当结论。")
    lines.append(f"状态分布：{data['status_totals']}")

    rf = data.get("reflection") or {}
    if rf.get("total"):
        dt = rf.get("direction_vs_timing") or {}
        lines.append("")
        lines.append("错因拆分（方向错 vs 节奏错）")
        lines.append("-" * 78)
        lines.append(f"方向错 {dt.get('direction_wrong', 0)}   "
                     f"节奏错 {dt.get('timing_wrong', 0)}   总判定 {rf['total']}")
        others = {k: v for k, v in (rf.get("by_verdict") or {}).items()
                  if k not in ("direction_wrong", "timing_wrong")}
        if others:
            lines.append(f"其他：{others}")
        lines.append("方向错 → 该改推理；节奏错 → 该改入场时点，不要推翻方向。")
    return "\n".join(lines)


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description="多周期判断质量读数")
    ap.add_argument("--days", type=int, default=30)
    ap.add_argument("--asset", type=str, default=None)
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args(argv)
    days = min(max(1, args.days), 365)
    conn = _conn()
    try:
        data = collect(conn, days, args.asset)
    finally:
        conn.close()
    if args.json:
        print(json.dumps(data, ensure_ascii=False, indent=2))
    else:
        print(render(data))
    return 0 if data["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
