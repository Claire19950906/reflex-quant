#!/usr/bin/env python3
"""recompute_tech_scores.py —— 用 v450 事件族口径重算 tech_signals.score（幂等）。

为什么需要
----------
v450 起打分换成"事件族去重 + logistic 压缩"，但存量表没跟着重算：
18,522 行里 **18,503 行仍是旧口径的 0.95**（旧逻辑把每条命中 Σw 封顶）。
后果是整条链路建立在假分数上：

  · `tech_detector.grade_of` 把几乎每条信号都判成 A（0.95 ≥ 0.78）；
  · 桥接 `--min-score 0.58` 门槛形同虚设，什么都能过；
  · 前端"信号强度"、归因里的形态证据，看到的强度全是一个常数。

本脚本只从 `signal_json` 反算分数，**不重跑指标、不碰任何其它列**。
打分函数直接复用 `tech_detector._score_from_hits`（同一份代码，不复制公式，
避免两处口径漂移）；`role` 缺失的历史行按 `_CONTEXT_ONLY` 判定是否背景证据。

用法
----
    python3 scripts/recompute_tech_scores.py --dry-run            # 只报分布差异
    python3 scripts/recompute_tech_scores.py                      # 实写全量
    python3 scripts/recompute_tech_scores.py --since-days 90      # 只重算近 90 天
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import sqlite3
import sys
from collections import Counter
from pathlib import Path
from typing import Any

BACKEND = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND / "src_python"))

import tech_detector as td

DB_PATH = BACKEND / "trident_event_bus.db"
OUT_DIR = BACKEND / "reports" / "tech_engine"
BATCH = 500


def _items(signal_json: str) -> list[dict[str, Any]]:
    try:
        data = json.loads(signal_json or "[]")
    except (TypeError, ValueError):
        return []
    if not isinstance(data, list):
        return []
    out = []
    for d in data:
        if not isinstance(d, dict):
            continue
        if not d.get("role") and str(d.get("k") or "") in td._CONTEXT_ONLY:
            d = {**d, "role": "context"}
        out.append(d)
    return out


def recompute(conn: sqlite3.Connection, since_ts: int | None,
              dry_run: bool) -> dict[str, Any]:
    sql = "SELECT id, asset, ts, score, signal_json FROM tech_signals"
    params: tuple = ()
    if since_ts is not None:
        sql += " WHERE ts >= ?"
        params = (int(since_ts),)
    sql += " ORDER BY id"
    rows = conn.execute(sql, params).fetchall()

    before: Counter = Counter()
    after: Counter = Counter()
    updates: list[tuple[float, int]] = []
    a_before = a_after = pass_min = 0
    for rid, _asset, _ts, score, sj in rows:
        old = float(score or 0.0)
        new, _families, _total = td._score_from_hits(_items(sj))
        before[round(old, 2)] += 1
        after[round(new, 1)] += 1
        a_before += 1 if old >= 0.78 else 0
        a_after += 1 if new >= 0.78 else 0
        pass_min += 1 if new >= 0.58 else 0
        if abs(new - old) > 0.0005:
            updates.append((float(new), int(rid)))

    written = 0
    if not dry_run and updates:
        for i in range(0, len(updates), BATCH):
            chunk = updates[i:i + BATCH]
            conn.executemany("UPDATE tech_signals SET score = ? WHERE id = ?", chunk)
            conn.commit()
            written += len(chunk)

    return {
        "rows": len(rows),
        "changed": len(updates),
        "written": written,
        "before_top": before.most_common(6),
        "after_top": after.most_common(10),
        "before_a": a_before,
        "after_a": a_after,
        "after_pass_min": pass_min,
    }


def render_report(stats: dict[str, Any], since_ts: int | None,
                  dry_run: bool) -> str:
    scope = "全量" if since_ts is None else dt.datetime.fromtimestamp(
        since_ts).strftime("%Y-%m-%d 起")
    lines = [
        "# 技术信号评分重算（v452）",
        "",
        f"生成时间：{dt.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}　"
        f"范围：{scope}　模式：{'试算（未写库）' if dry_run else '实写'}",
        "",
        f"扫描 {stats['rows']} 行，分数变化 {stats['changed']} 行，"
        f"实际写库 {stats['written']} 行。",
        "",
        "> 口径：复用 `tech_detector._score_from_hits`（事件族去重 + logistic 压缩），"
        "只从 signal_json 反算，不重跑指标。",
        "",
        "## 重算前（旧口径，Σw 封顶 0.95）",
        "",
        "| 分数 | 行数 |",
        "|---|---|",
    ]
    for k, v in stats["before_top"]:
        lines.append(f"| {k} | {v} |")
    lines += ["", f"A 级（≥0.78）：**{stats['before_a']}** 行", "",
              "## 重算后（v450 事件族口径）", "", "| 分数 | 行数 |", "|---|---|"]
    for k, v in stats["after_top"]:
        lines.append(f"| {k} | {v} |")
    lines += ["", f"A 级（≥0.78）：**{stats['after_a']}** 行；"
                   f"≥0.58（桥接门槛）：**{stats['after_pass_min']}** 行", ""]
    return "\n".join(lines)


def main() -> int:
    ap = argparse.ArgumentParser(description="用 v450 口径重算 tech_signals.score")
    ap.add_argument("--dry-run", action="store_true", help="只报分布，不写库")
    ap.add_argument("--since-days", type=int, default=0,
                    help="0=全量；>0 只重算最近 N 天（按 K 线 ts）")
    ap.add_argument("--no-write-report", action="store_true")
    args = ap.parse_args()

    since_ts = None
    if args.since_days > 0:
        since_ts = int((dt.datetime.now() -
                        dt.timedelta(days=args.since_days)).timestamp())

    conn = sqlite3.connect(str(DB_PATH), timeout=30)
    conn.row_factory = sqlite3.Row
    try:
        stats = recompute(conn, since_ts, args.dry_run)
        print(json.dumps({k: v for k, v in stats.items()
                          if not k.endswith("_top")}, ensure_ascii=False))
        if not args.no_write_report:
            OUT_DIR.mkdir(parents=True, exist_ok=True)
            out = OUT_DIR / "v452_score_recompute.md"
            out.write_text(render_report(stats, since_ts, args.dry_run),
                           encoding="utf-8")
            print(f"report -> {out}")
        return 0
    finally:
        conn.close()


if __name__ == "__main__":
    raise SystemExit(main())
