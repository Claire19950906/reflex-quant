"""opt_impact_default.py —— impact_score 静默 5 分的代价，以及"接门卫分"能救回多少（只读）

背景（2026-11-14 实测）：
  ai_decisions 8865 条里 **8375 条（94.5%）的 impact_score 恰好 = 5.0**。
  这不是模型"判断中性"，而是字段没返回、按 default 兜底 —— 因为
  `_parse_result` 在模型缺字段时直接写入 5.0，落库后与"真的打了 5 分"无法区分。

  更关键的是：这 8375 条里有 **7680 条**对应的新闻在 `raw_news.gate_impact_1to100`
  里已经有门卫给的 0-100 分（最大值 95），却没有被用来替代默认值。
  只有 417 条决策的 impact_score 被门卫分改写过 —— 说明 v323 的兜底只在部分路径生效。

本脚本回答两个问题（全部只读）：
  1. 若用门卫分（/10）替换默认 5.0，base_mult 的分辨率会从多少升到多少？
  2. 权重分辨率（S14 口径）会从多少升到多少？

用法: python3 opt_impact_default.py
"""
from __future__ import annotations

import sqlite3
import sys
from pathlib import Path
from typing import Any, Dict, List

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src_python"))

BACKEND_DIR = Path(__file__).resolve().parents[2]
DB_PATH = BACKEND_DIR / "trident_event_bus.db"

from signals.signal_weight_evaluator import _base_mult  # noqa: E402


def load(conn: sqlite3.Connection) -> List[Dict[str, Any]]:
    rows = conn.execute(
        "SELECT d.id, d.sentiment_score s, d.heat_score h, d.impact_score i, "
        "       d.impact_magnitude m, d.target_asset a, d.ts, "
        "       r.gate_impact_1to100 g "
        "FROM ai_decisions d LEFT JOIN raw_news r ON r.id = d.news_id"
    ).fetchall()
    out = []
    for r in rows:
        out.append({
            "id": r["id"], "s": r["s"], "h": r["h"], "i": float(r["i"] or 5.0),
            "m": r["m"], "g": int(r["g"] or 0), "ts": int(r["ts"] or 0),
            "asset": str(r["a"] or "").upper(),
        })
    return out


def base_of(row: Dict[str, Any], impact: float) -> float:
    return _base_mult(row["s"], row["m"], row["h"], impact_score=impact)



def project_weight_effect(conn: sqlite3.Connection) -> None:
    """把"接门卫分"的收益投影到权重层面（只读、不改库）。

    做法：拿决策时点已落库的六个乘子不变，只把 base_mult 用门卫分重算一遍，
    再用生产公式合成权重，比较：
      · distinct(权重)   —— S14 口径
      · 逐小时桶内超额 ρ —— S4 口径
    """
    from signals.signal_weight_evaluator import compose_weight
    from opt_weight_shadow import bucket_rho

    rows = conn.execute(
        "SELECT h.signal_weight w, h.impact_mult im, h.consensus_mult c, h.source_mult s, "
        "       h.reaction_mult rt, h.regime_mult g, h.time_mult t, "
        "       a.sentiment_score sc, a.heat_score hs, a.impact_magnitude mg, "
        "       a.impact_score isc, r.gate_impact_1to100 gi, a.ts, "
        "       abs(a.forward_move_pct) mv "
        "FROM signal_weight_history h JOIN ai_decisions a ON a.id = h.ai_decision_id "
        "LEFT JOIN raw_news r ON r.id = a.news_id "
        "WHERE h.trigger_kind='insert' AND h.decision_space='ai' "
        "AND a.forward_move_pct IS NOT NULL").fetchall()
    if not rows:
        print("没有可投影的配对")
        return

    out = []
    for r in rows:
        base_now = _base_mult(r["sc"], r["mg"], r["hs"], impact_score=r["isc"])
        use_gate = (abs(float(r["isc"] or 5.0) - 5.0) < 1e-9) and int(r["gi"] or 0) > 0
        base_fix = _base_mult(r["sc"], r["mg"], r["hs"],
                              impact_score=(int(r["gi"]) / 10.0)) if use_gate else base_now
        w_fix = compose_weight(base_fix, r["im"], r["c"], r["s"], r["rt"], r["g"], r["t"])["weight"]
        out.append({"ts": int(r["ts"] or 0), "mv": float(r["mv"] or 0.0),
                    "abs_move": float(r["mv"] or 0.0),
                    "w": float(r["w"] or 0.0), "w_fix": w_fix, "changed": use_gate})

    n = len(out)
    d_now = len({round(x["w"], 6) for x in out})
    d_fix = len({round(x["w_fix"], 6) for x in out})
    print(f"\n权重投影 (n={n}, 其中会被门卫分改写 {sum(1 for x in out if x['changed'])} 条):")
    print(f"  分辨率 distinct: 现状 {d_now} ({d_now/n:.1%}) → 接门卫分 {d_fix} ({d_fix/n:.1%})")
    r_now, hrs = bucket_rho(out, "w")
    r_fix, _ = bucket_rho(out, "w_fix")
    print(f"  S4 口径桶内 ρ : 现状 {r_now} → 接门卫分 {r_fix}   (小时桶 {hrs})")


def main() -> int:
    conn = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True, timeout=30)
    conn.row_factory = sqlite3.Row
    rows = load(conn)
    n = len(rows)
    if not n:
        print("没有决策数据")
        return 1

    eq5 = [r for r in rows if abs(r["i"] - 5.0) < 1e-9]
    eq5_with_gate = [r for r in eq5 if r["g"] > 0]
    print(f"决策总数 {n}")
    print(f"  impact_score = 5.0 : {len(eq5)} ({len(eq5)/n:.1%})")
    print(f"  其中新闻已有门卫分  : {len(eq5_with_gate)} "
          f"({len(eq5_with_gate)/max(1,len(eq5)):.1%} of 5.0)")

    cur_base = [base_of(r, r["i"]) for r in rows]
    fix_base = [base_of(r, (r["g"] / 10.0) if r["g"] > 0 else r["i"]) for r in rows]
    print("\nbase_mult 分辨率:")
    print(f"  现状 distinct = {len({round(v,6) for v in cur_base})}")
    print(f"  接门卫分后    = {len({round(v,6) for v in fix_base})}")

    # 权重分辨率：按同一套乘子重算（impact 变化会影响权重）
    try:
        from signals.signal_weight_evaluator import compose_weight
        hrows = conn.execute(
            "SELECT h.base_mult b, h.impact_mult im, h.consensus_mult c, h.source_mult s, "
            "       h.reaction_mult r, h.regime_mult g, h.time_mult t, h.signal_weight w "
            "FROM signal_weight_history h "
            "WHERE h.trigger_kind='insert' AND h.decision_space='ai'").fetchall()
        distinct_now = len({round(float(x["w"]), 6) for x in hrows})
        print(f"\n落库权重分辨率 (insert): distinct = {distinct_now}")
        print("  说明: 门卫分若要真正改善权重, 必须重新合成 base —— 不在本脚本的只读范围内。")
    except Exception as e:  # pragma: no cover
        print(f"  跳过权重分辨率: {e}")

    project_weight_effect(conn)

    print("\n结论口径:")
    print("  · impact_score 5.0 是**缺省**而非判断, 但落库后无法与真实 5 分区分")
    print("  · 7680 条明明有门卫分却没用上 → 改 persist 的兜底条件即可, 不动公式")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
