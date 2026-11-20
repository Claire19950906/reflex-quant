"""v339: 「反思一键生效」批量落地脚本（默认 dry-run）。

背景：`strategy_param_adjustments` 积压 98 条 pending，其中 100% 的 param_key
带命名空间前缀（score./sizing./filter.），审批端白名单不认 → 点「批准」恒 400；
即便认了，`add_version(source='reflection')` 还有「每天 1 个版本」的硬限制，
逐条应用要 98 天。

用法（必须用带 dotenv 的解释器）
----
  PY=/Library/Frameworks/Python.framework/Versions/3.14/bin/python3
  $PY apply_param_backlog.py                 # 只看方案
  $PY apply_param_backlog.py --json          # 机器可读
  $PY apply_param_backlog.py --apply         # 真写库
  $PY apply_param_backlog.py --apply --max-age-days 14
"""

from __future__ import annotations

import argparse
import json
import sys

import db
import param_adjust_apply as _pa


def _fmt(v) -> str:
    return "—" if v is None else str(v)


def print_plan(plan: dict) -> None:
    s = plan["summary"]
    print(f"新鲜 pending(<={plan['max_age_days']}天): {plan['fresh_pending']}    "
          f"过期 pending: {plan['stale_pending']}（应用时将标 expired）")
    print(f"可映射分组: {s['mappable_groups']}  "
          f"(settings {s['settings_groups']} / 策略 {s['strategy_groups']})    "
          f"其中方向冲突已裁决: {s['conflict_groups']}    "
          f"不可映射: {s['unmappable_rows']}")
    print()
    print(f"{'参数(canonical)':32} {'桶':9} {'方向':5} {'幅度':6} {'条数':4} {'裁决'} ")
    print("-" * 92)
    for g in plan["groups"]:
        note = ""
        if g["conflict"]:
            w = g["direction_weights"]
            note = "冲突→选权重高者 " + " ".join(f"{k}:{v}" for k, v in w.items())
        print(f"{g['canonical_key']:32} {g['bucket']:9} {g['direction']:5} "
              f"{g['magnitude']:<6} {len(g['adjustment_ids']):<4} {note}")
    if plan["unmappable"]:
        print()
        print(f"不可自动应用（人工处理，{len(plan['unmappable'])} 条）:")
        reasons = {}
        for u in plan["unmappable"]:
            reasons.setdefault(u["param_key"], [0, u["reason"]])
            reasons[u["param_key"]][0] += 1
        for k, (n, why) in sorted(reasons.items()):
            print(f"  {k:44} {n:3} 条  {why}")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true", help="真写库（默认只预览）")
    ap.add_argument("--max-age-days", type=int, default=_pa.DEFAULT_MAX_AGE_DAYS,
                    help="超过该天数的 pending 判过期（默认 7）")
    ap.add_argument("--json", action="store_true", help="输出 JSON")
    args = ap.parse_args()

    conn = db.get_connection()
    # strategy_store.get_current_strategy 依赖 row_factory=Row（否则 row["x"] 报 TypeError），
    # 调用方必须先设好，这是该模块的隐含契约。
    import sqlite3 as _sq
    conn.row_factory = _sq.Row
    conn.execute("PRAGMA busy_timeout=60000")
    try:
        plan = _pa.build_plan(conn, max_age_days=args.max_age_days)
        res = _pa.apply_plan(conn, plan, dry_run=not args.apply)

        if args.json:
            print(json.dumps({"plan": plan, "result": res}, ensure_ascii=False,
                             indent=1, default=str))
            return 0

        print_plan(plan)
        print()
        print("=== %s ===" % ("已写入" if args.apply else "预览（未写库）"))
        for r in res["results"]:
            print(f"  {r['canonical_key']:32} {r['status']:12} "
                  f"{_fmt(r.get('old_value'))} → {_fmt(r.get('new_value'))}")
        if res.get("strategy_version_id"):
            print(f"  新策略版本: id={res['strategy_version_id']} (source=reflection)")
        if res.get("stale_expired"):
            print(f"  判过期: {res['stale_expired']} 条 → status='expired'")
        if not args.apply:
            print("\n确认无误后执行: %s apply_param_backlog.py --apply" % sys.executable)
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
