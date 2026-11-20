#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""recalc_regime_weight.py — 用补齐后的 regime 重算信号权重 (v337)

背景
----
`_regime_mult` 原来不认 `volatile_high` / `volatile_low` / `mean_revert`
这三个实际在库里流通的标签，全部落到 1.0 → `signal_weight_history` 的
regime_mult 只有 2 个取值（1.0 占 90.5%、1.2 占 9.5%），这就是"乘子坍缩"。
标签映射修好后，历史行仍是旧值；本脚本把**旧行按同一份 compose_weight 纯函数
重算一遍**（其余 6 维保持库里的原值不变，只替换 regime 这一维），
让留痕与实际口径一致，可复算、可审计。

只处理 `regime_mult=1.0` 且决策已补出 regime 的行，不动其它行。

用法:
    python3 recalc_regime_weight.py --dry-run
    python3 recalc_regime_weight.py --commit
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parents[2] / "src_python"
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

import db # noqa: E402
from signals.signal_weight_evaluator import _regime_mult, compose_weight, WEIGHT_PROFILE  # noqa: E402


def run(commit: bool = False, limit: int = 0) -> dict:
    conn = db.get_connection()
    stats = {"scanned": 0, "changed": 0, "before": {}, "after": {}}
    try:
        cur = conn.cursor()
        for r in cur.execute(
            "SELECT regime_mult AS m, COUNT(*) AS n FROM signal_weight_history GROUP BY 1"
        ).fetchall():
            stats["before"][str(r[0])] = r[1]
        rows = conn.execute(
            """SELECT w.id, w.base_mult, w.consensus_mult, w.source_mult,
                      w.reaction_mult, w.time_mult, w.impact_mult,
                      w.regime_mult, d.regime_at_decision
                 FROM signal_weight_history w
                 JOIN ai_decisions d ON d.id = w.decision_id
                WHERE w.regime_mult = 1.0
                  AND COALESCE(d.regime_at_decision,'') <> ''"""
        ).fetchall()
        if limit:
            rows = rows[:limit]
        for rid, base, cons, src, react, tm, imp, old_reg, regime in rows:
            stats["scanned"] += 1
            new_reg = _regime_mult(regime)
            if abs(new_reg - 1.0) < 1e-9:
                continue
            comp = compose_weight(
                float(base or 1.0), float(imp or 1.0), float(cons or 1.0),
                float(src or 1.0), float(react or 1.0), new_reg, float(tm or 1.0),
                profile=WEIGHT_PROFILE,
            )
            stats["changed"] += 1
            if commit:
                cur.execute(
                    """UPDATE signal_weight_history
                          SET regime_mult = ?, signal_weight = ?
                        WHERE id = ?""",
                    (round(new_reg, 6), round(float(comp["weight"]), 6), rid),
                )
                if stats["changed"] % 500 == 0:
                    conn.commit()
        if commit:
            conn.commit()
            for r in cur.execute(
                "SELECT regime_mult AS m, COUNT(*) AS n FROM signal_weight_history GROUP BY 1"
            ).fetchall():
                stats["after"][str(r[0])] = r[1]
    finally:
        conn.close()
    return stats


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--commit", action="store_true")
    ap.add_argument("--limit", type=int, default=0)
    a = ap.parse_args()
    out = run(commit=a.commit, limit=a.limit)
    print(f"[recalc-regime] scanned={out['scanned']} changed={out['changed']} commit={a.commit}")
    print(f"  before: {out['before']}")
    if out["after"]:
        print(f"  after : {out['after']}")
