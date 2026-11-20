# -*- coding: utf-8 -*-
"""正期望择优 / 停单策略（v449，只读）。

把「能不能赚钱」那张矩阵从"看"变成"用"：

  - 允许开火的格子 = 扣成本后净期望 > 0、样本 ≥ 30、t ≥ 2；
  - 一个都没有 → **当日停单**，不是"再调个参数试试"；
  - 有 → 只在这些持有期/品种上开火，并给出建议持有期。

当前实测（2030 笔、30bps）：5 个持有期全部为负，唯一接近打平的是 1d。
所以本模块今天的输出就是"停单"——这不是坏消息，是省钱的结论。

只读、不写库、不发网络请求。
"""
from __future__ import annotations

import sqlite3
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

DB_PATH = Path(__file__).resolve().parents[1] / "trident_event_bus.db"

MIN_N = 30
MIN_T = 2.0
DEFAULT_COST_BPS = 30


def _ro_conn(db_path: Optional[Path] = None) -> sqlite3.Connection:
    p = Path(db_path) if db_path else DB_PATH
    conn = sqlite3.connect(f"file:{p}?mode=ro", uri=True, timeout=20.0)
    conn.row_factory = sqlite3.Row
    return conn


def _confirm_ok(action: str, entry_price: float, base_close: float,
                min_move_pct: float) -> bool:
    """决策前窗口是否已同向走够 min_move_pct；不做前视。"""
    if not base_close or base_close <= 0:
        return False
    move = (float(entry_price) / float(base_close) - 1.0) * 100.0
    return move >= min_move_pct if action == "BUY" else move <= -min_move_pct


def _confirm_gated(conn: sqlite3.Connection, entries: List[Dict],
                   since_ts: int) -> tuple[List[Dict], Dict[str, int]]:
    """按 v447 校准过的同向闸筛一遍：只留"决策前 20 分钟已同向 ≥0.10%"。

    不额外放宽品种：v447 只验证过 BTC，其他品种即使同向也不进这个 cohort。
    """
    import entry_guard as eg

    assets = sorted({e["asset"] for e in entries})
    # 只加载会被闸门真正判定的品种（默认仅 BTC），别为 4 个用不上的品种查 5m 全量
    need = [a for a in assets if a in eg.ENFORCE_ASSETS]
    bars = {a: eg.load_ohlc(conn, a, "5m", since_ts - 2 * 86400) for a in need}
    kept: List[Dict] = []
    stats = {"input": len(entries), "kept": 0, "asset_filtered": 0, "no_bars": 0}
    for e in entries:
        if e["asset"] not in eg.ENFORCE_ASSETS:
            stats["asset_filtered"] += 1
            continue
        b5 = bars.get(e["asset"]) or []
        # 只取"已经收完"的 5m bar：bar 起点 + 5 分钟 ≤ 入场时刻，避免用入场后的收盘价
        prior = [b for b in b5 if b["ts"] + 300 <= e["entry_ts"]]
        if len(prior) < 2:
            stats["no_bars"] += 1
            continue
        start_ts = e["entry_ts"] - eg.CONFIRM_WINDOW_MIN * 60
        base = next((b for b in prior if b["ts"] >= start_ts), None)
        if not base or base["close"] <= 0:
            stats["no_bars"] += 1
            continue
        same = _confirm_ok(e["action"], e["entry_price"], base["close"],
                           eg.CONFIRM_MIN_MOVE_PCT)
        if same:
            kept.append(e)
    stats["kept"] = len(kept)
    return kept, stats


def _cell_view(c: Dict[str, Any]) -> Dict[str, Any]:
    return {"horizon": c["horizon"], "net": c["net"], "gross": c["gross"],
            "cost_to_move": c["cost_to_move"], "verdict": c["verdict"]}


def _cohort(rep: Dict[str, Any], cost_bps: int) -> Dict[str, Any]:
    cells = rep.get("cells") or []
    allowed = [
        c for c in cells
        if (c["net"].get("avg") or -1) > 0
        and (c["net"].get("n") or 0) >= MIN_N
        and (c["net"].get("t_stat") is not None and c["net"]["t_stat"] >= MIN_T)
    ]
    allowed.sort(key=lambda c: -(c["net"]["avg"] or -1))
    weak = [
        c for c in cells
        if c not in allowed
        and (c["net"].get("avg") or -1) > 0
        and (c["net"].get("n") or 0) >= MIN_N
        and (c["net"].get("t_stat") is not None and c["net"]["t_stat"] >= 1.0)
    ]
    weak.sort(key=lambda c: (-(c["net"].get("t_stat") or 0), -(c["net"]["avg"] or -1)))
    best = max(cells, key=lambda c: (c["net"].get("avg")
                                     if c["net"].get("avg") is not None else -9e9),
               default=None)
    by_asset = rep.get("by_asset") or []
    stop_assets = [
        a for a in by_asset
        if (a["net"].get("n") or 0) >= MIN_N
        and (a["net"].get("avg") or 0) < 0
        and (a["net"].get("t_stat") is not None and a["net"]["t_stat"] <= -2.0)
    ]
    if allowed:
        reason = (f"{allowed[0]['horizon']} / {cost_bps}bps 净 "
                  f"{allowed[0]['net']['avg']:+.3f}%/笔（n={allowed[0]['net']['n']}，"
                  f"t={allowed[0]['net']['t_stat']}）")
        hold = allowed[0]["horizon"]
        instruments = [a["asset"] for a in by_asset
                       if (a["net"].get("avg") or 0) > 0 and (a["net"].get("n") or 0) >= MIN_N]
    else:
        best_txt = "—"
        if best and best["net"].get("avg") is not None:
            best_txt = (f"最好的 {best['horizon']} 也只有 {best['net']['avg']:+.3f}%/笔"
                        f"（n={best['net']['n']}，t={best['net']['t_stat']}）")
        reason = (f"没有正期望格子（{cost_bps}bps）：{best_txt}")
        hold = None
        instruments = []
    return {
        "entries": rep.get("entries") or 0,
        "allow": bool(allowed),
        "weak": bool(weak and not allowed),
        "action": "可开火" if allowed else ("仅观察仓" if weak else "停单"),
        "reason": reason,
        "recommended_hold": hold,
        "allowed_instruments": instruments,
        "allowed_cells": [_cell_view(c) for c in allowed],
        "weak_cells": [_cell_view(c) for c in weak],
        "cells": [_cell_view(c) for c in cells],
        "best_cell": _cell_view(best) if best else None,
        "stop_assets": stop_assets,
        "all_assets": by_asset,
    }


def build(days: int = 60, bars_days: int = 120,
          cost_bps: int = DEFAULT_COST_BPS,
          db_path: Optional[Path] = None) -> Dict[str, Any]:
    import cost_horizon_eval as ce

    now = int(time.time())
    since = now - days * 86400
    conn = _ro_conn(db_path)
    try:
        entries = ce.load_entries(conn, since_ts=since, limit=5000)
        assets = sorted({e["asset"] for e in entries})
        bars: Dict[str, Dict[str, List]] = {}
        for a in assets:
            bars[a] = {}
            for _label, _sec, tf in ce.HORIZONS:
                bars[a][tf] = ce.load_bars(conn, a, tf, now - bars_days * 86400)
        gated_entries, gate_stats = _confirm_gated(conn, entries, since)
    finally:
        conn.close()

    raw_rep = ce.evaluate(entries, bars, cost_tiers=[cost_bps])
    gated_rep = ce.evaluate(gated_entries, bars, cost_tiers=[cost_bps]) if gated_entries else {}
    raw = _cohort(raw_rep, cost_bps)
    gated = _cohort(gated_rep, cost_bps) if gated_rep else {
        "entries": 0, "allow": False, "weak": False, "action": "停单",
        "reason": "同向闸后没有样本",
        "recommended_hold": None, "allowed_instruments": [],
        "allowed_cells": [], "weak_cells": [], "best_cell": None,
        "stop_assets": [], "all_assets": [],
    }
    gated["gate_stats"] = gate_stats

    # 先看校准后的 cohort（真正可执行的），再看全样本。
    # 强证据（t≥2）才叫"允许"；t 只有 1~2 的只能叫"观察仓"，不建仓。
    if gated["allow"]:
        active, allow, action = dict(gated, cohort="gated"), True, "可开火"
    elif raw["allow"]:
        active, allow, action = dict(raw, cohort="raw"), True, "可开火"
    elif gated["weak"]:
        active, allow, action = dict(gated, cohort="gated"), False, "仅观察仓"
    elif raw["weak"]:
        active, allow, action = dict(raw, cohort="raw"), False, "仅观察仓"
    else:
        active, allow, action = dict(raw, cohort="raw"), False, "停单"

    # v461：任何"可开火"都必须再过一遍样本工厂（5m 真实结算 + 时间分段），
    # 否则 20m K 线结算会把持有期悄悄拉长，制造假的正期望。
    factory = None
    try:
        import sample_factory as _sf
        factory = _sf.build(asset="BTC", days=min(days, 60), folds=4, cost_bps=cost_bps)
    except Exception as exc:  # 工厂失败不阻断主报告，但要标出来
        factory = {"verdict": "未运行", "headline": f"样本工厂失败：{exc}"}

    if factory and factory.get("verdict") == "样本不足":
        allow = False
        action = "样本不足"
        headline = f"样本不足，不建仓：{factory.get('headline')}"
    elif allow and factory and factory.get("verdict") != "可开火":
        allow = False
        action = "仅观察仓"
        headline = (f"策略矩阵看着为正（{active['reason']}），但**样本工厂没通过**："
                    f"{factory.get('headline')}；先不建仓")

    if action == "样本不足":
        pass  # 保留上面已写好的样本不足说明，不再走"停单"文案
    elif allow:
        headline = (f"允许开火（{active['cohort']} 口径，且通过分段）：{active['reason']}；"
                    f"建议持有 {active['recommended_hold']}，"
                    f"品种 {', '.join(active['allowed_instruments']) or '—'}")
    elif action == "仅观察仓":
        wc = active["weak_cells"][0]
        headline = (f"不建仓，只放观察仓：{cost_bps}bps 下最好的格子是 "
                    f"{wc['horizon']} 净 {wc['net']['avg']:+.3f}%/笔"
                    f"（n={wc['net']['n']}，t={wc['net']['t_stat']}），"
                    f"t<{MIN_T}，还够不上'正期望'")
    elif gated["entries"] and gated["entries"] < MIN_N:
        raw_txt = "全样本也没有正期望" if not raw["allow"] else "全样本尚可但未经验证"
        headline = (f"停单：同向闸后只剩 {gated['entries']} 笔（<{MIN_N}），"
                    f"样本不足以下注；{raw_txt}")
    else:
        headline = (f"停单：{cost_bps}bps 成本下，全样本与同向闸后样本都没有"
                    f"满足 净期望>0、n≥{MIN_N}、t≥{MIN_T} 的格子；{gated['reason']}")

    return {
        "generated_at": now,
        "window_days": days,
        "cost_bps": cost_bps,
        "allow": allow,
        "action": action,
        "cohort": active["cohort"],
        "headline": headline,
        "halt_reason": None if allow else headline,
        "recommended_hold": active.get("recommended_hold"),
        "allowed_instruments": active.get("allowed_instruments") or [],
        "allowed_cells": active.get("allowed_cells") or [],
        "weak_cells": active.get("weak_cells") or [],
        "best_cell": active.get("best_cell"),
        "stop_assets": raw.get("stop_assets") or [],
        "raw": raw,
        "gated": gated,
        "factory": factory,
        "criteria": {"min_n": MIN_N, "min_t": MIN_T, "cost_bps": cost_bps,
                     "confirm_window_min": 20, "confirm_min_move_pct": 0.10,
                     "enforce_assets": "BTC"},
        "note": ("样本优先用影子账本/实盘已结算，回放模拟只给观察；净收益已扣往返成本；"
                 "同向 cohort 沿用 v447 校准（20 分钟 / 0.10%，仅 BTC）。"
                 "样本<30 或 t<2 一律不算'正期望'；t 在 1~2 只给观察仓。"),
    }


def _main() -> int:
    import argparse
    import json
    ap = argparse.ArgumentParser(description="正期望择优 / 停单策略")
    ap.add_argument("--days", type=int, default=60)
    ap.add_argument("--cost", type=int, default=DEFAULT_COST_BPS)
    a = ap.parse_args()
    rep = build(days=a.days, cost_bps=a.cost)
    print(json.dumps(rep, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
