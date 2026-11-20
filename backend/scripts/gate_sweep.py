# -*- coding: utf-8 -*-
"""开仓闸阈值网格搜索（v447，只读）。

背景：v446 的 entry_guard 阈值是拿 11 笔小样本拍的（BUY≥0.85 / SELL≤0.15、
30 分钟同向 0.05%）。用 2030 笔历史信号一跑，位置闸在 1h 档反而是"反向"的。
所以这里把阈值扫一遍，回答两个问题：

  1. 入场位置（追得多深）到底有没有预测力？——算分方向相关系数，再看分桶。
  2. 同向确认（决策前已朝同方向走）在哪个窗口/门槛下最稳？——网格扫，看跨持有期是否一致。

口径与 v423 / v446 完全一致：
  - 位置只用**决策前已收盘**的 K 线（不用包含入场时刻那根，避免前视）；
  - 出场用 cost_horizon_eval.exit_at（尾部未走完/空洞超限一律跳过）；
  - 净收益 = 毛收益 - 该品种往返成本（默认 30bps，可换档）。

统计诚实原则：组合多 = 多重比较，所以只认「t≥2 且跨持有期同号且 n≥100」，
其余一律写成"扫出来的噪声"，不当作可用结论。

用法:
  python3 scripts/gate_sweep.py --days 60
  python3 scripts/gate_sweep.py --days 60 --cost-bps 5
"""
from __future__ import annotations

import argparse
import json
import math
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src_python"))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import cost_horizon_eval as ce
import entry_guard as eg

HORIZONS = [("20m", 1_200, "5m"), ("1h", 3_600, "15m"), ("4h", 14_400, "1h")]
POS_WINDOWS = [(20, 4), (60, 12), (120, 24)]      # (分钟, 5m 根数)
CONFIRM_WINDOWS = [10, 20, 30, 60, 90]            # 分钟
CONFIRM_MOVES = [0.05, 0.10, 0.15, 0.20, 0.30]    # 同向最小幅度 %
POS_THRESHOLDS = [0.85, 0.90, 0.95, 0.99]
MIN_N = 100


def _pearson(xs: List[float], ys: List[float]) -> Optional[float]:
    n = len(xs)
    if n < 3:
        return None
    mx, my = sum(xs) / n, sum(ys) / n
    sx = math.sqrt(sum((x - mx) ** 2 for x in xs))
    sy = math.sqrt(sum((y - my) ** 2 for y in ys))
    if sx <= 0 or sy <= 0:
        return None
    cov = sum((xs[i] - mx) * (ys[i] - my) for i in range(n))
    return round(cov / (sx * sy), 3)


def _t(a: List[float], b: List[float]) -> Optional[float]:
    """正 t = 前一组（放行）更好；等价 _welch_t 取负。"""
    v = eg._welch_t(a, b)
    return None if v is None else round(-v, 2)


def _stats(xs: List[float]) -> Dict[str, Any]:
    if not xs:
        return {"n": 0, "avg": None, "win_rate": None}
    avg = sum(xs) / len(xs)
    return {"n": len(xs), "avg": round(avg, 4),
            "win_rate": round(sum(1 for x in xs if x > 0) / len(xs), 4)}


def build_features(days: int, cost_bps: float) -> List[Dict[str, Any]]:
    conn = eg._ro_conn()
    try:
        since = int(__import__("time").time()) - days * 86400
        entries = ce.load_entries(conn, since_ts=since, limit=5000)
        assets = sorted({e["asset"] for e in entries})
        bars: Dict[str, Dict[str, List[Dict[str, float]]]] = {}
        for _, _, tf in HORIZONS:
            bars[tf] = {a: eg.load_ohlc(conn, a, tf, since - 2 * 86400) for a in assets}
    finally:
        conn.close()

    feats: List[Dict[str, Any]] = []
    for e in entries:
        asset, action = e["asset"], e["action"]
        if action not in ("BUY", "SELL"):
            continue
        ts, px = int(e["entry_ts"]), float(e["entry_price"])
        b5 = bars["5m"].get(asset) or []
        row: Dict[str, Any] = {"asset": asset, "action": action,
                               "entry_ts": ts, "entry_price": px}
        # 位置：各窗口分位（0=最低 1=最高）
        for win, nb in POS_WINDOWS:
            p = eg._bar_position(eg._prior_bars(b5, ts, 300, nb), px)
            row[f"pos{win}"] = p.get("position")
        # 同向：各窗口的已走幅度（%）
        prior5 = [b for b in b5 if b["ts"] <= ts]
        for cw in CONFIRM_WINDOWS:
            row[f"mv{cw}"] = _move_before(prior5, ts, px, cw)
        # 各持有期净收益
        row["net"] = {}
        for label, sec, tf in HORIZONS:
            bs = bars[tf].get(asset) or []
            closes = [(b["ts"], b["close"]) for b in bs]
            exit_px = ce.exit_at(closes, ts + sec, {"5m": 300, "15m": 900, "1h": 3_600}[tf])
            g = ce.gross_pct(action, px, exit_px) if exit_px else None
            row["net"][label] = round(g - cost_bps / 100.0, 4) if g is not None else None
        feats.append(row)
    return feats


def _move_before(prior5: List[Dict[str, float]], ts: int, px: float,
                 cw_min: int) -> Optional[float]:
    start = ts - cw_min * 60
    base = None
    for b in prior5:
        if b["ts"] >= start:
            base = b
            break
    if base is None or not base["close"]:
        return None
    return round((px / base["close"] - 1.0) * 100.0, 4)


def _loc_block(r: Dict[str, Any], key: str, th: float) -> bool:
    p = r.get(key)
    if p is None:
        return False
    if r["action"] == "BUY":
        return float(p) >= th
    return float(p) <= 1.0 - th


def sweep(feats: List[Dict[str, Any]]) -> Dict[str, Any]:
    out: Dict[str, Any] = {"entries": len(feats)}

    # ① 位置到底有没有预测力：分方向相关系数
    corr = {}
    for label in [h[0] for h in HORIZONS]:
        for win, _ in POS_WINDOWS:
            xs, ys = [], []
            for r in feats:
                if r.get(f"pos{win}") is None or r["net"].get(label) is None:
                    continue
                # 追的程度：BUY 越靠顶越"追"，SELL 越靠底越"追"
                follow = float(r[f"pos{win}"]) if r["action"] == "BUY" \
                    else 1.0 - float(r[f"pos{win}"])
                xs.append(follow)
                ys.append(float(r["net"][label]))
            corr[f"{label}_pos{win}"] = {"r": _pearson(xs, ys), "n": len(xs)}
    out["corr_follow_vs_net"] = corr

    # ② 位置闸网格（单参数）
    loc_grid: Dict[str, Any] = {}
    for label in [h[0] for h in HORIZONS]:
        for win, _ in POS_WINDOWS:
            key = f"pos{win}"
            for th in POS_THRESHOLDS:
                sel = [r for r in feats
                       if r.get(key) is not None and r["net"].get(label) is not None]
                blocked = [r for r in sel if _loc_block(r, key, th)]
                kept = [r for r in sel if not _loc_block(r, key, th)]
                b, k = _stats([r["net"][label] for r in blocked]), \
                       _stats([r["net"][label] for r in kept])
                delta = round((k["avg"] or 0) - (b["avg"] or 0), 4) if b["n"] and k["n"] else None
                # _welch_t(a,b) 返回 b-a，取负号让"正 t = 放行组更好"
                t = -eg._welch_t([r["net"][label] for r in kept],
                                 [r["net"][label] for r in blocked])
                loc_grid[f"{label}|{key}|{th:.2f}"] = {
                    "blocked": b, "kept": k, "delta": delta, "t": t,
                    "verdict": _v(b["n"] + k["n"], delta, t)}

    # ③ 同向闸网格
    conf_grid: Dict[str, Any] = {}
    for label in [h[0] for h in HORIZONS]:
        for cw in CONFIRM_WINDOWS:
            for mv in CONFIRM_MOVES:
                sel = [r for r in feats
                       if r.get(f"mv{cw}") is not None and r["net"].get(label) is not None]
                def same(r):
                    m = float(r[f"mv{cw}"])
                    return m >= mv if r["action"] == "BUY" else m <= -mv
                blocked = [r for r in sel if not same(r)]
                kept = [r for r in sel if same(r)]
                b, k = _stats([r["net"][label] for r in blocked]), \
                       _stats([r["net"][label] for r in kept])
                delta = round((k["avg"] or 0) - (b["avg"] or 0), 4) if b["n"] and k["n"] else None
                # _welch_t(a,b) 返回 b-a，取负号让"正 t = 放行组更好"
                t = -eg._welch_t([r["net"][label] for r in kept],
                                 [r["net"][label] for r in blocked])
                conf_grid[f"{label}|{cw}m|{mv:.2f}"] = {
                    "blocked": b, "kept": k, "delta": delta, "t": t,
                    "verdict": _v(b["n"] + k["n"], delta, t)}

    # ④ 跨持有期一致性：同向闸
    consistent = []
    for cw in CONFIRM_WINDOWS:
        for mv in CONFIRM_MOVES:
            ds, ts_ = [], []
            for label in [h[0] for h in HORIZONS]:
                g = conf_grid.get(f"{label}|{cw}m|{mv:.2f}") or {}
                if g.get("delta") is not None:
                    ds.append(g["delta"])
                    ts_.append(g.get("t"))
            if len(ds) == len(HORIZONS) and all(d > 0 for d in ds):
                consistent.append({"cond": f"{cw}m/{mv:.2f}%", "deltas": ds,
                                   "t": ts_, "min_delta": min(ds),
                                   "sig_all": all(x is not None and x >= 2.0 for x in ts_)})
    consistent.sort(key=lambda x: -x["min_delta"])

    # ⑤ 跨持有期一致性：位置闸
    loc_consistent = []
    for win, _ in POS_WINDOWS:
        for th in POS_THRESHOLDS:
            ds, ts_ = [], []
            for label in [h[0] for h in HORIZONS]:
                g = loc_grid.get(f"{label}|pos{win}|{th:.2f}") or {}
                if g.get("delta") is not None:
                    ds.append(g["delta"])
                    ts_.append(g.get("t"))
            if len(ds) == len(HORIZONS) and all(d > 0 for d in ds):
                loc_consistent.append({"cond": f"pos{win}≥{th:.2f}", "deltas": ds,
                                       "t": ts_, "min_delta": min(ds)})
    loc_consistent.sort(key=lambda x: -x["min_delta"])

    # ⑥ 选定组合分品种拆分：防止"只有一个品种在工作"被当成整条策略可用
    best_cw, best_mv = 20, 0.10
    by_asset: Dict[str, Any] = {}
    for asset in sorted({r["asset"] for r in feats}):
        def same(r):
            m = r.get(f"mv{best_cw}")
            if m is None:
                return None
            m = float(m)
            return m >= best_mv if r["action"] == "BUY" else m <= -best_mv

        for label in [h[0] for h in HORIZONS]:
            sel = [r for r in feats if r["asset"] == asset and r["net"].get(label) is not None]
            kept = [float(r["net"][label]) for r in sel if same(r) is True]
            blocked = [float(r["net"][label]) for r in sel if same(r) is False]
            if not kept and not blocked:
                continue
            d = None
            if kept and blocked:
                d = round(sum(kept) / len(kept) - sum(blocked) / len(blocked), 4)
            by_asset.setdefault(asset, {})[label] = {
                "kept": _stats(kept), "blocked": _stats(blocked),
                "delta": d, "t": _t(kept, blocked),
            }

    out.update({
        "location_grid": loc_grid,
        "confirm_grid": conf_grid,
        "by_asset_confirm": {"cond": f"{best_cw}m/{best_mv:.2f}%", "rows": by_asset},
        "location_consistent_positive": loc_consistent,
        "confirm_consistent_positive": consistent,
    })
    return out


def _v(n: int, delta: Optional[float], t: Optional[float]) -> str:
    if n < MIN_N:
        return f"n不足({n})"
    if delta is None:
        return "无可比"
    if t is not None and t >= 2.0:
        return f"有效（+{delta:.3f}pt，t={t:.2f}）"
    if delta > 0:
        return f"正向但不显著（+{delta:.3f}pt）"
    return f"反向（{delta:+.3f}pt）"


def main(argv: Sequence[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="开仓闸阈值网格搜索")
    ap.add_argument("--days", type=int, default=60)
    ap.add_argument("--cost-bps", type=float, default=30.0)
    ap.add_argument("--out", default=None, help="完整网格（含每个组合的 blocked/kept 明细）落盘路径")
    a = ap.parse_args(argv)
    feats = build_features(a.days, a.cost_bps)
    rep = sweep(feats)
    rep["days"] = a.days
    rep["cost_bps"] = a.cost_bps
    if a.out:
        Path(a.out).write_text(json.dumps(rep, ensure_ascii=False, indent=1), encoding="utf-8")
        print(f"完整网格已写入 {a.out}")
    print(json.dumps({k: v for k, v in rep.items() if not k.endswith("_grid")},
                     ensure_ascii=False, indent=2))
    print("\n--- 同向闸跨持有期（列表见上）---")
    for c in rep["confirm_consistent_positive"]:
        print(f"{c['cond']}: deltas={c['deltas']} t={c['t']}")
    print("\n--- 位置闸跨持有期 ---")
    for c in rep["location_consistent_positive"]:
        print(f"{c['cond']}: deltas={c['deltas']} t={c['t']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
