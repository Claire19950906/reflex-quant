# -*- coding: utf-8 -*-
"""v399 P1: 给历史决策补写程序化错误码（decision_errors）。

背景
----
`docs/archive/细节洞察能力评估-2026-11-16.html` 的 P1 第一条：反思的"三维细分"
名义上有 E001a–E033 支撑，实测 `decision_errors` 全库只有 23 行 —— 因为
`reflection_checkers.run_all_checkers()` 只被独立任务 `run_reflection.py`
调用过，那个任务几乎不跑。

v399 已把校验器接到反思落库前（`paper_reflection_worker._insert_one`），
**只对之后新产生的反思生效**。这个脚本补历史欠账：

  · 只处理**非降级**决策（降级决策没有真分析，跑校验器只会产假错因）；
  · 跳过已有错误码的决策（save_errors 本身 INSERT OR IGNORE 幂等）；
  · 默认 dry-run，`--commit` 才写库。

用法
----
  python3 scripts/backfill_decision_error_codes.py            # 预演
  python3 scripts/backfill_decision_error_codes.py --commit   # 落库
  python3 scripts/backfill_decision_error_codes.py --limit 50 # 只跑最近 50 条
  python3 scripts/backfill_decision_error_codes.py --since-hours 6 --commit
                                                              # 只补最近 6h（常驻增量用）

v453: 加 `--since-hours`。理由：归因链此前只有"一次性回填"，回填完就停在那个
时点（11-18 14:00 之后新决策一条错误码都没有）。常驻增量任务每 10 分钟只补
最近窗口内的新决策，才是"恢复即恢复"的正解；同时避免把停机期的陈旧决策
重新算一遍，制造错时样本。
"""
from __future__ import annotations

import argparse
import collections
import os
import sqlite3
import sys

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(BASE_DIR, "src_python"))

DB_PATH = os.path.join(BASE_DIR, "trident_event_bus.db")


def _open(readonly: bool) -> sqlite3.Connection:
    if readonly:
        conn = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True)
    else:
        conn = sqlite3.connect(DB_PATH, timeout=30)
    conn.row_factory = sqlite3.Row
    return conn


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--commit", action="store_true", help="写库（默认只读 dry-run）")
    ap.add_argument("--limit", type=int, default=0, help="只处理最近 N 条（0=全部）")
    ap.add_argument("--since-hours", type=float, default=0,
                    help="只处理最近 N 小时内的决策（0=不限；常驻增量建议 6）")
    args = ap.parse_args()

    from review import reflection_checkers as rc
    from review import reflection_dimension_map as rdm

    conn = _open(readonly=not args.commit)
    sql = (
        "SELECT a.* FROM ai_decisions a "
        "WHERE COALESCE(a.analysis_degraded, 0) = 0 "
        "  AND NOT EXISTS (SELECT 1 FROM decision_errors de "
        "                  WHERE de.decision_id = a.id) "
    )
    cutoff = ""
    if args.since_hours > 0:
        from datetime import datetime, timedelta
        cutoff = (datetime.now() - timedelta(hours=args.since_hours)).strftime(
            "%Y-%m-%d %H:%M:%S")
        # created_at 两种写法并存（带 T / 带空格），统一后再比，避免字典序把 T 当成更大
        sql += ("  AND replace(substr(a.created_at, 1, 19), 'T', ' ') >= ? ")
    sql += "ORDER BY a.id DESC"
    if args.limit > 0:
        sql += f" LIMIT {int(args.limit)}"
    rows = [dict(r) for r in conn.execute(sql, (cutoff,) if cutoff else ())]
    if cutoff:
        print(f"[scope] 只处理 created_at >= {cutoff}（--since-hours {args.since_hours}）")

    n_dec = 0
    n_err = 0
    n_dec_with_err = 0
    dims: collections.Counter = collections.Counter()
    codes: collections.Counter = collections.Counter()

    for d in rows:
        n_dec += 1
        try:
            errs = rc.run_all_checkers(d, conn=conn)
        except Exception as exc:                      # 单条失败不阻断整批
            print(f"  [skip] id={d.get('id')} {type(exc).__name__}: {exc}")
            continue
        if not errs:
            continue
        n_dec_with_err += 1
        n_err += len(errs)
        for e in errs:
            codes[e.get("error_code")] += 1
        try:
            dim, sub = rdm.resolve_dimension(
                error_codes=[f"{e.get('error_code')}:{int(e.get('severity') or 0)}"
                             for e in errs]
            ) or ("", "")
            dims[f"{dim or '(空)'}/{sub or '(空)'}"] += 1
        except Exception:
            pass
        if args.commit:
            rc.save_errors(errs, conn=conn)

    print("\n候选决策（非降级 + 尚无错误码）: ", len(rows))
    print(f"能产出错误码: {n_dec_with_err} 条 / 共 {n_err} 个码")
    print("错误码 Top:")
    for code, n in codes.most_common(8):
        print(f"  - {code}: {n}")
    print("按 severity 优先归类后:")
    for k, v in dims.most_common(8):
        print(f"  - {k}: {v}")

    if args.commit:
        print(f"\n[commit] 已写入 {n_err} 个错误码（INSERT OR IGNORE，重复跑幂等）")
    else:
        print("\n[dry-run] 未写库。确认后加 --commit")
    conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
