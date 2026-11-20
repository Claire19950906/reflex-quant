#!/usr/bin/env python3
"""极端局面的条件分布 —— 不问"往哪走"，只问"这种局面里赔率长什么样"。

为什么另开一个脚本：
  v408/v410 已经证明偏置（六维合成的一个数）**没有方向预测力** —— 五个资产、七个时段，
  命中率和"无脑做多"没有可分离差异。继续在"预测方向"上加码是浪费。
  但状态读数仍然有用，因为它描述的是**局面**：强趋势还是震荡、在价值区上面还是下面、
  是加仓上涨还是减仓下跌、波动在扩张还是收缩。
  对局面能问的问题不是"涨还是跌"，而是：
    · 在这种局面里，接下来 N 根的收益分布是什么形状（中位数偏哪、上下四分位多宽）？
    · 上涨概率和全样本比有没有差？
    · 先挨打还是先给糖（最大不利偏移 MAE vs 最大有利偏移 MFE）？
    · 赔率是不是不对称（右尾比左尾长，还是反过来）？

口径：
  · 每个时点用 `build_state(asof_ts=...)` **因果重算**，只用该时点之前的 K 线；
  · 前瞻 5 / 10 根，收益按收盘价计；
  · 条件命中样本 < MIN_N 一律不报（宁可不报，也不拿 12 个样本说事）；
  · 每个条件都同时给"全样本基线"，差值直接摆出来，不做显著性主张。

用法：
  python3 scripts/extreme_state_study.py --asset BTC --timeframe 4h --samples 600
  python3 scripts/extreme_state_study.py --asset XAU --timeframe 1d --samples 600
  python3 scripts/extreme_state_study.py --all
"""
import argparse
import json
import math
import os
import sqlite3
import sys
import time
from typing import Any, Dict, List, Optional, Tuple

BACKEND = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(BACKEND, "src_python"))
sys.path.insert(0, os.path.join(BACKEND, "scripts"))

import technical_state as st
from state_bias_validation import WARMUP, data_quality, load_series

DEFAULT_DB = os.path.join(BACKEND, "trident_event_bus.db")
OUT_DIR = os.path.join(BACKEND, "reports", "extreme_state")
HORIZONS = (5, 10)
MIN_N = 30
DEFAULT_SET = [("BTC", "4h"), ("ETH", "4h"), ("BTC", "1d"), ("XAU", "1d"), ("XAG", "1d")]


# ---------------------------------------------------------------------------
# 条件：只读局面，不读方向
# ---------------------------------------------------------------------------

def conditions_of(state: Dict[str, Any]) -> List[str]:
    """从一个状态读数里抽出"它满足哪些局面条件"。"""
    out: List[str] = []
    tr = state.get("trend") or {}
    vo = state.get("volatility") or {}
    va = state.get("value_area") or {}
    lev = state.get("leverage") or {}
    fl = state.get("flow") or {}
    pos = state.get("positioning") or {}

    adx = tr.get("adx")
    direction = tr.get("direction") or ""
    if adx is not None and adx >= 35:
        if "空头" in direction:
            out.append("强趋势·空头")
        elif "多头" in direction:
            out.append("强趋势·多头")
    if adx is not None and adx < 20:
        out.append("无趋势")

    bbw = vo.get("bb_width_percentile")
    if bbw is not None:
        if bbw >= 80:
            out.append("波动扩张")
        elif bbw <= 20:
            out.append("波动收缩")

    position = va.get("position")
    if position == "below_value":
        out.append("价在价值区下方")
    elif position == "above_value":
        out.append("价在价值区上方")

    rsi = tr.get("rsi")
    if rsi is not None:
        if rsi <= 30:
            out.append("RSI超卖")
        elif rsi >= 70:
            out.append("RSI超买")

    label = lev.get("label")
    if label in ("减仓下跌", "加仓上涨", "减仓上涨", "加仓下跌"):
        out.append(label)
    crowding = lev.get("crowding")
    if crowding in ("空头拥挤", "多头拥挤"):
        out.append(crowding)

    taker = fl.get("taker") or {}
    ratio = taker.get("buy_ratio")
    if ratio is not None:
        if ratio <= 0.42:
            out.append("主动卖极端")
        elif ratio >= 0.58:
            out.append("主动买极端")
    if fl.get("cvd_divergence") == "confirmed":
        out.append("CVD背离成立")

    gap = pos.get("gap_pct")
    if gap is not None and abs(gap) >= 30:
        out.append("散大户分歧大")

    # 组合条件：趋势与位置同时成立才构成一个"局面"
    if "强趋势·空头" in out and "价在价值区下方" in out:
        out.append("强空+价值区下方")
    if "强趋势·多头" in out and "价在价值区上方" in out:
        out.append("强多+价值区上方")
    return out


# ---------------------------------------------------------------------------
# 统计
# ---------------------------------------------------------------------------

def quantile(vals: List[float], q: float) -> Optional[float]:
    if not vals:
        return None
    pos = (len(vals) - 1) * q
    lo, hi = int(math.floor(pos)), int(math.ceil(pos))
    if lo == hi:
        return vals[lo]
    frac = pos - lo
    return vals[lo] * (1 - frac) + vals[hi] * frac


def dist_stats(rets: List[float], maes: List[float], mfes: List[float]) -> Dict[str, Any]:
    if not rets:
        return {"n": 0}
    s = sorted(rets)
    return {
        "n": len(rets),
        "mean": round(sum(rets) / len(rets), 4),
        "median": round(quantile(s, 0.5), 4),
        "up_pct": round(sum(1 for r in rets if r > 0) / len(rets) * 100.0, 2),
        "q25": round(quantile(s, 0.25), 4),
        "q75": round(quantile(s, 0.75), 4),
        "p10": round(quantile(s, 0.10), 4),
        "p90": round(quantile(s, 0.90), 4),
        "mae": round(sum(maes) / len(maes), 4) if maes else None,
        "mfe": round(sum(mfes) / len(mfes), 4) if mfes else None,
    }


def asymmetry(stats: Dict[str, Any]) -> Optional[float]:
    """右尾/左尾赔率：p90 涨幅 ÷ |p10 跌幅|。>1 = 右尾更长，<1 = 左尾更长。"""
    if not stats or stats.get("n", 0) == 0:
        return None
    p90, p10 = stats.get("p90"), stats.get("p10")
    if p90 is None or p10 in (None, 0):
        return None
    return round(p90 / abs(p10), 2)


def note_of(cond_stats: Dict[str, Any], base_stats: Dict[str, Any],
            asym: Optional[float], asym_base: Optional[float]) -> str:
    """把"这个条件比全样本好在哪/差在哪"翻成一句话。只描述，不做预测主张。"""
    if cond_stats.get("n", 0) < MIN_N:
        return "样本不足，不评"
    d_up = cond_stats["up_pct"] - base_stats["up_pct"]
    d_med = cond_stats["median"] - base_stats["median"]
    parts = []
    if abs(d_up) >= 5:
        parts.append(f"上涨占比{d_up:+.0f}pt")
    if abs(d_med) >= 0.5:
        parts.append(f"中位收益{d_med:+.2f}pt")
    if asym is not None and asym_base is not None and abs(asym - asym_base) >= 0.2:
        parts.append(f"赔率{asym:.2f} vs 基线{asym_base:.2f}")
    if not parts:
        return "与全样本几乎无差别"
    return "；".join(parts)


# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------

def evaluate(conn: sqlite3.Connection, asset: str, timeframe: str,
             samples: int, step: int, shift_back: int, bars: int) -> Dict[str, Any]:
    series = load_series(conn, asset, timeframe)
    dq = data_quality(series)
    payload: Dict[str, Any] = {
        "asset": asset, "timeframe": timeframe, "data_quality": dq,
        "series_len": len(series), "conditions": [], "baseline": {}, "window": None,
    }
    if not dq.get("ok"):
        payload["verdict"] = "数据不可用"
        return payload
    if len(series) < WARMUP + max(HORIZONS) + 5:
        payload["verdict"] = "样本不足，不给结论"
        return payload

    closes = [c for _, c in series]
    max_h = max(HORIZONS)
    eligible = list(range(WARMUP, len(closes) - max_h, step))
    end = len(eligible) - shift_back
    eligible = eligible[max(0, end - samples):end]

    base: Dict[int, List[Tuple[float, float, float]]] = {h: [] for h in HORIZONS}
    buckets: Dict[Tuple[str, int], List[Tuple[float, float, float]]] = {}
    visited = 0
    for i in eligible:
        state = st.build_state(conn, asset, timeframe, bars=bars, asof_ts=series[i][0])
        if state.get("error"):
            continue
        visited += 1
        conds = conditions_of(state)
        entry = closes[i]
        for h in HORIZONS:
            ret = (closes[i + h] - entry) / entry * 100.0
            seg = closes[i:i + h + 1]
            mfe = (max(seg) - entry) / entry * 100.0
            mae = (min(seg) - entry) / entry * 100.0
            base[h].append((ret, mae, mfe))
            for c in conds:
                buckets.setdefault((c, h), []).append((ret, mae, mfe))

    payload["window"] = {
        "from": time.strftime("%Y-%m-%d", time.localtime(series[eligible[0]][0])) if eligible else None,
        "to": time.strftime("%Y-%m-%d", time.localtime(series[eligible[-1]][0])) if eligible else None,
        "points": visited,
    }
    for h in HORIZONS:
        rows = base[h]
        payload["baseline"][str(h)] = dist_stats([r for r, _, _ in rows],
                                                 [a for _, a, _ in rows],
                                                 [f for _, _, f in rows])

    names = sorted({c for (c, _h) in buckets})
    for name in names:
        item: Dict[str, Any] = {"condition": name}
        for h in HORIZONS:
            rows = buckets.get((name, h), [])
            cs = dist_stats([r for r, _, _ in rows], [a for _, a, _ in rows],
                            [f for _, _, f in rows])
            bs = payload["baseline"][str(h)]
            cs["asymmetry"] = asymmetry(cs)
            cs["note"] = note_of(cs, bs, cs.get("asymmetry"), asymmetry(bs))
            item[str(h)] = cs
        payload["conditions"].append(item)

    payload["verdict"] = "已统计（描述口径，不做方向预测主张）"
    return payload


# ---------------------------------------------------------------------------
# 输出
# ---------------------------------------------------------------------------

def _fmt(v: Any, nd: int = 2) -> str:
    if v is None:
        return "—"
    if isinstance(v, float):
        return f"{v:.{nd}f}"
    return str(v)


def _markdown(payload: Dict[str, Any]) -> str:
    a, tf = payload["asset"], payload["timeframe"]
    dq = payload["data_quality"]
    lines = [f"# 极端局面条件分布 · {a} {tf}", ""]
    lines.append(f"- K 线：{payload['series_len']} 根（重复收盘 {_fmt(dq.get('dup_close_pct'),1)}%、"
                 f"主间隔 {dq.get('gap_main_sec')}s 占 {_fmt(dq.get('gap_main_share_pct'),1)}%）")
    if payload.get("window"):
        w = payload["window"]
        lines.append(f"- 评估：{w['points']} 个时点，{w['from']} → {w['to']}（因果重算，asof 截断）")
    lines.append(f"- 结论：{payload.get('verdict')}")
    lines.append("")
    lines.append("## 全样本基线")
    lines.append("")
    lines.append("| 前瞻 | n | 均值 | 中位 | 上涨占比 | Q25 | Q75 | P10 | P90 | MAE | MFE | 赔率 P90/|P10| |")
    lines.append("|---|---|---|---|---|---|---|---|---|---|---|---|")
    for h in HORIZONS:
        b = payload["baseline"][str(h)]
        lines.append(f"| +{h} 根 | {b.get('n','—')} | {_fmt(b.get('mean'))} | {_fmt(b.get('median'))} | "
                     f"{_fmt(b.get('up_pct'),1)}% | {_fmt(b.get('q25'))} | {_fmt(b.get('q75'))} | "
                     f"{_fmt(b.get('p10'))} | {_fmt(b.get('p90'))} | {_fmt(b.get('mae'))} | "
                     f"{_fmt(b.get('mfe'))} | {_fmt(asymmetry(b))} |")
    lines.extend(["", "## 各局面（对比全样本）", ""])
    lines.append("| 局面 | 前瞻 | n | 中位 | 上涨占比 | P10 | P90 | MAE | MFE | 赔率 | 与基线的差 |")
    lines.append("|---|---|---|---|---|---|---|---|---|---|---|")
    for item in payload["conditions"]:
        for h in HORIZONS:
            s = item[str(h)]
            if s.get("n", 0) < MIN_N:
                continue
            lines.append(f"| {item['condition']} | +{h} | {s['n']} | {_fmt(s.get('median'))} | "
                         f"{_fmt(s.get('up_pct'),1)}% | {_fmt(s.get('p10'))} | {_fmt(s.get('p90'))} | "
                         f"{_fmt(s.get('mae'))} | {_fmt(s.get('mfe'))} | {_fmt(s.get('asymmetry'))} | "
                         f"{s.get('note','')} |")
    lines.extend(["", f"> 样本门槛 n≥{MIN_N}；不足的条件不上表（不拿十几个样本说事）。",
                  "> MAE = 窗口内先打到的最大不利偏移（负数），MFE = 最大有利偏移。",
                  "> 这张表回答的是\"这种局面里赔率/波动长什么样\"，**不回答\"接下来涨还是跌\"**。"])
    return "\n".join(lines) + "\n"


def _write_index(results: List[Dict[str, Any]]) -> None:
    lines = ["# 极端局面条件分布 · 汇总", "",
             "口径：因果重算（asof 截断）、前瞻 5/10 根、与全样本基线对比；样本 n<30 不上表。",
             "用途：**描述赔率结构**，不主张方向。", "",
             "| 资产 | 周期 | 时段 | 时点 | 最有差别的局面（+5 根） | 结论 |",
             "|---|---|---|---|---|---|"]
    for p in results:
        best, best_d = None, 0.0
        for item in p.get("conditions", []):
            s = item.get("5", {})
            if s.get("n", 0) < MIN_N:
                continue
            b = p.get("baseline", {}).get("5", {})
            if not b.get("n"):
                continue
            d = abs((s.get("median") or 0) - (b.get("median") or 0))
            if d > best_d:
                best, best_d = item["condition"], d
        w = p.get("window") or {}
        lines.append(f"| {p['asset']} | {p['timeframe']} | {w.get('from','—')}→{w.get('to','—')} | "
                     f"{w.get('points','—')} | {best or '—'} | {p.get('verdict','')} |")
    path = os.path.join(OUT_DIR, "latest.md")
    with open(path, "w", encoding="utf-8") as fh:
        fh.write("\n".join(lines) + "\n")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--asset")
    ap.add_argument("--timeframe", default="4h")
    ap.add_argument("--samples", type=int, default=600)
    ap.add_argument("--step", type=int, default=1)
    ap.add_argument("--shift-back", type=int, default=0)
    ap.add_argument("--bars", type=int, default=240, help="状态窗口（取最近多少根 K 线）")
    ap.add_argument("--db", default=DEFAULT_DB)
    ap.add_argument("--no-write", action="store_true")
    ap.add_argument("--all", action="store_true", help="跑默认的一组资产/周期")
    ap.add_argument("--index-only", action="store_true", help="只重建汇总（读已有 json）")
    args = ap.parse_args()

    os.makedirs(OUT_DIR, exist_ok=True)

    if args.index_only:
        results = []
        for fn in sorted(os.listdir(OUT_DIR)):
            if fn.endswith(".json") and fn != "latest.json":
                with open(os.path.join(OUT_DIR, fn), encoding="utf-8") as fh:
                    results.append(json.load(fh))
        _write_index(results)
        print(f"[OK] 汇总已重建（{len(results)} 份）", flush=True)
        return 0

    targets = DEFAULT_SET if args.all else [(args.asset, args.timeframe)]
    if not args.asset and not args.all:
        ap.error("要么给 --asset，要么给 --all")

    conn = sqlite3.connect(f"file:{args.db}?mode=ro", uri=True)
    conn.execute("PRAGMA busy_timeout=30000")
    results = []
    for asset, tf in targets:
        t0 = time.time()
        payload = evaluate(conn, asset, tf, args.samples, args.step,
                           args.shift_back, args.bars)
        print(f"[{asset} {tf}] {payload.get('verdict')}  "
              f"({payload.get('window') or {}} / {time.time()-t0:.1f}s)", flush=True)
        results.append(payload)
        if not args.no_write:
            tag = f"{asset}_{tf}" + (f"_back{args.shift_back}" if args.shift_back else "")
            with open(os.path.join(OUT_DIR, f"{tag}.json"), "w", encoding="utf-8") as fh:
                json.dump(payload, fh, ensure_ascii=False, indent=1)
            with open(os.path.join(OUT_DIR, f"{tag}.md"), "w", encoding="utf-8") as fh:
                fh.write(_markdown(payload))
    if not args.no_write:
        _write_index(results)
    conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
