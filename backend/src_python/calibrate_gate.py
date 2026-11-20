#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
calibrate_gate.py — v321.3 P1: 闸门**分组**校准 (需求 #1)
================================================================================

问题
----
闸门「整体拒对率 ≈ 基础概率」(实测 81.0% vs 81.9%, 提升 **+0.03pp**) 看起来
毫无价值 —— 拦和不拦一样准。

但那是**整体**数字, 它把分组差异抹平了。用实际结算结果按分组算 (实测 321 条
有真实结果的决策):

    WTI  命中率 53.7%  基础 48.3%  →  **+5.42pp**
    XAU  命中率 50.3%  基础 47.4%  →  **+2.93pp**
    BTC  命中率 52.5%  基础 50.8%  →  **+1.64pp**
    ETH  命中率 37.5%  基础 52.8%  →  **-15.31pp**

结论很明确:
  · 闸门在 ETH 上**拦得对** —— 那里的信号系统性反向, 拦掉是价值;
  · 闸门在 WTI/XAU/BTC 上**拦的是好信号** —— 拦掉它们是在毁掉有正增量的判断。

所以闸门不该是「一刀切」, 而该**按资产×方向**用各自的判别力。

做法
----
1. 扫 ai_decisions 里**有方向 + 有真实结算结果**的决策 (forward_move_pct
   是 decision_truth 用真实 K 线回填的, 不是估计值);
2. 按 (asset, action) 算命中率, 与**同方向基础概率**对比 (基础概率用同一样本
   集里"涨的占比/跌的占比"估计 → 同口径对比);
3. 样本不足 (< MIN_SAMPLES) 的分组**不写表** —— 宁可没有, 不用小样本硬调;
4. 落表 gate_calibration, 由 decision_guard 读取。

诚实边界
--------
· 命中率/基础概率都是**已结算的实际结果**, 不是模型估计;
· 分组样本只有 50~150 条 —— 属于"够看方向、不够做精细调参"的量级, 所以
  本脚本**只产出建议**, 是否采纳由 applied 标记控制, 不擅自改交易阈值;
· 窗口默认 180 天, 覆盖市场状态切换 (不同 regime 下同一资产的判别力可能反号)。

用法
----
    python3 calibrate_gate.py --dry-run      # 只看建议, 不写库
    python3 calibrate_gate.py                # 落库
    python3 calibrate_gate.py --apply        # 落库 + 标记为已被闸门采纳
"""
from __future__ import annotations

import argparse
import math
import os
import sqlite3
import sys
import time
from collections import defaultdict
from typing import Any, Dict, List, Tuple

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

DB_PATH = os.environ.get("TRIDENT_DB_PATH") or os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "trident_event_bus.db",
)

MIN_SAMPLES = 30          # 低于这个样本数不写表
DEFAULT_WINDOW_DAYS = 180
POSITIVE_LIFT_PP = 1.0    # 高于它 → 正增量 (闸门不该拦)
NEGATIVE_LIFT_PP = -3.0   # 低于它 → 系统性反向 (闸门应更严)
# 显著性门槛: lift 的 95% 置信下界必须 > 0 (正增量) / 上界 < 0 (负增量)
# 才允许 applied=1 去改交易门槛。
#
# 为什么必须加这道闸: 分组样本只有 30~109 条, lift 的标准误约
# sqrt(p*(1-p)/n) ≈ 4~9 个百分点 —— 实测 XAU BUY (+5.38pp, n=109) 的
# 95% 置信下界仍是 **-4.1pp** (跨过 0) → **统计上不显著**。
# 拿不显著的差异去改放行门槛 = 用噪音改交易行为。
# 所以: 不显著 → 只记录 (applied=0, 供研究端看), **不影响交易**;
#       样本攒够 → 下次校准自动生效。这跟项目里其它"建议需审批"的链路一致。
Z95 = 1.96


def _significant(hits: int, n: int, base_rate: float) -> Tuple[bool, bool, float]:
    """返回 (显著正, 显著负, lift 的 95% 置信下界 pp)。

    用二项比例的标准误近似: SE = sqrt(p0*(1-p0)/n);
    lift 的 95% 置信下界 = (hit_rate - base_rate) - Z95*SE。
    显著负 = lift 的**上界** < 0, 即 (hit-base) + Z95*SE < 0。
    """
    if n <= 0:
        return False, False, 0.0
    hit_rate = hits / n
    lift_pp = (hit_rate - base_rate) * 100.0
    se = math.sqrt(max(base_rate * (1.0 - base_rate), 1e-9) / n)
    lower_pp = lift_pp - Z95 * se * 100.0
    upper_pp = lift_pp + Z95 * se * 100.0
    return (lower_pp > 0.0), (upper_pp < 0.0), lower_pp


def _base_rates(rows: List[Dict[str, Any]]) -> Dict[str, float]:
    """同方向基础概率: 该方向上市场随机走的概率 (同一样本集, 同口径)。"""
    out: Dict[str, float] = {}
    for act in ("BUY", "SELL"):
        sub = [r for r in rows if r["act"] == act]
        if not sub:
            continue
        if act == "BUY":
            out[act] = sum(1 for r in sub if r["mv"] > 0) / len(sub)
        else:
            out[act] = sum(1 for r in sub if r["mv"] < 0) / len(sub)
    return out


def compute(conn: sqlite3.Connection, window_days: int = DEFAULT_WINDOW_DAYS
            ) -> Dict[str, Any]:
    cutoff = int(time.time()) - window_days * 86400
    raw = conn.execute(
        "SELECT target_asset asset, suggested_action act, forward_move_pct mv "
        "FROM ai_decisions "
        "WHERE suggested_action IN ('BUY','SELL') "
        "  AND forward_move_pct IS NOT NULL AND ts >= ?",
        (cutoff,),
    ).fetchall()
    rows = [dict(r) for r in raw]
    base = _base_rates(rows)

    groups: Dict[Tuple[str, str], List[Dict[str, Any]]] = defaultdict(list)
    for r in rows:
        groups[(str(r["asset"] or "").upper(), r["act"])].append(r)

    out: List[Dict[str, Any]] = []
    for (asset, act), rs in sorted(groups.items()):
        if not asset or len(rs) < MIN_SAMPLES:
            continue
        hits = sum(1 for x in rs if (x["mv"] > 0 if act == "BUY" else x["mv"] < 0))
        hit_rate = hits / len(rs)
        base_rate = base.get(act, 0.5)
        lift = (hit_rate - base_rate) * 100.0
        avg_abs = sum(abs(x["mv"]) for x in rs) / len(rs)
        sig_pos, sig_neg, lower_pp = _significant(hits, len(rs), base_rate)
        if sig_pos:
            note = f"显著正增量 (95%置信下界 {lower_pp:+.1f}pp): 闸门不应拦该分组"
        elif sig_neg:
            note = f"显著负增量 (95%置信上界 {lower_pp:+.1f}pp): 信号系统性反向, 闸门应更严"
        elif lift >= POSITIVE_LIFT_PP:
            note = f"正增量但不显著 (95%置信下界 {lower_pp:+.1f}pp): 仅记录, 不改门槛"
        elif lift <= NEGATIVE_LIFT_PP:
            note = f"负增量但不显著 (95%置信上界 {lower_pp:+.1f}pp): 仅记录, 不改门槛"
        else:
            note = "无显著判别力: 维持默认门槛"
        out.append({
            "asset": asset, "action": act, "n_samples": len(rs),
            "n_hits": hits, "hit_rate": round(hit_rate, 4),
            "base_rate": round(base_rate, 4), "lift_pp": round(lift, 3),
            "lift_ci95_low_pp": round(lower_pp, 3),
            "significant": bool(sig_pos or sig_neg),
            "avg_abs_move": round(avg_abs, 4), "window_days": window_days,
            "note": note,
        })
    return {"rows": out, "base_rates": base, "n_decisions": len(rows),
            "cutoff": cutoff}


def persist(conn: sqlite3.Connection, rows: List[Dict[str, Any]],
            apply: bool = False, force: bool = False) -> int:
    if not rows:
        return 0
    now = int(time.time())
    # applied=1 只给**统计显著**的分组 —— 不显著的只记录, 不改交易门槛。
    # --force-apply: 用户明确拍板「采信这批分组差异」(即使统计不显著)。
    # 这是**人的决策**, 不是系统的自作主张 —— 所以要显式开关, 且留痕在 note 里。
    conn.executemany(
        "INSERT INTO gate_calibration "
        "(asset, action, n_samples, n_hits, hit_rate, base_rate, lift_pp, "
        " avg_abs_move, window_days, computed_at, applied, note) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?) "
        "ON CONFLICT(asset, action) DO UPDATE SET "
        " n_samples=excluded.n_samples, n_hits=excluded.n_hits, "
        " hit_rate=excluded.hit_rate, base_rate=excluded.base_rate, "
        " lift_pp=excluded.lift_pp, avg_abs_move=excluded.avg_abs_move, "
        " window_days=excluded.window_days, computed_at=excluded.computed_at, "
        " applied=excluded.applied, note=excluded.note",
        [(r["asset"], r["action"], r["n_samples"], r["n_hits"], r["hit_rate"],
          r["base_rate"], r["lift_pp"], r["avg_abs_move"], r["window_days"],
          now,
          1 if ((apply and r.get("significant")) or force) else 0,
          (r["note"] + (" [强制采信: 用户显式确认]" if force else "")))
         for r in rows],
    )
    conn.commit()
    return len(rows)


def main() -> int:
    ap = argparse.ArgumentParser(description="闸门分组校准")
    ap.add_argument("--db", default=DB_PATH)
    ap.add_argument("--days", type=int, default=DEFAULT_WINDOW_DAYS)
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--apply", action="store_true",
                    help="标记为已被闸门采纳 (仅统计显著的分组)")
    ap.add_argument("--force-apply", action="store_true",
                    help="强制采信全部分组 (即使统计不显著) —— 用户的显式决策")
    args = ap.parse_args()
    conn = sqlite3.connect(args.db, timeout=120)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout=60000")
    try:
        try:
            import db as _db
            _db.migrate(conn)
        except Exception as exc:
            print(f"[WARN] migrate 跳过: {type(exc).__name__}: {exc}", flush=True)
        st = compute(conn, window_days=args.days)
        print(f"[CALIBRATE] 窗口 {args.days} 天, 参与统计的已结算方向决策 {st['n_decisions']} 条")
        print("[CALIBRATE] 基础概率: " + ", ".join(
            f"{k}={v:.1%}" for k, v in st["base_rates"].items()))
        print(f'  {"资产":<8}{"方向":<6}{"n":>5}{"命中":>9}{"基础":>9}{"提升pp":>9}  说明')
        for r in st["rows"]:
            print(f'  {r["asset"]:<8}{r["action"]:<6}{r["n_samples"]:>5}'
                  f'{r["hit_rate"]:>9.1%}{r["base_rate"]:>9.1%}{r["lift_pp"]:>+9.2f}  {r["note"]}')
        if not args.dry_run:
            n = persist(conn, st["rows"], apply=args.apply, force=args.force_apply)
            print(f'[CALIBRATE] 已写入 gate_calibration {n} 行'
                  f'{"(强制采信全部)" if args.force_apply else "(仅统计显著的 applied=1)"}')
        else:
            print("[CALIBRATE] dry-run, 未写库")
    finally:
        conn.close()
    return 0


__all__ = ["compute", "persist", "MIN_SAMPLES"]

if __name__ == "__main__":
    raise SystemExit(main())
