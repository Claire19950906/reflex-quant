# -*- coding: utf-8 -*-
"""v453: 用「决策级归因」验证洞察 —— 不等成交（口径修正版）。

背景
----
`insight_verifier` 的判定门槛是 `MIN_TRADES_FOR_VERDICT = 12` 笔成交，
而 quick_sim 全库累计只有 11 笔 → `verified` 在数学上不可达。

`insight_decision_attr.py`（v409.7）已有替代口径：用「门卫放行组 vs 拦截组」
决策后 24h 的真实收益差给规则结账。但该模块**从未被调用**（全仓无调用方），
且直接接线会出错 —— 所以这里先修口径，再接：

  1. `insight_direction` 的真实含义是**参数调整方向**（提高=+1 / 降低=-1），
     由 `insight_hypothesis._extract_direction` 从"提高/降低"这类词里抽出来，
     **不是**看涨/看跌。拿它当 BUY/SELL 去分组，等于用"降低买入门槛"这条
     建议去测"卖出信号"的收益 —— 会产出假 verified。
  2. 决策级归因真正能测的只有**门槛类规则**（`score.*` / `filter.*`）：
     它验的是"被门卫拦下的同向决策，事后收益是否确实差于放行的"。
     组合/仓位类（`sizing.*`）与纯叙事类洞察不走这个口径。
  3. 证据方向要与建议方向对账：
       · 建议"提高门槛"(+1) + 拦截组确实更差 → verified
       · 建议"降低门槛"(-1) + 拦截组确实更差 → 反了，判 failed
     （同一份证据，建议相反，结论就相反。）

用法
----
  python3 scripts/verify_insights_decision_level.py            # 预演
  python3 scripts/verify_insights_decision_level.py --commit   # 落库
  python3 scripts/verify_insights_decision_level.py --days 7   # 决策回看天数
"""
from __future__ import annotations

import argparse
import os
import sqlite3
import sys

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(BASE_DIR, "src_python"))

DB_PATH = os.path.join(BASE_DIR, "trident_event_bus.db")

# 门槛类参数键 → 它守的那一边（只有这两类能走决策级归因）
_GATE_SIDE = {
    "score.buy_min_score": "BUY",
    "score.sell_min_score": "SELL",
    "filter.buy_min_score": "BUY",
    "filter.sell_min_score": "SELL",
    "min_event_strength": "",      # 门槛但两侧共用 → 可分资产、不可分方向，暂不测
    "signal_threshold": "",
}


def _open(readonly: bool) -> sqlite3.Connection:
    if readonly:
        conn = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True)
    else:
        conn = sqlite3.connect(DB_PATH, timeout=30)
    conn.row_factory = sqlite3.Row
    return conn


def _gate_side(param_key: str) -> str:
    key = (param_key or "").strip().lower()
    return _GATE_SIDE.get(key, "")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--commit", action="store_true", help="写库（默认 dry-run）")
    ap.add_argument("--days", type=int, default=7, help="决策回看天数（默认 7）")
    args = ap.parse_args()

    import insight_decision_attr as ida

    conn = _open(readonly=not args.commit)
    rows = [dict(r) for r in conn.execute(
        "SELECT id, rule, asset, insight_param_key, insight_direction, observe_only,"
        " created_at, verification_verdict FROM review_insights"
        " WHERE active = 1 AND insight_direction IN (1, -1) ORDER BY id"
    )]

    judged = skipped = 0
    tally: dict = {}
    for ins in rows:
        side = _gate_side(ins.get("insight_param_key") or "")
        if not side:
            skipped += 1
            continue
        # 把"参数方向"翻成"这次要测的门卫方向"，并记住建议方向用于对账
        advice = int(ins.get("insight_direction") or 0)
        probe = dict(ins)
        probe["insight_direction"] = 1 if side == "BUY" else -1
        res = ida.attribute_insight(conn, probe, days_back=args.days)
        if res.get("verdict") in ("insufficient_sample", "not_applicable"):
            skipped += 1
            print(f"  [skip] id={ins['id']} {res.get('verdict')} {res.get('reason','')[:60]}")
            continue
        score = res.get("score")
        blocked_worse = score is not None and score >= 0.7
        blocked_better = score is not None and score <= 0.3
        if blocked_worse:
            verdict = "verified" if advice > 0 else "failed"
        elif blocked_better:
            verdict = "verified" if advice < 0 else "failed"
        else:
            verdict = "partial"
        judged += 1
        tally[verdict] = tally.get(verdict, 0) + 1
        print(f"  [judge] id={ins['id']} {verdict} score={score} "
              f"n={res.get('n_allowed')}/{res.get('n_blocked')} "
              f"建议={'提高' if advice > 0 else '降低'}门槛({side})")
        print(f"          {(ins.get('rule') or '')[:64]}")
        print(f"          {res.get('reason','')[:80]}")
        if args.commit:
            conn.execute(
                "UPDATE review_insights SET verification_score=?, verification_verdict=?,"
                " verification_method='decision_attr', verification_sample_n=?,"
                " verified_at=datetime('now','localtime') WHERE id=?",
                (score, verdict, int(res.get("used") or 0), ins["id"]),
            )

    print(f"\n门槛类候选: {judged + skipped} · 判出结论: {judged} · 跳过(非门槛/样本不足): {skipped}")
    print("结论分布:", tally)
    if args.commit:
        conn.commit()
        print("[commit] 已写入 verification_* （method='decision_attr'）")
    else:
        print("[dry-run] 未写库。确认后加 --commit")
    conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
