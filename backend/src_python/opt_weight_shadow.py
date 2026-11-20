"""opt_weight_shadow.py —— 影子权重：把"异动/市场状态"接进权重的效果先算出来（v336 P1-3）

背景（实测 2026-11-14）：
  决策时点 4529 条配对里，六个乘子只有 impact(source) 在动：
    base     121 值 ρ=-0.033
    impact   173 值 ρ=+0.309   ← 唯一真正有区分力的
    consensus  4 值 ρ=+0.006
    source    25 值 ρ=-0.100
    reaction   1 值（恒 1.0）  ← 决策时点根本没传 abnormal
    regime     2 值 ρ=-0.182
    time       1 值（恒 1.0）  ← insert 时 age≈0，设计如此

  所以"六维权重"实际只有 1.5 维在工作。要补 reaction/regime 就必须改公式，
  而公式会真实影响下单权重。本脚本**不改任何东西**，只是把影子权重算出来，
  用同一套 Spearman 口径对比"接了之后到底有没有用"。

影子公式：
  weight_shadow = base × impact × source × reaction_proxy × regime_proxy
    · reaction_proxy：决策前 60 分钟内该品种是否有异动，方向一致 +/相反 −
    · regime_proxy  ：决策时点 market_style_snapshot 的 regime → 1.2/0.85/0.7
  （consensus 只有 4 个取值，先不进；time 在决策时点注定是 1.0，不进）

用法：
  python3 opt_weight_shadow.py
  python3 opt_weight_shadow.py --window 120
"""
from __future__ import annotations

import argparse
import bisect
import sqlite3
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

sys.path.insert(0, str(Path(__file__).resolve().parent))

BACKEND_DIR = Path(__file__).resolve().parents[1]
DB_PATH = BACKEND_DIR / "trident_event_bus.db"

from opt_weight_diag import MULT_COLS, spearman, bucket_means
from signals.signal_weight_evaluator import compose_weight

REGIME_MULT = {
    "trending_up": 1.2, "trending_down": 1.2, "trending": 1.2, "trend": 1.2,
    "range_bound": 0.85, "range": 0.85, "sideways": 0.85, "consolidation": 0.85,
    "volatile": 0.7, "high_vol": 0.7, "choppy": 0.7,
}


def load_decision_rows(conn: sqlite3.Connection) -> List[Dict[str, Any]]:
    cols = ", ".join(f"w.{c}" for c in MULT_COLS)
    rows = conn.execute(
        f"SELECT w.ai_decision_id, w.signal_weight, {cols}, "
        "       a.target_asset, a.ts, a.suggested_action, a.forward_move_pct "
        "FROM signal_weight_history w JOIN ai_decisions a ON a.id = w.ai_decision_id "
        "WHERE w.trigger_kind='insert' AND a.forward_move_pct IS NOT NULL "
        # 与 S4 同源: 每个决策只取**最早**一条 insert 权重，否则同一决策的
        # 多条 insert 记录会被重复计数，把 ρ 稀释掉（实测 0.190 vs 0.288）
        "AND w.id=(SELECT MIN(w2.id) FROM signal_weight_history w2 "
        "WHERE w2.ai_decision_id=w.ai_decision_id AND "
        "w2.decision_space=w.decision_space AND w2.trigger_kind='insert')"
    ).fetchall()
    out = []
    for r in rows:
        d = {c: float(r[c] or 1.0) for c in MULT_COLS}
        d["id"] = int(r["ai_decision_id"])
        d["asset"] = str(r["target_asset"] or "").upper()
        d["ts"] = int(r["ts"] or 0)
        d["action"] = str(r["suggested_action"] or "").upper()
        d["weight"] = float(r["signal_weight"] or 0.0)
        d["abs_move"] = abs(float(r["forward_move_pct"] or 0.0))
        out.append(d)
    return out


def _index_moves(conn):
    per: Dict[str, Tuple[List[int], List[str]]] = {}
    try:
        rows = conn.execute("SELECT asset, ts, move_kind, move_pct "
                            "FROM critical_price_moves ORDER BY asset, ts").fetchall()
    except sqlite3.Error:
        return per
    for r in rows:
        a = str(r["asset"] or "").upper()
        e = per.setdefault(a, ([], []))
        e[0].append(int(r["ts"]))
        e[1].append(f"{r['move_kind']}|{float(r['move_pct'] or 0.0)}")
    return per


def _index_regime(conn):
    per: Dict[str, Tuple[List[int], List[str]]] = {}
    try:
        rows = conn.execute("SELECT asset, ts, regime FROM market_style_snapshot "
                            "ORDER BY asset, ts").fetchall()
    except sqlite3.Error:
        return per
    for r in rows:
        a = str(r["asset"] or "").upper()
        e = per.setdefault(a, ([], []))
        e[0].append(int(r["ts"]))
        e[1].append(str(r["regime"] or "").lower())
    return per


def reaction_proxy(idx, asset: str, ts: int, action: str,
                   window_sec: int) -> Tuple[Optional[float], Optional[str]]:
    """决策前 window 内该品种最近一次异动 → 乘子。无数据返回 (None, None)。"""
    e = idx.get(asset)
    if not e or not e[0]:
        return None, None
    poss, metas = e
    i = bisect.bisect_right(poss, ts) - 1
    if i < 0:
        return None, None
    if ts - poss[i] > window_sec:
        return None, None
    kind, pct = metas[i].split("|")
    pct = float(pct)
    if action == "BUY":
        same = pct > 0
    elif action == "SELL":
        same = pct < 0
    else:
        return 1.0, kind
    # 方向一致 → 顺势加分；相反 → 逆势扣分；幅度越大越极端
    strong = abs(pct) >= 1.5
    if same:
        return (1.3 if strong else 1.15), kind
    return (0.7 if strong else 0.85), kind


def regime_proxy(idx, asset: str, ts: int) -> Tuple[Optional[float], Optional[str]]:
    e = idx.get(asset)
    if not e or not e[0]:
        return None, None
    poss, metas = e
    i = bisect.bisect_right(poss, ts) - 1
    if i < 0:
        return None, None
    r = metas[i]
    m = REGIME_MULT.get(r)
    return (m, r) if m else (None, r)


def bucket_rho(rows: List[Dict[str, Any]], key: str) -> Tuple[Optional[float], int]:
    """S4 同口径：逐小时算"桶内超额"的 Spearman，再按样本量加权。

    为什么必须这样算：混合口径把平静时段(±0.2%)和剧烈时段(±3%)放一起排秩，
    小时之间的量级差会盖过同一小时内权重差异的效果。
    """
    groups: Dict[int, List[Dict[str, Any]]] = {}
    for d in rows:
        groups.setdefault(d["ts"] // 3600, []).append(d)
    num = 0.0
    den = 0
    used = 0
    for _, g in groups.items():
        if len(g) < 3:
            continue
        ys = sorted(d["abs_move"] for d in g)
        med = ys[len(ys) // 2]
        xs = [d[key] for d in g]
        yy = [d["abs_move"] - med for d in g]
        r = spearman(xs, yy)
        if r is None:
            continue
        num += r * len(g)
        den += len(g)
        used += 1
    return (round(num / den, 3) if den else None), used


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--window", type=int, default=60, help="回看异动窗口（分钟）")
    args = ap.parse_args()

    conn = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True, timeout=30)
    conn.row_factory = sqlite3.Row

    rows = load_decision_rows(conn)
    if not rows:
        print("没有可用的决策时点权重配对")
        return 1
    moves = _index_moves(conn)
    regimes = _index_regime(conn)

    win = args.window * 60
    cov_react = cov_regime = 0
    for d in rows:
        rm, kind = reaction_proxy(moves, d["asset"], d["ts"], d["action"], win)
        gm, reg = regime_proxy(regimes, d["asset"], d["ts"])
        d["r_mult"] = rm
        d["g_mult"] = gm
        d["r_kind"] = kind
        d["regime"] = reg
        if rm is not None:
            cov_react += 1
        if gm is not None:
            cov_regime += 1
        # 用**生产公式**从历史乘子重算两档，做真正的 A/B（不是近似）
        d["w_legacy"] = compose_weight(
            d["base_mult"], d["impact_mult"], d["consensus_mult"], d["source_mult"],
            d["reaction_mult"], d["regime_mult"], d["time_mult"],
            profile="legacy")["weight"]
        d["w_impact_led"] = compose_weight(
            d["base_mult"], d["impact_mult"], d["consensus_mult"], d["source_mult"],
            d["reaction_mult"], d["regime_mult"], d["time_mult"],
            profile="impact_led")["weight"]
        # 对照组：只用现有三维相乘（不接新输入）—— 用来剥离"换组合方式"带来的变化
        d["weight_control"] = d["base_mult"] * d["impact_mult"] * d["source_mult"]
        d["weight_shadow"] = (d["base_mult"] * d["impact_mult"] * d["source_mult"]
                              * (rm if rm is not None else 1.0)
                              * (gm if gm is not None else 1.0))

    ys = [d["abs_move"] for d in rows]
    rho_actual = spearman([d["weight"] for d in rows], ys)
    rho_control = spearman([d["weight_control"] for d in rows], ys)
    rho_shadow = spearman([d["weight_shadow"] for d in rows], ys)

    n = len(rows)
    print(f"决策时点配对 {n} 条 · 异动回看 {args.window} 分钟")
    print(f"  可用输入覆盖: reaction {cov_react}/{n} · regime {cov_regime}/{n}")
    print(f"  Spearman ρ（权重 vs |实际波动|）")
    print(f"    现有权重       {rho_actual}")
    print(f"    对照组(三维)   {rho_control}")
    print(f"    影子权重       {rho_shadow}")
    if rho_actual is not None and rho_shadow is not None:
        print(f"    影子-对照      {round(rho_shadow - (rho_control or 0), 3)}  ← 新输入的真实增量")

    # 分桶单调性（都是正值越大越好）
    for label, key in (("现有", "weight"), ("影子", "weight_shadow")):
        means = bucket_means([d[key] for d in rows], ys)
        print(f"  {label}权重三分桶 |波动| 均值: {means}")

    print("\n  S4 同口径（逐小时桶内超额，按样本量加权）:")
    for label, key in (("库里的 signal_weight", "weight"),
                       ("公式重算 legacy", "w_legacy"),
                       ("公式重算 impact_led", "w_impact_led"),
                       ("对照组(三维)", "weight_control"),
                       ("影子(接异动/状态)", "weight_shadow")):
        r, used = bucket_rho(rows, key)
        print(f"    {label:<18} rho={r}  小时桶={used}")

    # 影子权重的实际分布（看看会不会把权重整体推高/压低）
    vals = sorted(d["weight_shadow"] for d in rows)
    if vals:
        print(f"  影子权重分布: p10={round(vals[n//10],2)} p50={round(vals[n//2],2)} "
              f"p90={round(vals[9*n//10],2)} max={round(vals[-1],2)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
