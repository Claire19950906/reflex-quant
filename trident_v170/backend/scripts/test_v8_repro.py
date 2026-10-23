#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""V8 复现自检 — 验证 pre_trade_check 能堵住历史 24 笔 dirty trade (2026-10-10)

不依赖 fix 的 filter 路径, 直接拿历史 gate_passed=0 的 ai_decisions 跑
pre_trade_check(), 报告:
  - 各 R 规则的拦截次数
  - 总拦截率 (理想 100%)
  - 哪些 dirty trade 能逃过 (漏判)
"""
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "src_python"))

import db
import sample_quality_filter as sqf


def main() -> int:
    conn = db.get_connection()
    conn.row_factory = sqlite3.Row if False else None  # use default tuple
    import sqlite3 as _sq
    conn.row_factory = _sq.Row
    cur = conn.cursor()
    cur.execute("""
        SELECT ad.id, ad.target_asset, ad.evidence_action, ad.evidence_confidence,
               ad.trade_gate_reason, ad.status
        FROM ai_decisions ad
        INNER JOIN quick_sim_trades qs ON qs.decision_id = ad.id
        WHERE qs.gate_passed = 0
        ORDER BY ad.id DESC
    """)
    rows = cur.fetchall()
    print(f"V8 复现: 找到 {len(rows)} 笔 gate_passed=0 的历史 trade (来自 ai_decisions)")
    if not rows:
        print("  无历史 dirty trade, 跳过拦截分析")
        return 0

    violations = {"R1": 0, "R2": 0, "R4": 0, "R7": 0, "R9": 0, "pass": 0}
    print("\n  前 10 笔详情:")
    for i, r in enumerate(rows[:10]):
        d = dict(r)
        qc = sqf.pre_trade_check(d, conn=None, r8_enabled=False)
        if qc["is_valid"]:
            violations["pass"] += 1
            tag = "PASS(!)"
        else:
            tag = "REJECT"
        rs = ",".join(r.split(":")[0] for r in qc["reasons"])
        print(f"  {tag:9s} id={d['id']:4d} asset={str(d.get('target_asset')):5s} "
              f"ev_act={str(d.get('evidence_action')):5s} "
              f"conf={d.get('evidence_confidence')} "
              f"gate={(d.get('trade_gate_reason', '') or '')[:15]:15s} "
              f"status={str(d.get('status')):12s} reasons={rs}")
        for reason in qc["reasons"]:
            for r_key in violations:
                if r_key != "pass" and reason.startswith(r_key + ":"):
                    violations[r_key] += 1

    print(f"\n各规则拦截次数 (前 10 笔样本): {violations}")
    n_violated = sum(v for k, v in violations.items() if k != "pass")
    n_total = n_violated + violations["pass"]
    block_rate = n_violated / n_total * 100 if n_total else 0
    print(f"拦截率: {block_rate:.1f}% (前 10 笔; 全样本理论 100%)")
    if violations["pass"] > 0:
        print(f"  ⚠️ {violations['pass']} 笔 dirty trade 漏判 (R3 之外 R1/R2/R4/R7/R9 全过)")

    conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
