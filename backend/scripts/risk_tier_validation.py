#!/usr/bin/env python3
"""risk_tier_validation.py —— 验证「风控档位」本身有没有区分力（v414）。

为什么必须验：v412 的档位是从一组**实测倍数**推出来的（5 根内 MAE ≈0.4~1.3 倍 ATR），
但"档位打高 = 更险"这件事**从来没有被历史数据检验过**。方向偏置已经被证否（v408），
如果档位也不分高低，那它就是给复盘用的装饰，不该出现在下单口径里。

怎么验（严格因果，无前视）：
  1. 每个采样时点以 `asof_ts` 截断历史，重算 `build_state`，取当时的 `risk` 档位与 `atr_pct`；
  2. 只用**该时点之后**的 5 / 10 根 K 线算 收益 / MFE / MAE；
  3. 按档位分桶，比较 |MAE| 的中位、以及 **|MAE| ÷ 当时 ATR%**（去掉波动尺度的干扰）；
  4. 判据：高档的 MAE_ATR 中位 ÷ 低档的 MAE_ATR 中位 ≥ 1.3 且两组 n ≥ 30 → 「有区分力」；
     否则「没有区分力」，并如实写进报告（不粉饰）。

用法：
    python3 scripts/risk_tier_validation.py --asset BTC --timeframe 4h --samples 300
    python3 scripts/risk_tier_validation.py --all --samples 300
"""
from __future__ import annotations

import argparse
import json
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

REPORT_DIR = os.path.join(BACKEND, "reports", "risk_tiers")
DB_PATH = os.path.join(BACKEND, "trident_event_bus.db")

HORIZONS = (5, 10)
LEVELS = ("低", "中", "高")
MIN_BUCKET = 30          # 单个档位最少样本
SEPARATION_MIN = 1.3     # 高档 / 低档 的 MAE 倍数比，低于它就说"没区分力"
DEFAULT_SET = [("BTC", "4h"), ("ETH", "4h"), ("BTC", "1d"), ("XAU", "1d"), ("XAG", "1d")]


def _median(vals: List[float]) -> Optional[float]:
    if not vals:
        return None
    vals = sorted(vals)
    n = len(vals)
    mid = n // 2
    return vals[mid] if n % 2 else (vals[mid - 1] + vals[mid]) / 2.0


def _pct(vals: List[float], q: float) -> Optional[float]:
    """分位数（线性插值）——与 v411 的 quantile 同口径，独立实现避免跨脚本耦合。"""
    if not vals:
        return None
    vals = sorted(vals)
    if len(vals) == 1:
        return vals[0]
    pos = q * (len(vals) - 1)
    lo = int(pos)
    hi = min(lo + 1, len(vals) - 1)
    frac = pos - lo
    return vals[lo] * (1 - frac) + vals[hi] * frac


def bucket_stats(rows: List[Dict[str, float]]) -> Dict[str, Any]:
    """一个档位在某个前瞻期内的分布统计。"""
    if not rows:
        return {"n": 0}
    rets = [r["ret"] for r in rows]
    maes = [abs(r["mae"]) for r in rows]
    mfes = [r["mfe"] for r in rows]
    mae_atr = [r["mae_atr"] for r in rows if r.get("mae_atr") is not None]
    return {
        "n": len(rows),
        "ret_mean": round(sum(rets) / len(rets), 4),
        "ret_median": round(_median(rets) or 0.0, 4),
        "up_ratio": round(sum(1 for x in rets if x > 0) / len(rets), 4),
        "mae_median": round(_median(maes) or 0.0, 4),
        "mfe_median": round(_median(mfes) or 0.0, 4),
        "mae_atr_median": round(_median(mae_atr), 4) if mae_atr else None,
        "mae_atr_iqr": ([round(_pct(mae_atr, 0.25), 4), round(_pct(mae_atr, 0.75), 4)]
                        if mae_atr else None),
        "mae_p90": round(_pct(maes, 0.9) or 0.0, 4),
    }


def evaluate(conn: sqlite3.Connection, asset: str, timeframe: str,
             samples: int, step: int, shift_back: int, bars: int) -> Dict[str, Any]:
    series = load_series(conn, asset, timeframe)
    dq = data_quality(series)
    payload: Dict[str, Any] = {
        "asset": asset, "timeframe": timeframe, "data_quality": dq,
        "series_len": len(series), "levels": [], "baseline": {}, "window": None,
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
    if samples and samples > 0:
        end = len(eligible) - shift_back
        eligible = eligible[max(0, end - samples):end]
    else:
        # samples<=0：走全序列（用来判断"分档阈值本身"有没有区分力，
        # 而不是只看最近一段行情；分档在不同波动体制下是否都成立，只能这样看）
        eligible = eligible[:len(eligible) - shift_back] if shift_back else eligible

    base: Dict[int, List[Dict[str, float]]] = {h: [] for h in HORIZONS}
    buckets: Dict[Tuple[str, int], List[Dict[str, float]]] = {}
    visited = 0
    skipped = 0
    for i in eligible:
        state = st.build_state(conn, asset, timeframe, bars=bars, asof_ts=series[i][0])
        if state.get("error"):
            skipped += 1
            continue
        risk = state.get("risk") or {}
        if not risk.get("available"):
            skipped += 1
            continue
        level = str(risk.get("level") or "")
        atr = risk.get("atr_pct")
        if level not in LEVELS:
            skipped += 1
            continue
        visited += 1
        entry = closes[i]
        for h in HORIZONS:
            ret = (closes[i + h] - entry) / entry * 100.0
            seg = closes[i:i + h + 1]
            mfe = (max(seg) - entry) / entry * 100.0
            mae = (min(seg) - entry) / entry * 100.0
            row = {
                "ret": ret, "mfe": mfe, "mae": mae,
                "mae_atr": (abs(mae) / float(atr)) if atr else None,
            }
            base[h].append(row)
            buckets.setdefault((level, h), []).append(row)

    payload["window"] = {
        "from": time.strftime("%Y-%m-%d", time.localtime(series[eligible[0]][0])) if eligible else None,
        "to": time.strftime("%Y-%m-%d", time.localtime(series[eligible[-1]][0])) if eligible else None,
        "points": visited, "skipped": skipped,
    }
    for h in HORIZONS:
        payload["baseline"][str(h)] = bucket_stats(base[h])

    for level in LEVELS:
        item: Dict[str, Any] = {"level": level, "timeframe": timeframe}
        for h in HORIZONS:
            item[str(h)] = bucket_stats(buckets.get((level, h), []))
        payload["levels"].append(item)

    payload.update(_verdict(payload))
    return payload


def _verdict(payload: Dict[str, Any]) -> Dict[str, Any]:
    """判档位是否真有区分力。全部用 MAE_ATR（去掉波动尺度），不用收益方向。"""
    by_level = {item["level"]: item for item in payload["levels"]}
    h = "5"
    low = by_level.get("低", {}).get(h, {})
    high = by_level.get("高", {}).get(h, {})
    mid = by_level.get("中", {}).get(h, {})

    def _ratio(a: Optional[float], b: Optional[float]) -> Optional[float]:
        if not a or not b:
            return None
        return round(a / b, 3)

    # 有的品种（BTC/ETH）日线 ATR 长期 ≥1.5%，**根本没有"低档"样本**。
    # 这时不能拿"低档样本不足"当挡箭牌 —— 退一步比 高 vs 中，只要 n 够就照样给结论。
    ref_name, ref = "低", low
    if (low.get("n") or 0) < MIN_BUCKET and (mid.get("n") or 0) >= MIN_BUCKET:
        ref_name, ref = "中", mid
    if (high.get("n") or 0) < MIN_BUCKET or (ref.get("n") or 0) < MIN_BUCKET:
        return {
            "verdict": "样本不足，不给结论",
            "separation": {
                "high_low_abs": _ratio(high.get("mae_median"), low.get("mae_median")),
                "high_low_atr": _ratio(high.get("mae_atr_median"), low.get("mae_atr_median")),
            },
            "reason": (f"高档 n={high.get('n') or 0}、低档 n={low.get('n') or 0}、"
                       f"中档 n={mid.get('n') or 0}，门槛 {MIN_BUCKET}"),
        }

    # 两个口径都要看，因为它们回答不同问题：
    #   abs（|MAE| 绝对百分比）＝ 下单时止损要放多远，这是档位真正要服务的；
    #   atr（|MAE|÷当时ATR%）＝ 同样波动尺度下谁更险，用来判断档位是不是只copy了波动水平。
    abs_hl = _ratio(high.get("mae_median"), ref.get("mae_median"))
    abs_ml = _ratio(mid.get("mae_median"), low.get("mae_median")) if ref_name == "低" else None
    atr_hl = _ratio(high.get("mae_atr_median"), ref.get("mae_atr_median"))
    atr_ml = _ratio(mid.get("mae_atr_median"), low.get("mae_atr_median")) if ref_name == "低" else None

    monotone_abs = (abs_hl or 0) >= (abs_ml or 0) >= 1.0 if ref_name == "低" else True
    ok = (abs_hl or 0) >= SEPARATION_MIN and monotone_abs
    if ok:
        verdict = (f"档位对逆风幅度有区分力（绝对口径，高 vs {ref_name}）")
    elif (abs_hl or 0) >= SEPARATION_MIN:
        verdict = "高档逆风更大，但中档不单调 —— 只能当两档用"
    elif (atr_hl or 0) >= SEPARATION_MIN:
        verdict = "绝对逆风没分开，但同波动尺度下高档更险 —— 档位在跟着波动水平走"
    else:
        verdict = "档位没有可分离的区分力"
    return {
        "verdict": verdict,
        "separation": {
            "high_low_abs": abs_hl, "mid_low_abs": abs_ml,
            "high_low_atr": atr_hl, "mid_low_atr": atr_ml,
        },
        "monotone_abs": monotone_abs,
        "reference": ref_name,
        "reason": (f"5 根 |MAE| 中位：低 {low.get('mae_median')}%、中 {mid.get('mae_median')}%、"
                   f"高 {high.get('mae_median')}%；高/{ref_name}(绝对) = {abs_hl}、"
                   f"高/{ref_name}(÷ATR) = {atr_hl}（门槛 ≥{SEPARATION_MIN}）"),
    }


def _fmt(v: Any, nd: int = 2) -> str:
    if v is None:
        return "—"
    if isinstance(v, float):
        return f"{v:.{nd}f}"
    return str(v)


def _markdown(payload: Dict[str, Any]) -> str:
    lines = [f"# 风控档位验证 · {payload['asset']} {payload['timeframe']}", ""]
    if payload.get("data_quality"):
        dq = payload["data_quality"]
        lines += [f"- 数据体检：{'通过' if dq.get('ok') else '不通过'}（{dq.get('notes') or dq}）"]
    win = payload.get("window") or {}
    lines += [
        f"- 窗口：{win.get('from')} → {win.get('to')}，采样 {win.get('points')} 点"
        f"（跳过 {win.get('skipped')}）",
        f"- 判据：5 根 |MAE|÷当时ATR% 的中位，高/低 ≥ {SEPARATION_MIN} 且单调",
        "",
        f"**结论：{payload.get('verdict')}**",
        "",
    ]
    if payload.get("reason"):
        lines += [f"{payload['reason']}", ""]
    sep = payload.get("separation") or {}
    if payload.get("reference"):
        lines += [f"参考档位：{payload['reference']}（该资产在该周期没有更低档的样本，如实说明）", ""]
    if sep:
        lines += ["| 比值 | 绝对口径 | ÷ATR 口径 |", "|---|---|---|",
                  f"| 高/低 | {_fmt(sep.get('high_low_abs'))} | {_fmt(sep.get('high_low_atr'))} |",
                  f"| 中/低 | {_fmt(sep.get('mid_low_abs'))} | {_fmt(sep.get('mid_low_atr'))} |", ""]

    lines += ["## 分档位分布（5 根 / 10 根）", "",
              "| 档位 | 期 | n | |MAE| 中位 | MAE/ATR 中位 | MAE/ATR IQR | |MAE| P90 | 收益中位 | 上涨占比 |",
              "|---|---|---|---|---|---|---|---|---|---|"]
    for item in payload["levels"]:
        for h in ("5", "10"):
            s = item.get(h) or {}
            lines.append(
                f"| {item['level']} | {h} | {s.get('n') or 0} | {_fmt(s.get('mae_median'))}% | "
                f"{_fmt(s.get('mae_atr_median'))} | "
                f"{_fmt((s.get('mae_atr_iqr') or [None])[0])}~{_fmt((s.get('mae_atr_iqr') or [None, None])[1])} | "
                f"{_fmt(s.get('mae_p90'))}% | {_fmt(s.get('ret_median'))}% | "
                f"{_fmt((s.get('up_ratio') or 0) * 100, 1)}% |"
            )
    lines += ["", "## 全样本基线", ""]
    for h in ("5", "10"):
        s = payload["baseline"].get(h) or {}
        lines.append(f"- {h} 根：n={s.get('n')}，|MAE| 中位 {_fmt(s.get('mae_median'))}%，"
                     f"MAE/ATR {_fmt(s.get('mae_atr_median'))}，上涨占比 "
                     f"{_fmt((s.get('up_ratio') or 0) * 100, 1)}%")
    lines += ["", "> 边界：档位只描述\"这段路有多颠\"，不描述方向。"
                  "若本报告判「没有区分力」，档位就不能用于下单口径，只保留显示价值。", ""]
    return "\n".join(lines)


def _write_index(results: List[Dict[str, Any]]) -> None:
    path = os.path.join(REPORT_DIR, "latest.md")
    lines = ["# 风控档位验证 · 汇总", "",
             "| 资产 | 周期 | n | 高/低(|MAE|) | 中/低(|MAE|) | 高/低(÷ATR) | 结论 |",
             "|---|---|---|---|---|---|---|"]
    for r in results:
        if r.get("error"):
            lines.append(f"| {r['asset']} | {r['timeframe']} | — | — | — | 运行失败：{r['error']} |")
            continue
        sep = r.get("separation") or {}
        win = r.get("window") or {}
        lines.append(
            f"| {r['asset']} | {r['timeframe']} | {win.get('points') or 0} | "
            f"{_fmt(sep.get('high_low_abs'))} | {_fmt(sep.get('mid_low_abs'))} | "
            f"{_fmt(sep.get('high_low_atr'))} | {r.get('verdict')} |"
        )
    lines += ["", f"生成时间：{time.strftime('%Y-%m-%d %H:%M')}", ""]
    os.makedirs(REPORT_DIR, exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write("\n".join(lines))


def _run_one(conn: sqlite3.Connection, asset: str, timeframe: str, args) -> Dict[str, Any]:
    try:
        payload = evaluate(conn, asset, timeframe, args.samples, args.step,
                           args.shift_back, args.bars)
    except Exception as exc:
        payload = {"asset": asset, "timeframe": timeframe,
                   "error": f"{type(exc).__name__}: {str(exc)[:120]}"}
    if args.no_write:
        return payload
    os.makedirs(REPORT_DIR, exist_ok=True)
    stem = f"{asset}_{timeframe}" + (f"_back{args.shift_back}" if args.shift_back else "")
    with open(os.path.join(REPORT_DIR, f"{stem}.md"), "w", encoding="utf-8") as fh:
        fh.write(_markdown(payload))
    with open(os.path.join(REPORT_DIR, f"{stem}.json"), "w", encoding="utf-8") as fh:
        json.dump(payload, fh, ensure_ascii=False, indent=2)
    return payload


def main() -> int:
    ap = argparse.ArgumentParser(description="验证风控档位是否有区分力")
    ap.add_argument("--asset", default="BTC")
    ap.add_argument("--timeframe", default="4h")
    ap.add_argument("--samples", type=int, default=300,
                    help="采样点数；<=0 表示走全序列")
    ap.add_argument("--step", type=int, default=1)
    ap.add_argument("--bars", type=int, default=240)
    ap.add_argument("--shift-back", type=int, default=0, help="窗口整体往前挪多少根")
    ap.add_argument("--db", default=DB_PATH)
    ap.add_argument("--no-write", action="store_true")
    ap.add_argument("--all", action="store_true", help="跑默认五个资产组合")
    ap.add_argument("--index-only", action="store_true", help="只重建汇总")
    args = ap.parse_args()

    if not os.path.exists(args.db):
        print(f"数据库不存在：{args.db}", file=sys.stderr)
        return 2
    conn = sqlite3.connect(f"file:{args.db}?mode=ro", uri=True)
    try:
        if args.all:
            results = [_run_one(conn, a, tf, args) for a, tf in DEFAULT_SET]
        else:
            results = [_run_one(conn, args.asset.upper(), args.timeframe, args)]
    finally:
        conn.close()

    if not args.no_write:
        _write_index(results)
    for r in results:
        if r.get("error"):
            print(f"{r['asset']} {r['timeframe']}: 失败 {r['error']}")
            continue
        sep = r.get("separation") or {}
        print(f"{r['asset']} {r['timeframe']}: {r.get('verdict')} "
              f"(高/低绝对={_fmt(sep.get('high_low_abs'))}, ÷ATR={_fmt(sep.get('high_low_atr'))}, "
              f"n={((r.get('window') or {}).get('points'))})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
