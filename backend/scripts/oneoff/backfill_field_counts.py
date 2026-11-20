#!/usr/bin/env python3
"""从 field_sources 派生回填 5 个计数字段。

背景（v343）：
    ai_decisions 的 llm_field_count / rule_field_count / business_kb_field_count /
    historical_field_count 全表都是 0 —— 说明写入端那条 UPDATE 没落地
    （或者被后续流程重写成默认值）。而 field_sources 本身是有内容的，
    按它重算：llm 字段数 1~7（均值 4.48），根本不是 0。

    这两个计数是"模型到底回了几个字段"的唯一量化指标，
    它恒为 0 时，前面"evidence_confidence 90% 没填"这种事就没法从报表上看出来。

做法：
    只读 field_sources / missing_fields，重算计数后写回 4 个计数字段。
    不改 field_sources 本身，不臆造任何值；重复运行结果一致（幂等）。

用法：
    python3 backfill_field_counts.py            # 试运行
    python3 backfill_field_counts.py --apply
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src_python"))

import db
import db_retry


def _counts(field_sources: dict) -> dict[str, int]:
    c = {"llm": 0, "rule": 0, "business_kb": 0, "historical": 0, "ensemble": 0}
    for _f, s in (field_sources or {}).items():
        s = str(s or "")
        if s == "llm":
            c["llm"] += 1
        elif s.startswith("rule"):
            c["rule"] += 1
        elif s.startswith("business_knowledge"):
            c["business_kb"] += 1
        elif s == "historical_pattern":
            c["historical"] += 1
        elif s == "llm_ensemble_consensus":
            c["ensemble"] += 1
    return c


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true")
    args = ap.parse_args()

    conn = sqlite3.connect(db.get_db_path(), timeout=30, isolation_level=None)
    conn.execute("PRAGMA busy_timeout=30000")

    rows = conn.execute(
        "SELECT id, field_sources, llm_field_count FROM ai_decisions"
    ).fetchall()

    changed = 0
    dist: dict[int, int] = {}
    for rid, fs_raw, old in rows:
        try:
            fs = json.loads(fs_raw or "{}")
        except (json.JSONDecodeError, TypeError):
            fs = {}
        c = _counts(fs)
        dist[c["llm"]] = dist.get(c["llm"], 0) + 1
        if c["llm"] == int(old or 0):
            continue
        changed += 1
        if args.apply:
            db_retry.run_write(conn, lambda cur, _i=rid, _c=c: cur.execute(
                "UPDATE ai_decisions SET llm_field_count=?, rule_field_count=?, "
                "business_kb_field_count=?, historical_field_count=? WHERE id=?",
                (_c["llm"], _c["rule"], _c["business_kb"], _c["historical"], _i)))

    print(f"ai_decisions {len(rows)} 行 · 需要修正 {changed} 行")
    print("  llm 字段数分布:", dict(sorted(dist.items())))
    if not args.apply:
        print("\n(试运行，未写库 — 加 --apply 生效)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
