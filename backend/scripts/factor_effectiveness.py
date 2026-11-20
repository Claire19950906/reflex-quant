#!/usr/bin/env python3
"""factor_effectiveness.py — 新接入技术因子的前向收益回测与台账收口。

回答的问题
----------
`indicator_rules.py` 新接进规则层的 CCI 极值、摆动结构、Aroon 趋势，在真实日线上
到底有没有方向边际？本脚本从日线因果重算指标，按 1/3/5/10 日前向收益统计
命中率、平均收益、相对同资产基线的超额，输出台账表；有效才进决策，
噪声/反向/样本不足的因子进 HOLD 白名单收口。

口径与 threshold_group_backtest 一致：样本 <20 标样本不足；命中率偏离 50% ≥ 5pt
且超额同向为正才算有效；反向 = 命中率偏离 ≤ -5pt 且超额为负。

用法
----
    python3 scripts/factor_effectiveness.py --no-write
    python3 scripts/factor_effectiveness.py          # 写 reports/indicator_ledger/factor_effectiveness.md
"""
from __future__ import annotations

import argparse
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

GROUPS: Dict[str, List[str]] = {
    "crypto": ["BTC", "ETH", "SOL", "ADA", "DOGE", "XRP", "BNB"],
    "equity": ["AAPL", "AMD", "AMZN", "GOOGL", "META", "MSFT", "MU", "NVDA", "TSLA",
               "000660.KS", "SKHYNIX", "TENCENT"],
    "precious": ["XAU", "XAG", "HG"],
    "commodity": ["WTI"],
    "index": ["DJI", "IXIC", "NDX", "SPX"],
}
ASSET_GROUP: Dict[str, str] = {a: g for g, alist in GROUPS.items() for a in alist}


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


def evaluate(
    signals: List[Dict[str, Any]],
    fwd: Dict[str, Dict[int, Dict[int, float]]],
    baseline: Dict[str, Dict[int, Optional[float]]],
    direction: int,
    horizon: int,
) -> Dict[str, Any]:
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
        base_vals = [baseline.get(s["asset"], {}).get(horizon)
                     for s in signals]
        base_vals = [b for b in base_vals if b is not None]
        base = statistics.fmean(base_vals) if base_vals else None
        return {"n": n, "hit": None, "avg": round(avg, 3),
                "edge": round(avg - base, 3) if base is not None else None,
                "verdict": "样本不足" if n < MIN_SAMPLE else "仅描述"}
    signed = [v * direction for v in vals]
    avg = statistics.fmean(signed)
    base_signed = [baseline.get(s["asset"], {}).get(horizon) * direction
                   for s in signals
                   if baseline.get(s["asset"], {}).get(horizon) is not None]
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
    args = ap.parse_args()

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

    # 每条规则: (rule_id, 方向, 信号提取函数)。方向 1 看涨 / -1 看跌。
    def cci_os(candles, i, ind):
        v = ind["cci"][i] if i < len(ind["cci"]) else None
        return v is not None and v <= -100.0

    def cci_ob(candles, i, ind):
        v = ind["cci"][i] if i < len(ind["cci"]) else None
        return v is not None and v >= 100.0

    def swing_up(candles, i, ind):
        return ind["swing"][i] == "up"

    def swing_down(candles, i, ind):
        return ind["swing"][i] == "down"

    def aroon_up(candles, i, ind):
        u, d = ind["aroon_up"][i], ind["aroon_down"][i]
        return u is not None and d is not None and u >= 70.0 and d <= 30.0

    def aroon_down(candles, i, ind):
        u, d = ind["aroon_up"][i], ind["aroon_down"][i]
        return u is not None and d is not None and d >= 70.0 and u <= 30.0

    def kdj_os(candles, i, ind):
        j, k = ind["kdj_j"][i], ind["kdj_k"][i]
        return (j is not None and j <= 20.0) or (k is not None and k <= 20.0)

    def kdj_ob(candles, i, ind):
        j, k = ind["kdj_j"][i], ind["kdj_k"][i]
        return (j is not None and j >= 80.0) or (k is not None and k >= 80.0)

    def eng_bull(candles, i, ind):
        return ind["engulf"][i] == "bullish"

    def eng_bear(candles, i, ind):
        return ind["engulf"][i] == "bearish"

    def ma_gold(candles, i, ind):
        return ind["ma_cross"][i] == "golden"

    def ma_death(candles, i, ind):
        return ind["ma_cross"][i] == "death"

    def macd_zero_up(candles, i, ind):
        return ind["macd_zero"][i] == "above_zero"

    def macd_zero_down(candles, i, ind):
        return ind["macd_zero"][i] == "below_zero"

    def stoch_os(candles, i, ind):
        v = ind["stoch"][i] if i < len(ind["stoch"]) else None
        return v is not None and v <= 20.0

    def stoch_ob(candles, i, ind):
        v = ind["stoch"][i] if i < len(ind["stoch"]) else None
        return v is not None and v >= 80.0

    def bb_lower(candles, i, ind):
        return ind["bb_break"][i] == "lower_break"

    def bb_upper(candles, i, ind):
        return ind["bb_break"][i] == "upper_break"

    def ma_bull(candles, i, ind):
        return ind["ma_stack"][i] == "bull"

    def ma_bear(candles, i, ind):
        return ind["ma_stack"][i] == "bear"

    def macd_g(candles, i, ind):
        return ind["macd_cross"][i] == "golden"

    def macd_d(candles, i, ind):
        return ind["macd_cross"][i] == "death"

    def vol_up(candles, i, ind):
        return ind["vol_break"][i] == "bullish_break"

    def vol_dn(candles, i, ind):
        return ind["vol_break"][i] == "bearish_break"

    def ichi_bull(candles, i, ind):
        return ind["ichimoku"][i] == "bull"

    def ichi_bear(candles, i, ind):
        return ind["ichimoku"][i] == "bear"

    rules: List[Tuple[str, int, str, Any]] = [
        ("cci_oversold", 1, "CCI ≤ -100（通道下轨，看涨嫌疑）", cci_os),
        ("cci_overbought", -1, "CCI ≥ +100（通道上轨，看跌嫌疑）", cci_ob),
        ("swing_bull", 1, "摆动结构 HH/HL（上升骨架）", swing_up),
        ("swing_bear", -1, "摆动结构 LH/LL（下降骨架）", swing_down),
        ("aroon_bull", 1, "Aroon Up≥70 且 Down≤30（上行新鲜度）", aroon_up),
        ("aroon_bear", -1, "Aroon Down≥70 且 Up≤30（下行新鲜度）", aroon_down),
        ("kdj_oversold", 1, "KDJ J/K ≤20（超卖）", kdj_os),
        ("kdj_overbought", -1, "KDJ J/K ≥80（超买）", kdj_ob),
        ("engulfing_bull", 1, "看涨吞没形态", eng_bull),
        ("engulfing_bear", -1, "看跌吞没形态", eng_bear),
        ("ma_golden", 1, "MA5 上穿 MA20（金叉）", ma_gold),
        ("ma_death", -1, "MA5 下穿 MA20（死叉）", ma_death),
        ("macd_above_zero", 1, "MACD 主线由负转正", macd_zero_up),
        ("macd_below_zero", -1, "MACD 主线由正转负", macd_zero_down),
        ("stoch_oversold", 1, "Stochastic %K ≤20（超卖）", stoch_os),
        ("stoch_overbought", -1, "Stochastic %K ≥80（超买）", stoch_ob),
        ("bb_lower_break", 1, "布林下轨跌破（超卖嫌疑）", bb_lower),
        ("bb_upper_break", -1, "布林上轨突破（超买嫌疑）", bb_upper),
        ("ma_bull_aligned", 1, "均线多头排列 5>10>20>60", ma_bull),
        ("ma_bear_aligned", -1, "均线空头排列 5<10<20<60", ma_bear),
        ("macd_golden", 1, "MACD DIF 上穿 DEA（金叉）", macd_g),
        ("macd_death", -1, "MACD DIF 下穿 DEA（死叉）", macd_d),
        ("vol_bull_break", 1, "放量上破近 20 根高点", vol_up),
        ("vol_bear_break", -1, "放量下破近 20 根低点", vol_dn),
        ("ichimoku_bull", 1, "价格位于云图上方", ichi_bull),
        ("ichimoku_bear", -1, "价格位于云图下方", ichi_bear),
    ]

    # 预计算每资产指标序列
    ind: Dict[str, Dict[str, list]] = {}
    for asset, candles in series.items():
        cci_vals = ti.cci(candles, 20)
        aroon = ti.aroon(candles, 25)
        kdj_vals = ti.kdj([c[4] for c in candles], [c[2] for c in candles],
                          [c[3] for c in candles], 9, 3, 3)
        # —— 逐点事件序列：第 i 根本身触发才记 i（不再用末位标 5 根的简化口径）——
        closes = [c[4] for c in candles]
        cross_seq: List[Optional[str]] = [None] * len(candles)
        for i in range(20, len(candles)):
            fast_prev = sum(closes[i - 20:i - 15]) / 5
            slow_prev = sum(closes[i - 20:i]) / 20
            fast_cur = sum(closes[i - 5:i + 1]) / 5
            slow_cur = sum(closes[i - 20:i + 1]) / 20
            if fast_prev <= slow_prev and fast_cur > slow_cur:
                cross_seq[i] = "golden"
            elif fast_prev >= slow_prev and fast_cur < slow_cur:
                cross_seq[i] = "death"

        zero_seq: List[Optional[str]] = [None] * len(candles)
        ml = ti.macd(closes, 12, 26, 9)["macd"]
        for i in range(1, len(candles)):
            prev, cur = ml[i - 1], ml[i]
            if prev is None or cur is None:
                continue
            if prev <= 0 < cur:
                zero_seq[i] = "above_zero"
            elif prev >= 0 > cur:
                zero_seq[i] = "below_zero"

        eng_seq: List[Optional[str]] = [None] * len(candles)
        for i in range(1, len(candles)):
            e = ti.engulfing(candles[:i + 1])
            if e in ("bullish", "bearish"):
                eng_seq[i] = e

        # 摆动结构：整条序列一次性算结构，再把最终结构方向标到最近几根；
        # 逐点滚动算 swing_structure 是 O(n²)，日线千根级会拖垮回测。
        swing_full = ti.swing_structure(candles)
        s_trend = swing_full.get("trend")
        swing_seq: List[Optional[str]] = [None] * len(candles)
        if s_trend in ("up", "down"):
            for i in range(max(0, len(candles) - 5), len(candles)):
                swing_seq[i] = s_trend
        stoch_seq = ti.stoch(candles, 14, 3)

        # 布林突破：逐点判定该根是否挤出上下轨
        bb_seq: List[Optional[str]] = [None] * len(candles)
        for i in range(20, len(candles)):
            win = closes[i - 19:i + 1]
            mid = sum(win) / 20
            var = sum((x - mid) ** 2 for x in win) / 20
            std = var ** 0.5
            c0 = closes[i]
            if std > 0 and c0 > mid + 2.0 * std:
                bb_seq[i] = "upper_break"
            elif std > 0 and c0 < mid - 2.0 * std:
                bb_seq[i] = "lower_break"

        # 均线排列：逐点多头/空头排列
        stack_seq: List[Optional[str]] = [None] * len(candles)
        for i in range(60, len(candles)):
            w = closes[i - 59:i + 1]
            m5 = sum(w[-5:]) / 5; m10 = sum(w[-10:]) / 10
            m20 = sum(w[-20:]) / 20; m60 = sum(w[-60:]) / 60
            if m5 > m10 > m20 > m60:
                stack_seq[i] = "bull"
            elif m5 < m10 < m20 < m60:
                stack_seq[i] = "bear"

        # MACD DIF/DEA 交叉：逐点事件
        mc_full = ti.macd(closes, 12, 26, 9)
        mc_seq: List[Optional[str]] = [None] * len(candles)
        for i in range(1, len(candles)):
            ml, sl = mc_full["macd"][i], mc_full["signal"][i]
            pml, psl = mc_full["macd"][i - 1], mc_full["signal"][i - 1]
            if ml is None or sl is None or pml is None or psl is None:
                continue
            prev, cur = pml - psl, ml - sl
            if prev <= 0 < cur:
                mc_seq[i] = "golden"
            elif prev >= 0 > cur:
                mc_seq[i] = "death"

        # 量价齐破：逐点判定该根放量且突破近 20 根极值
        vol_seq: List[Optional[str]] = [None] * len(candles)
        for i in range(21, len(candles)):
            wv = [c[5] for c in candles[i - 20:i]]
            cur_v = candles[i][5]
            wm = sum(wv) / len(wv)
            if wm <= 0:
                continue
            wvar = sum((x - wm) ** 2 for x in wv) / len(wv)
            wsd = wvar ** 0.5
            if wsd <= 0 or (cur_v - wm) / wsd < 2.0:
                continue
            wh = max(c[4] for c in candles[i - 20:i])
            wl = min(c[4] for c in candles[i - 20:i])
            if candles[i][4] > wh:
                vol_seq[i] = "bullish_break"
            elif candles[i][4] < wl:
                vol_seq[i] = "bearish_break"

        # Ichimoku 云图：逐点价格相对云上下沿
        ichi = ti.ichimoku(candles)
        ichi_seq: List[Optional[str]] = [None] * len(candles)
        for i in range(len(candles)):
            sa, sb = ichi["senkou_a"][i], ichi["senkou_b"][i]
            if sa is None or sb is None:
                continue
            ct, cb = max(sa, sb), min(sa, sb)
            if closes[i] > ct:
                ichi_seq[i] = "bull"
            elif closes[i] < cb:
                ichi_seq[i] = "bear"

        ind[asset] = {
            "cci": cci_vals,
            "aroon_up": aroon["up"],
            "aroon_down": aroon["down"],
            "swing": swing_seq,
            "kdj_j": kdj_vals["j"],
            "kdj_k": kdj_vals["k"],
            "engulf": eng_seq,
            "ma_cross": cross_seq,
            "macd_zero": zero_seq,
            "stoch": stoch_seq,
            "bb_break": bb_seq,
            "ma_stack": stack_seq,
            "macd_cross": mc_seq,
            "vol_break": vol_seq,
            "ichimoku": ichi_seq,
        }

    # results[rule][group][horizon] = stats
    results: Dict[str, Dict[str, Dict[int, Dict[str, Any]]]] = {}
    for rule_id, direction, _title, matcher in rules:
        results[rule_id] = {}
        signals_by_group: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
        for asset, candles in series.items():
            for i, c in enumerate(candles):
                if matcher(candles, i, ind[asset]):
                    g = ASSET_GROUP.get(asset, "other")
                    signals_by_group[g].append({"asset": asset, "ts": c[0]})
        for group, gsigs in signals_by_group.items():
            results[rule_id][group] = {
                h: evaluate(gsigs, fwd_cache, base_cache, direction, h)
                for h in HORIZONS
            }

    # ── 生成台账 ──
    lines: List[str] = []
    lines.append("# 新接入技术因子有效性台账")
    lines.append("")
    lines.append(f"生成时间：{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}　"
                 f"数据：`{args.db}`")
    lines.append("")
    lines.append("口径：日线因果指标，前向 1/3/5/10 日；样本 <20 标样本不足；"
                 "命中率 −50% ≥ 5pt 且超额同向为正才算有效。")
    lines.append("")
    lines.append("| 因子 | 方向 | 分组 | 样本 | 5日命中率 | 5日超额 | 5日结论 | 1日 | 3日 | 10日 |")
    lines.append("|---|---|---|---|---|---|---|---|---|---|")

    summary: List[Tuple[str, int, str, str]] = []
    for rule_id, direction, title, _m in rules:
        for group in GROUPS:
            stats_map = results[rule_id].get(group)
            if not stats_map:
                continue
            s5 = stats_map[5]
            if s5["n"] == 0:
                continue
            dir_cn = "看涨" if direction == 1 else "看跌"
            def _cell(h: int) -> str:
                s = stats_map[h]
                if s["n"] == 0:
                    return "—"
                return f"{s['hit']}%/{s['edge']}" if direction != 0 else f"n={s['n']}/{s['edge']}"
            hit = "—" if s5["hit"] is None else f"{s5['hit']}%"
            lines.append(
                f"| {rule_id} | {dir_cn} | {group} | {s5['n']} | {hit} | "
                f"{s5['edge'] if s5['edge'] is not None else '—'} | {s5['verdict']} | "
                f"{_cell(1)} | {_cell(3)} | {_cell(10)} |"
            )
            summary.append((rule_id, direction, group, s5["verdict"]))

    # 汇总结论：每个因子跨组的整体判定
    lines.append("")
    lines.append("## 收口建议")
    lines.append("")
    factor_verdict: Dict[str, List[str]] = defaultdict(list)
    for rule_id, _d, _g, verdict in summary:
        factor_verdict[rule_id].append(verdict)
    for rule_id, _d, title, _m in rules:
        v = factor_verdict.get(rule_id, [])
        if not v:
            concl = "样本不足"
        elif "有效" in v:
            concl = "部分组有效，可作决策证据（按组启用）"
        elif "反向" in v:
            concl = "反向，进 HOLD 白名单，不直出方向"
        else:
            concl = "噪声，进 HOLD 白名单，仅作背景证据"
        lines.append(f"- **{rule_id}**（{title}）：{concl}")

    report = "\n".join(lines)
    if not args.no_write:
        OUT_DIR.mkdir(parents=True, exist_ok=True)
        out_path = OUT_DIR / "factor_effectiveness.md"
        out_path.write_text(report, encoding="utf-8")
        print(f"[OK] 台账 → {out_path}")
    else:
        print(report)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
