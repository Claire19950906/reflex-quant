#!/usr/bin/env python3
"""signal_review_closed_loop.py — 技术信号后验复盘闭环。

做什么
------
把 `abnormal_signals` 里已落库的技术证据（adx/mfi/cci/aroon/swing/supertrend 等）
按触发日重放，用日线前向 1/3/5/10 日收益检验方向是否兑现，产出复盘台账：

  · 每个 signal_kind 按方向分组：样本数、前向各周期命中率、平均超额、噪声率；
  · 兑现差（反向或噪声）的因子给出降级/观察建议；
  · 历史信号不写回线上规则，结论只进报告，人工复核后再动阈值/白名单。

只读库，不写线上逻辑。信号若落在数据末尾（未来周期取不到），该周期标 no_data。
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import statistics
import sys
from collections import defaultdict
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

BACKEND = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND / "src_python"))
DB_PATH = BACKEND / "trident_event_bus.db"
OUT_DIR = BACKEND / "reports" / "indicator_ledger"

# 纳入复盘的技术证据 kind（其余异动/宏观类不在本闭环口径内）
TECH_KINDS = {
    "adx_strong_bull", "adx_strong_bear",
    "mfi_oversold", "mfi_overbought",
    "cci_oversold", "cci_overbought",
    "swing_bull", "swing_bear",
    "aroon_bull", "aroon_bear",
    "supertrend_bull", "supertrend_bear",
}
HORIZONS = (1, 3, 5, 10)
MIN_SAMPLE = 10
# 方向 → 收益符号：看涨取正、看跌取反；未明方向不做方向判定
SIGNED = {"bullish": 1, "bearish": -1}


def load_daily(conn: sqlite3.Connection) -> Dict[str, List[list]]:
    rows = conn.execute(
        "SELECT asset, ts, open, high, low, close, volume FROM ohlcv_daily "
        "WHERE close IS NOT NULL ORDER BY asset, ts"
    ).fetchall()
    series: Dict[str, List[list]] = defaultdict(list)
    for asset, ts, o, h, l, c, v in rows:
        series[asset].append([int(ts), float(o or c), float(h), float(l),
                              float(c), float(v or 0.0)])
    return series


def fwd_table(candles: List[list]) -> Dict[int, Dict[int, float]]:
    """每根 ts → {horizon: 前向收益率%}；ts 未精确命中时回落到触发日或之前最近一根。"""
    times = [c[0] for c in candles]
    out: Dict[int, Dict[int, float]] = {}
    for i, c in enumerate(candles):
        base = c[4]
        if not base:
            continue
        cell: Dict[int, float] = {}
        for h in HORIZONS:
            j = i + h
            if j < len(candles):
                cell[h] = (candles[j][4] - base) / base * 100.0
        out[c[0]] = cell

    import bisect
    resolved: Dict[int, Dict[int, float]] = {}
    for anchor_s in list(out.keys()):
        pos = bisect.bisect_right(times, anchor_s) - 1  # 触发日或之前最近一根
        if pos >= 0:
            resolved[anchor_s] = out[times[pos]]
    return resolved


def _verdict(hit: Optional[float], edge: Optional[float], n: int) -> str:
    if n < MIN_SAMPLE:
        return "样本不足"
    if edge is not None and edge < 0 and (hit or 0) < 50.0:
        return "反向，建议降级/观察"
    if hit is not None and hit - 50.0 >= 5.0 and (edge or 0) > 0:
        return "有效"
    return "噪声，仅作背景证据"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default=str(DB_PATH))
    ap.add_argument("--no-write", action="store_true")
    args = ap.parse_args()

    conn = sqlite3.connect(args.db)
    series = load_daily(conn)
    fwd: Dict[str, Dict[int, Dict[int, float]]] = {
        a: fwd_table(c) for a, c in series.items()
    }

    rows = conn.execute(
        "SELECT signal_kind, direction, asset, ts, anchor_ts FROM abnormal_signals "
        "WHERE signal_kind IN (" + ",".join("?" * len(TECH_KINDS)) + ") "
        "ORDER BY asset, ts",
        tuple(TECH_KINDS),
    ).fetchall()

    # 补充来源：tech_signals.signal_json 里已回灌的趋势/极值证据（历史样本，
    # 前向收益口径与 abnormal_signals 复盘一致）。
    tech_rows = conn.execute(
        "SELECT asset, ts, signal_json FROM tech_signals ORDER BY asset, ts"
    ).fetchall()
    conn.close()

    # bucket: (kind, direction) → {horizon: [signed returns]}，另记样本/no_data
    bucket: Dict[Tuple[str, str], Dict[int, List[float]]] = defaultdict(
        lambda: {h: [] for h in HORIZONS})
    counts: Dict[Tuple[str, str], int] = defaultdict(int)

    for kind, direction, asset, ts, anchor in rows:
        # anchor_ts 是毫秒触发 K 线，回灌时归一到秒；取不到回退 ts
        anchor_s = int(anchor) // 1000 if anchor and anchor > 10_000_000_000 else int(ts)
        sign = SIGNED.get(direction)
        if sign is None:
            continue
        cell = fwd.get(asset, {}).get(anchor_s) or fwd.get(asset, {}).get(int(ts))
        if not cell:
            # 再按"触发日或之前最近一根"兜底
            asset_fwd = fwd.get(asset)
            if asset_fwd:
                import bisect
                times = sorted(asset_fwd.keys())
                pos = bisect.bisect_right(times, anchor_s) - 1
                if pos >= 0:
                    cell = asset_fwd[times[pos]]
        if not cell:
            continue
        # 只统计有"主判周期"前向收益的信号，否则短样本会把无后验的样本算进来
        if 5 not in cell:
            continue
        counts[(kind, direction)] += 1
        for h in HORIZONS:
            if h in cell:
                bucket[(kind, direction)][h].append(sign * cell[h])

    # tech_signals 补充样本：signal_json 里回灌的趋势/极值证据，方向由 kind 后缀推断。
    BULL_KINDS = {"adx_strong_bull", "supertrend_bull", "swing_bull", "aroon_bull",
                  "mfi_oversold", "cci_oversold", "kdj_oversold", "engulfing_bull",
                  "stoch_oversold", "bb_lower_break", "ma_bull_aligned", "macd_golden",
                  "ichimoku_bull", "vol_bull_break"}
    BEAR_KINDS = {"adx_strong_bear", "supertrend_bear", "swing_bear", "aroon_bear",
                  "mfi_overbought", "cci_overbought", "kdj_overbought", "engulfing_bear",
                  "stoch_overbought", "bb_upper_break", "ma_bear_aligned", "macd_death",
                  "vol_bear_break", "ichimoku_bear"}

    def _absorb(asset: str, ts: int, kind: str) -> None:
        if kind in BULL_KINDS:
            direction, sign = "bullish", 1
        elif kind in BEAR_KINDS:
            direction, sign = "bearish", -1
        else:
            return
        cell = fwd.get(asset, {}).get(int(ts))
        if not cell:
            asset_fwd = fwd.get(asset)
            if asset_fwd:
                import bisect
                times = sorted(asset_fwd.keys())
                pos = bisect.bisect_right(times, int(ts)) - 1
                if pos >= 0:
                    cell = asset_fwd[times[pos]]
        if not cell or 5 not in cell:
            return
        counts[(kind, direction)] += 1
        for h in HORIZONS:
            if h in cell:
                bucket[(kind, direction)][h].append(sign * cell[h])

    for asset, ts, sig_json in tech_rows:
        try:
            items = json.loads(sig_json or "[]")
        except Exception:
            continue
        seen: set[str] = set()
        for it in items:
            k = str(it.get("k") or "") if isinstance(it, dict) else ""
            if k in seen:
                continue
            seen.add(k)
            _absorb(asset, int(ts), k)

    lines: List[str] = []
    lines.append("# 技术信号后验复盘台账")
    lines.append("")
    lines.append(f"生成时间：{__import__('datetime').datetime.now().strftime('%Y-%m-%d %H:%M:%S')}　"
                 f"数据：`{args.db}`")
    lines.append("")
    lines.append("口径：`abnormal_signals` 技术证据按触发日重放，日线前向 "
                 + "/".join(f"{h}日" for h in HORIZONS) + "；"
                 + f"看涨取正、看跌取反；样本 <{MIN_SAMPLE} 标样本不足。")
    lines.append("")
    lines.append("| 因子 | 方向 | 样本 | 5日命中率 | 5日超额 | 5日结论 | 1日 | 3日 | 10日 |")
    lines.append("|---|---|---|---|---|---|---|---|---|")

    summary: List[Dict[str, Any]] = []
    for (kind, direction), vals in sorted(bucket.items()):
        n = counts[(kind, direction)]
        main_h = 5
        main = vals[main_h]
        edge = statistics.fmean(main) if main else None
        hit = (sum(1 for x in main if x > 0) / len(main) * 100.0) if main else None
        verdict = _verdict(hit, edge, n)

        def _cell(h: int) -> str:
            v = vals[h]
            if not v:
                return "—"
            e = statistics.fmean(v)
            ht = sum(1 for x in v if x > 0) / len(v) * 100.0
            return f"{ht:.1f}%/{e:.3f}"

        lines.append(
            f"| {kind} | {direction} | {n} | "
            f"{'—' if hit is None else f'{hit:.1f}%'} | "
            f"{'—' if edge is None else f'{edge:.3f}'} | {verdict} | "
            f"{_cell(1)} | {_cell(3)} | {_cell(10)} |"
        )
        summary.append({"kind": kind, "direction": direction, "n": n,
                        "hit": hit, "edge": edge, "verdict": verdict})

    lines.append("")
    lines.append("## 降级/观察建议")
    lines.append("")
    downgrade = [s for s in summary if s["verdict"] == "反向，建议降级/观察"]
    noise = [s for s in summary if s["verdict"] == "噪声，仅作背景证据"]
    if downgrade:
        for s in downgrade:
            lines.append(f"- **{s['kind']}**（{s['direction']}）：n={s['n']}，"
                         f"命中 {s['hit']:.1f}%，超额 {s['edge']:.3f} → 复核方向与阈值，"
                         f"必要时进 HOLD 白名单。")
    if noise:
        for s in noise:
            hit_s = "—" if s["hit"] is None else f"{s['hit']:.1f}%"
            edge_s = "—" if s["edge"] is None else f"{s['edge']:.3f}"
            lines.append(f"- {s['kind']}（{s['direction']}）：n={s['n']}，"
                         f"命中 {hit_s}，超额 {edge_s} → 维持背景证据，"
                         f"不单独直出方向。")
    if not downgrade and not noise:
        lines.append("- 本期无反向因子；噪声样本见上表。")

    report = "\n".join(lines)
    if not args.no_write:
        OUT_DIR.mkdir(parents=True, exist_ok=True)
        out_path = OUT_DIR / "signal_review.md"
        out_path.write_text(report, encoding="utf-8")
        print(f"[OK] 复盘台账 → {out_path}")
    else:
        print(report)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
