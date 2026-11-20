#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""最小可用回测跑道（v341）。

补的空缺：`backtest_runs` / `backtest_event_outcomes` 这两张表从未有过数据（
拿清空前备份对比也是 0），意味着**任何阈值/规则改动都无法回答"改完更好还是更差"**。
之前 S3（5 日超额命中 77.1%）和 E3（次日 49.62%）的口径冲突，也只能靠人工解释。

本脚本只吃 `ohlcv_daily`（历史全量保留，不受数据纪元影响），做单规则前视收益统计：

  规则   ret1d >= thresh      （当日涨幅 ≥ 阈值，即"快涨"）
  前瞻   fwd1 / fwd3 / fwd5  （信号日收盘 → N 日后收盘）
  产出   n / 胜率(fwd_h>0) / 均值 / 中位 / 25-75 分位 / 相对全样本基线超额

不写交易、不碰决策、不造伪信号 —— 只统计"如果按这条规则每天都信，历史会怎样"。

用法:
  python3 run_backtest.py --asset BTC --asset XAU --thresh 3 --horizon 5
  python3 run_backtest.py --all --thresh 3 --horizon 5 --apply   # 落 backtest_runs
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import statistics
import sys
import time
from typing import Any

GATE_MIN_BARS = 250       # 少于两年日线不做结论


def _load(conn, asset: str) -> list[tuple[int, float]]:
    rows = conn.execute(
        "SELECT ts, close FROM ohlcv_daily WHERE asset=? AND close>0 ORDER BY ts",
        (asset,),
    ).fetchall()
    return [(int(t), float(c)) for t, c in rows]


def _ret(closes: list[float], i: int, h: int) -> float | None:
    j = i + h
    if j >= len(closes):
        return None
    base = closes[i]
    if base <= 0:
        return None
    return (closes[j] - base) / base * 100.0


def backtest_rule(prices: list[tuple[int, float]], thresh: float,
                  horizon: int) -> dict[str, Any]:
    closes = [c for _, c in prices]
    ret1 = [None] + [(closes[i] - closes[i - 1]) / closes[i - 1] * 100.0
                     if closes[i - 1] > 0 else None for i in range(1, len(closes))]
    sig = [i for i, r in enumerate(ret1) if r is not None and r >= thresh]
    fwd = [f for f in (_ret(closes, i, horizon) for i in sig) if f is not None]
    base = [v for v in (_ret(closes, i, horizon) for i in range(len(closes))) if v is not None]

    if not fwd:
        return {"asset_n": len(fwd), "ok": False}
    win = sum(1 for f in fwd if f > 0) / len(fwd)
    base_avg = statistics.mean(base) if base else 0.0
    q = statistics.quantiles(fwd, n=4) if len(fwd) >= 4 else [0, 0, 0]
    return {
        "ok": True,
        "n": len(fwd),
        "winrate": round(win, 4),
        "avg_pct": round(statistics.mean(fwd), 2),
        "median_pct": round(statistics.median(fwd), 2),
        "p25": round(q[0], 2), "p75": round(q[2], 2),
        "base_avg_pct": round(base_avg, 2),
        "edge_pct": round(statistics.mean(fwd) - base_avg, 2),
        # 净超额 = 毛超额 − 往返成本：4bps*2 + 2bps*2 = 0.12%
        "cost_pct": round(round_trip_cost_pct(), 2),
        "net_edge_pct": round(statistics.mean(fwd) - base_avg - round_trip_cost_pct(), 2),
    }


def run(conn, assets: list[str], thresh: float, horizon: int, apply: bool) -> list[dict]:
    out = []
    for a in assets:
        prices = _load(conn, a)
        if len(prices) < GATE_MIN_BARS:
            out.append({"asset": a, "skipped": f"bars={len(prices)}<{GATE_MIN_BARS}"})
            print(f"  SKIP {a:8s} bars={len(prices)} 不足 {GATE_MIN_BARS}")
            continue
        r = backtest_rule(prices, thresh, horizon)
        r["asset"] = a
        if not r.get("ok"):
            print(f"  NONE {a:8s} 信号太少")
            out.append(r)
            continue
        print(f"  OK   {a:8s} bars={len(prices)} 信号n={r['n']:4d} "
              f"胜率={r['winrate']*100:5.1f}% 均值={r['avg_pct']:+6.2f}% "
              f"基线={r['base_avg_pct']:+6.2f}% 超额={r['edge_pct']:+6.2f}%")
        out.append(r)

    if apply:
        now = int(time.time())
        cur = conn.cursor()
        # 注意：backtest_runs 的 strategy_id / version_id 是 NOT NULL 且外键到
        # strategies(id) / strategy_versions__legacy(id) —— 要写进去就得先往配置表
        # 里造一条"策略"，那属于替用户决定策略注册，不擅自做。
        # 所以这里只写聚合统计表 backtest_event_outcomes，逐条明细落 JSON 审计文件。
        detail_path = None
        for r in out:
            if not r.get("ok"):
                continue
            params = json.dumps({"rule": "ret1d>=thresh", "thresh": thresh,
                                 "horizon": horizon, "asset": r["asset"]}, ensure_ascii=False)
            _ = params   # 明细落 JSON，不写 backtest_runs（见上）
            cur.execute(
                # v342: 改成 UPSERT。原来这里是裸 INSERT，而
                # backtest_event_outcomes 有 UNIQUE(type_code, asset) ——
                # 同一个阈值/窗口跑第二遍就直接崩 "UNIQUE constraint failed"，
                # 所以这张表一直停在第一次（甚至在第一次就崩在半截），
                # backtest_runs 也就永远是 0 行。
                "INSERT INTO backtest_event_outcomes (type_code, asset, sample_n, win_n, "
                "loss_n, avg_pnl_4h, last_updated) VALUES (?, ?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(type_code, asset) DO UPDATE SET "
                "sample_n=excluded.sample_n, win_n=excluded.win_n, loss_n=excluded.loss_n, "
                "avg_pnl_4h=excluded.avg_pnl_4h, last_updated=excluded.last_updated",
                (f"ret1d>={thresh:g}_fwd{horizon}d", r["asset"], r["n"],
                 int(round(r["winrate"] * r["n"])) if r["n"] else 0,
                 r["n"] - int(round(r["winrate"] * r["n"])) if r["n"] else 0,
                 r["avg_pct"], now),
            )
        conn.commit()
        # 明细审计
        try:
            import os
            os.makedirs(os.path.join("runtime", "backtest"), exist_ok=True)
            detail_path = os.path.join("runtime", "backtest", f"run_{now}.json")
            with open(detail_path, "w", encoding="utf-8") as f:
                json.dump({"thresh": thresh, "horizon": horizon, "created_at": now,
                           "rows": out}, f, ensure_ascii=False, indent=1)
        except Exception as e:
            print(f"[WARN] 明细未落盘: {e}")
        print(f"[APPLY] backtest_event_outcomes 写 {sum(1 for r in out if r.get('ok'))} 行"
              f" · 明细 {detail_path}")
    return out


MIN_SAMPLE = 30      # 单一阈值下至少这么多样本才参与"选优"，否则容易选出噪声
MIN_EDGE = 0.20      # 净超额低于 0.2% 视为"薄利"
# 成本默认：taker 手续费 4bps + 滑点 2bps，一开一平两趟 → 合计 12bps = 0.12%
DEFAULT_FEE_BPS = 4.0
DEFAULT_SLIP_BPS = 2.0


def round_trip_cost_pct(fee_bps: float = DEFAULT_FEE_BPS,
                        slip_bps: float = DEFAULT_SLIP_BPS) -> float:
    """一开一平往返成本，百分数。"""
    return (fee_bps + slip_bps) * 2 / 100.0


def pick_best(rows: list[dict], min_sample: int = MIN_SAMPLE) -> dict[str, Any]:
    """在给定品种的多阈值结果里选一个最优阈值。

    不是单纯取最大超额 —— 小样本的高超额是噪声。规则：
      1. 只考虑 n >= min_sample 的阈值；
      2. 在这些里取 edge_pct 最大；
      3. 若最优 edge <= 0 → 判 blocked（这个品种上"追快涨"是负期望，不该做多）；
      4. 若没有任何阈值满足样本下限 → insufficient（阈值对该品种太极端，样本不够）。
    """
    usable = [r for r in rows if r.get("ok") and r.get("n", 0) >= min_sample]
    if not usable:
        return {"verdict": "insufficient", "reason": f"没有阈值达到 n>={min_sample}"}
    best = max(usable, key=lambda r: r.get("net_edge_pct", r["edge_pct"]))
    net = best.get("net_edge_pct", best["edge_pct"])
    if net < MIN_EDGE:
        if net <= 0:
            return {"verdict": "blocked", "thresh": best["thresh"],
                    "edge_pct": best["edge_pct"], "net_edge_pct": net,
                    "winrate": best["winrate"], "n": best["n"],
                    "reason": f"扣完成本仍为负({net:+.2f}%)：该品种追涨是负期望"}
        return {"verdict": "weak", "thresh": best["thresh"], "edge_pct": best["edge_pct"],
                "net_edge_pct": net, "winrate": best["winrate"], "n": best["n"],
                "reason": f"净超额仅 {net:+.2f}%，不值得单独做"}
    return {"verdict": "use", "thresh": best["thresh"], "edge_pct": best["edge_pct"],
            "net_edge_pct": net, "winrate": best["winrate"], "n": best["n"],
            "reason": f"净超额 {net:+.2f}% / 胜率 {best['winrate']*100:.1f}%"}


def sweep(conn, assets: list[str], threshes: list[float], horizon: int,
          min_sample: int = MIN_SAMPLE, apply: bool = False) -> dict[str, Any]:
    """多阈值 × 多品种矩阵扫描，给每个品种一个"该用什么阈值"的判定。"""
    matrix = {}
    for a in assets:
        prices = _load(conn, a)
        if len(prices) < GATE_MIN_BARS:
            continue
        rows = []
        for t in threshes:
            r = backtest_rule(prices, t, horizon)
            if r.get("ok"):
                r["thresh"] = t
                rows.append(r)
        if not rows:
            continue
        matrix[a] = {"bars": len(prices), "rows": rows, "pick": pick_best(rows, min_sample)}

    out = {"horizon": horizon, "threshes": threshes, "min_sample": min_sample,
           "cost_pct": round_trip_cost_pct(), "assets": matrix}
    if apply:
        import os
        now = int(time.time())
        os.makedirs(os.path.join("runtime", "backtest"), exist_ok=True)
        path = os.path.join("runtime", "backtest", f"threshold_map_{now}.json")
        with open(path, "w", encoding="utf-8") as f:
            json.dump(out, f, ensure_ascii=False, indent=1)
        out["path"] = path
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--asset", action="append", default=[])
    ap.add_argument("--all", action="store_true")
    ap.add_argument("--thresh", type=float, default=3.0)
    ap.add_argument("--horizon", type=int, default=5)
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--sweep", action="store_true", help="多阈值矩阵扫描，给每品种选阈值")
    ap.add_argument("--threshes", default="2,3,5,8")
    ap.add_argument("--min-n", type=int, default=MIN_SAMPLE)
    args = ap.parse_args()

    import db
    conn = sqlite3.connect(db.get_db_path(), timeout=30)
    conn.execute("PRAGMA busy_timeout=30000")

    assets = args.asset
    if args.all or not assets:
        assets = [r[0] for r in conn.execute(
            "SELECT asset, COUNT(*) n FROM ohlcv_daily GROUP BY asset "
            f"HAVING n>={GATE_MIN_BARS} ORDER BY n DESC").fetchall()]

    if args.sweep:
        ths = [float(x) for x in args.threshes.split(",") if x.strip()]
        print(f"=== 阈值矩阵扫描 · 前瞻 {args.horizon} 日 · 阈值 {ths} ===")
        res = sweep(conn, assets, ths, args.horizon, args.min_n, args.apply)
        print(f"{'品种':<10}{'判定':<12}{'阈值':>6}{'毛超额':>9}{'净超额':>9}{'胜率':>9}{'n':>7}")
        for a, m in res["assets"].items():
            pk = m["pick"]
            if pk["verdict"] == "insufficient":
                print(f"{a:<10}{'样本不足':<12}{'-':>6}{'-':>9}{'-':>9}{'-':>7}")
            else:
                label = {"blocked": "禁追涨", "weak": "薄利勿用", "use": "可用"}[pk["verdict"]]
                print(f"{a:<10}{label:<12}"
                      f"{pk['thresh']:>6g}{pk['edge_pct']:>+9.2f}{pk.get('net_edge_pct', 0):>+9.2f}"
                      f"{pk['winrate']*100:>8.1f}%{pk['n']:>7d}")
        print(f"\n写入 {res.get('path') or '(试运行未落盘，加 --apply)'}")
        return 0

    print(f"=== 回测规则 ret1d>={args.thresh:g}% · 前瞻 {args.horizon} 日 ===")
    res = run(conn, assets, args.thresh, args.horizon, args.apply)
    if not args.apply:
        print("\n(试运行，未落库 — 加 --apply 写入 backtest_runs)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
