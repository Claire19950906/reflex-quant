#!/usr/bin/env python3
"""threshold_group_backtest.py — 按资产分组回测指标阈值，给分组参数建议。

回答的问题
----------
台账（indicator_effectiveness_ledger）是按「因子」汇总的，但同一阈值在不同
资产上表现可能完全相反：加密波动大、阈值要更宽；股指/贵金属波动小，阈值要更紧。
本脚本直接从日线重算指标（因果、无未来函数），对每组资产、每个候选阈值统计：
  · 触发根数 / 占比
  · 前向 1/3/5/10 日收益、命中率、相对同资产基线的超额
输出 Markdown 报告到 reports/indicator_ledger/threshold_groups.md。

覆盖的规则（与 indicator_rules.py / tech_detector.py 同一套口径）
----------------------------------------------------------------
  · RSI 超卖反弹（看涨）：RSI(14) ≤ 阈值
  · RSI 超买回落（看跌）：RSI(14) ≥ 阈值
  · MFI 超卖反弹（看涨）：MFI(14) ≤ 阈值
  · MFI 超买回落（看跌）：MFI(14) ≥ 阈值
  · ADX 趋势强度：ADX(14) ≥ 阈值（只描述强度，不判方向）

分组：crypto / equity / precious / commodity / index。

约束：只读库、不写线上逻辑；样本不足的分组直接标"样本不足"，不硬给结论。

用法
----
    python3 scripts/threshold_group_backtest.py --no-write
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import statistics
import sys
from collections import defaultdict
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

BACKEND = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND / "src_python"))
DB_PATH = BACKEND / "trident_event_bus.db"
OUT_DIR = BACKEND / "reports" / "indicator_ledger"

HORIZONS: Tuple[int, ...] = (1, 3, 5, 10)
MIN_SAMPLE = 20

# 资产分组（可按实际标的补充）
GROUPS: Dict[str, List[str]] = {
    "crypto": ["BTC", "ETH", "SOL", "ADA", "DOGE", "XRP", "BNB"],
    "equity": ["AAPL", "AMD", "AMZN", "GOOGL", "META", "MSFT", "MU", "NVDA", "TSLA",
               "000660.KS", "SKHYNIX", "TENCENT"],
    "precious": ["XAU", "XAG", "HG"],
    "commodity": ["WTI"],
    "index": ["DJI", "IXIC", "NDX", "SPX"],
}
ASSET_GROUP: Dict[str, str] = {a: g for g, alist in GROUPS.items() for a in alist}

# 候选阈值（遍历回测，不默认写死）
RSI_OS_CANDIDATES = [25.0, 30.0, 35.0]
RSI_OB_CANDIDATES = [65.0, 70.0, 75.0]
MFI_OS_CANDIDATES = [15.0, 20.0, 25.0]
MFI_OB_CANDIDATES = [75.0, 80.0, 85.0]
ADX_CANDIDATES = [15.0, 20.0, 25.0, 30.0]


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


def forward_returns(candles: List[list]) -> Dict[int, Dict[int, float]]:
    idx = {c[0]: i for i, c in enumerate(candles)}
    out: Dict[int, Dict[int, float]] = {h: {} for h in HORIZONS}
    for ts, i in idx.items():
        base = candles[i][4]
        if not base:
            continue
        for h in HORIZONS:
            j = i + h
            if j < len(candles):
                out[h][ts] = (candles[j][4] - base) / base * 100.0
    return out


def baseline_means(fwd: Dict[int, Dict[int, float]]) -> Dict[int, Optional[float]]:
    res: Dict[int, Optional[float]] = {}
    for h in HORIZONS:
        vals = list(fwd[h].values())
        res[h] = round(statistics.fmean(vals), 4) if vals else None
    return res


def evaluate_rule(
    signals: List[Dict[str, Any]],
    fwd: Dict[str, Dict[int, Dict[int, float]]],
    baseline: Dict[str, Dict[int, Optional[float]]],
    direction: int,
    horizon: int,
) -> Dict[str, Any]:
    """direction: 1 看涨 / -1 看跌 / 0 中性。"""
    vals: List[float] = []
    for s in signals:
        r = fwd.get(s["asset"], {}).get(horizon, {}).get(s["ts"])
        if r is not None:
            vals.append(r)
    if not vals:
        return {"n": 0, "hit": None, "avg": None, "edge": None, "verdict": "样本不足"}
    n = len(vals)
    if direction == 0:
        avg = statistics.fmean(vals)
        base = statistics.fmean(
            [b for s in signals for b in [baseline.get(s["asset"], {}).get(horizon)] if b is not None]
        ) if signals else None
        return {"n": n, "hit": None, "avg": round(avg, 3),
                "edge": round(avg - base, 3) if base is not None else None,
                "verdict": "样本不足" if n < MIN_SAMPLE else "仅描述"}
    signed = [v * direction for v in vals]
    avg = statistics.fmean(signed)
    base_signed = []
    for s in signals:
        b = baseline.get(s["asset"], {}).get(horizon)
        if b is not None:
            base_signed.append(b * direction)
    edge = round(avg - statistics.fmean(base_signed), 3) if base_signed else None
    hit = round(sum(1 for v in signed if v > 0) / n * 100.0, 2)
    if n < MIN_SAMPLE:
        verdict = "样本不足"
    elif hit - 50.0 >= 5.0 and (edge or 0) > 0:
        verdict = "有效"
    elif hit - 50.0 <= -5.0 and (edge or 0) < 0:
        verdict = "反向"
    else:
        verdict = "噪声"
    return {"n": n, "hit": hit, "avg": round(avg, 3), "edge": edge, "verdict": verdict}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--no-write", action="store_true")
    ap.add_argument("--db", default=str(DB_PATH))
    ap.add_argument("--horizons", default="1,3,5,10",
                    help="前向收益周期，逗号分隔，如 1,3,5,10")
    ap.add_argument("--min-sample", type=int, default=MIN_SAMPLE,
                    help="分组最小样本数，低于此标样本不足")
    ap.add_argument("--groups", default=",".join(GROUPS.keys()),
                    help="只回测指定分组，逗号分隔")
    ap.add_argument("--family", default="rsi,mfi,adx",
                    help="要回测的指标族，逗号分隔：rsi/mfi/adx")
    args = ap.parse_args()

    horizons = tuple(int(x) for x in args.horizons.split(",") if x.strip())
    families = {x.strip() for x in args.family.split(",") if x.strip()}
    run_groups = {x.strip() for x in args.groups.split(",") if x.strip()}
    # 运行期覆盖模块级常量，供下游函数读取
    globals()["HORIZONS"] = horizons
    globals()["MIN_SAMPLE"] = args.min_sample
    active_groups = {g: a for g, a in GROUPS.items() if g in run_groups}

    import technical_indicators as ti

    if not Path(args.db).exists():
        print(f"[FATAL] DB not found: {args.db}", file=sys.stderr)
        return 1

    conn = sqlite3.connect(args.db)
    series = load_daily(conn)
    conn.close()

    fwd_cache: Dict[str, Dict[int, Dict[int, float]]] = {}
    base_cache: Dict[str, Dict[int, Optional[float]]] = {}
    for asset, candles in series.items():
        fwd_cache[asset] = forward_returns(candles)
        base_cache[asset] = baseline_means(fwd_cache[asset])

    # 预计算每资产指标序列
    ind: Dict[str, Dict[str, List[Optional[float]]]] = {}
    for asset, candles in series.items():
        closes = [c[4] for c in candles]
        ind[asset] = {
            "rsi": ti.rsi(closes, 14),
            "mfi": ti.mfi(candles, 14),
            "adx": ti.adx(candles, 14)["adx"],
        }

    def kind_match(kind: str, threshold: float, v: float, rule: str) -> bool:
        if rule.endswith("_os"):
            return v <= threshold
        if rule.endswith("_ob"):
            return v >= threshold
        return v >= threshold  # adx

    # 规则定义：(rule_id, 指标, 方向, 阈值候选, 规则后缀, 所属族)
    all_rules: List[Tuple[str, str, int, List[float], str, str]] = [
        ("rsi_oversold", "rsi", 1, RSI_OS_CANDIDATES, "_os", "rsi"),
        ("rsi_overbought", "rsi", -1, RSI_OB_CANDIDATES, "_ob", "rsi"),
        ("mfi_oversold", "mfi", 1, MFI_OS_CANDIDATES, "_os", "mfi"),
        ("mfi_overbought", "mfi", -1, MFI_OB_CANDIDATES, "_ob", "mfi"),
        ("adx_trend", "adx", 0, ADX_CANDIDATES, "_adx", "adx"),
    ]
    rules = [r for r in all_rules if r[5] in families]

    # results[rule][group][threshold] = {horizon: stats}
    results: Dict[str, Dict[str, Dict[float, Dict[int, Dict[str, Any]]]]] = {}
    for rule_id, kind, direction, candidates, suffix, _fam in rules:
        results[rule_id] = {}
        for threshold in candidates:
            signals = []
            for asset, candles in series.items():
                if ASSET_GROUP.get(asset, "other") not in run_groups:
                    continue
                seq = ind[asset][kind]
                for i, c in enumerate(candles):
                    v = seq[i] if i < len(seq) else None
                    if v is None:
                        continue
                    if kind_match(kind, threshold, v, suffix):
                        signals.append({"asset": asset, "ts": c[0]})
            by_group: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
            for s in signals:
                g = ASSET_GROUP.get(s["asset"], "other")
                by_group[g].append(s)
            for group, gsigs in by_group.items():
                results[rule_id].setdefault(group, {})[threshold] = {
                    h: evaluate_rule(gsigs, fwd_cache, base_cache, direction, h)
                    for h in HORIZONS
                }

    # ── 生成报告 ──
    lines: List[str] = []
    lines.append("# 指标阈值分组回测")
    lines.append("")
    lines.append(f"生成时间：{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}　"
                 f"数据：`{args.db}`")
    lines.append("")
    lines.append("分组：" + "、".join(f"{g}（{len(a)} 标的）" for g, a in active_groups.items()))
    lines.append("")
    lines.append("口径：日线因果指标（RSI/MFI/ADX 14），前向 "
                 + "/".join(f"{h}日" for h in HORIZONS) + "；"
                 + f"样本 <{MIN_SAMPLE} 标样本不足；命中率 −50% ≥ 5pt 且超额为正才算有效。")
    lines.append("")

    rule_title = {
        "rsi_oversold": "RSI 超卖反弹（看涨）",
        "rsi_overbought": "RSI 超买回落（看跌）",
        "mfi_oversold": "MFI 超卖反弹（看涨）",
        "mfi_overbought": "MFI 超买回落（看跌）",
        "adx_trend": "ADX 趋势强度（中性，只看强度）",
    }
    for rule_id, kind, direction, candidates, suffix, _fam in rules:
        lines.append(f"## {rule_title[rule_id]}")
        lines.append("")
        for group in active_groups:
            if group not in results[rule_id]:
                continue
            lines.append(f"### {group}")
            lines.append("")
            h_main = HORIZONS[-1] if len(HORIZONS) else 5
            other_labels = "/".join(f"{h}日" for h in HORIZONS if h != h_main) or "—"
            lines.append(f"| 阈值 | 样本 | {h_main}日命中率 | {h_main}日超额 | {h_main}日结论 | {other_labels} |")
            lines.append("|---|---|---|---|---|---|")
            for threshold in candidates:
                stats_map = results[rule_id][group].get(threshold)
                if not stats_map:
                    continue
                h_main = HORIZONS[-1] if len(HORIZONS) else 5
                s_main = stats_map[h_main]
                def _cell(h: int) -> str:
                    if h not in stats_map:
                        return "—"
                    s = stats_map[h]
                    if s["n"] == 0:
                        return "—"
                    if direction == 0:
                        return f"n={s['n']} 超额 {s['edge']}"
                    return f"{s['hit']}%/{s['edge']}"
                hit = "—" if s_main["hit"] is None else f"{s_main['hit']}%"
                other = " | ".join(_cell(h) for h in HORIZONS if h != h_main)
                lines.append(
                    f"| {threshold:g} | {s_main['n']} | {hit} | "
                    f"{s_main['edge'] if s_main['edge'] is not None else '—'} | {s_main['verdict']} | "
                    f"{other} |"
                )
            lines.append("")

    report = "\n".join(lines)
    if not args.no_write:
        OUT_DIR.mkdir(parents=True, exist_ok=True)
        out_path = OUT_DIR / "threshold_groups.md"
        out_path.write_text(report, encoding="utf-8")
        print(f"[OK] 报告 → {out_path}")
    else:
        print(report)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
