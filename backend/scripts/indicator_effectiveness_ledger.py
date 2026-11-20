#!/usr/bin/env python3
"""indicator_effectiveness_ledger.py —— 技术指标有效性台账（只读，不改线上逻辑）。

回答的问题
----------
不是"指标算得对不对"（那是单测的事），而是"**这个因子出现过之后，价格真的往那个方向走吗**"。
对 tech_signals 里每条历史信号，从 ohlcv_daily 独立重算前向收益（1/3/5/10 个交易日），
按「因子 × 方向 × 资产」分组，算命中率与相对无信号基线的超额。

三条硬约束（对齐项目口径）
--------------------------
1. **因果**：`uses_future=1` 的信号（依赖未来 K 线确认的转折点）一律剔除，不参与评估；
2. **不造数**：样本 < MIN_SAMPLE 直接判"样本不足"，不给结论；
3. **有基线**：超额 = 信号后收益 - 同资产同期无条件收益。没有基线的"命中 55%"毫无意义
   （牛市里随便什么信号都涨）。

方向映射是**显式可配置**的，写在 BULLISH / BEARISH 里：
新因子没登记就只报"绝对波动"，不猜方向——猜方向等于自己给自己造结论。

用法
----
    python3 scripts/indicator_effectiveness_ledger.py                 # 跑全量并写报告
    python3 scripts/indicator_effectiveness_ledger.py --min-sample 50 --no-write
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
DB_PATH = BACKEND / "trident_event_bus.db"
OUT_DIR = BACKEND / "reports" / "indicator_ledger"

HORIZONS: Tuple[int, ...] = (1, 3, 5, 10)
MIN_SAMPLE = 30
# 命中/超额同时满足才算有效；边际 +5pt 对应二项检验在 n=100 时约 1σ，
# 低于这个量的差异基本都是噪声，不值得拿来调参。
EDGE_HIT = 5.0
#: 分层格子要求更高样本：格子越细越容易"看起来有效"，n≥200 是下限不是保险。
STRATUM_MIN_SAMPLE = 200

# 因子 → 预期方向。只登记信号里真实出现过的 key（见 signal_json 的 "k"）。
# v450：方向表从 6 个因子扩到全部可定向因子。之前只登记了 RSI/背离/EMA，
# 其余 30 多个因子一律按"中性"处理，台账只能写"仅描述（无方向）"，
# 等于把 ADX/SuperTrend/KDJ/Stoch/布林带这些最容易判对错的因子放在盲区里。
# 方向按教科书含义登记；结论里的"反向"= 实际走势与教科书含义相反，不是"没用"。
BULLISH = {
    # 均值回归 / 超卖
    "rsi_oversold", "kdj_oversold", "stoch_oversold", "mfi_oversold",
    "cci_oversold", "bb_lower_break", "support_test",
    # 反转 / 背离
    "div_bottom", "reversal_bottom", "engulfing_bull", "pattern_hammer",
    # 趋势 / 交叉
    "ema_golden", "ma_golden", "macd_bull_cross", "macd_golden", "macd_above_zero",
    "adx_strong_bull", "supertrend_bull", "swing_bull", "aroon_bull",
    "ichimoku_bull", "ma_bull_aligned", "vol_bull_break",
}
BEARISH = {
    # 均值回归 / 超买
    "rsi_overbought", "kdj_overbought", "stoch_overbought", "mfi_overbought",
    "cci_overbought", "bb_upper_break", "resistance",
    # 反转 / 背离
    "div_top", "reversal_top", "engulfing_bear", "pattern_shooting_star",
    # 趋势 / 交叉
    "ema_death", "ma_death", "macd_bear_cross", "macd_death", "macd_below_zero",
    "adx_strong_bear", "supertrend_bear", "swing_bear", "aroon_bear",
    "ichimoku_bear", "ma_bear_aligned", "vol_bear_break",
}
# 方向中性的因子：只报"信号后波动放大/收敛"，不报命中率
NEUTRAL = {"pct_1d", "pct_3d", "vol_spike", "vol_extreme", "atr_expand",
           "rsi_extreme", "liquidation", "rsi_stretch", "value_zone", "ma_slope",
           "pattern_doji", "market_state", "volume_zspike", "atr_expansion"}


# ---------------------------------------------------------------------------
# 数据加载
# ---------------------------------------------------------------------------

def load_daily(conn: sqlite3.Connection) -> Dict[str, List[Tuple[int, float]]]:
    rows = conn.execute(
        "SELECT asset, ts, close FROM ohlcv_daily WHERE close IS NOT NULL ORDER BY asset, ts"
    ).fetchall()
    series: Dict[str, List[Tuple[int, float]]] = defaultdict(list)
    for asset, ts, close in rows:
        series[asset].append((int(ts), float(close)))
    return series


# ── 分层维度（v456）──────────────────────────────────────────────────────
# 与决策端共用 tech_vol_regime 的口径：台账和线上如果各算一套 ATR 分位，
# 报告里的"高波动格子"和线上拦下来的"高波动"就会对不上，验证等于白做。
sys.path.insert(0, str(BACKEND / "src_python"))
from tech_vol_regime import (
    asset_class, load_bars, vol_bucket_series,
)


def load_signals(conn: sqlite3.Connection) -> List[Dict[str, Any]]:
    rows = conn.execute(
        """SELECT asset, ts, close, trigger_type, signal_json, uses_future
           FROM tech_signals ORDER BY ts"""
    ).fetchall()
    out = []
    for asset, ts, close, ttype, sj, uf in rows:
        factors: List[str] = []
        if sj:
            try:
                arr = json.loads(sj)
                factors = [x.get("k") for x in arr if isinstance(x, dict) and x.get("k")]
            except (ValueError, TypeError):
                factors = []
        out.append({"asset": asset, "ts": int(ts), "close": close, "type": ttype,
                    "factors": factors, "uses_future": int(uf or 0)})
    return out


# ---------------------------------------------------------------------------
# 收益计算
# ---------------------------------------------------------------------------

def forward_returns(series: List[Tuple[int, float]]) -> Dict[int, Dict[int, float]]:
    """每个 ts 的前向收益：{horizon: {ts: ret_pct}}。最后一个 horizon 之外不填。"""
    idx = {ts: i for i, (ts, _) in enumerate(series)}
    out: Dict[int, Dict[int, float]] = {h: {} for h in HORIZONS}
    for ts, i in idx.items():
        base = series[i][1]
        if not base:
            continue
        for h in HORIZONS:
            j = i + h
            if j < len(series):
                out[h][ts] = (series[j][1] - base) / base * 100.0
    return out


def baseline_means(fwd: Dict[int, Dict[int, float]]) -> Dict[int, Optional[float]]:
    """同资产无条件基线：全序列前向收益均值（这就是"没有信号时"的期望）。"""
    res: Dict[int, Optional[float]] = {}
    for h in HORIZONS:
        vals = list(fwd[h].values())
        res[h] = round(statistics.fmean(vals), 4) if vals else None
    return res


def baseline_hit_rates(fwd: Dict[int, Dict[int, float]]) -> Dict[int, Optional[float]]:
    """同资产无条件"上涨概率"。

    ★ 关键修正（v450）：拿命中率跟 50% 比是错的 —— 样本里多数资产长期正漂移，
    随便一天买入的 5 日上涨概率本来就有 53%~56%。看涨信号必须打败这个基线，
    看跌信号必须打败 1-基线，否则只是"跟着漂移走"。
    """
    res: Dict[int, Optional[float]] = {}
    for h in HORIZONS:
        vals = list(fwd[h].values())
        res[h] = round(sum(1 for v in vals if v > 0) / len(vals) * 100.0, 2) if vals else None
    return res


# ---------------------------------------------------------------------------
# 统计
# ---------------------------------------------------------------------------

def _pct(part: int, whole: int) -> Optional[float]:
    return round(part / whole * 100.0, 2) if whole else None


def summarize(samples: List[Dict[str, Any]], horizon: int, baseline: float | None,
              direction: int, min_sample: int,
              base_hit: float | None = None) -> Dict[str, Any]:
    """direction: 1=看涨（收益为正算命中）、-1=看跌、0=中性（只看绝对波动）。"""
    vals = [s["ret"][horizon] for s in samples if s["ret"].get(horizon) is not None]
    if not vals:
        return {"n": 0, "hit_rate": None, "avg_ret": None, "edge": None, "verdict": "样本不足"}
    n = len(vals)
    if direction == 0:
        avg = statistics.fmean(vals)
        return {"n": n, "hit_rate": None, "avg_ret": round(avg, 3),
                "avg_abs": round(statistics.fmean([abs(v) for v in vals]), 3),
                "edge": round(avg - baseline, 3) if baseline is not None else None,
                "verdict": "样本不足" if n < min_sample else "仅描述（无方向）"}
    # 看跌因子必须先把收益翻到"因子方向"上再比：
    # 否则"跌了但跌得比基线少"会被算成正超额，结论与命中率互相打架。
    signed = [v * direction for v in vals]
    avg = statistics.fmean(signed)
    base_signed = baseline * direction if baseline is not None else None
    hit_rate = _pct(sum(1 for v in signed if v > 0), n)
    edge = round(avg - base_signed, 3) if base_signed is not None else None
    ref = base_hit if base_hit is not None else 50.0
    if n < min_sample:
        verdict = "样本不足"
    elif hit_rate is not None and hit_rate - ref >= EDGE_HIT and (edge or 0) > 0:
        verdict = "有效"
    elif hit_rate is not None and hit_rate - ref <= -EDGE_HIT and (edge or 0) < 0:
        verdict = "反向"
    else:
        verdict = "噪声"
    return {"n": n, "hit_rate": hit_rate, "base_hit": round(ref, 2),
            "avg_ret": round(avg, 3), "edge": edge, "verdict": verdict}


def build_ledger(conn: sqlite3.Connection, min_sample: int) -> Dict[str, Any]:
    series = load_daily(conn)
    bars = load_bars(conn)
    vol_cache = {a: vol_bucket_series(b) for a, b in bars.items()}
    signals = load_signals(conn)
    fwd_cache: Dict[str, Dict[int, Dict[int, float]]] = {}
    base_cache: Dict[str, Dict[int, Optional[float]]] = {}
    base_hit_cache: Dict[str, Dict[int, Optional[float]]] = {}
    for asset, rows in series.items():
        fwd_cache[asset] = forward_returns(rows)
        base_cache[asset] = baseline_means(fwd_cache[asset])
        base_hit_cache[asset] = baseline_hit_rates(fwd_cache[asset])

    excluded_future = 0
    excluded_no_kline = 0
    used = 0
    # factor → direction → samples
    by_factor: Dict[str, Dict[int, List[Dict[str, Any]]]] = defaultdict(lambda: defaultdict(list))
    by_type: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
    # (维度, 取值, 因子) → samples；维度 = class / vol / both
    by_stratum: Dict[Tuple[str, str, str], List[Dict[str, Any]]] = defaultdict(list)

    for sig in signals:
        if sig["uses_future"]:
            excluded_future += 1
            continue
        fwd = fwd_cache.get(sig["asset"])
        if not fwd:
            excluded_no_kline += 1
            continue
        ret = {h: fwd[h].get(sig["ts"]) for h in HORIZONS}
        if ret[1] is None:
            excluded_no_kline += 1
            continue
        used += 1
        cls = asset_class(sig["asset"])
        vol = vol_cache.get(sig["asset"], {}).get(sig["ts"])
        item = {"asset": sig["asset"], "ts": sig["ts"], "type": sig["type"], "ret": ret,
                "cls": cls, "vol": vol}
        by_type[sig["type"]].append(item)
        for f in sig["factors"]:
            direction = 1 if f in BULLISH else (-1 if f in BEARISH else 0)
            by_factor[f][direction].append(item)
            if vol and direction:
                by_stratum[("class", cls, f)].append(item)
                by_stratum[("vol", vol, f)].append(item)
                by_stratum[("both", f"{cls}/{vol}", f)].append(item)

    def _baseline_for(samples: List[Dict[str, Any]], horizon: int) -> Optional[float]:
        """按样本的资产构成加权：不同资产的漂移不同，不能用单一全局均值当基准。"""
        if not samples:
            return None
        per_asset = defaultdict(list)
        for s in samples:
            b = base_cache.get(s["asset"], {}).get(horizon)
            if b is not None:
                per_asset[s["asset"]].append(b)
        if not per_asset:
            return None
        weights = {a: len(v) for a, v in per_asset.items()}
        total = sum(weights.values())
        return sum(per_asset[a][0] * weights[a] for a in per_asset) / total

    def _base_hit_for(samples: List[Dict[str, Any]], horizon: int, direction: int) -> Optional[float]:
        """按样本资产构成加权：看涨用 P(涨)，看跌用 P(跌)=1-P(涨)。"""
        if not samples:
            return None
        per_asset = defaultdict(list)
        for s in samples:
            b = base_hit_cache.get(s["asset"], {}).get(horizon)
            if b is not None:
                per_asset[s["asset"]].append(b)
        if not per_asset:
            return None
        weights = {a: len(v) for a, v in per_asset.items()}
        total = sum(weights.values())
        up = sum(per_asset[a][0] * weights[a] for a in per_asset) / total
        return up if direction >= 0 else 100.0 - up

    rows: List[Dict[str, Any]] = []
    for factor, dir_map in sorted(by_factor.items()):
        for direction, samples in dir_map.items():
            for h in HORIZONS:
                base = _baseline_for(samples, h)
                bh = _base_hit_for(samples, h, direction)
                stat = summarize(samples, h, base, direction, min_sample, bh)
                rows.append({"group": "factor", "name": factor,
                             "direction": {1: "看涨", -1: "看跌", 0: "中性"}[direction],
                             "horizon": h, "baseline": round(base, 3) if base is not None else None,
                             **stat})
    for ttype, samples in sorted(by_type.items()):
        for h in HORIZONS:
            base = _baseline_for(samples, h)
            stat = summarize(samples, h, base, 0, min_sample)   # trigger_type 本身不带方向
            rows.append({"group": "trigger", "name": ttype, "direction": "无方向",
                         "horizon": h, "baseline": round(base, 3) if base is not None else None,
                         **stat})

    used_ts = sorted(it["ts"] for s in by_type.values() for it in s)
    cut_ts = used_ts[int(len(used_ts) * 0.7)] if used_ts else 0
    for (dim, key, factor), samples in sorted(by_stratum.items()):
        direction = 1 if factor in BULLISH else -1
        for h in HORIZONS:
            base = _baseline_for(samples, h)
            bh = _base_hit_for(samples, h, direction)
            stat = summarize(samples, h, base, direction, max(min_sample, STRATUM_MIN_SAMPLE), bh)
            # 时间外样本：只看最近 30% 的信号。全样本漂亮、后段翻车的格子不能放行。
            ho = [x for x in samples if x["ts"] > cut_ts]
            ho_base = _baseline_for(ho, h)
            ho_bh = _base_hit_for(ho, h, direction)
            ho_stat = summarize(ho, h, ho_base, direction, 60, ho_bh)
            rows.append({"group": "stratum", "dim": dim, "key": key, "name": factor,
                         "direction": {1: "看涨", -1: "看跌"}[direction],
                         "horizon": h, "baseline": round(base, 3) if base is not None else None,
                         "holdout_n": ho_stat.get("n"),
                         "holdout_edge": ho_stat.get("edge"),
                         "holdout_hit": ho_stat.get("hit_rate"),
                         "holdout_base_hit": ho_stat.get("base_hit"),
                         **stat})

    stratum_rows = [r for r in rows if r["group"] == "stratum"]
    tested = len(stratum_rows)

    def _z(r: Dict[str, Any]) -> float:
        """单格二项近似的 z：命中率相对同资产基线概率的偏离，按自身 n 缩放。

        不能只看 5pt：n=200 时 5pt 是 1.4σ，n=800 时同样的 5pt 已经是 3σ。
        用绝对值门槛会把"大样本里的小优势"和"小样本里的大噪声"混为一谈。
        """
        if not r.get("n") or r.get("hit_rate") is None or r.get("base_hit") is None:
            return 0.0
        p0 = float(r["base_hit"]) / 100.0
        p1 = float(r["hit_rate"]) / 100.0
        n = int(r["n"])
        if not (0 < p0 < 1) or n <= 0:
            return 0.0
        se = (p0 * (1 - p0) / n) ** 0.5
        return (p1 - p0) / se if se else 0.0

    zs = [_z(r) for r in stratum_rows]
    tested_n = sum(1 for r in stratum_rows if r.get("n"))
    cells_5pct = sum(1 for z in zs if z >= 1.645)          # 单侧 5%
    expected_5pct = round(tested_n * 0.05, 1)
    # Bonferroni：α=0.05 摊到 tested_n 个格子，单格需的 z
    alpha = 0.05 / max(tested_n, 1)
    z_bonf = round(statistics.NormalDist().inv_cdf(1.0 - alpha), 2)
    max_z = round(max(zs), 2) if zs else 0.0
    cells_bonf = sum(1 for z in zs if z >= z_bonf)

    return {
        "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "db": str(DB_PATH),
        "signals_total": len(signals),
        "signals_used": used,
        "excluded_uses_future": excluded_future,
        "excluded_no_kline": excluded_no_kline,
        "min_sample": min_sample,
        "horizons": list(HORIZONS),
        "rows": rows,
        "assets_with_kline": sorted(series.keys()),
        "strata": {
            "dimensions": ["class", "vol", "both"],
            "min_sample": STRATUM_MIN_SAMPLE,
            "cells_tested": tested,
            "cells_with_sample": tested_n,
            "cells_passing_5pct": cells_5pct,
            "expected_by_chance_5pct": expected_5pct,
            "max_z": max_z,
            "bonferroni_z": z_bonf,
            "cells_passing_bonferroni": cells_bonf,
            "note": f"单侧 5% 通关 {cells_5pct} 格，纯随机预期 {expected_5pct} 格；"
                    f"最强格 z={max_z}，Bonferroni 要 {z_bonf}σ。"
                    "分层在多重比较下没有可宣称的优势，默认不进放行口径。",
        },
    }


# ---------------------------------------------------------------------------
# 报告
# ---------------------------------------------------------------------------

def render_markdown(ledger: Dict[str, Any]) -> str:
    lines = [
        "# 技术指标有效性台账",
        "",
        f"生成时间：{ledger['generated_at']}　数据：`{ledger['db']}`",
        "",
        f"信号总数 {ledger['signals_total']}，参与评估 {ledger['signals_used']}；"
        f"剔除 uses_future {ledger['excluded_uses_future']} 条（依赖未来 K 线确认，不能用来验证）、"
        f"无对齐日线 {ledger['excluded_no_kline']} 条。",
        "",
        f"判定门槛：样本 ≥ {ledger['min_sample']}；命中率要打败**同资产无条件方向概率**"
        f"（不是 50%）且超额同向，≥ {EDGE_HIT}pt 才算有效。",
        "",
        "## 按因子（5 日）",
        "",
        "| 因子 | 方向 | 样本 | 命中率 | 基线命中 | 平均收益 | 基线 | 超额 | 结论 |",
        "|---|---|---|---|---|---|---|---|---|",
    ]
    rows = [r for r in ledger["rows"] if r["group"] == "factor" and r["horizon"] == 5]
    rows.sort(key=lambda r: (r["edge"] is None, -(r["edge"] or 0)))
    for r in rows:
        lines.append(
            f"| {r['name']} | {r['direction']} | {r['n']} | "
            f"{r['hit_rate'] if r['hit_rate'] is not None else '—'} | "
            f"{r.get('base_hit', '—')} | "
            f"{r['avg_ret'] if r['avg_ret'] is not None else '—'} | "
            f"{r['baseline'] if r['baseline'] is not None else '—'} | "
            f"{r['edge'] if r['edge'] is not None else '—'} | {r['verdict']} |"
        )
    lines += ["", "## 按触发类型（5 日，无方向，只看绝对波动与偏置）", "",
              "| 类型 | 样本 | 平均收益 | 平均振幅 | 基线 | 超额 |", "|---|---|---|---|---|---|"]
    for r in [x for x in ledger["rows"] if x["group"] == "trigger" and x["horizon"] == 5]:
        lines.append(
            f"| {r['name']} | {r['n']} | {r['avg_ret']} | {r.get('avg_abs', '—')} | "
            f"{r['baseline'] if r['baseline'] is not None else '—'} | "
            f"{r['edge'] if r['edge'] is not None else '—'} |"
        )
    st = ledger.get("strata") or {}
    if st:
        lines += ["", "## 分层（品种类别 / 波动状态，5 日）", "",
                  f"测了 {st['cells_with_sample']} 个有样本的格子（class / vol / both 三个维度同时测），"
                  f"单侧 5% 通关 {st['cells_passing_5pct']} 格，纯随机预期 {st['expected_by_chance_5pct']} 格；"
                  f"最强格 z={st['max_z']}，Bonferroni 需 {st['bonferroni_z']}σ。", "",
                  "| 维度 | 取值 | 因子 | 方向 | 样本 | 命中率 | 基线命中 | 超额 | 结论 |",
                  "|---|---|---|---|---|---|---|---|---|"]
        srows = [r for r in ledger["rows"]
                 if r["group"] == "stratum" and r["horizon"] == 5 and r["n"] >= 200]
        srows.sort(key=lambda r: (r["edge"] is None, -(r["edge"] or 0)))
        for r in srows[:14]:
            lines.append(f"| {r['dim']} | {r['key']} | {r['name']} | {r['direction']} | {r['n']} | "
                         f"{r['hit_rate']} | {r.get('base_hit', '—')} | "
                         f"{r['edge'] if r['edge'] is not None else '—'} | {r['verdict']} |")
        lines += ["", f"> {st['note']}"]

    lines += ["", "## 全部周期（因子）", "",
              "| 因子 | 方向 | 周期 | 样本 | 命中率 | 超额 | 结论 |", "|---|---|---|---|---|---|---|"]
    for r in sorted([x for x in ledger["rows"] if x["group"] == "factor"],
                    key=lambda r: (r["name"], r["horizon"])):
        lines.append(f"| {r['name']} | {r['direction']} | {r['horizon']}d | {r['n']} | "
                     f"{r['hit_rate'] if r['hit_rate'] is not None else '—'} | "
                     f"{r['edge'] if r['edge'] is not None else '—'} | {r['verdict']} |")
    lines += ["", "> 口径：收益为 `ohlcv_daily` 独立重算的前向收益（%），基线为同资产同期无条件均值。",
              "> 「反向」不代表因子无用——可能只是方向映射与这类资产的实际走势相反，值得单独复核。", ""]
    return "\n".join(lines)


def main() -> int:
    ap = argparse.ArgumentParser(description="技术指标有效性台账")
    ap.add_argument("--db", default=str(DB_PATH))
    ap.add_argument("--min-sample", type=int, default=MIN_SAMPLE)
    ap.add_argument("--no-write", action="store_true")
    args = ap.parse_args()

    conn = sqlite3.connect(f"file:{args.db}?mode=ro", uri=True)
    try:
        ledger = build_ledger(conn, args.min_sample)
    finally:
        conn.close()

    md = render_markdown(ledger)
    print(md)
    if not args.no_write:
        OUT_DIR.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now().strftime("%Y-%m-%d")
        (OUT_DIR / "latest.md").write_text(md, encoding="utf-8")
        (OUT_DIR / f"{stamp}.md").write_text(md, encoding="utf-8")
        (OUT_DIR / "latest.json").write_text(json.dumps(ledger, ensure_ascii=False, indent=2),
                                            encoding="utf-8")
        print(f"\n报告已写入 {OUT_DIR}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
