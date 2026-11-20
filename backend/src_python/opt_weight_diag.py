# -*- coding: utf-8 -*-
"""opt_weight_diag.py — v336 权重乘子的单因子标定

问题（实测）:
    signal_weight_history 49606 行, 但 6 个乘子里 4 个近乎常数:
        consensus 4 个取值(均值 1.004) / source 27(0.995) / regime 2(1.019) / time 7(0.789)
    也就是说"权重自动学习"实际上只学了 base(信号强度) 与 impact(影响力) 两维,
    其余四维在任何决策上都不改变排序 —— 它们只出现在 explain 文本里。

本模块做单因子拆解（只用**决策时点**权重, 不用事后回填, 避免循环论证）:
    对每个乘子 m:
      · 覆盖 n / 取值个数
      · Spearman(m, |forward_move_pct|)  —— 这个维度有没有"影响幅度"的区分力
      · Spearman(m, is_correct)          —— 有没有"方向对错"的区分力
      · 分桶均值：把 m 分低/中/高三档, 看 |move| 是否单调
    判据: |ρ| ≥ 0.05 且取值 ≥ 5 且分桶单调 → active；否则 dead（建议重标定或下线）。

只读。
"""
from __future__ import annotations

import argparse
import json
import sqlite3
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

BACKEND = Path(__file__).resolve().parents[1]
DB = BACKEND / "trident_event_bus.db"

MULT_COLS = ("base_mult", "impact_mult", "consensus_mult", "source_mult",
             "reaction_mult", "regime_mult", "time_mult")
RHO_MIN = 0.05
DISTINCT_MIN = 5


def _ro() -> sqlite3.Connection:
    c = sqlite3.connect(f"file:{DB}?mode=ro", uri=True, timeout=30)
    c.row_factory = sqlite3.Row
    return c


def spearman(xs: Sequence[float], ys: Sequence[float]) -> Optional[float]:
    n = len(xs)
    if n < 8:
        return None

    def ranks(vals):
        order = sorted(range(n), key=lambda i: vals[i])
        r = [0.0] * n
        i = 0
        while i < n:
            j = i
            while j + 1 < n and vals[order[j + 1]] == vals[order[i]]:
                j += 1
            avg = (i + j) / 2.0 + 1.0
            for k in range(i, j + 1):
                r[order[k]] = avg
            i = j + 1
        return r

    rx, ry = ranks(list(xs)), ranks(list(ys))
    mx, my = sum(rx) / n, sum(ry) / n
    num = sum((a - mx) * (b - my) for a, b in zip(rx, ry))
    dx = sum((a - mx) ** 2 for a in rx) ** 0.5
    dy = sum((b - my) ** 2 for b in ry) ** 0.5
    if dx == 0 or dy == 0:
        return None
    return round(num / (dx * dy), 3)


def load_pairs(conn: sqlite3.Connection) -> List[Dict[str, Any]]:
    cols = ", ".join(f"w.{c}" for c in MULT_COLS)
    rows = conn.execute(
        f"SELECT w.signal_weight, {cols}, a.forward_move_pct, a.is_correct "
        "FROM signal_weight_history w JOIN ai_decisions a ON a.id = w.ai_decision_id "
        "WHERE w.trigger_kind='insert' AND a.forward_move_pct IS NOT NULL"
    ).fetchall()
    out = []
    for r in rows:
        d = {c: float(r[c] or 0.0) for c in MULT_COLS}
        d["abs_move"] = abs(float(r["forward_move_pct"] or 0.0))
        grade = str(r["is_correct"] or "").upper()
        d["graded"] = grade in ("WIN", "LOSS")
        d["correct"] = 1 if grade == "WIN" else 0
        d["weight"] = float(r["signal_weight"] or 0.0)
        out.append(d)
    return out


def bucket_means(values: Sequence[float], ys: Sequence[float]) -> List[float]:
    """按 values 三分位切桶, 返回各桶 ys 均值（用于看单调性）。"""
    n = len(values)
    if n < 15:
        return []
    order = sorted(range(n), key=lambda i: values[i])
    cuts = [n // 3, 2 * n // 3]
    groups = [order[:cuts[0]], order[cuts[0]:cuts[1]], order[cuts[1]:]]
    means = []
    for g in groups:
        if not g:
            means.append(0.0)
        else:
            means.append(round(sum(ys[i] for i in g) / len(g), 3))
    return means


def diag(pairs: List[Dict[str, Any]]) -> Dict[str, Any]:
    graded = [p for p in pairs if p.get("graded")]
    out: Dict[str, Any] = {"n_pairs": len(pairs), "n_graded": len(graded), "factors": {}}
    for c in MULT_COLS:
        xs = [p[c] for p in pairs]
        absy = [p["abs_move"] for p in pairs]
        distinct = len({round(v, 4) for v in xs})
        rho_abs = spearman(xs, absy)
        rho_hit = None
        if len(graded) >= 30:
            rho_hit = spearman([p[c] for p in graded], [float(p["correct"]) for p in graded])
        means = bucket_means(xs, absy)
        monotone = (len(means) == 3 and means[0] < means[1] < means[2]) or \
                   (len(means) == 3 and means[0] > means[1] > means[2])
        # 判定不能只看单调性：S 形/U 形的分桶也会"末段回落"，但 ρ 仍然强。
        #   无方差（取值 < 5）        → dead（这一维在决策上根本没起作用）
        #   |ρ| ≥ 0.05               → active（有区分力）
        #   |ρ| < 0.02 且不单调       → dead
        #   其余                      → weak（方向对但证据薄）
        if distinct < DISTINCT_MIN:
            verdict = "dead"
        elif rho_abs is not None and abs(rho_abs) >= RHO_MIN:
            verdict = "active"
        elif rho_abs is not None and abs(rho_abs) < 0.02 and not monotone:
            verdict = "dead"
        else:
            verdict = "weak"
        out["factors"][c] = {
            "distinct": distinct, "rho_abs_move": rho_abs, "rho_hit": rho_hit,
            "bucket_abs_move": means, "monotone": monotone,
            "verdict": verdict,
        }
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()
    conn = _ro()
    try:
        rep = diag(load_pairs(conn))
    finally:
        conn.close()
    if args.json:
        print(json.dumps(rep, ensure_ascii=False, indent=2))
        return 0
    print(f"决策时点权重 × 真值配对 {rep['n_pairs']} 条（仅 trigger_kind='insert'）")
    print(f"{'乘子':<16}{'取值':>6}{'ρ(|幅度|)':>11}{'ρ(对错)':>10}  分桶|幅度|            判定")
    for c, v in rep["factors"].items():
        print(f"{c:<16}{v['distinct']:>6}{str(v['rho_abs_move']):>11}{str(v['rho_hit']):>10}  "
              f"{str(v['bucket_abs_move']):<22}{v['verdict']}")
    dead = [c for c, v in rep["factors"].items() if v["verdict"] == "dead"]
    weak = [c for c, v in rep["factors"].items() if v["verdict"] == "weak"]
    print(f"\n无区分力（建议重标定/下线）: {dead}")
    print(f"证据薄（方向对但 ρ 不足）: {weak}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
