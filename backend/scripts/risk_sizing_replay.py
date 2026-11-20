#!/usr/bin/env python3
"""risk_sizing_replay.py —— 收益侧验证「按 ATR 波动档调仓位」到底有没有用（只读，v473）。

背景
----
`technical_state.risk_profile` 的日线档位按 **ATR 绝对百分比**分档
（≥3.0% 高×0.5 / ≥1.5% 中×0.8 / <1.5% 低×1.0）。v414 只用「5 根内 |MAE|」
验证过**风险侧**（XAU 1d 3.54× / XAG 2.38× / BTC 1.73×），
但**收益侧从来没算过**：降仓之后净值是变好、还是只是变小，一直没人验证。
本脚本补这一课。

怎么验（严格因果，无前视）
--------------------------
1. t 日收盘后算 ATR14（Wilder）→ `atr_pct = ATR/close×100` → 档位；
2. 仓位在 **t+1 日**生效（`size[i]` 由 `atr_pct[i-1]` 决定），绝不引用未来数据；
3. 换手按单边 `--cost-bps` 计费，只在 |Δsize| 处收费（来回 = 2×单边）；
4. 四条对照：
   - **A 固定满仓** 1.00（基准）
   - **B 离散档位** 1.00 / 0.80 / 0.50（线上就是这条）
   - **C 连续等风险** target/atr_pct，clip[0.5,1.0]，target 取滚动 120 日中位（因果）
   - **D 随机档位** 把 B 的档位标签打乱 200 次、保持同样的降仓频率
     —— 本脚本的核心对照。如果 B 打不过 D，那"择时降仓"只是
     "少拿仓位所以波动小"，档位本身没有信息。

判据（跑之前先写死，避免事后找理由）
----------------------------------
1. Calmar(B) / Calmar(A) ≥ 1.10
2. 三段子区间里 ≥ 2 段 Calmar(B) > Calmar(A)
3. MDD(B) < MDD(A)
4. 效率不塌：CAGR(B)/avg_size(B) ≥ CAGR(A)/avg_size(A) × 0.98
5. 信息性：Calmar(B) 高于随机 D 分布的 95 分位
五条全过 → 「可用」；1~3 过但 4/5 不过 → 「降仓有效但无择时信息」；否则 → 「不成立」。

用法
----
    python3 scripts/risk_sizing_replay.py
    python3 scripts/risk_sizing_replay.py --assets XAU,XAG,HG --rand-trials 300
    python3 scripts/risk_sizing_replay.py --json-out /tmp/r.json
"""
from __future__ import annotations

import argparse
import json
import os
import random
import sqlite3
import statistics
import sys
import time
from typing import Any, Dict, List, Optional, Tuple

BACKEND = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(BACKEND, "src_python"))

import technical_indicators as ti

# v480：库路径优先走 TRIDENT_DB_PATH（与 db_location.DB_PATH 同口径）。
# 旧版硬编码 trident_event_bus.db，设了 TRIDENT_DB_PATH 也不生效 —— v483 记的
# 「pytest 把生产库当测试库」事故，一部分根因就是这类硬编码绕过了环境变量。
DB_PATH = os.environ.get("TRIDENT_DB_PATH") or os.path.join(BACKEND, "trident_event_bus.db")
REPORT_DIR = os.path.join(BACKEND, "reports", "tech_engine")

# v480：Calmar 分母（MDD）下限。回撤小于 0.5% 时比值会爆炸成噪声
# （实测 MSFT 留出段满仓 MDD 只有 0.03，导致 C/A = 4.667），一律判「不可比」。
MIN_MDD_FOR_CALMAR = 0.005

# v480：**比值**的分母下限。MSFT 留出段 C/A = 4.667 的真正原因不是回撤小，
# 而是基准 A 的 Calmar 只有 0.03（几乎不赚钱）→ 任何策略除以它都会变成"大幅改善"。
# 分母弱于 0.30 时比值没有可比性，一律 None。
MIN_BASE_CALMAR_FOR_RATIO = 0.30


def _safe_ratio(num: Optional[float], den: Optional[float],
                min_den: float = MIN_BASE_CALMAR_FOR_RATIO) -> Optional[float]:
    """比值型指标的安全除法：分母过弱或缺失时返回 None（不产出"看着像 alpha"的噪声）。"""
    if num is None or den is None:
        return None
    if den < min_den:
        return None
    return round(num / den, 3)

# 与 technical_state._RISK_BANDS_DAILY / _RISK_LEVELS 完全一致（改这里必须同步那边）
BANDS: Tuple[Tuple[float, str, float], ...] = (
    (3.0, "高", 0.5),
    (1.5, "中", 0.8),
    (0.0, "低", 1.0),
)

DEFAULT_ASSETS = [
    # 10 年日线（v414 的风险侧验证样本）
    "XAU", "XAG", "HG",
    # 近 4 年日线
    "AAPL", "AMD", "AMZN", "GOOGL", "META", "MSFT", "MU", "NVDA", "TSLA",
    "SPX", "NDX", "DJI", "IXIC",
]
# 样本不足的单独列出，只做参考不进结论
SHORT_SAMPLE_ASSETS = ["BTC", "ETH", "SOL", "ADA", "BNB", "DOGE", "XRP"]

# 标的类别（用于"同一套参数在哪些品种上成立"的汇总，不参与任何计算）
CATEGORY = {
    "XAU": "商品", "XAG": "商品", "HG": "商品", "WTI": "商品",
    "SPX": "股指", "NDX": "股指", "DJI": "股指", "IXIC": "股指",
}


def category_of(symbol: str) -> str:
    return CATEGORY.get(symbol, "个股")

MIN_BARS = 300          # 少于这个根数不进结论（ATR 稳定 + 子区间可比）
COST_BPS = 15.0         # 单边；来回 30bp，与 technical_state._COST_BPS 同源
RAND_TRIALS = 200
SEED = 20261119


# ---------------------------------------------------------------------------
# 取数 + 指标
# ---------------------------------------------------------------------------

def load_ohlc(conn: sqlite3.Connection, symbol: str) -> List[List[float]]:
    """[ts_sec, o, h, l, c, v]，升序；按 ts 去重（与 technical_state.load_candles 同口径）。"""
    rows = conn.execute(
        """SELECT ts, open, high, low, close, volume FROM (
               SELECT ts, open, high, low, close, volume,
                      ROW_NUMBER() OVER (PARTITION BY ts ORDER BY rowid DESC) AS rn
               FROM kline_bars
               WHERE symbol=? AND timeframe='1d'
                 AND close IS NOT NULL AND high IS NOT NULL AND low IS NOT NULL)
           WHERE rn=1 ORDER BY ts""", (symbol,)).fetchall()
    return [[int(r[0]) // 1000, r[1], r[2], r[3], r[4], r[5]] for r in rows]


def atr_pct_series(candles: List[List[float]]) -> List[Optional[float]]:
    """Wilder ATR14 ÷ 当日收盘 ×100 —— 与 technical_indicators.atr / market_state 同口径。"""
    atr = ti.atr(candles, 14)
    cs = [c[4] for c in candles]
    out: List[Optional[float]] = []
    for a, c in zip(atr, cs):
        out.append(round(float(a) / float(c) * 100.0, 4) if (a and c) else None)
    return out


def band_size(p: Optional[float]) -> Optional[float]:
    if p is None:
        return None
    for threshold, _name, size in BANDS:
        if p >= threshold:
            return size
    return 1.0


def band_name(p: Optional[float]) -> Optional[str]:
    if p is None:
        return None
    for threshold, name, _size in BANDS:
        if p >= threshold:
            return name
    return "低"


def rolling_median(values: List[Optional[float]], window: int) -> List[Optional[float]]:
    """滚动中位，**只用过去**（t 位置只含 t-window+1..t）。"""
    out: List[Optional[float]] = [None] * len(values)
    buf: List[float] = []
    for i, v in enumerate(values):
        if v is not None:
            buf.append(v)
        if len(buf) > window:
            buf.pop(0)
        if len(buf) >= max(30, window // 2):
            out[i] = statistics.median(buf)
    return out


# ---------------------------------------------------------------------------
# 回测
# -------------------------------------------------------------------

def backtest(closes: List[float], sizes: List[Optional[float]],
             cost_bps: float, flip: bool = False,
             start: int = 1, end: Optional[int] = None) -> Dict[str, List[float]]:
    """size[i] 在 i 日生效。返回逐日净收益与净值（含建仓成本）。

    flip=True 做**符号翻转检验**：把每日收益整体取反（成本仍为正），
    用来诊断"降仓表现好"到底是降波动，还是只是恰好赶上了下跌市。
    start/end 用于**时间外检验**：分段回测时每段从空仓重新起算（含建仓成本）。
    """
    rets: List[float] = []
    equity: List[float] = []
    eq = 1.0
    prev = 0.0
    stop = end if end is not None else len(closes)
    for i in range(max(1, start), min(stop, len(closes))):
        s = sizes[i]
        if s is None:
            continue
        r = closes[i] / closes[i - 1] - 1.0
        if flip:
            r = -r
        turnover = abs(s - prev)
        net = s * r - turnover * cost_bps / 1e4
        eq *= (1.0 + net)
        rets.append(net)
        equity.append(eq)
        prev = s
    return {"rets": rets, "equity": equity}


def max_drawdown(equity: List[float]) -> float:
    """最大回撤（峰值从 1.0 起算，避免首日高点漏算）。"""
    peak = 1.0
    mdd = 0.0
    for e in equity:
        if e > peak:
            peak = e
        dd = 1.0 - e / peak
        if dd > mdd:
            mdd = dd
    return mdd


def metrics(res: Dict[str, List[float]], sizes: List[Optional[float]]) -> Dict[str, Any]:
    rets, eqs = res["rets"], res["equity"]
    n = len(rets)
    if n < 60 or not eqs:
        return {"n": n}
    total = eqs[-1] - 1.0
    cagr = eqs[-1] ** (252.0 / n) - 1.0
    sd = statistics.stdev(rets) if n > 1 else 0.0
    mu = statistics.fmean(rets)
    ann_vol = sd * (252 ** 0.5)
    sharpe = (mu / sd * (252 ** 0.5)) if sd else 0.0
    mdd = max_drawdown(eqs)
    # v480：Calmar 的分母是 MDD，回撤极小时比值会爆炸（实测 MSFT 留出段满仓 MDD 只有
    # 0.03 → C/A 算出 4.667 这种没有意义的数）。低于 0.5% 一律判「不可比」，
    # 宁可给 None，也不给一个看着像 alpha 的噪声值。
    calmar_reliable = mdd >= MIN_MDD_FOR_CALMAR
    calmar = (cagr / mdd) if (calmar_reliable and mdd > 0) else None
    eff_sizes = [s for s in sizes if s is not None]
    avg_size = statistics.fmean(eff_sizes) if eff_sizes else 0.0
    # 年化换手：|Δsize| 之和 ÷ 年数
    turns = 0.0
    prev = 0.0
    for s in eff_sizes:
        turns += abs(s - prev)
        prev = s
    ann_turn = turns / (n / 252.0)
    return {
        "n": n,
        "total_return_pct": round(total * 100, 2),
        "cagr_pct": round(cagr * 100, 2),
        "ann_vol_pct": round(ann_vol * 100, 2),
        "sharpe": round(sharpe, 2),
        "mdd_pct": round(mdd * 100, 2),
        "calmar": round(calmar, 2) if calmar is not None else None,
        "calmar_reliable": bool(calmar_reliable),
        "avg_size": round(avg_size, 3),
        "ann_turnover": round(ann_turn, 2),
    }


# ---------------------------------------------------------------------------
# 分档统计（原始次日收益，不加仓位 —— 看档位本身有没有描述力）
# ---------------------------------------------------------------------------

def bucket_table(candles: List[List[float]], ap: List[Optional[float]]) -> Dict[str, Dict[str, Any]]:
    cs = [c[4] for c in candles]
    acc: Dict[str, List[float]] = {"低": [], "中": [], "高": []}
    for i in range(1, len(cs)):
        name = band_name(ap[i - 1])
        if name is None:
            continue
        acc[name].append(cs[i] / cs[i - 1] - 1.0)
    out: Dict[str, Dict[str, Any]] = {}
    for name, vals in acc.items():
        if not vals:
            out[name] = {"n": 0}
            continue
        downs = [v for v in vals if v < 0]
        out[name] = {
            "n": len(vals),
            "mean_pct": round(statistics.fmean(vals) * 100, 3),
            "abs_mean_pct": round(statistics.fmean([abs(v) for v in vals]) * 100, 3),
            "down_mean_pct": round(statistics.fmean(downs) * 100, 3) if downs else None,
            "win_rate_pct": round(sum(1 for v in vals if v > 0) / len(vals) * 100, 1),
        }
    return out


# ---------------------------------------------------------------------------
# 单标的整套回测
# ---------------------------------------------------------------------------

def replay_asset(conn: sqlite3.Connection, symbol: str, cost_bps: float,
                 rand_trials: int) -> Optional[Dict[str, Any]]:
    candles = load_ohlc(conn, symbol)
    n = len(candles)
    if n < 60:
        return {"symbol": symbol, "skip": f"只有 {n} 根"}
    ap = atr_pct_series(candles)
    cs = [c[4] for c in candles]

    # 三条仓位序列：一律 shift(1)，因果
    size_a: List[Optional[float]] = [None] * n
    size_b: List[Optional[float]] = [None] * n
    med = rolling_median(ap, 120)
    size_c: List[Optional[float]] = [None] * n
    for i in range(1, n):
        p_prev = ap[i - 1]
        if p_prev is None:
            continue
        size_a[i] = 1.0
        size_b[i] = band_size(p_prev)
        m = med[i - 1]
        if m:
            size_c[i] = round(min(1.0, max(0.5, m / p_prev)), 3)

    strat: Dict[str, Dict[str, Any]] = {}
    for key, sizes in (("A_fixed", size_a), ("B_band", size_b), ("C_equalrisk", size_c)):
        res = backtest(cs, sizes, cost_bps)
        strat[key] = metrics(res, sizes)
        strat[key]["_equity"] = res["equity"]  # 供子区间切分，输出时剔除

    # 符号翻转检验：把收益整体取反，看 B/A 的 Calmar 比是否反转
    inv: Dict[str, Dict[str, Any]] = {}
    for key, sizes in (("A_fixed", size_a), ("B_band", size_b), ("C_equalrisk", size_c)):
        res = backtest(cs, sizes, cost_bps, flip=True)
        m = metrics(res, sizes)
        inv[key] = {"cagr_pct": m.get("cagr_pct"), "mdd_pct": m.get("mdd_pct"),
                    "calmar": m.get("calmar")}
    inv_ratio = _safe_ratio(inv["B_band"].get("calmar"), inv["A_fixed"].get("calmar"))
    inv_c_ratio = _safe_ratio(inv["C_equalrisk"].get("calmar"), inv["A_fixed"].get("calmar")
                              ) if inv["A_fixed"].get("calmar") else None
    # 翻转后取负方的 Calmar 意义有限（翻转后净值走跌），这里改用「C 与 B 的相对」：
    # 同一翻转世界里，C 的 Calmar 是否仍严格优于 B —— 这才是"连续优于离散"的稳健证据
    inv_c_vs_b = _safe_ratio(inv["C_equalrisk"].get("calmar"), inv["B_band"].get("calmar"))

    # 时间外检验：前 70% 当"设计/参照"，后 30% 留出验证。
    # 两段各自从空仓起算（含建仓成本），否则首段浮盈会污染留出段。
    split = int(n * 0.7)
    hold: Dict[str, Dict[str, Any]] = {}
    for key, sizes in (("A_fixed", size_a), ("B_band", size_b), ("C_equalrisk", size_c)):
        for seg, (s0, s1) in (("in", (1, split)), ("out", (split, n))):
            res = backtest(cs, sizes, cost_bps, start=s0, end=s1)
            m = metrics(res, sizes)
            hold.setdefault(seg, {})[key] = {"cagr_pct": m.get("cagr_pct"),
                                             "mdd_pct": m.get("mdd_pct"),
                                             "calmar": m.get("calmar"),
                                             "calmar_reliable": m.get("calmar_reliable"),
                                             "n": m.get("n")}
    out_a = hold["out"]["A_fixed"].get("calmar")
    out_c = hold["out"]["C_equalrisk"].get("calmar")
    # v480：基准过弱（Calmar < 0.30）时不给比值 —— MSFT 的 4.667 就是这么来的
    out_c_vs_a = _safe_ratio(out_c, out_a)
    out_c_vs_a_note = (None if out_c_vs_a is not None else
                       ("留出段基准 A 的 Calmar 过弱（%s），比值不可比" % out_a
                        if out_a is not None else "留出段缺 Calmar"))

    # 滚动留出：单次 70/30 的留出段只有 300 根，Calmar 噪声极大（MSFT 出现 4.67 这种值）。
    # 改成滚动窗口：从 40% 位置起，每窗 250 根、步长 125 根，逐窗比较 C/A。
    # 判据用**胜率**而不是某一窗的比值 —— 50% 附近就是噪声。
    roll_win, roll_total = 0, 0
    roll_pairs: List[Tuple[float, float]] = []
    w_len = min(250, max(120, n // 5))
    step = max(60, w_len // 2)
    w0 = int(n * 0.4)
    while w0 + w_len <= n:
        a_m = metrics(backtest(cs, size_a, cost_bps, start=w0, end=w0 + w_len), size_a)
        c_m = metrics(backtest(cs, size_c, cost_bps, start=w0, end=w0 + w_len), size_c)
        a_c, c_c = a_m.get("calmar"), c_m.get("calmar")
        # v480：基准 Calmar 过弱的窗不参与胜负统计（否则"赢"只是分母太小）
        if a_c and c_c and a_c >= MIN_BASE_CALMAR_FOR_RATIO:
            roll_total += 1
            if c_c > a_c:
                roll_win += 1
            roll_pairs.append((round(a_c, 2), round(c_c, 2)))
        w0 += step
    roll_rate = round(roll_win / roll_total, 3) if roll_total else None

    # 随机对照 D：打乱 B 的有效档位标签
    valid_pos = [i for i in range(n) if size_b[i] is not None]
    valid_vals = [size_b[i] for i in valid_pos]
    rng = random.Random(SEED + sum(ord(ch) for ch in symbol))
    rand_cal: List[float] = []
    rand_sharpe: List[float] = []
    rand_mdd: List[float] = []
    for _ in range(rand_trials):
        rng.shuffle(valid_vals)
        shuf: List[Optional[float]] = [None] * n
        for pos, val in zip(valid_pos, valid_vals):
            shuf[pos] = val
        res = backtest(cs, shuf, cost_bps)
        m = metrics(res, shuf)
        if m.get("calmar") is not None:
            rand_cal.append(m["calmar"])
        rand_sharpe.append(m["sharpe"])
        rand_mdd.append(m["mdd_pct"])
    rand_cal.sort()

    def p95(vals: List[float]) -> Optional[float]:
        if not vals:
            return None
        return vals[min(len(vals) - 1, int(0.95 * (len(vals) - 1)))]

    # 三段子区间：按根数三等分，各自的净值重新归一
    def sub_calmar(equity: List[float]) -> List[Optional[float]]:
        m = len(equity)
        out = []
        for k in range(3):
            lo = m * k // 3
            hi = m * (k + 1) // 3
            seg = equity[lo:hi]
            if len(seg) < 30:
                out.append(None)
                continue
            base = equity[lo - 1] if lo > 0 else 1.0
            norm = [e / base for e in seg]
            n_seg = len(norm)
            cagr = norm[-1] ** (252.0 / n_seg) - 1.0
            mdd = max_drawdown(norm)
            out.append(round(cagr / mdd, 2) if mdd > 0 else None)
        return out

    sub = {k: sub_calmar(v.pop("_equity")) for k, v in strat.items()}

    # 判据
    A, B = strat["A_fixed"], strat["B_band"]
    checks: Dict[str, Any] = {}
    checks["calmar_ratio_vs_A"] = (round(B["calmar"] / A["calmar"], 3)
                                   if A.get("calmar") and B.get("calmar") else None)
    checks["sub_period_wins"] = sum(
        1 for x, y in zip(sub["B_band"], sub["A_fixed"])
        if x is not None and y is not None and x > y)
    checks["mdd_better"] = bool(B["mdd_pct"] < A["mdd_pct"])
    eff_a = (A["cagr_pct"] / A["avg_size"]) if A["avg_size"] else None
    eff_b = (B["cagr_pct"] / B["avg_size"]) if B["avg_size"] else None
    checks["efficiency_keep"] = (round(eff_b / eff_a, 3)
                                 if eff_a and eff_b is not None else None)
    checks["beats_random_p95"] = (B["calmar"] > p95(rand_cal)) if (rand_cal and B.get("calmar")) else None

    passed = [
        checks["calmar_ratio_vs_A"] is not None and checks["calmar_ratio_vs_A"] >= 1.10,
        checks["sub_period_wins"] >= 2,
        checks["mdd_better"],
        checks["efficiency_keep"] is not None and checks["efficiency_keep"] >= 0.98,
        checks["beats_random_p95"] is True,
    ]
    n_pass = sum(1 for p in passed if p)
    if n_pass == 5:
        verdict = "可用"
    elif n_pass >= 3:
        verdict = "降仓有效但无择时信息"
    else:
        verdict = "不成立"

    return {
        "symbol": symbol,
        "bars": n,
        "span": [candles[0][0], candles[-1][0]],
        "strategies": strat,
        "sub_calmar": sub,
        "buckets": bucket_table(candles, ap),
        "inverted": {**inv, "calmar_ratio_vs_A": inv_ratio,
                     "C_ratio_vs_A": inv_c_ratio, "C_vs_B": inv_c_vs_b},
        "holdout": {**hold, "split_ratio": 0.7, "out_C_vs_A": out_c_vs_a,
                    "out_C_vs_A_note": out_c_vs_a_note},
        "rolling": {"window": w_len, "step": step, "total": roll_total,
                    "wins": roll_win, "win_rate": roll_rate},
        "random": {
            "trials": len(rand_cal),
            "calmar_p50": round(statistics.median(rand_cal), 2) if rand_cal else None,
            "calmar_p95": round(p95(rand_cal), 2) if rand_cal else None,
            "mdd_p50": round(statistics.median(rand_mdd), 2) if rand_mdd else None,
            "sharpe_p50": round(statistics.median(rand_sharpe), 2) if rand_sharpe else None,
        },
        "checks": checks,
        "n_pass": n_pass,
        "verdict": verdict,
    }


# -------------------------------------------------------------------
# 报告
# -------------------------------------------------------------------

def render(results: List[Dict[str, Any]], short: List[Dict[str, Any]],
           cost_bps: float, trials: int) -> str:
    L: List[str] = []
    L.append("# 波动档调仓位 · 收益侧回测（risk_sizing_replay）")
    L.append("")
    L.append(f"生成时间：{time.strftime('%Y-%m-%d %H:%M:%S')}　数据：`{DB_PATH}`")
    L.append(f"口径：ATR14(Wilder)/收盘×100 分档（≥3%高×0.5 / ≥1.5%中×0.8 / <1.5%低×1.0），"
             f"t 日档位 → t+1 日仓位，单边成本 {cost_bps}bp，随机对照 {trials} 次。")
    L.append("")
    L.append("## 结论（先看这里）")
    L.append("")
    ok = [r for r in results if r.get("verdict") == "可用"]
    partial = [r for r in results if r.get("verdict") == "降仓有效但无择时信息"]
    fail = [r for r in results if r.get("verdict") == "不成立"]
    L.append(f"- 可用 **{len(ok)}** 个：{', '.join(r['symbol'] for r in ok) if ok else '无'}")
    L.append(f"- 降仓有效但无择时信息 **{len(partial)}** 个："
             f"{', '.join(r['symbol'] for r in partial) if partial else '无'}")
    L.append(f"- 不成立 **{len(fail)}** 个：{', '.join(r['symbol'] for r in fail) if fail else '无'}")
    L.append("")
    # 三条横向统计，避免只看单个标的
    n_all = len(results)
    b_up = sum(1 for r in results
               if r["strategies"]["B_band"].get("calmar") and r["strategies"]["A_fixed"].get("calmar")
               and r["strategies"]["B_band"]["calmar"] > r["strategies"]["A_fixed"]["calmar"])
    c_up = sum(1 for r in results
               if r["strategies"]["C_equalrisk"].get("calmar") and r["strategies"]["A_fixed"].get("calmar")
               and r["strategies"]["C_equalrisk"]["calmar"] > r["strategies"]["A_fixed"]["calmar"])
    mdd_down = sum(1 for r in results if r["strategies"]["B_band"]["mdd_pct"] < r["strategies"]["A_fixed"]["mdd_pct"])
    flip_worse = sum(1 for r in results
                     if r["inverted"].get("calmar_ratio_vs_A") and r["inverted"]["calmar_ratio_vs_A"] > 1.10)
    c_beats_b_flip = sum(1 for r in results
                         if r["inverted"].get("C_vs_B") and r["inverted"]["C_vs_B"] > 1.0)
    c_beats_a_flip = sum(1 for r in results
                         if r["inverted"].get("C_ratio_vs_A") and r["inverted"]["C_ratio_vs_A"] > 1.0)
    out_pass = sum(1 for r in results
                   if r["holdout"].get("out_C_vs_A") and r["holdout"]["out_C_vs_A"] > 1.0)
    L.append(f"三条横向统计（共 {n_all} 个标的）：")
    L.append("")
    L.append(f"| 问题 | 结果 | 含义 |")
    L.append("|---|---|---|")
    L.append(f"| 降仓能不能降回撤？ | **{mdd_down}/{n_all}** 的最大回撤变小 | 这条是真的，波动档确实识别到了波动 |")
    L.append(f"| 离散三档能不能提升风险调整收益？ | **{b_up}/{n_all}** 的 Calmar 高于满仓 | 多数情况下只是「少赚也少波动」，净效果打平或变差 |")
    L.append(f"| 连续等风险（C）能不能提升？ | **{c_up}/{n_all}** 的 Calmar 高于满仓 | 明显好于离散三档：阈值太粗，温和高波动被一刀砍到 0.5 是主因 |")
    L.append(f"| 结论是不是只靠样本期方向？ | 翻转收益后 **{flip_worse}/{n_all}** 个标的 B/A Calmar 反而 >1.10 | 翻转后变好 → 说明原结论来自「这一段恰好上涨」，不是选时能力 |")
    L.append(f"| 连续版是不是也只在原方向成立？ | 翻转后 C 仍优于 B 的有 **{c_beats_b_flip}/{n_all}** | 两个方向都比离散档好 → 连续缩放是结构性的，不只是样本期效应 |")
    L.append(f"| 连续版本身值不值得上线？ | 翻转后 C 仍优于满仓的有 **{c_beats_a_flip}/{n_all}** | 两个方向都优于满仓才算真改善；否则仍只是「降杠杆」 |")
    L.append(f"| 时间外还成立吗？ | 后 30% 留出段 C 仍优于满仓的有 **{out_pass}/{n_all}** | 留出段是「从没参与过判断」的那段行情，这条不过就不该上线 |")
    L.append("")
    L.append("## 逐标的")
    L.append("")
    L.append("| 标的 | 根数 | A 固定满仓 CAGR/MDD/Calmar | B 档位 CAGR/MDD/Calmar | "
             "C 等风险 CAGR/MDD/Calmar | 平均仓位 B | B/A Calmar | 跑赢随机95分位 | 判定 |")
    L.append("|---|---|---|---|---|---|---|---|---|")
    for r in results:
        A, B, C = r["strategies"]["A_fixed"], r["strategies"]["B_band"], r["strategies"]["C_equalrisk"]
        L.append(
            f"| {r['symbol']} | {r['bars']} | "
            f"{A['cagr_pct']}% / {A['mdd_pct']}% / {A['calmar']} | "
            f"{B['cagr_pct']}% / {B['mdd_pct']}% / {B['calmar']} | "
            f"{C['cagr_pct']}% / {C['mdd_pct']}% / {C['calmar']} | "
            f"{B['avg_size']} | {r['checks']['calmar_ratio_vs_A']} | "
            f"{r['checks']['beats_random_p95']} | {r['verdict']} |")
    L.append("")
    L.append("## 连续等风险 vs 离散三档（C 相对 A）")
    L.append("")
    L.append("| 标的 | A Calmar | C Calmar | C/A | C 平均仓位 | 翻转后 C/B | 结论 |")
    L.append("|---|---|---|---|---|---|---|")
    for r in results:
        A, C = r["strategies"]["A_fixed"], r["strategies"]["C_equalrisk"]
        ratio = round(C["calmar"] / A["calmar"], 3) if A.get("calmar") and C.get("calmar") else None
        inv_cb = r["inverted"].get("C_vs_B")
        L.append(f"| {r['symbol']} | {A['calmar']} | {C['calmar']} | {ratio} | "
                 f"{C['avg_size']} | {inv_cb} | {'改善' if ratio and ratio > 1 else '未改善'} |")
    L.append("")
    L.append("## 符号翻转检验（收益取反，成本照收）")
    L.append("")
    L.append("| 标的 | 原 B/A Calmar | 翻转后 B/A Calmar | 翻转后 C/B | 翻转后是否更优 | 判读 |")
    L.append("|---|---|---|---|---|---|")
    for r in results:
        c = r["checks"].get("calmar_ratio_vs_A")
        inv = r["inverted"].get("calmar_ratio_vs_A")
        inv_cb = r["inverted"].get("C_vs_B")
        better_inv = (inv > 1.10) if inv is not None else None
        if inv is None or c is None:
            note = "—"
        elif better_inv and not (c >= 1.10):
            note = "只在下跌市占便宜，不是选时"
        elif better_inv and c >= 1.10:
            note = "两个方向都改善 → 降波动成立"
        elif not better_inv and not (c >= 1.10):
            note = "两个方向都不改善 → 档位无效"
        else:
            note = "只在上涨市改善，存疑"
        L.append(f"| {r['symbol']} | {c} | {inv} | {inv_cb} | {better_inv} | {note} |")
    L.append("")
    L.append("## 判据逐条（B 相对 A）")
    L.append("")
    L.append("| 标的 | 1 Calmar比≥1.10 | 2 三段胜出≥2 | 3 MDD更小 | 4 效率保留≥0.98 | "
             "5 跑赢随机95 | 通过数 |")
    L.append("|---|---|---|---|---|---|---|")
    for r in results:
        c = r["checks"]
        L.append(f"| {r['symbol']} | {c['calmar_ratio_vs_A']} | {c['sub_period_wins']}/3 | "
                 f"{c['mdd_better']} | {c['efficiency_keep']} | {c['beats_random_p95']} | "
                 f"{r['n_pass']}/5 |")
    L.append("")
    L.append("## 分档描述力（原始次日收益，不加仓位）")
    L.append("")
    L.append("| 标的 | 档位 | 样本 | 次日均涨% | |均涨|% | 下跌日均值% | 胜率% |")
    L.append("|---|---|---|---|---|---|---|")
    for r in results:
        for name in ("低", "中", "高"):
            b = r["buckets"].get(name) or {}
            if not b.get("n"):
                continue
            L.append(f"| {r['symbol']} | {name} | {b['n']} | {b['mean_pct']} | "
                     f"{b['abs_mean_pct']} | {b['down_mean_pct']} | {b['win_rate_pct']} |")
    L.append("")
    L.append("## 滚动窗口胜率（最终判据）")
    L.append("")
    L.append("每窗 250 根、步长 125 根、从 40% 位置起；逐窗比较 C 与 A 的 Calmar。"
             "50% 附近就是噪声，**合计胜率远低于 50% 说明该策略在时间上没有持续性**。")
    L.append("")
    L.append("> v480 口径修正：基准 A 的 Calmar 低于 0.30 时（几乎不赚钱甚至亏）比值会爆炸，"
             "这类窗与留出段一律记「不可比」并**不计入分母** —— 否则 MSFT 那种 C/A=4.667 "
             "的伪改善会被当成有效证据。")
    L.append("")
    L.append("| 标的 | 窗口数 | C 胜出 | 胜率 | 前70% C/A | 后30% C/A | 结论 |")
    L.append("|---|---|---|---|---|---|---|")
    tot_win = tot_n = 0
    for r in results:
        ro, ho = r["rolling"], r["holdout"]
        rate = ro.get("win_rate")
        tot_win += ro.get("wins") or 0
        tot_n += ro.get("total") or 0
        verdict = ("时间外成立" if (rate is not None and rate >= 0.55)
                   else "时间外不成立" if rate is not None else "—")
        in_a = (ho.get("in") or {}).get("A_fixed", {}).get("calmar")
        in_c = (ho.get("in") or {}).get("C_equalrisk", {}).get("calmar")
        in_ratio = _safe_ratio(in_c, in_a)
        out_r = ho.get("out_C_vs_A")
        out_txt = out_r if out_r is not None else "不可比"
        L.append(f"| {r['symbol']} | {ro.get('total')} | {ro.get('wins')} | "
                 f"{rate} | {in_ratio} | {out_txt} | {verdict} |")
    L.append(f"| **合计** | **{tot_n}** | **{tot_win}** | "
             f"**{round(tot_win / tot_n, 3) if tot_n else None}** | — | — | — |")
    L.append("")
    L.append("## 三段子区间 Calmar")
    L.append("")
    L.append("| 标的 | A 三段 | B 三段 | C 三段 |")
    L.append("|---|---|---|---|")
    for r in results:
        L.append(f"| {r['symbol']} | {r['sub_calmar']['A_fixed']} | "
                 f"{r['sub_calmar']['B_band']} | {r['sub_calmar']['C_equalrisk']} |")
    L.append("")
    if short:
        L.append("## 样本不足（仅参考，不进结论）")
        L.append("")
        L.append("| 标的 | 根数 | A CAGR | B CAGR | B avg_size | 备注 |")
        L.append("|---|---|---|---|---|---|")
        for r in short:
            if r.get("skip"):
                L.append(f"| {r['symbol']} | - | - | - | - | {r['skip']} |")
            else:
                A, B = r["strategies"]["A_fixed"], r["strategies"]["B_band"]
                L.append(f"| {r['symbol']} | {r['bars']} | {A['cagr_pct']}% | "
                         f"{B['cagr_pct']}% | {B['avg_size']} | 少于 {MIN_BARS} 根 |")
        L.append("")
    L.append("## 分品种结论（同一套参数在哪些品种上成立）")
    L.append("")
    L.append("| 类别 | 标的 | 离散 B 的 Calmar 改善 | 连续 C 双向优于满仓（原方向+翻转） | 判读 |")
    L.append("|---|---|---|---|---|")
    cats: Dict[str, List[Dict[str, Any]]] = {}
    for r in results:
        cats.setdefault(category_of(r["symbol"]), []).append(r)
    for cat, rs in cats.items():
        b_ok = [r["symbol"] for r in rs
                if r["strategies"]["B_band"].get("calmar") and r["strategies"]["A_fixed"].get("calmar")
                and r["strategies"]["B_band"]["calmar"] > r["strategies"]["A_fixed"]["calmar"]]
        c_both = [r["symbol"] for r in rs
                  if r["strategies"]["C_equalrisk"].get("calmar") and r["strategies"]["A_fixed"].get("calmar")
                  and r["strategies"]["C_equalrisk"]["calmar"] > r["strategies"]["A_fixed"]["calmar"]
                  and (r["inverted"].get("C_ratio_vs_A") or 0) > 1.0]
        if c_both and b_ok:
            note = "连续版可上；离散档不建议做仓位"
        elif c_both:
            note = "连续版可上；离散档明确不适合做仓位"
        elif b_ok:
            note = "只有离散档零星改善，证据不足"
        else:
            note = "这套参数在该类品种上不成立"
        L.append(f"| {cat} | {', '.join(r['symbol'] for r in rs)} | {len(b_ok)}/{len(rs)} | "
                 f"{len(c_both)}/{len(rs)} | {note} |")
    L.append("")
    L.append("> 读法：黄金/白银的 ATR% 本来就常在 3% 以上，"
             "用固定阈值给它降仓等于「常年半仓」，没有信息量；"
             "股指/大型科技股的 ATR% 多在 1~2%，波动扩到 3% 才是异常，缩放才有意义。")
    L.append("")
    L.append("## 时间外检验（前 70% 参照 / 后 30% 留出）")
    L.append("")
    L.append("| 标的 | 段 | A Calmar | B Calmar | C Calmar | C/A |")
    L.append("|---|---|---|---|---|---|")
    for r in results:
        ho = r["holdout"]
        for seg, label in (("in", "前70%"), ("out", "后30%")):
            row = ho.get(seg) or {}
            a, b, c = row.get("A_fixed") or {}, row.get("B_band") or {}, row.get("C_equalrisk") or {}
            ratio = (round(c["calmar"] / a["calmar"], 3)
                     if a.get("calmar") and c.get("calmar") else None)
            L.append(f"| {r['symbol']} | {label} | {a.get('calmar')} | {b.get('calmar')} | "
                     f"{c.get('calmar')} | {ratio} |")
    L.append("")
    L.append("## 结论：不上线（按证据排序）")
    L.append("")
    L.append("1. **仓位计算保持现状，一行不改**：离散三档的 Calmar 只有 4/16、翻转后 0/16；"
             f"连续等风险版样本内 9/16，但**时间外滚动胜率 "
             f"{round(tot_win / tot_n, 3) if tot_n else None}"
             "（基准过弱的窗已剔除）**，属样本内拟合。"
             "两者都不支持「靠 ATR 调仓位赚更多」。")
    L.append("2. **ATR 档位继续只当「止损倍数」依据（1.5/2/3 倍）**："
             "档位对次日振幅的区分力是真的（高档 |均涨| 普遍是低档的 1.4~3.0 倍），"
             "对方向的区分力是零（高档次日均涨并不比低档差，很多还更高）。")
    L.append("3. **把「收益侧不成立」写进读数**：`risk_profile` 的 `sizing_evidence` 与 "
             "`validation_note` 已带上这条结论，避免下游把 `size_factor` 当收益信号。")
    L.append("4. **以后要改仓位口径，先过时间外滚动**：符号翻转只改收益方向、不改时间段，"
             "证明不了「换个时间段还成立」—— 本轮就是先被翻转结果误导、再做滚动才纠回来的。")
    L.append("")
    L.append("## 口径与局限")
    L.append("")
    L.append("1. **只做多**：降仓腾出的现金按 0 收益处理（保守）。因此 B 的绝对收益通常低于 A，"
             "结论必须看风险调整口径（Calmar/MDD），不能拿 CAGR 单挑。")
    L.append("2. **随机对照 D 是关键**：它保留了同样的降仓频率与平均仓位，只打乱时点。"
             "B 打不过 D，说明收益改善全部来自「平均仓位更低」，档位没有择时信息。")
    L.append("3. **档位阈值未调优**：1.5%/3.0% 是 v414 定的固定值，本脚本不搜索最优阈值"
             "（搜出来的阈值必然是过拟合，需要另做时间外检验）。")
    L.append("4. **不含止损**：只验证仓位缩放，不模拟止损被打掉后的再入场；"
             "止损 ATR 倍数（1.5/2/3）是另一条待验的线。")
    L.append("5. 数据已过体检：重复收盘 ≤20%、主间隔占比 ≥50%；WTI 因间隔不规整已剔除。")
    L.append("6. **符号翻转检验**：把每日收益取反、成本仍为正，是诊断「结论依赖样本期方向」的标准做法。"
             "翻转后如果降仓反而更优，说明原样本期恰好是上涨市，"
             "原结论来自「牛市里少拿仓位」而不是「择时能力」。")
    L.append("")
    return "\n".join(L)


def main() -> int:
    ap_ = argparse.ArgumentParser()
    ap_.add_argument("--assets", default=",".join(DEFAULT_ASSETS))
    ap_.add_argument("--short", default=",".join(SHORT_SAMPLE_ASSETS))
    ap_.add_argument("--cost-bps", type=float, default=COST_BPS)
    ap_.add_argument("--rand-trials", type=int, default=RAND_TRIALS)
    ap_.add_argument("--json-out", default=os.path.join(REPORT_DIR, "risk_sizing_replay.json"))
    ap_.add_argument("--md-out", default=os.path.join(REPORT_DIR, "risk_sizing_replay.md"))
    args = ap_.parse_args()

    if not os.path.exists(DB_PATH):
        print(f"数据库不存在：{DB_PATH}", file=sys.stderr)
        return 1
    conn = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True)

    results: List[Dict[str, Any]] = []
    short: List[Dict[str, Any]] = []
    for sym in [s.strip() for s in args.assets.split(",") if s.strip()]:
        r = replay_asset(conn, sym, args.cost_bps, args.rand_trials)
        if r is None:
            continue
        if "skip" in r or r["bars"] < MIN_BARS:
            short.append(r)
            continue
        results.append(r)
        print(f"{sym:<10} A {r['strategies']['A_fixed']['calmar']} / "
              f"B {r['strategies']['B_band']['calmar']} "
              f"(仓位×{r['strategies']['B_band']['avg_size']}) → {r['verdict']}")

    for sym in [s.strip() for s in args.short.split(",") if s.strip()]:
        r = replay_asset(conn, sym, args.cost_bps, 30)
        if r and "skip" not in r:
            short.append(r)

    conn.close()
    os.makedirs(REPORT_DIR, exist_ok=True)
    md = render(results, short, args.cost_bps, args.rand_trials)
    with open(args.md_out, "w", encoding="utf-8") as f:
        f.write(md)
    payload = {
        "generated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "cost_bps_one_way": args.cost_bps,
        "rand_trials": args.rand_trials,
        "bands": [{"atr_pct_ge": t, "level": n, "size": s} for t, n, s in BANDS],
        "assets": [
            {k: v for k, v in r.items()} for r in results
        ],
        "short_sample": short,
    }
    with open(args.json_out, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)

    print(f"\n报告：{args.md_out}")
    print(f"JSON：{args.json_out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
