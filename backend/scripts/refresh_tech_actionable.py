#!/usr/bin/env python3
"""refresh_tech_actionable.py —— 从台账自动生成桥接白名单（按周期分档 + 反向黑名单）。

为什么需要
----------
`runtime/tech_actionable.json` 原本是手抄台账结论：只放行 5 日口径达标的 3 个因子。
结果 90 天 419 条候选里 290 条（69%）死于白名单，影子账本一个月攒不到 5 条样本。
而台账本身是按 1/3/5/10 日四套周期算的 —— bb_upper_break 3 日超额 +1.2 判「有效」，
5 日只剩噪声；一套 5 日通吃等于把它的 3 日结论扔掉。

本脚本把「台账 → 白名单」变成一条可复跑的机械转换：
  · 每个周期单独放行：verdict=有效 且样本 ≥ MIN_N 的因子，按看涨/看跌分 bull/bear；
  · 反向黑名单：≥2 个周期判「反向」的因子一律降级为背景证据（如 vol_bear_break
    5 日命中 34%、超额 −1.35 —— 照原方向出信号等于系统性地做错方向）；
  · `regime`（MA200/ADX 环境闸门）与人工备注原样保留，不被脚本覆盖。

用法
----
    python3 scripts/refresh_tech_actionable.py --dry-run    # 只打印差异
    python3 scripts/refresh_tech_actionable.py              # 写回配置
"""
from __future__ import annotations

import argparse
import json
import shutil
from pathlib import Path
from typing import Any

BACKEND = Path(__file__).resolve().parents[1]
LEDGER = BACKEND / "reports" / "indicator_ledger" / "latest.json"
CONFIG = BACKEND / "src_python" / "runtime" / "tech_actionable.json"

HORIZONS = (1, 3, 5, 10)
#: 与旧配置同口径的样本门槛：低于 200 条的因子即使"有效"也只是巧合。
MIN_N = 200
#: 判「反向」至少要在几个周期上成立，防止单周期噪声被当成结构性反向。
MIN_REVERSE_HORIZONS = 2
#: 台账上没有任何因子达标时，沿用旧白名单的周期（旧白名单是 5 日口径）。
#: 不这么做的话，清理未来函数后白名单会被刷成空 → 技术信号彻底断流，
#: 等于用"更严格"换来了"没有信号"，反而更不诚实。
CARRY_OVER_HORIZON = 5

BULL_WORD = "看涨"
BEAR_WORD = "看跌"



#: 分层格子放行的三道闸：样本量、多重比较校正、时间外样本同向。
#: 只过其中一道就放行，等于把"测了 2,640 个格子挑出来的最好看的那个"当结论。
STRATA_MIN_N = 200
#: 时间外样本要同时过两关：命中率超额（方向对不对）和均值超额（赚不赚得回来）。
#: 只看命中率会被"错的时候亏更多"骗过去，只看均值会被少数极端值拉偏，两个都要。
STRATA_MIN_HOLDOUT_HIT_EDGE = 5.0
STRATA_MIN_HOLDOUT_EDGE = 1.0


def _cell_z(r: dict[str, Any]) -> float:
    if not r.get("n") or r.get("hit_rate") is None or r.get("base_hit") is None:
        return 0.0
    p0 = float(r["base_hit"]) / 100.0
    p1 = float(r["hit_rate"]) / 100.0
    se = (p0 * (1 - p0) / int(r["n"])) ** 0.5
    return (p1 - p0) / se if se else 0.0


def _build_strata(ledger: dict[str, Any]) -> dict[str, Any]:
    """从台账分层结果挑可放行的格子（v456）。

    判定顺序：n ≥ 200 → z ≥ Bonferroni 门槛 → 时间外命中率与均值超额都为正。
    最后一关是"时间外"，因为全样本超额一定掺了样本内的运气；
    这一关过不了的格子只进 candidates，不接管线上周期。
    """
    strata = ledger.get("strata") or {}
    bonf = float(strata.get("bonferroni_z") or 0.0)
    rows = [r for r in ledger.get("rows") or [] if r.get("group") == "stratum"]

    candidates: list[dict[str, Any]] = []
    for r in rows:
        if int(r.get("n") or 0) < STRATA_MIN_N:
            continue
        z = _cell_z(r)
        if z < bonf:
            continue
        ho_n = int(r.get("holdout_n") or 0)
        ho_hit, ho_base = r.get("holdout_hit"), r.get("holdout_base_hit")
        ho_edge = r.get("holdout_edge")
        ho_hit_edge = (float(ho_hit) - float(ho_base)
                       if ho_hit is not None and ho_base is not None else None)
        candidates.append({
            "dim": r.get("dim"), "key": str(r.get("key")),
            "factor": str(r.get("name")),
            "direction": "bull" if r.get("direction") == BULL_WORD else "bear",
            "horizon": int(r.get("horizon") or 0),
            "n": int(r.get("n") or 0), "z": round(z, 2),
            "edge": r.get("edge"), "holdout_n": ho_n,
            "holdout_edge": ho_edge, "holdout_hit_edge": ho_hit_edge,
        })

    cells = [c for c in candidates
             if int(c["holdout_n"]) >= 60
             and (c["holdout_hit_edge"] or 0) >= STRATA_MIN_HOLDOUT_HIT_EDGE
             and (c["holdout_edge"] or 0) >= STRATA_MIN_HOLDOUT_EDGE]
    best_holdout = max((c["holdout_edge"] or 0) for c in candidates) if candidates else 0.0
    enabled = bool(cells)
    note = ("分层格子达标且时间外同向，已接管相应周期。"
            if enabled else
            f"分层没有格子通过全部三关（最强候选的时间外均值超额 "
            f"{best_holdout:+.2f}pt，门槛 {STRATA_MIN_HOLDOUT_EDGE}pt / "
            f"命中率超额 {STRATA_MIN_HOLDOUT_HIT_EDGE}pt）。"
            "保持关闭：宁可不用分层，也不把多重比较挑出来的格子当优势。")
    return {
        "enabled": enabled,
        "version": "v456",
        "criteria": {"min_n": STRATA_MIN_N,
                     "bonferroni_z": bonf,
                     "min_holdout_n": 60,
                     "min_holdout_hit_edge": STRATA_MIN_HOLDOUT_HIT_EDGE,
                     "min_holdout_edge": STRATA_MIN_HOLDOUT_EDGE},
        "cells": cells,
        "candidates": candidates,
        "evidence": {"cells_with_sample": strata.get("cells_with_sample"),
                     "cells_passing_5pct": strata.get("cells_passing_5pct"),
                     "expected_by_chance_5pct": strata.get("expected_by_chance_5pct"),
                     "max_z": strata.get("max_z")},
        "note": note,
    }

def build(ledger: dict[str, Any], old: dict[str, Any]) -> dict[str, Any]:
    rows = [r for r in ledger.get("rows") or [] if r.get("group") == "factor"]

    by_horizon: dict[str, dict[str, list[str]]] = {}
    for h in HORIZONS:
        bull, bear = [], []
        for r in rows:
            if int(r.get("horizon") or 0) != h:
                continue
            if r.get("verdict") != "有效" or int(r.get("n") or 0) < MIN_N:
                continue
            name = str(r.get("name"))
            if r.get("direction") == BULL_WORD:
                bull.append(name)
            elif r.get("direction") == BEAR_WORD:
                bear.append(name)
        by_horizon[str(h)] = {"bull": sorted(bull), "bear": sorted(bear)}

    # 沿用兜底：本次台账 0 个因子达标时，不能把白名单刷空（等于让技术面断流）。
    carried: list[str] = []
    old_bull = [str(x) for x in (old.get("bull") or [])]
    old_bear = [str(x) for x in (old.get("bear") or [])]
    for h in HORIZONS:
        slot = by_horizon[str(h)]
        if slot["bull"] or slot["bear"]:
            continue
        if h == CARRY_OVER_HORIZON and (old_bull or old_bear):
            by_horizon[str(h)] = {"bull": sorted(old_bull), "bear": sorted(old_bear)}
            carried.append(str(h))

    reverse_by: dict[str, set[int]] = {}
    for r in rows:
        if r.get("verdict") == "反向":
            reverse_by.setdefault(str(r.get("name")), set()).add(
                int(r.get("horizon") or 0))
    blacklist = sorted(n for n, hs in reverse_by.items()
                       if len(hs) >= MIN_REVERSE_HORIZONS)

    strata = _build_strata(ledger)

    # 扁平字段保持旧语义（5 日口径），供老调用方 / 回放脚本继续可读。
    flat = by_horizon.get("5", {"bull": [], "bear": []})
    passing = sum(1 for h in HORIZONS
                  for k in ("bull", "bear")
                  if str(h) not in carried
                  for _ in by_horizon[str(h)][k])
    return {
        "bull": flat["bull"],
        "bear": flat["bear"],
        "by_horizon": by_horizon,
        "blacklist": blacklist,
        "strata": strata,
        "rule": (f"每个周期单独放行：verdict=有效 且样本 ≥{MIN_N}；"
                 f"≥{MIN_REVERSE_HORIZONS} 个周期判反向 → 黑名单（只作背景证据）"),
        "source": "reports/indicator_ledger/latest.json + reports/tech_engine/v451_regime.md",
        "revalidation": {
            "as_of": str(ledger.get("generated_at") or "")[:10],
            "signals_used": ledger.get("signals_used"),
            "factors_passing": passing,
            "carried_over_horizons": carried,
            "note": ("本次台账在清理未来函数后的因果样本上，0 个因子达到 ≥5pt 超额门槛；"
                     "5 日白名单沿用上一版结论（carried over），待样本积累后重估。"
                     if carried else "本次台账有因子达标，白名单按台账生成。"),
        },
    }


def main() -> int:
    ap = argparse.ArgumentParser(description="从台账生成桥接白名单")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    if not LEDGER.exists():
        print(f"[FATAL] 台账不存在：{LEDGER}（先跑 indicator_effectiveness_ledger.py）")
        return 1
    ledger = json.loads(LEDGER.read_text(encoding="utf-8"))

    old: dict[str, Any] = {}
    if CONFIG.exists():
        try:
            old = json.loads(CONFIG.read_text(encoding="utf-8"))
        except ValueError:
            old = {}
    new = build(ledger, old)

    out = dict(new)
    out["as_of"] = str(ledger.get("generated_at") or "")[:10]
    out["version"] = "v456"
    # 环境闸门与历史备注属于调参决策，脚本不碰；当前结论写进 note。
    if isinstance(old.get("regime"), dict):
        out["regime"] = old["regime"]
    if old.get("note"):
        out["note_prev"] = old["note"]
    out["note"] = out["revalidation"]["note"]

    print(json.dumps({
        "by_horizon": {h: {k: len(v) for k, v in s.items()}
                       for h, s in out["by_horizon"].items()},
        "blacklist": out["blacklist"],
        "carried_over": out["revalidation"]["carried_over_horizons"],
        "changed": out.get("by_horizon") != old.get("by_horizon")
                   or out.get("blacklist") != old.get("blacklist"),
        "dry_run": args.dry_run,
    }, ensure_ascii=False))

    if args.dry_run:
        return 0
    if CONFIG.exists():
        shutil.copy2(CONFIG, CONFIG.with_suffix(".json.bak"))
    CONFIG.write_text(json.dumps(out, ensure_ascii=False, indent=2) + "\n",
                      encoding="utf-8")
    print(f"written -> {CONFIG}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
