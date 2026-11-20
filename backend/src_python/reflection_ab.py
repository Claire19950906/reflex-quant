#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""reflection_ab.py — 回流 A/B（批次级集群随机，默认 30% 对照）

为什么是"批次级"而不是"逐条决策"
--------------------------------
prompt 里的「复盘经验」分区是**每批新闻构建一次**（一批 8~10 条新闻共用同一段文本），
逐条随机就得为每条新闻重建一次 prompt，代价和结果漂移都不划算。
所以按**批次**随机：整批要么带软启发、要么只带硬铁律，形成 cluster RCT。

分组做什么
----------
- treatment：现状（硬铁律 + 软启发）
- control  ：只注入硬铁律（kind=0），**软启发(kind=1)全部撤掉**
硬铁律是人工写的强制约束，任何组都保留，不能被 A/B 关掉。

口径
----
- 分组按 30 分钟时间桶做 md5 取模，稳定可复现，不依赖随机数状态；
- 分配记录落 `insight_ab_assignments`，评估时按批次时间窗回捞决策；
- 结果用 `shadow_reflections` 的 4h 真值，不用 `insight_hit_ids`（那是关键词兜底，假回流）。
- `REFLECTION_AB_RATE=0` 可一键关停（默认 0.3）。

用法
    python3 reflection_ab.py --evaluate [--days 14]
    python3 reflection_ab.py --assign          # 看当前桶会分到哪组（调试用）
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import sqlite3
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

import db

RATE_ENV = "REFLECTION_AB_RATE"
DEFAULT_RATE = 0.3
BUCKET_SEC = 1800          # 30 分钟一个分组桶
WINDOW_SEC = 1800          # 评估时一批决策的归属窗口


def rate() -> float:
    try:
        r = float(os.environ.get(RATE_ENV, DEFAULT_RATE))
    except Exception:
        r = DEFAULT_RATE
    return max(0.0, min(1.0, r))


def _bucket(now: Optional[float] = None) -> Tuple[str, int]:
    ts = int(now if now is not None else time.time())
    start = ts - (ts % BUCKET_SEC)
    return time.strftime("%Y%m%d-%H%M", time.localtime(start)), start


def assign_group(now: Optional[float] = None) -> Tuple[str, str, int]:
    """返回 (grp, batch_key, bucket_start)。grp ∈ {'treatment','control','off'}。"""
    key, start = _bucket(now)
    r = rate()
    if r <= 0:
        return "off", key, start
    h = int(hashlib.md5(key.encode()).hexdigest()[:8], 16) % 1000
    return ("control" if h < r * 1000 else "treatment"), key, start


def ensure_table(conn: sqlite3.Connection) -> None:
    conn.execute(
        "CREATE TABLE IF NOT EXISTS insight_ab_assignments("
        " batch_key TEXT PRIMARY KEY, ts TEXT NOT NULL, bucket_start INTEGER NOT NULL,"
        " grp TEXT NOT NULL, soft_ids TEXT NOT NULL DEFAULT '[]', note TEXT NOT NULL DEFAULT '')")


def log_assignment(key: str, grp: str, soft_ids: List[int], bucket_start: int,
                   note: str = "") -> None:
    conn = db.get_connection()
    try:
        conn.execute("PRAGMA busy_timeout=30000")
        ensure_table(conn)
        conn.execute(
            "INSERT OR REPLACE INTO insight_ab_assignments(batch_key, ts, bucket_start, grp, soft_ids, note)"
            " VALUES(?,?,?,?,?,?)",
            (key, time.strftime("%Y-%m-%dT%H:%M:%S"), int(bucket_start), grp,
             json.dumps(sorted(set(int(i) for i in soft_ids))), note),
        )
        conn.commit()
    finally:
        conn.close()


def evaluate(days: int = 14) -> Dict[str, Any]:
    conn = db.get_connection()
    conn.row_factory = sqlite3.Row
    try:
        if not conn.execute("SELECT 1 FROM sqlite_master WHERE name='insight_ab_assignments'").fetchone():
            return {"ok": True, "note": "尚未开始 A/B（无分配记录）", "groups": {}}
        since = int(time.time()) - days * 86400
        assigns = conn.execute(
            "SELECT * FROM insight_ab_assignments WHERE bucket_start>=? ORDER BY bucket_start",
            (since,)).fetchall()
        groups: Dict[str, Dict[str, Any]] = {}
        for a in assigns:
            g = str(a["grp"])
            acc = groups.setdefault(g, {
                "batches": 0, "n_decisions": 0, "n_shadow": 0, "ok": 0, "n_shadow24": 0, "ok24": 0,
                "conf_sum": 0.0, "conf_n": 0, "directional": 0, "claimed_ids": 0,
                "n_soft": 0,
            })
            acc["batches"] += 1
            try:
                acc["n_soft"] += len(json.loads(a["soft_ids"] or "[]"))
            except Exception:
                pass
            rows = conn.execute(
                "SELECT id, suggested_action, COALESCE(evidence_confidence, 0) AS conf,"
                "       COALESCE(insight_hit_ids,'') AS hit_ids"
                "  FROM ai_decisions WHERE ts>=? AND ts<?",
                (int(a["bucket_start"]), int(a["bucket_start"]) + WINDOW_SEC)).fetchall()
            for r in rows:
                acc["n_decisions"] += 1
                if str(r["suggested_action"] or "").upper() in ("BUY", "SELL"):
                    acc["directional"] += 1
                if float(r["conf"] or 0) > 0:
                    acc["conf_sum"] += float(r["conf"] or 0)
                    acc["conf_n"] += 1
                if (r["hit_ids"] or "").strip():
                    acc["claimed_ids"] += 1
                sr = conn.execute(
                    "SELECT reflection_effective, effective_24h FROM shadow_reflections"
                    " WHERE decision_id=? AND verdict IN ('direction_ok','direction_wrong')",
                    (r["id"],)).fetchone()
                if sr is not None:
                    acc["n_shadow"] += 1
                    acc["ok"] += int(sr["reflection_effective"] or 0)
                    if sr["effective_24h"] is not None:
                        acc["n_shadow24"] += 1
                        acc["ok24"] += int(sr["effective_24h"] or 0)
        out_groups = {}
        for g, a in groups.items():
            n = a["n_shadow"]
            out_groups[g] = {
                "batches": a["batches"],
                "n_decisions": a["n_decisions"],
                "n_shadow": n,
                "ok": a["ok"],
                "hit_rate": round(a["ok"] / n, 3) if n else None,
                "n_shadow_24h": a["n_shadow24"],
                "hit_rate_24h": round(a["ok24"] / a["n_shadow24"], 3) if a["n_shadow24"] else None,
                "avg_confidence": round(a["conf_sum"] / a["conf_n"], 3) if a["conf_n"] else None,
                "directional_ratio": round(a["directional"] / a["n_decisions"], 3) if a["n_decisions"] else None,
                "claimed_hit_ratio": round(a["claimed_ids"] / a["n_decisions"], 3) if a["n_decisions"] else None,
                "avg_soft_rules": round(a["n_soft"] / a["batches"], 2) if a["batches"] else None,
            }
        return {"ok": True, "days": days, "rate": rate(), "groups": out_groups}
    finally:
        conn.close()


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--evaluate", action="store_true")
    ap.add_argument("--assign", action="store_true")
    ap.add_argument("--days", type=int, default=14)
    args = ap.parse_args()
    if args.assign:
        g, k, s = assign_group()
        print(json.dumps({"grp": g, "batch_key": k, "bucket_start": s, "rate": rate()}, ensure_ascii=False))
        return 0
    print(json.dumps(evaluate(args.days), ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
