#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""eval_frozen —— 冻结评测集：给"改完有没有变差"一个可执行的答案。

缺口背景：
    这个仓库有 3,324 条测试，但都是"函数行为"测试；改一个 handler、
    换一层 repository、调一次 SQL，输出到底变了没有，没有基准可比。
    我说的"热延迟 2.1s → 0.024s、SQL 层零差异"，当时是靠人工比对两条 curl 输出。

做法（刻意做小）：
    1. `--freeze`：从库里挑 N 条真实 news（带决策），把结构化输出的
       **确定性字段** 存成 JSON 基准（去掉一切时间戳与易变字段）。
    2. 不带参数：重跑同一批，逐字段比对，输出"新增 / 变化 / 消失"。
       任何变化都打印具体字段与新旧值，退出码非 0 就表示漂移。

为什么是 N 条样本而不是全量：
    全量跑会打满内存（实测全量 pytest 能把可用内存打到 16MB）。
    60 条足以覆盖 主要 verdict 分布 + 有/无决策 两种分支。

用法：
    python3 backend/scripts/eval_frozen.py --freeze        # 建立基准（覆盖写）
    python3 backend/scripts/eval_frozen.py                 # 与基准比对
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from pathlib import Path

# v437: 脚本在 scripts/oneoff/ 子目录下，parent.parent 只到 scripts/，
# 基准与库会指错位置；需再退一层到 backend/。
BACKEND = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(BACKEND / "src_python"))
DB = BACKEND / "trident_event_bus.db"
BASELINE = BACKEND / "tests" / "eval" / "frozen_set.json"

SAMPLE_SIZE = 60
# 只比确定性字段：时间戳、耗时、随机 ID 一律不进基准
COMPARE_FIELDS = ("news_id", "decision_id", "verdict", "status",
                  "structured_check_mentioned", "structured_check_missing",
                  "structured_check_fabricated", "has_reasoning")


def pick_news_ids() -> list[int]:
    conn = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
    try:
        rows = conn.execute(
            "SELECT n.id FROM raw_news n JOIN ai_decisions d ON d.news_id = n.id "
            "GROUP BY n.id ORDER BY n.id DESC LIMIT ?", (SAMPLE_SIZE,)).fetchall()
    finally:
        conn.close()
    return [int(r[0]) for r in rows]


def snapshot(news_id: int) -> dict | None:
    from repository import news as repo
    row = repo.get_news_with_latest_decision(news_id)
    if not row:
        return None
    sc = row.get("structured_check") or {}
    if isinstance(sc, str):
        try:
            sc = json.loads(sc)
        except Exception:
            sc = {}
    out = {
        "news_id": int(row.get("id") or news_id),
        "decision_id": row.get("decision_id"),
        "verdict": row.get("verdict"),
        "status": row.get("status"),
        "structured_check_mentioned": len(sc.get("mentioned") or []),
        "structured_check_missing": len(sc.get("missing") or []),
        "structured_check_fabricated": len(sc.get("fabricated") or []),
        "has_reasoning": bool(row.get("reasoning")),
    }
    return out


def freeze() -> int:
    ids = pick_news_ids()
    cases = []
    for nid in ids:
        s = snapshot(nid)
        if s:
            cases.append(s)
    BASELINE.parent.mkdir(parents=True, exist_ok=True)
    BASELINE.write_text(json.dumps({"size": len(cases), "cases": cases},
                                   ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"[FREEZE] 基准已写入 {BASELINE}（{len(cases)} 条）")
    return 0


def compare() -> int:
    if not BASELINE.exists():
        print(f"[ERROR] 基准不存在，先跑 --freeze")
        return 2
    base = {c["news_id"]: c for c in json.loads(BASELINE.read_text(encoding="utf-8"))["cases"]}

    drift, missing, ok = 0, 0, 0
    for nid, exp in base.items():
        now = snapshot(nid)
        if now is None:
            print(f"[MISSING] news_id={nid} 现在查不到数据")
            missing += 1
            continue
        diffs = [(f, exp.get(f), now.get(f)) for f in COMPARE_FIELDS
                 if exp.get(f) != now.get(f)]
        if diffs:
            drift += 1
            print(f"[DRIFT] news_id={nid}")
            for f, old, new in diffs:
                print(f"        {f}: {old!r} -> {new!r}")
        else:
            ok += 1

    print(f"[RESULT] 一致 {ok} / 漂移 {drift} / 消失 {missing}（共 {len(base)} 条）")
    return 0 if drift == 0 and missing == 0 else 1


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--freeze", action="store_true")
    args = ap.parse_args()
    return freeze() if args.freeze else compare()


if __name__ == "__main__":
    sys.exit(main())
