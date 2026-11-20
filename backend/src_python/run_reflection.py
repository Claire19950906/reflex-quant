#!/usr/bin/env python3
"""反思 runner —— 程序化校验器 + LLM 盲区反思 对一组 ai_decisions 一次性跑。

用法:
  python3 run_reflection.py --limit 50                # 默认最近 50 条决策
  python3 run_reflection.py --scope unsettled --limit 100
  python3 run_reflection.py --decision-id 123          # 单条
  python3 run_reflection.py --skip-llm               # 只跑程序化校验器
  python3 run_reflection.py --auto-promote           # LLM 盲区 rule 自动写到 review_insights (active=0)

不影响 review_agent.py / engine.py —— 单独运行的反思通道。
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from typing import List, Dict, Any

# 让 Python 找到 backend/src_python 的模块
import os
THIS_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, THIS_DIR)

import db
from review import reflection_checkers as rc
import blindspot_reflector as br


def _fetch_decisions(scope: str, limit: int, decision_id: int = None) -> List[Dict[str, Any]]:
    """根据 scope 拉 ai_decisions 行（dict 形态）。"""
    conn = db.get_connection()
    conn.row_factory = db.sqlite3.Row if hasattr(db, "sqlite3") else None
    import sqlite3
    conn.row_factory = sqlite3.Row

    if decision_id is not None:
        rows = conn.execute(
            "SELECT * FROM ai_decisions WHERE id = ?", (decision_id,)
        ).fetchall()
    elif scope == "unsettled":
        rows = conn.execute(
            """SELECT * FROM ai_decisions
               WHERE settled = 0 AND suggested_action IN ('BUY','SELL')
               ORDER BY id DESC LIMIT ?""",
            (limit,),
        ).fetchall()
    elif scope == "today":
        rows = conn.execute(
            """SELECT * FROM ai_decisions
               WHERE date(created_at, 'localtime') = date('now', 'localtime')
               ORDER BY id DESC LIMIT ?""",
            (limit,),
        ).fetchall()
    else:  # recent / all
        rows = conn.execute(
            "SELECT * FROM ai_decisions ORDER BY id DESC LIMIT ?",
            (limit,),
        ).fetchall()

    return [dict(r) for r in rows]


def run(limit: int = 50, scope: str = "recent", decision_id: int = None,
        skip_llm: bool = False, auto_promote: bool = False,
        require_min_clean: int = 0) -> Dict[str, Any]:
    """反思系统主入口 (v202+: 加 require_min_clean 硬门槛)

    Args:
        limit, scope, decision_id, skip_llm, auto_promote: 同 v200
        require_min_clean: 清洁样本最小阈值, 0 = 不强制 (默认)
                          >0 时如果清洁样本 < 阈值, 不跑反思 (raise RuntimeError)
                          这是反思系统的"硬门槛"防御, 防止基于事故样本下结论
    """
    started = time.time()
    decisions = _fetch_decisions(scope, limit, decision_id)
    print(f"[reflection] scope={scope} decisions={len(decisions)} skip_llm={skip_llm}")

    # v202+: 样本质量 audit (反思系统入口硬门槛)
    conn = db.get_connection()
    import sqlite3
    conn.row_factory = sqlite3.Row

    try:
        import sample_quality_filter as sqf
        audit = sqf.audit_trades(conn=conn)
        n_valid = audit['valid']
        n_total = audit['total']
        gate = sqf.require_min_clean_samples(n_valid, min_required=require_min_clean)
        print(f"[reflection-audit] clean={n_valid}/{n_total} by_reason={audit['by_reason']}")
        print(f"[reflection-gate] allowed={gate['allowed']} reason={gate['reason']}")
        if require_min_clean > 0 and not gate['allowed']:
            raise RuntimeError(
                "反思系统硬门槛触发: 清洁样本 {} < 阈值 {}. 拒绝跑反思. 建议: "
                "(1) 修复 gate / K 线 fallback 等系统 bug; "
                "(2) 等清洁样本足够; "
                "(3) 临时设 require_min_clean=0 跳过门槛.".format(n_valid, require_min_clean)
            )
    except ImportError:
        # sample_quality_filter 不可用, 跳过 audit
        print("[reflection-audit] sample_quality_filter 不可用, 跳过 audit")

    total_checker_errs = 0
    total_llm_errs = 0
    total_insights = 0
    per_decision: List[Dict[str, Any]] = []

    for dec in decisions:
        did = dec.get("id")
        # 1) 程序化校验器
        checker_errs = rc.run_all_checkers(dec, conn=conn)
        inserted = rc.save_errors(checker_errs, conn=conn)
        total_checker_errs += inserted

        # 2) LLM 盲区反思 (可选)
        llm_result = {}
        if not skip_llm:
            llm_result = br.reflect_and_persist(dec, conn=conn, auto_promote=auto_promote)
            total_llm_errs += llm_result.get("decision_errors_inserted", 0)
            total_insights += len(llm_result.get("insight_ids_created", []))

        per_decision.append({
            "decision_id": did,
            "action": dec.get("suggested_action"),
            "asset": dec.get("target_asset"),
            "checker_errors": len(checker_errs),
            "checker_inserted": inserted,
            "checker_codes": [e["error_code"] for e in checker_errs],
            "llm_blindspots": llm_result.get("blindspots_found", 0) if not skip_llm else "n/a",
            "llm_inserted": llm_result.get("decision_errors_inserted", 0) if not skip_llm else 0,
        })

    # 抽最近的 5 个错误码作为注入 prompt 的"前车之鉴"
    recent = rc.get_recent_errors(limit=8, conn=conn)
    prompt_inject = rc.format_errors_for_prompt(recent)

    elapsed = round(time.time() - started, 2)
    summary = {
        "elapsed_seconds": elapsed,
        "decisions_processed": len(decisions),
        "checker_errors_total": total_checker_errs,
        "llm_blindspots_total": total_llm_errs,
        "new_insights_created": total_insights,
        "auto_promote": auto_promote,
        "per_decision": per_decision[:30],
        "prompt_inject_preview": prompt_inject,
    }
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return summary


def main():
    ap = argparse.ArgumentParser(description="反思 runner (program checkers + LLM blindspot)")
    ap.add_argument("--limit", type=int, default=50, help="limit")
    ap.add_argument("--scope", choices=["recent", "today", "unsettled"], default="recent")
    ap.add_argument("--decision-id", type=int, default=None, help="single decision")
    ap.add_argument("--skip-llm", action="store_true", help="only run program checkers")
    ap.add_argument("--auto-promote", action="store_true", help="auto-write LLM rules to review_insights (active=0)")
    ap.add_argument("--require-min-clean", type=int, default=0,
                    help="v202+: 清洁样本最小阈值, 0=不强制, >0=不足时 raise RuntimeError")
    args = ap.parse_args()
    run(limit=args.limit, scope=args.scope, decision_id=args.decision_id,
        skip_llm=args.skip_llm, auto_promote=args.auto_promote,
        require_min_clean=args.require_min_clean)


if __name__ == "__main__":
    main()