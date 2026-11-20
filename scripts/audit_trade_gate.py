#!/usr/bin/env python3
"""成交漏斗体检：决策 → 闸门 → 模拟单（只读）。

为什么需要它：
    实测 ai_decisions 327 条里，只有 2 条的 evidence_confidence ≥ 闸门阈值(20)，
    而这 2 条正好就是全部 quick_sim 成交 —— 也就是说"成交率 0.6%"并不是策略挑剔，
    而是卡在一个**99% 的决策根本没有填**的字段上。

    quick_sim 的选股 SQL 里有一句 `COALESCE(ad.evidence_confidence,0) >= GATE`，
    而 evidence_confidence 既不在 _LLM_TRACKED_FIELDS、也不在 FIELD_WEIGHTS，
    missing_fields 里一次都没出现过 —— 它是个**没有生产者、只有消费者**的孤儿字段。

用法：
    python3 scripts/audit_trade_gate.py [--json]
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys

sys.path.insert(0, "backend/src_python")
import config
import db


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()

    gate = float(getattr(config, "EVIDENCE_CONFIDENCE_GATE", 0) or 0)
    conn = sqlite3.connect(f"file:{db.get_db_path()}?mode=ro", uri=True, timeout=30)

    total = conn.execute("SELECT COUNT(*) FROM ai_decisions").fetchone()[0]
    conf0 = conn.execute(
        "SELECT COUNT(*) FROM ai_decisions WHERE COALESCE(evidence_confidence,0)=0"
    ).fetchone()[0]
    passed = conn.execute(
        "SELECT COUNT(*) FROM ai_decisions WHERE COALESCE(evidence_confidence,0)>=?",
        (gate,)).fetchone()[0]
    try:
        trades = conn.execute("SELECT COUNT(*) FROM quick_sim_trades").fetchone()[0]
    except sqlite3.OperationalError:
        trades = 0
    # 通过了闸门但 action 仍是 HOLD 的（过了闸门也没下单）
    try:
        passed_hold = conn.execute(
            "SELECT COUNT(*) FROM ai_decisions WHERE COALESCE(evidence_confidence,0)>=? "
            "AND UPPER(COALESCE(suggested_action,'')) IN ('HOLD','')", (gate,)).fetchone()[0]
    except sqlite3.OperationalError:
        passed_hold = 0

    out = {
        "gate_threshold": gate,
        "decisions_total": total,
        "confidence_zero": conf0,
        "gate_passed": passed,
        "gate_pass_rate": round(passed / total, 4) if total else 0,
        "passed_but_hold": passed_hold,
        "sim_trades": trades,
        "bottleneck": (
            "闸门依赖 evidence_confidence，而该字段 99% 的决策未填 → 成交被卡死在个位数"
            if total and passed / total < 0.05 else "闸门通过率正常"
        ),
    }
    if args.json:
        print(json.dumps(out, ensure_ascii=False, indent=1))
        return 0

    print(f"闸门阈值 EVIDENCE_CONFIDENCE_GATE = {gate}")
    print(f"决策总数            : {total}")
    print(f"  confidence = 0    : {conf0} ({conf0 / total:.1%})" if total else "")
    print(f"  通过闸门          : {passed} ({passed / total:.1%})" if total else "")
    print(f"  通过但仍是 HOLD   : {passed_hold}")
    print(f"模拟成交            : {trades}")
    print(f"\n判定：{out['bottleneck']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
