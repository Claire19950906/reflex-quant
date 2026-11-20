# -*- coding: utf-8 -*-
"""opt_move_shape.py — v336 用"冲击形状"替代拍脑袋的固定阈值

问题:
    现在 fast/slow 的判定是两条硬阈值——
        fast: |1h| ≥ 1.5%        slow: |1d| ≥ 3%
    阈值只能回答"幅度够不够大", 回答不了"这波是怎么走出来的":
    同样是 1 小时 1.5%, 一根针插上去又弹回, 和一路阴跌, 是两种完全不同的信号。

做法:
    1. 按 duration 选合适的 K 线周期（1m/5m/15m/1h）取窗口内收盘序列;
    2. `fit_shape()` 用 logistic 拟合累计路径 y(t)=A/(1+e^{-k(t-t0)}),
       网格搜索 k / t0, 报 R² —— R² 低说明这段走势本身不像单峰冲击;
    3. 用**峰值出现的位置 + 单根最大跳变占全程的比例**分类:
         peak_frac ≤ 0.25            → attack（快涨/快跌：冲击集中在前 1/4）
         peak_frac ≥ 0.60            → drift（慢涨/慢跌：一路磨上去/磨下来）
         0.25 < peak_frac < 0.60     → mid
         单根跳变 ≥ 50% 全程          → gap（跳空/一步到位）
         R² < 0.60                   → irregular（来回震，不像单向冲击）
    4. 与现有 move_kind 对账, 输出混淆矩阵与一致率。

只读：不写库, 不改 critical_price_moves 的任何标签。
"""
from __future__ import annotations

import argparse
import json
import math
import sqlite3
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

BACKEND = Path(__file__).resolve().parents[1]
DB = BACKEND / "trident_event_bus.db"

MIN_BARS = 8
R2_MIN = 0.60
GAP_FRAC = 0.50


def _ro() -> sqlite3.Connection:
    c = sqlite3.connect(f"file:{DB}?mode=ro", uri=True, timeout=30)
    c.row_factory = sqlite3.Row
    return c


def pick_timeframe(duration_min: int) -> str:
    if duration_min <= 90:
        return "1m"
    if duration_min <= 360:
        return "5m"
    if duration_min <= 1440:
        return "15m"
    return "1h"


def detect_ts_scale(conn: sqlite3.Connection) -> int:
    """kline_bars.ts 有的库是秒、有的是毫秒 —— 用最大值判断，别猜。"""
    m = conn.execute("SELECT max(ts) m FROM kline_bars").fetchone()["m"]
    return 1000 if (m or 0) > 10 ** 12 else 1


def load_bars(conn: sqlite3.Connection, asset: str, ts: int, duration_min: int,
              timeframe: Optional[str] = None, scale: int = 1) -> List[Tuple[int, float]]:
    """K 线优先；kline_bars 没覆盖的品种回退 ohlcv_intraday（1m/15m）。

    实测缺口：kline_bars 只覆盖 BTC/ETH/SOL/XAU，而 WTI/MU/SKHYNIX/BNB/XRP
    的分钟线在 ohlcv_intraday 里 —— 不接这条回退，形状分类会有一半品种直接失效。
    """
    tf = timeframe or pick_timeframe(duration_min)
    # 关键口径：critical_price_moves.ts 是**异动终点（峰值时刻）**，不是起点。
    # 实测 BTC 2026-11-13 07:01 那条：start_price 68046 → peak 69260 = ts 那一刻，
    # ts 之后价格是回落的。窗口必须取 [ts-duration, ts]，否则整条序列符号是反的。
    start, end = (int(ts) - duration_min * 60) * scale, int(ts) * scale
    rows = conn.execute(
        "SELECT ts, close FROM kline_bars WHERE symbol=? AND timeframe=? "
        "AND ts>=? AND ts<=? AND close IS NOT NULL ORDER BY ts",
        (asset, tf, start, end)).fetchall()
    if len(rows) < MIN_BARS:
        # 回退：ohlcv_intraday（秒级时间戳）
        iv = "15m" if tf in ("15m", "1h", "4h") else "1m"
        rows = conn.execute(
            "SELECT ts, close FROM ohlcv_intraday WHERE asset=? AND interval=? "
            "AND ts>=? AND ts<=? AND close IS NOT NULL ORDER BY ts",
            (asset, iv, start // scale, end // scale)).fetchall()
    if len(rows) < MIN_BARS and tf != "1m":
        rows = conn.execute(
            "SELECT ts, close FROM kline_bars WHERE symbol=? AND timeframe='1m' "
            "AND ts>=? AND ts<=? AND close IS NOT NULL ORDER BY ts",
            (asset, start, end)).fetchall()
    return [(int(r["ts"]) // scale, float(r["close"])) for r in rows]


# ---------------------------------------------------------------- 形状拟合

def _r2(y: Sequence[float], yhat: Sequence[float]) -> float:
    mean = sum(y) / len(y)
    sst = sum((v - mean) ** 2 for v in y)
    if sst <= 1e-12:
        return 0.0
    sse = sum((a - b) ** 2 for a, b in zip(y, yhat))
    return max(0.0, 1.0 - sse / sst)


def fit_shape(bars: Sequence[Tuple[int, float]]) -> Optional[Dict[str, Any]]:
    """对一段价格序列拟合冲击形状。返回形状特征。

    关键口径（踩过的坑）:
        不能拿"极值出现的位置"当快慢判据 —— 单边上涨的极值永远在窗口末尾,
        于是所有走势都会被误判成 drift。这里改用 **t80**:
        "走到全程 80% 用了窗口的几成时间", 才是"快"与"慢"的区别。
    """
    if len(bars) < MIN_BARS:
        return None
    t0 = bars[0][0]
    span = max(1, bars[-1][0] - t0)
    xs = [(t - t0) / span for t, _ in bars]          # 0..1
    base = bars[0][1]
    if base <= 0:
        return None
    ys = [((c - base) / base) * 100 for _, c in bars]
    final = ys[-1]
    sign = 1 if final >= 0 else -1
    aligned = [sign * y for y in ys]                  # 朝最终方向为正
    amp = max(aligned)
    if amp <= 1e-9:
        return {"label": "flat", "r2": 0.0, "t80": None, "retrace": 0.0,
                "gap_frac": 0.0, "total_pct": round(final, 3),
                "n_bars": len(bars), "timeframe": None}

    # t80: 首次走到 80% 幅度的时点（窗口占比）
    t80_idx = next((i for i, v in enumerate(aligned) if v >= 0.8 * amp), len(aligned) - 1)
    t80 = xs[t80_idx]

    # 回撤：偏离历史最高的最大比例（V 型/来回震 → irregular）
    run_max, retrace = aligned[0], 0.0
    for v in aligned:
        run_max = max(run_max, v)
        retrace = max(retrace, (run_max - v) / amp)

    steps = [abs(aligned[i + 1] - aligned[i]) for i in range(len(aligned) - 1)]
    max_step = max(steps) if steps else 0.0
    gap_frac = max_step / amp

    # logistic 拟合（幅度固定为 amp，k / t0 自由）—— 衡量"像不像一次性冲击"
    best = (-1.0, 4.0, 0.5)
    for k in (4, 6, 8, 10, 12, 16, 20, 26, 32, 40):
        for t0grid in [i / 20 for i in range(1, 19)]:
            yhat = [amp / (1 + math.exp(-k * (x - t0grid))) for x in xs]
            score = _r2(aligned, yhat)
            if score > best[0]:
                best = (score, k, t0grid)
    r2, k_best, t0_best = best

    if retrace >= 0.50:
        label = "irregular"      # 冲高回落 / 来回震 → 不是单向冲击
    elif gap_frac >= GAP_FRAC:
        label = "gap"
    elif t80 <= 0.25:
        label = "attack"
    elif t80 >= 0.60:
        label = "drift"
    else:
        label = "mid"

    return {
        "label": label, "r2": round(r2, 3), "t80": round(t80, 3),
        "retrace": round(retrace, 3), "gap_frac": round(gap_frac, 3),
        "total_pct": round(final, 3), "amp_pct": round(amp * sign, 3),
        "k": k_best, "t0": round(t0_best, 3), "n_bars": len(bars),
        "timeframe": None,
    }


def shape_to_kind(label: str, move_pct: float) -> Optional[str]:
    """把形状映射成现有四类标签（方向由符号决定）。"""
    up = move_pct > 0
    if label == "attack":
        return "fast_rise" if up else "fast_fall"
    if label == "drift":
        return "slow_rise" if up else "slow_fall"
    return None


def scan(conn: sqlite3.Connection, limit: int = 0) -> List[Dict[str, Any]]:
    sql = ("SELECT id, asset, ts, move_pct, move_kind, duration_min FROM critical_price_moves "
           "ORDER BY ts DESC")
    if limit:
        sql += f" LIMIT {int(limit)}"
    scale = detect_ts_scale(conn)
    out = []
    for r in conn.execute(sql):
        tf = pick_timeframe(int(r["duration_min"] or 60))
        bars = load_bars(conn, r["asset"], int(r["ts"]), int(r["duration_min"] or 60), tf, scale)
        f = fit_shape(bars)
        if not f:
            out.append({**dict(r), "shape": "no_bars", "r2": None, "t80": None,
                        "gap_frac": None, "timeframe": tf, "agree": None})
            continue
        f["timeframe"] = tf
        kind2 = shape_to_kind(f["label"], float(r["move_pct"]))
        agree = (kind2 == r["move_kind"]) if kind2 else None
        out.append({**dict(r), "shape": f["label"], "r2": f["r2"],
                    "t80": f.get("t80"), "gap_frac": f["gap_frac"],
                    "timeframe": tf, "suggested_kind": kind2, "agree": agree})
    return out


def summarize(rows: List[Dict[str, Any]]) -> Dict[str, Any]:
    shapes: Dict[str, int] = {}
    conf: Dict[str, Dict[str, int]] = {}
    agree_n = comparable = 0
    for r in rows:
        shapes[r["shape"]] = shapes.get(r["shape"], 0) + 1
        if r["shape"] != "no_bars":
            c = conf.setdefault(r["move_kind"], {})
            c[r["shape"]] = c.get(r["shape"], 0) + 1
        if r["agree"] is not None:
            comparable += 1
            agree_n += 1 if r["agree"] else 0
    return {
        "n": len(rows), "shapes": shapes,
        "comparable": comparable, "agree": agree_n,
        "agree_pct": round(100.0 * agree_n / comparable, 1) if comparable else None,
        "confusion": conf,
        "by_old_kind": {k: sum(1 for r in rows if r["move_kind"] == k) for k in
                        {r["move_kind"] for r in rows}},
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()
    conn = _ro()
    try:
        rows = scan(conn, args.limit)
        rep = summarize(rows)
    finally:
        conn.close()

    if args.json:
        print(json.dumps(rep, ensure_ascii=False, indent=2))
        return 0
    print(f"可拟合 {rep['comparable']} / {rep['n']} 条（其余年代久远/无分钟线）")
    print("形状分布（形状与旧 fast/slow 标签是**正交的两件事**：")
    print("  旧标签回答'窗口多长'，形状回答'这波是怎么走出来的'）：")
    for k, v in sorted(rep["shapes"].items(), key=lambda x: -x[1]):
        print(f"  {k:<10} {v}")
    print("\n旧标签内部的形状构成：")
    for k, v in rep["confusion"].items():
        total = sum(v.values())
        mix = ", ".join(f"{kk} {100.0*vv/total:.0f}%" for kk, vv in
                        sorted(v.items(), key=lambda x: -x[1]))
        print(f"  {k:<10} ({total} 条) → {mix}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
