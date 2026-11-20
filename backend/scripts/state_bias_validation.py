#!/usr/bin/env python3
"""状态读数的有效性回测：偏置（bias）到底有没有预测力。

为什么做这个：
  v402 只验证了"单个因子"（tech_signals 里的 rsi_oversold / div_top …），
  而技术状态层是新的一层 —— 它把六个维度压成一个 [-1,1] 的偏置。这一层从来没被验过。
  如果不验，它就是又一个"看起来很懂市场"的黑箱。

方法：
  · 逐时点**因果重算**（asof_ts：只用当时能看到的数据），不做前视。
  · 每个时点记录：偏置、覆盖度、usable、各分量方向。
  · 前瞻收益取同一 K 线序列的 +h 根（1d 周期即 +1/3/5/10 日），
    有符号收益 signed = fwd × sign(bias)：>0 表示"偏置方向对了"。
  · 同时给出每个**分量**单独的方向命中率 —— 因为历史里 OI/主动成交只有 3 天，
    杠杆/主动性/持仓三维在回测里根本不存在，能验的只有价格派生的三维。

诚实边界：
  · 这不是策略回测（没扣手续费、没做仓位），只回答"方向对不对"。
  · 命中率 52% 和 48% 在 n=600 时都可能落在噪声区间内，报告一律给样本数与区间提示。
  · 1d 周期下 OI/资金费率/多空比历史上没有，覆盖度上限只有 0.60 —— 结论只对"价格结构"负责。

用法：
  python3 scripts/state_bias_validation.py --asset BTC --timeframe 1d --samples 600
"""
import argparse
import json
import math
import os
import sqlite3
import sys
import time
from typing import Any, Dict, List, Optional, Tuple

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "src_python"))
import technical_state as ts_state

DEFAULT_DB = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "trident_event_bus.db")
REPORT_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "reports", "state_bias")
HORIZONS = (1, 3, 5, 10)
WARMUP = 300          # 至少这么多根，指标才稳定（240 根窗口 + 余量）
USABLE_EDGE = 0.45    # 与 technical_state.bias_of 同门槛


def load_series(conn: sqlite3.Connection, asset: str, timeframe: str) -> List[Tuple[int, float]]:
    # 与 technical_state.load_candles 同口径：按 ts 去重（XAU 历史段有大量重复 ts 行）
    rows = conn.execute(
        """SELECT ts, close FROM (
               SELECT ts, close, ROW_NUMBER() OVER (PARTITION BY ts ORDER BY rowid DESC) AS rn
               FROM kline_bars WHERE symbol=? AND timeframe=? AND close IS NOT NULL)
           WHERE rn=1 ORDER BY ts""", (asset, timeframe)).fetchall()
    return [(int(r[0]) // 1000, float(r[1])) for r in rows]


def spearman(x: List[float], y: List[float]) -> Optional[float]:
    """秩相关（不引 scipy，样本量小、够用）。"""
    n = len(x)
    if n < 20:
        return None

    def rank(vals: List[float]) -> List[float]:
        order = sorted(range(n), key=lambda i: vals[i])
        rk = [0.0] * n
        i = 0
        while i < n:
            j = i
            while j + 1 < n and vals[order[j + 1]] == vals[order[i]]:
                j += 1
            avg = (i + j) / 2.0 + 1
            for k in range(i, j + 1):
                rk[order[k]] = avg
            i = j + 1
        return rk

    rx, ry = rank(x), rank(y)
    mx, my = sum(rx) / n, sum(ry) / n
    cov = sum((a - mx) * (b - my) for a, b in zip(rx, ry))
    den = math.sqrt(sum((a - mx) ** 2 for a in rx) * sum((b - my) ** 2 for b in ry))
    return cov / den if den else None


def data_quality(series: List[Tuple[int, float]]) -> Dict[str, Any]:
    """开盘序列体检：重复收盘价占比 + 主间隔占比。

    为什么必须查：XAU 的 kline_bars 实测 55%~67% 的相邻收盘完全相同、时间间隔不规整，
    这类"假 K 线"喂进任何回测都会算出荒谬的前瞻收益（实测 12h 上涨占比只有 15%）。
    回测前先证明数据是数据，否则算得再漂亮都是错的。
    """
    if len(series) < 50:
        return {"ok": False, "reason": f"只有 {len(series)} 根"}
    closes = [c for _, c in series]
    ts = [t for t, _ in series]
    dup = sum(1 for i in range(1, len(closes)) if closes[i] == closes[i - 1]) / (len(closes) - 1) * 100
    gaps = sorted(ts[i] - ts[i - 1] for i in range(1, len(ts)))
    main_gap = gaps[len(gaps) // 2]
    main_share = sum(1 for g in gaps if g == main_gap) / len(gaps) * 100
    return {"ok": dup <= 20 and main_share >= 50,
            "dup_close_pct": round(dup, 1), "gap_main_sec": main_gap,
            "gap_main_share_pct": round(main_share, 1)}


def bucket_of(bias: Optional[float], coverage: float) -> str:
    if bias is None or coverage < 0.5:
        return "覆盖不足"
    if bias <= -USABLE_EDGE:
        return "强空(≤-0.45)"
    if bias <= -0.15:
        return "偏空"
    if bias < 0.15:
        return "中性"
    if bias < USABLE_EDGE:
        return "偏多"
    return "强多(≥0.45)"


def summarize(rows: List[Dict[str, Any]], horizon: int, key: str = "bias") -> Dict[str, Any]:
    """按信号强度分桶，算方向命中率与平均同向收益。"""
    buckets: Dict[str, List[float]] = {"强空(≤-0.45)": [], "偏空": [], "中性": [], "偏多": [],
                                       "强多(≥0.45)": [], "覆盖不足": []}
    all_pairs: List[Tuple[float, float]] = []          # (signal, fwd)
    signed: List[float] = []
    for r in rows:
        sig = r.get(key)
        fwd = r["fwd"].get(horizon)
        if sig is None or fwd is None:
            continue
        if key == "bias":
            b = bucket_of(sig, r.get("coverage") or 0.0)
        else:
            b = ("空" if sig < 0 else "多") if r.get("coverage", 0) >= 0.5 else "覆盖不足"
        buckets.setdefault(b, []).append(fwd)
        all_pairs.append((sig, fwd))
        signed.append(fwd * (1 if sig > 0 else -1))

    out = {"horizon": horizon, "n": len(all_pairs)}
    out["ic"] = round(spearman([p[0] for p in all_pairs], [p[1] for p in all_pairs]) or 0.0, 3) \
        if all_pairs else None
    out["hit"] = round(sum(1 for s in signed if s > 0) / len(signed) * 100, 2) if signed else None
    # 基线：同一批样本里"无脑做多"的上涨占比。偏置的命中率必须拿来跟它比 ——
    # 如果偏置 51% 而无脑做多 55%，那这个偏置方向是负贡献的。
    out["base_up"] = round(sum(1 for p in all_pairs if p[1] > 0) / len(all_pairs) * 100, 2) if all_pairs else None
    out["signed_mean"] = round(sum(signed) / len(signed), 3) if signed else None
    out["abs_mean"] = round(sum(abs(p[1]) for p in all_pairs) / len(all_pairs), 3) if all_pairs else None
    out["buckets"] = []
    for name, vals in buckets.items():
        if not vals:
            continue
        hit = sum(1 for v in vals if v > 0) / len(vals) * 100
        mean = sum(vals) / len(vals)
        out["buckets"].append({"bucket": name, "n": len(vals),
                               "mean_fwd": round(mean, 3), "up_ratio": round(hit, 2)})
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--asset", default="BTC")
    ap.add_argument("--timeframe", default="1d")
    ap.add_argument("--samples", type=int, default=600, help="取最近 N 个可评估时点")
    ap.add_argument("--step", type=int, default=1, help="每隔几根取一个时点")
    ap.add_argument("--shift-back", type=int, default=0,
                    help="把评估窗口整体往前挪 N 个时点（用于跨行情阶段复验，避免只看最近一段）")
    ap.add_argument("--warmup", type=int, default=WARMUP, help="暖机根数（指标需要的前置样本）")
    ap.add_argument("--db", default=DEFAULT_DB)
    ap.add_argument("--no-write", action="store_true")
    args = ap.parse_args()

    conn = sqlite3.connect(f"file:{args.db}?mode=ro", uri=True)
    series = load_series(conn, args.asset, args.timeframe)
    if len(series) < args.warmup + max(HORIZONS):
        print(f"[ERR] {args.asset} {args.timeframe} 只有 {len(series)} 根，样本不足", flush=True)
        return 1

    quality = data_quality(series)
    print(f"[INFO] 数据体检：重复收盘 {quality.get('dup_close_pct')}% | "
          f"主间隔 {quality.get('gap_main_sec')}s 占 {quality.get('gap_main_share_pct')}% | "
          f"{'通过' if quality.get('ok') else '不通过'}", flush=True)

    max_h = max(HORIZONS)
    eligible = list(range(args.warmup, len(series) - max_h, args.step))
    end = len(eligible) - args.shift_back
    eligible = eligible[max(0, end - args.samples):end]
    print(f"[INFO] {args.asset} {args.timeframe} 共 {len(series)} 根，"
          f"评估 {len(eligible)} 个时点，从 {time.strftime('%Y-%m-%d', time.localtime(series[eligible[0]][0]))} 起",
          flush=True)

    rows: List[Dict[str, Any]] = []
    t0 = time.time()
    for n, i in enumerate(eligible, 1):
        ts_i, c_i = series[i]
        state = ts_state.build_state(conn, args.asset, args.timeframe, 240, asof_ts=ts_i)
        b = ts_state.bias_of(state)
        fwd = {}
        for h in HORIZONS:
            if i + h < len(series):
                fwd[h] = (series[i + h][1] - c_i) / c_i * 100.0
        rows.append({"ts": ts_i, "close": c_i, "bias": b.get("bias"),
                     "coverage": b.get("coverage"), "usable": b.get("usable"),
                     "components": b.get("components") or {}, "missing": b.get("missing") or [],
                     "fwd": fwd})
        if n % 100 == 0:
            print(f"  ... {n}/{len(eligible)}（{round(time.time() - t0, 1)}s）", flush=True)

    bias_stats = [summarize(rows, h) for h in HORIZONS]
    # 分量单独评定：只有覆盖度 ≥0.5 的时点参与，按分量方向与前瞻收益同向率
    comp_names = sorted({k for r in rows for k in r["components"]})
    comp_rows: List[Dict[str, Any]] = []
    for cname in comp_names:
        tmp = [r for r in rows if r["components"].get(cname) is not None]
        if len(tmp) < 30:
            comp_rows.append({"component": cname, "n": len(tmp), "note": "样本 <30，不给结论"})
            continue
        entry: Dict[str, Any] = {"component": cname, "n": len(tmp)}
        for h in HORIZONS:
            pairs = [(r["components"][cname], r["fwd"][h]) for r in tmp if r["fwd"].get(h) is not None]
            if not pairs:
                continue
            hit = sum(1 for s, f in pairs if (f > 0) == (s > 0)) / len(pairs) * 100
            entry[f"h{h}"] = {"hit": round(hit, 2),
                              "ic": round(spearman([p[0] for p in pairs], [p[1] for p in pairs]) or 0.0, 3)}
        comp_rows.append(entry)

    payload = {
        "meta": {"asset": args.asset, "timeframe": args.timeframe, "data_quality": quality,
                 "samples": len(rows), "lean": USABLE_EDGE,
                 "window": [rows[0]["ts"], rows[-1]["ts"]] if rows else None,
                 "baseline_abs_ret": [round(sum(abs(r["fwd"][h]) for r in rows if r["fwd"].get(h))
                                             / max(1, sum(1 for r in rows if r["fwd"].get(h))), 3)
                                      for h in HORIZONS]},
        "bias": bias_stats,
        "components": comp_rows,
        "rows": rows,
    }

    stamp = time.strftime("%Y-%m-%d")
    print("\n=== 偏置 ===", flush=True)
    for s in bias_stats:
        print(f"h={s['horizon']}  n={s['n']}  IC={s['ic']}  命中={s['hit']}%  "
              f"基线(无脑做多)={s['base_up']}%  同向均值={s['signed_mean']}%  绝对均值={s['abs_mean']}%",
              flush=True)
        for bk in s["buckets"]:
            print(f"    {bk['bucket']:14s} n={bk['n']:4d} 均值 {bk['mean_fwd']:7.3f}% 上涨占比 {bk['up_ratio']:5.2f}%",
                  flush=True)
    print("\n=== 分量 ===", flush=True)
    for c in comp_rows:
        if c.get("note"):
            print(f"{c['component']:12s} n={c['n']} {c['note']}", flush=True)
            continue
        bits = " | ".join(f"h{h}: 命中 {c[f'h{h}']['hit']}% IC {c[f'h{h}']['ic']}"
                          for h in HORIZONS if f"h{h}" in c)
        print(f"{c['component']:12s} n={c['n']:4d} {bits}", flush=True)

    verdict = (_verdict(bias_stats, rows) if quality.get("ok")
               else f"**数据不可用**：重复收盘 {quality.get('dup_close_pct')}%、"
                    f"主间隔仅占 {quality.get('gap_main_share_pct')}% —— K 线本身不成序列，"
                    f"任何前瞻收益都不可信（结论已作废）")
    print(f"\n[结论] {verdict}", flush=True)
    payload["verdict"] = verdict

    if not args.no_write:
        os.makedirs(REPORT_DIR, exist_ok=True)
        tag = f"{args.asset}_{args.timeframe}" + (f"_back{args.shift_back}" if args.shift_back else "")
        jpath = os.path.join(REPORT_DIR, f"{tag}.json")
        with open(jpath, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, ensure_ascii=False)
        mpath = os.path.join(REPORT_DIR, f"{tag}.md")
        with open(mpath, "w", encoding="utf-8") as fh:
            fh.write(_markdown(payload))
        print(f"[OK] {jpath}\n[OK] {mpath}", flush=True)
        _write_index()
    return 0


def _write_index() -> None:
    """把目录下所有结果汇总成一页，避免每次只看到单资产一张表。"""
    import glob
    items = []
    for fp in glob.glob(os.path.join(REPORT_DIR, "*.json")):
        try:
            with open(fp, encoding="utf-8") as fh:
                d = json.load(fh)
            m = d["meta"]
            h3 = next((x for x in d["bias"] if x["horizon"] == 3), d["bias"][0])
            items.append((m["asset"], m["timeframe"], m, h3, d.get("verdict", ""), d["meta"]["data_quality"]))
        except Exception:
            continue
    if not items:
        return
    items.sort(key=lambda x: (x[0], x[1]))
    lines = ["# 状态偏置有效性 · 汇总", "",
             "口径：因果重算（asof_ts 截断），前瞻 3 根 K 线；命中 = 偏置方向与收益同向比例；"
             "基线 = 同一批样本无脑做多的上涨占比。", "",
             "| 资产 | 周期 | 时段 | n | IC | 方向命中 | 基线 | 结论 |",
             "|---|---|---|---|---|---|---|---|"]
    for asset, tf, m, h3, verdict, q in items:
        win = (f"{time.strftime('%Y-%m-%d', time.localtime(m['window'][0]))}"
               f"→{time.strftime('%Y-%m-%d', time.localtime(m['window'][1]))}") if m.get("window") else "-"
        lines.append(f"| {asset} | {tf} | {win} | {h3['n']} | {h3['ic']} | {h3['hit']}% | "
                     f"{h3['base_up']}% | {verdict} |")
    lines += ["", "> 每个资产/周期的明细（分桶、分量）见同名 `.md`。"]
    with open(os.path.join(REPORT_DIR, "latest.md"), "w", encoding="utf-8") as fh:
        fh.write("\n".join(lines))


def _verdict(stats: List[Dict[str, Any]], rows: Optional[List[Dict[str, Any]]] = None) -> str:
    """一句话结论：偏置方向命中率 vs 无脑做多基线。

    两条硬约束，缺一条就不许说"有优势"：
      1) n ≥ 250：150 个样本下 55% 与 50% 的差异完全在噪声区间（标准误约 ±4pt）；
      2) 窗口 ≥ 120 天：单一行情段里"命中高"往往只是顺势 —— 跌了 57% 的月份，
         任何偏空的读数都会命中 57%，这不是预测力，是行情本身的方向。
    """
    h = next((s for s in stats if s["horizon"] == 3), stats[0])
    if not h.get("hit") or not h.get("base_up"):
        return "样本不足，不给结论"
    edge = h["hit"] - h["base_up"]
    days = 0
    if rows:
        days = (rows[-1]["ts"] - rows[0]["ts"]) / 86400.0
    if h["n"] < 250 or days < 120:
        return (f"**样本/窗口不足以判定**（n={h['n']}，窗口 {days:.0f} 天；"
                f"命中 {h['hit']}% vs 基线 {h['base_up']}%）。"
                f"单一行情段里的高命中通常只是顺势，不能当作可交易优势")
    if edge >= 8:
        return f"偏置方向命中 {h['hit']}% vs 基线 {h['base_up']}%（+{edge:.1f}pt，h={h['horizon']}）→ 有方向性优势"
    if edge <= -8:
        return (f"偏置方向命中 {h['hit']}% vs 基线 {h['base_up']}%（{edge:.1f}pt，h={h['horizon']}）"
                f"→ **不如无脑做多**，只能当描述用")
    return (f"偏置方向命中 {h['hit']}% vs 基线 {h['base_up']}%（{edge:+.1f}pt，h={h['horizon']}）"
            f"→ 差异落在噪声区间，**不具备预测力**")


def _markdown(payload: Dict[str, Any]) -> str:
    m = payload["meta"]
    lines = [
        f"# 状态偏置有效性回测 · {m['asset']} {m['timeframe']}",
        "",
        f"- 样本：{m['samples']} 个时点（因果重算，asof_ts 截断，无前视）",
        f"- 时段：{time.strftime('%Y-%m-%d', time.localtime(m['window'][0]))} → "
        f"{time.strftime('%Y-%m-%d', time.localtime(m['window'][1]))}",
        f"- 门槛：|bias| ≥ {m['lean']} 且覆盖度 ≥ 50%",
        f"- 结论：{payload['verdict']}",
        "",
        "## 偏置分层（前瞻收益）",
        "",
        "|  horizon |  n  |  IC  | 方向命中 | 基线(无脑做多) | 同向均值 | 绝对均值 |",
        "|---|---|---|---|---|---|---|",
    ]
    for s in payload["bias"]:
        lines.append(f"| +{s['horizon']} 根 | {s['n']} | {s['ic']} | {s['hit']}% | "
                     f"{s['base_up']}% | {s['signed_mean']}% | {s['abs_mean']}% |")
    lines += ["", "## 分桶明细", ""]
    for s in payload["bias"]:
        lines += [f"### +{s['horizon']} 根", "", "| 桶 | n | 平均前瞻收益 | 上涨占比 |", "|---|---|---|---|"]
        for bk in s["buckets"]:
            lines.append(f"| {bk['bucket']} | {bk['n']} | {bk['mean_fwd']}% | {bk['up_ratio']}% |")
        lines.append("")
    lines += ["## 分量单独评定", "", "| 分量 | n | h1 | h3 | h5 | h10 |", "|---|---|---|---|---|---|"]
    for c in payload["components"]:
        if c.get("note"):
            lines.append(f"| {c['component']} | {c['n']} | {c['note']} | - | - | - |")
            continue
        cells = []
        for h in HORIZONS:
            k = f"h{h}"
            cells.append(f"命中 {c[k]['hit']}% / IC {c[k]['ic']}" if k in c else "-")
        lines.append(f"| {c['component']} | {c['n']} | " + " | ".join(cells) + " |")
    lines += ["", "> 口径提示：IC 为信号与前瞩收益的秩相关；命中 = 信号方向与收益同向的比例。",
              "> 1d 周期下 OI/资金费率/多空比历史缺失，覆盖度上限 0.60，结论只对价格结构负责。", ""]
    return "\n".join(lines)


if __name__ == "__main__":
    sys.exit(main())
