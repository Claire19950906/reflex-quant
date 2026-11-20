"""
tech_detector.py — v303 R12: 技术指标组合异动检测 (替代纯涨跌幅统计)
==============================================================================
背景
----
旧 `backfill_critical_moves.detect_moves()` 只做一件事:
    pct = (close[i+k] - close[i]) / close[i] * 100
即"事后涨跌幅统计"。用户反馈"价格异动识别并不准确",因为:
  * 完全没有读 OHLCV — volume 根本没参与
  * 没有 RSI/MACD, 谈不上背离
  * 没有 pivot 高低点配对, 谈不上支撑位
  * 且 ts 取窗口**起点** i, 标记时间本身就错位

设计红线(用户明确要求)
--------------------
1. **禁止未来函数**: 任何一根 bar 的判定只允许使用 index <= t 的数据。
   唯一例外是 `reversal`(大涨大跌转折点), 用户明确许可该场景使用
   未来函数, 但会打 `uses_future=True` 标记且单独开关 (见 CONFIG)。
2. **技术指标组合**: 单指标不触发, 必须 >=2 类信号确认, 避免噪音。
3. 支撑位 / 爆单热力 作为额外信号参与打分。

实现要点
--------
所有指标都在 `indicators()` 里按索引 i 递推计算, 保证 causal:
    - EMA/SMA   : 只用 [0..i]
    - RSI       : Wilder 平滑, 只用 [0..i]
    - MACD      : EMA12-EMA26, signal 为 9 日 EMA, 只用 [0..i]
    - volume_ratio: vol[i] / mean(vol[i-L..i])  —— 分母含 i, 仍 causal
    - pivot     : 左右各 W 根确认, 定义上需要右侧 W 根, 故
                `confirmed[i] = (i+W <= last)`, 调用方自行决定延迟处理。
                这是"因果 pivot": 牺牲 W 根延迟换无未来函数。
"""
from __future__ import annotations

import json
import math
import os
import sqlite3
import sys
from typing import Any

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import technical_indicators as ti

# ───────────────────────── 配置 ─────────────────────────
CONFIG = {
    "ema_fast": 12,
    "ema_slow": 26,
    "ma_support": 20,
    "rsi_period": 14,
    "rsi_overbought": 70.0,
    "rsi_oversold": 30.0,
    "divergence_lookback": 60,      # 回看多少根找两个可比高点/低点
    "pivot_window": 3,              # 左右各 3 根确认 pivot (causal, 有延迟)
    "min_pivot_gap": 5,             # 两个 pivot 至少间隔 5 根
    "vol_window": 20,
    "vol_spike_ratio": 2.0,         # 量比 >= 2 视为激增
    "vol_extreme_ratio": 3.5,
    "atr_period": 14,
    "atr_expand_ratio": 1.6,        # ATR / ATR均值 >= 1.6 视为波动放大
    "pct_move_1d": 3.0,
    "pct_move_3d": 5.0,
    "score_mid": 1.8,               # 分数拐点：Σw=1.8 时约 0.5 分
    "score_slope": 1.0,             # 拐点处的展开速度
    "min_event_families": 2,        # 至少 2 个独立证据族才产出信号
    "reversal_lookahead": 3,        # ⚠ 仅历史审计用，线上默认关闭(见 enable_reversal_future)
    "reversal_pivot_window": 5,     # 转折点用的枢轴确认窗（比普通 pivot 更严）
    "reversal_lookback": 20,        # 枢轴必须是近 20 根的极值，避免每段小波动都算转折
    "reversal_min_pct": 3.0,        # 因果转折点: 从枢轴起至少反向 3%
    "reversal_atr_mult": 1.5,       # 且不低于 1.5×ATR%，避免低波动里被固定 3% 噪声触发
    # 全局开关: True 才会产出 uses_future=1 的审计信号；线上必须保持 False
    "enable_reversal_future": False,
}

ALLOW_FUTURE = CONFIG["enable_reversal_future"]


# ─────────────────── 证据族与打分（v450） ───────────────────
# 旧口径把每一条命中按权重相加再封顶 0.95，结果是 8880 条信号里 8877 条都是 0.95
# —— 分不出强弱，min_score 过滤和前端"信号强度"全部失效。
# 新口径：
#   1) 只有"事件"（新发生的变化）计分；趋势/排列这类"状态"只作背景(context)；
#   2) 同一族内互相关（RSI/KDJ/Stoch 同属动量，SuperTrend/ADX 同属趋势）只取最大权重，
#      防止同一个观点被重复计数；
#   3) 分数用指数压缩 1-exp(-Σw/3)，天然分布在 0~1，不会全部顶到上限。
_EVENT_FAMILY: dict[str, str] = {
    "div_top": "divergence", "div_bottom": "divergence",
    "reversal_top": "reversal", "reversal_bottom": "reversal",
    "pct_1d": "price", "pct_3d": "price",
    "vol_spike": "volume", "vol_extreme": "volume",
    "vol_bull_break": "volume", "vol_bear_break": "volume",
    "atr_expand": "volatility",
    "rsi_oversold": "momentum", "rsi_overbought": "momentum",
    "rsi_extreme": "momentum",
    "kdj_oversold": "momentum", "kdj_overbought": "momentum",
    "stoch_oversold": "momentum", "stoch_overbought": "momentum",
    "bb_lower_break": "momentum", "bb_upper_break": "momentum",
    "support_test": "support", "resistance": "support",
}

# 只作背景的状态型证据：不参与打分，但仍写进 signal_json 供前端/归因展示。
_CONTEXT_ONLY: frozenset[str] = frozenset({
    "adx_strong_bull", "adx_strong_bear",
    "supertrend_bull", "supertrend_bear",
    "swing_bull", "swing_bear",
    "aroon_bull", "aroon_bear",
    "ma_bull_aligned", "ma_bear_aligned",
    "ichimoku_bull", "ichimoku_bear",
    "liquidation",
})


def _score_from_hits(hits: list[dict]) -> tuple[float, int, float]:
    """返回 (score, 证据族数, 权重和)。只用 role!=context 的事件。"""
    by_family: dict[str, float] = {}
    for h in hits:
        if h.get("role") == "context":
            continue
        fam = _EVENT_FAMILY.get(str(h.get("k") or ""))
        if not fam:
            continue
        by_family[fam] = max(by_family.get(fam, 0.0), float(h.get("w") or 0.0))
    total = sum(by_family.values())
    # logistic：Σw=2.3 → 0.50；3.4 → 0.73；4.5 → 0.88。既不会全部顶格，也不会挤在一档。
    raw = 1.0 / (1.0 + math.exp(-(total - CONFIG["score_mid"]) / CONFIG["score_slope"]))
    score = round(min(0.99, max(0.05, raw)), 3)
    return score, len(by_family), round(total, 3)


def grade_of(score: float) -> str:
    """分数 → 等级。A 可直接执行/展示，B 需其他证据确认，C 仅观察。"""
    if score >= 0.78:
        return "A"
    if score >= 0.58:
        return "B"
    return "C"

def _ema_series(vals: list[float], period: int) -> list[float | None]:
    """EMA, 只用 [0..i]。不足 period 时返回 None。"""
    out: list[float | None] = [None] * len(vals)
    if len(vals) < period:
        return out
    k = 2.0 / (period + 1.0)
    seed = sum(vals[:period]) / period
    out[period - 1] = seed
    prev = seed
    for i in range(period, len(vals)):
        prev = vals[i] * k + prev * (1 - k)
        out[i] = prev
    return out


def _rsi_series(closes: list[float], period: int) -> list[float | None]:
    """Wilder RSI, 只用 [0..i]。"""
    n = len(closes)
    out: list[float | None] = [None] * n
    if n <= period:
        return out
    gains, losses = [], []
    for i in range(1, period + 1):
        ch = closes[i] - closes[i - 1]
        gains.append(max(ch, 0.0))
        losses.append(max(-ch, 0.0))
    ag = sum(gains) / period
    al = sum(losses) / period
    out[period] = 100.0 if al == 0 else 100.0 - 100.0 / (1.0 + ag / al)
    for i in range(period + 1, n):
        ch = closes[i] - closes[i - 1]
        ag = (ag * (period - 1) + max(ch, 0.0)) / period
        al = (al * (period - 1) + max(-ch, 0.0)) / period
        out[i] = 100.0 if al == 0 else 100.0 - 100.0 / (1.0 + ag / al)
    return out


def _atr_series(rows: list[dict], period: int) -> list[float | None]:
    """Wilder ATR, 只用 [0..i]。"""
    n = len(rows)
    out: list[float | None] = [None] * n
    if n <= period:
        return out
    trs: list[float] = []
    for i in range(n):
        h, l = rows[i]["high"], rows[i]["low"]
        if i == 0:
            trs.append(h - l)
        else:
            pc = rows[i - 1]["close"]
            trs.append(max(h - l, abs(h - pc), abs(l - pc)))
    seed = sum(trs[1:period + 1]) / period
    out[period] = seed
    prev = seed
    for i in range(period + 1, n):
        prev = (prev * (period - 1) + trs[i]) / period
        out[i] = prev
    return out


def _macd_series(closes: list[float], fast: int, slow: int, sig: int):
    """MACD DIF/DEA/HIST, 只用 [0..i]。"""
    ef, es = _ema_series(closes, fast), _ema_series(closes, slow)
    dif: list[float | None] = [None] * len(closes)
    for i in range(len(closes)):
        if ef[i] is not None and es[i] is not None:
            dif[i] = ef[i] - es[i]
    valid = [(i, v) for i, v in enumerate(dif) if v is not None]
    dea: list[float | None] = [None] * len(closes)
    hist: list[float | None] = [None] * len(closes)
    if len(valid) >= sig:
        start = valid[sig - 1][0]
        vals = [v for _, v in valid]
        dea_v = sum(vals[:sig]) / sig
        dea[start] = dea_v
        k = 2.0 / (sig + 1.0)
        for i, v in valid[sig:]:
            dea_v = v * k + dea_v * (1 - k)
            dea[i] = dea_v
        for i, v in enumerate(dif):
            if v is not None and dea[i] is not None:
                hist[i] = v - dea[i]
    return dif, dea, hist
def _confirmed_pivots(rows: list[dict], window: int) -> tuple[dict[int, str], dict[int, bool]]:
    """因果 pivot: 返回 (idx -> 'high'|'low', idx -> confirmed)。

    idx 仅在 right window 存在时才有资格成为 pivot; `confirmed[i]`
    标记"该 pivot 的右窗口是否已具备", 调用方据此处理 pivot_window 根延迟。
    """
    kinds: dict[int, str] = {}
    confirmed: dict[int, bool] = {}
    last = len(rows) - 1
    for i in range(len(rows)):
        lo, hi = i - window, i + window
        if lo < 0 or hi > last:
            continue
        h, l = rows[i]["high"], rows[i]["low"]
        is_high = all(rows[j]["high"] <= h for j in range(lo, hi + 1) if j != i)
        is_low = all(rows[j]["low"] >= l for j in range(lo, hi + 1) if j != i)
        if is_high:
            kinds[i] = "high"
            confirmed[i] = True
        elif is_low:
            kinds[i] = "low"
            confirmed[i] = True
    return kinds, confirmed


def indicators(rows: list[dict]) -> dict[str, Any]:
    """预计算全部指标序列。索引 i 的值只依赖 [0..i]。"""
    closes = [r["close"] for r in rows]
    vols = [float(r["volume"] or 0.0) for r in rows]
    n = len(rows)
    W = CONFIG

    ema_f = _ema_series(closes, W["ema_fast"])
    ema_s = _ema_series(closes, W["ema_slow"])
    dif, dea, hist = _macd_series(closes, W["ema_fast"], W["ema_slow"], 9)
    rsi = _rsi_series(closes, W["rsi_period"])
    atr = _atr_series(rows, W["atr_period"])

    # 量比: vol[i] / mean(vol[i-vw+1..i]) —— 分母含 i, 仍 causal
    vw = W["vol_window"]
    vol_ratio: list[float | None] = [None] * n
    for i in range(n):
        if i >= vw - 1:
            base = sum(vols[i - vw + 1:i + 1]) / vw
            vol_ratio[i] = (vols[i] / base) if base > 0 else None

    # ATR 扩张比: atr[i] / mean(atr[i-vw+1..i])
    atr_expand: list[float | None] = [None] * n
    for i in range(n):
        if i >= vw:
            seg = atr[i - vw + 1:i + 1]
            if all(x is not None for x in seg) and seg:
                m = sum(seg) / vw
                atr_expand[i] = (atr[i] / m) if m > 0 else None

    pivots, pivot_conf = _confirmed_pivots(rows, W["pivot_window"])

    # 趋势类证据序列（供决策链趋势同向/逆势打分与台账回灌）
    candles = [[r["ts"], r["open"], r["high"], r["low"], r["close"],
               float(r["volume"] or 0.0)] for r in rows]
    adx_full = ti.adx(candles, 14)
    st_full = ti.supertrend(candles, 10, 3.0)
    swing_full = ti.swing_structure(candles)
    aroon_full = ti.aroon(candles, 25)
    kdj_full = ti.kdj(closes, [r["high"] for r in rows], [r["low"] for r in rows], 9, 3, 3)
    stoch_full = ti.stoch(candles, 14, 3)
    bb_full = ti.bollinger(closes, 20, 2.0)
    stack_full = ti.ma_stack(closes)
    ichi_full = ti.ichimoku(candles)

    return {
        "ema_fast": ema_f, "ema_slow": ema_s,
        "dif": dif, "dea": dea, "hist": hist,
        "rsi": rsi, "atr": atr,
        "vol_ratio": vol_ratio, "atr_expand": atr_expand,
        "pivots": pivots, "pivot_confirmed": pivot_conf,
        "adx": adx_full["adx"], "plus_di": adx_full["plus_di"], "minus_di": adx_full["minus_di"],
        "st_dir": st_full["direction"],
        "swing": swing_full.get("trend"),
        "aroon_up": aroon_full["up"], "aroon_down": aroon_full["down"],
        "kdj_j": kdj_full["j"], "kdj_k": kdj_full["k"],
        "stoch": stoch_full,
        "bb_upper": bb_full["upper"], "bb_mid": bb_full["mid"], "bb_lower": bb_full["lower"],
        "ma_bull": stack_full.get("bull_aligned"), "ma_bear": stack_full.get("bear_aligned"),
        "ichi_a": ichi_full["senkou_a"], "ichi_b": ichi_full["senkou_b"],
    }


def _find_divergence(rows, ind, i, kind: str) -> dict | None:
    """在 [i-lookback, i] 内找两个 pivot 判断背离。仅用 <= i 的数据。"""
    W = CONFIG
    piv, conf = ind["pivots"], ind["pivot_confirmed"]
    rsi = ind["rsi"]
    lo = max(0, i - W["divergence_lookback"])
    idxs = [j for j in range(lo, i + 1)
            if piv.get(j) == kind and conf.get(j) and rsi[j] is not None]
    if len(idxs) < 2:
        return None
    a, b = idxs[-2], idxs[-1]
    if b - a < W["min_pivot_gap"]:
        return None
    # 因果护栏（v450）：枢轴 b 要等右侧 pivot_window 根走完才算成立，
    # 在 i < b+W 时用它就是拿未来数据判现在。只在确认后的第一根产一次，
    # 避免同一个背离在之后几十根里反复重发。
    pw = W["pivot_window"]
    if i < b + pw or i > b + pw + 1:
        return None
    if kind == "high":
        pa, pb, ra, rb = rows[a]["high"], rows[b]["high"], rsi[a], rsi[b]
        if pb > pa and rb < ra:   # 顶背离: 价格创新高, RSI 未创新高
            return {"type": "top", "a": a, "b": b, "pivot_a": pa, "pivot_b": pb,
                    "rsi_a": round(ra, 2), "rsi_b": round(rb, 2)}
    else:
        pa, pb, ra, rb = rows[a]["low"], rows[b]["low"], rsi[a], rsi[b]
        if pb < pa and rb > ra:   # 底背离: 价格创新低, RSI 未创新低
            return {"type": "bottom", "a": a, "b": b, "pivot_a": pa, "pivot_b": pb,
                    "rsi_a": round(ra, 2), "rsi_b": round(rb, 2)}
    return None


def _support_levels(rows, ind, i) -> dict | None:
    """支撑位: 最近一个已确认的 pivot low, 且当前 close 距其 <=3%。

    v450 因果护栏: pivot 未走完右侧 pivot_window 根时不可用；
    并且只在"刚刚贴近"的那一根返回（上一根还在 3% 之外），
    否则同一根支撑会被连续几十根重复标记成 support_test。
    """
    piv, conf = ind["pivots"], ind["pivot_confirmed"]
    pw = CONFIG["pivot_window"]
    for j in range(i, max(-1, i - CONFIG["divergence_lookback"]), -1):
        if piv.get(j) == "low" and conf.get(j):
            if j + pw > i:
                continue
            lvl = rows[j]["low"]
            c = rows[i]["close"]
            if lvl > 0 and abs(c - lvl) / lvl <= 0.03:
                prev_c = rows[i - 1]["close"] if i >= 1 else None
                if prev_c is not None and abs(prev_c - lvl) / lvl <= 0.03:
                    return None          # 上一根已在区间内 → 不是新触碰
                return {"level": round(lvl, 4), "pivot_idx": j,
                        "dist_pct": round((c - lvl) / lvl * 100, 2)}
    return None
def _classify(hits: list[dict]) -> str:
    ks = {h["k"] for h in hits}
    if any(k.startswith("div_") for k in ks):
        return "divergence"
    if "reversal_top" in ks or "reversal_bottom" in ks:
        return "reversal"
    if "liquidation" in ks:
        return "liquidation"
    if "vol_extreme" in ks or "vol_spike" in ks:
        return "volume"
    if "support_test" in ks:
        return "support"
    if "atr_expand" in ks:
        return "volatility"
    if ks & {"pct_1d", "pct_3d"}:
        return "pct"
    return "combo"


def _match_liq(ts: int, liq_by_ts: dict) -> dict | None:
    """按 12h 窗口匹配强平热度。"""
    for key, val in liq_by_ts.items():
        if abs(int(key) - int(ts)) <= 12 * 3600:
            return val
    return None


def detect_signals(rows: list[dict], liq_by_ts: dict | None = None) -> list[dict]:
    """主检测入口。rows 必须**升序** OHLCV。

    组合门槛: >=2 类"强信号"(weight>=0.8) 才产出, 单指标不触发。
    liq_by_ts: 可选 {ts_epoch: payload}, 参与打分。
    """
    if len(rows) < CONFIG["ma_support"] + 5:
        return []
    ind = indicators(rows)
    W = CONFIG
    liq_by_ts = liq_by_ts or {}
    out: list[dict] = []

    for i in range(len(rows)):
        if i < W["ma_support"]:
            continue
        c, pc = rows[i]["close"], rows[i - 1]["close"]
        if c <= 0 or pc <= 0:
            continue
        hits: list[dict] = []

        # 1) 涨跌幅（事件）
        pct_1d = (c - pc) / pc * 100.0
        p3 = rows[i - 3]["close"]
        pct_3d = (c - p3) / p3 * 100.0 if p3 > 0 else 0.0
        if abs(pct_1d) >= W["pct_move_1d"]:
            hits.append({"k": "pct_1d", "v": round(pct_1d, 2), "w": 1.0, "role": "event"})
        if abs(pct_3d) >= W["pct_move_3d"]:
            hits.append({"k": "pct_3d", "v": round(pct_3d, 2), "w": 1.0, "role": "event"})

        # 2) 量能激增（事件）
        vr = ind["vol_ratio"][i]
        if vr is not None and vr >= W["vol_extreme_ratio"]:
            hits.append({"k": "vol_extreme", "v": round(vr, 2), "w": 1.2, "role": "event"})
        elif vr is not None and vr >= W["vol_spike_ratio"]:
            hits.append({"k": "vol_spike", "v": round(vr, 2), "w": 0.9, "role": "event"})

        # 3) 背离 (顶/底)：只在枢轴确认后的第一根产出，且枢轴必须已确认
        div = _find_divergence(rows, ind, i, "high") or _find_divergence(rows, ind, i, "low")
        if div:
            hits.append({"k": f"div_{div['type']}", "v": div, "w": 1.5, "role": "event"})

        # 4) 波动放大（事件）
        ae = ind["atr_expand"][i]
        if ae is not None and ae >= W["atr_expand_ratio"]:
            hits.append({"k": "atr_expand", "v": round(ae, 2), "w": 1.1, "role": "event"})

        # 5) RSI 极值：只在"进入极值区"的那一根记事件（v450）
        #    极值驻留是状态，不是信号；逐根重发会把同一段超卖刷成几十条。
        r = ind["rsi"][i]
        r_prev = ind["rsi"][i - 1] if i >= 1 else None
        if r is not None:
            if r >= W["rsi_overbought"] and (r_prev is None or r_prev < W["rsi_overbought"]):
                hits.append({"k": "rsi_overbought", "v": round(r, 1), "w": 0.8, "role": "event"})
            elif r <= W["rsi_oversold"] and (r_prev is None or r_prev > W["rsi_oversold"]):
                hits.append({"k": "rsi_oversold", "v": round(r, 1), "w": 0.8, "role": "event"})

        # 6) 均线穿越：台账复核为噪声/反向 → 只作背景证据，不参与打分也不单独触发。
        ef, es = ind["ema_fast"][i], ind["ema_slow"][i]
        pef, pes = ind["ema_fast"][i - 1], ind["ema_slow"][i - 1]
        if None not in (ef, es, pef, pes):
            if pef <= pes and ef > es and pct_1d > 0:
                hits.append({"k": "ema_golden", "v": round(ef, 4), "w": 0.8, "role": "context"})
            elif pef >= pes and ef < es and pct_1d < 0:
                hits.append({"k": "ema_death", "v": round(ef, 4), "w": 0.8, "role": "context"})

        # 7) MACD 柱反转：同样只作背景证据（台账：bear_cross 反向、bull_cross 噪声）。
        h0, h1 = ind["hist"][i], ind["hist"][i - 1]
        if h0 is not None and h1 is not None:
            if h1 > 0 >= h0:
                hits.append({"k": "macd_bear_cross", "v": round(h0, 5), "w": 0.9, "role": "context"})
            elif h1 < 0 <= h0:
                hits.append({"k": "macd_bull_cross", "v": round(h0, 5), "w": 0.9, "role": "context"})

        # 8) 支撑位测试：只在"刚贴近"的那一根（_support_levels 内部已做因果护栏）
        sup = _support_levels(rows, ind, i)
        if sup:
            hits.append({"k": "support_test", "v": sup, "w": 0.8, "role": "event"})

        # 9) 合约爆单热力 (外部数据)
        ts = rows[i]["ts"]
        lk = _match_liq(ts, liq_by_ts)
        if lk:
            hits.append({"k": "liquidation", "v": lk, "w": 1.3, "role": "event"})

        # 10) 趋势结构证据：只作背景（context），不进分数、不单独触发。
        adx_v = ind["adx"][i] if i < len(ind["adx"]) else None
        pdi = ind["plus_di"][i] if i < len(ind["plus_di"]) else None
        ndi = ind["minus_di"][i] if i < len(ind["minus_di"]) else None
        if adx_v is not None and pdi is not None and ndi is not None and adx_v >= 20.0:
            if pdi > ndi and adx_v >= 30.0:
                hits.append({"k": "adx_strong_bull", "v": round(adx_v, 1), "w": 0.7, "role": "context"})
            elif ndi > pdi and adx_v >= 30.0:
                hits.append({"k": "adx_strong_bear", "v": round(adx_v, 1), "w": 0.7, "role": "context"})
        st_dir = ind["st_dir"][i] if i < len(ind["st_dir"]) else None
        if st_dir == 1:
            hits.append({"k": "supertrend_bull", "v": round(rows[i]["close"], 4), "w": 0.6, "role": "context"})
        elif st_dir == -1:
            hits.append({"k": "supertrend_bear", "v": round(rows[i]["close"], 4), "w": 0.6, "role": "context"})
        if ind["swing"] == "up":
            hits.append({"k": "swing_bull", "v": "HH_HL", "w": 0.6, "role": "context"})
        elif ind["swing"] == "down":
            hits.append({"k": "swing_bear", "v": "LH_LL", "w": 0.6, "role": "context"})
        a_up = ind["aroon_up"][i] if i < len(ind["aroon_up"]) else None
        a_down = ind["aroon_down"][i] if i < len(ind["aroon_down"]) else None
        if a_up is not None and a_down is not None and a_up >= 70.0 and a_down <= 30.0:
            hits.append({"k": "aroon_bull", "v": {"up": a_up, "down": a_down}, "w": 0.6, "role": "context"})
        elif a_up is not None and a_down is not None and a_down >= 70.0 and a_up <= 30.0:
            hits.append({"k": "aroon_bear", "v": {"up": a_up, "down": a_down}, "w": 0.6, "role": "context"})

        # KDJ / Stoch 极值：进入区间的那一根才算事件
        kj = ind["kdj_j"][i] if i < len(ind["kdj_j"]) else None
        kk = ind["kdj_k"][i] if i < len(ind["kdj_k"]) else None
        kj_p = ind["kdj_j"][i - 1] if i >= 1 else None
        kk_p = ind["kdj_k"][i - 1] if i >= 1 else None
        in_os = (kj is not None and kj <= 20.0) or (kk is not None and kk <= 20.0)
        prev_os = (kj_p is not None and kj_p <= 20.0) or (kk_p is not None and kk_p <= 20.0)
        in_ob = (kj is not None and kj >= 80.0) or (kk is not None and kk >= 80.0)
        prev_ob = (kj_p is not None and kj_p >= 80.0) or (kk_p is not None and kk_p >= 80.0)
        if in_os and not prev_os:
            hits.append({"k": "kdj_oversold", "v": {"j": kj, "k": kk}, "w": 0.6, "role": "event"})
        elif in_ob and not prev_ob:
            hits.append({"k": "kdj_overbought", "v": {"j": kj, "k": kk}, "w": 0.6, "role": "event"})

        sv = ind["stoch"][i] if i < len(ind["stoch"]) else None
        sv_p = ind["stoch"][i - 1] if i >= 1 else None
        if sv is not None and sv <= 20.0 and (sv_p is None or sv_p > 20.0):
            hits.append({"k": "stoch_oversold", "v": {"stoch": sv}, "w": 0.6, "role": "event"})
        elif sv is not None and sv >= 80.0 and (sv_p is None or sv_p < 80.0):
            hits.append({"k": "stoch_overbought", "v": {"stoch": sv}, "w": 0.6, "role": "event"})

        # 布林带外收盘：只在"从带内跑到带外"的那一根
        bu = ind["bb_upper"][i] if i < len(ind["bb_upper"]) else None
        bl = ind["bb_lower"][i] if i < len(ind["bb_lower"]) else None
        cur_close = rows[i]["close"]
        prev_close = rows[i - 1]["close"]
        bu_p = ind["bb_upper"][i - 1] if i >= 1 and i - 1 < len(ind["bb_upper"]) else None
        bl_p = ind["bb_lower"][i - 1] if i >= 1 and i - 1 < len(ind["bb_lower"]) else None
        if bl is not None and cur_close < bl and (bl_p is None or prev_close >= bl_p):
            hits.append({"k": "bb_lower_break", "v": {"close": cur_close}, "w": 0.7, "role": "event"})
        elif bu is not None and cur_close > bu and (bu_p is None or prev_close <= bu_p):
            hits.append({"k": "bb_upper_break", "v": {"close": cur_close}, "w": 0.7, "role": "event"})

        if ind["ma_bull"]:
            hits.append({"k": "ma_bull_aligned", "v": "5>10>20>60", "w": 0.5, "role": "context"})
        elif ind["ma_bear"]:
            hits.append({"k": "ma_bear_aligned", "v": "5<10<20<60", "w": 0.5, "role": "context"})

        # 放量上/下破：逐点判定该根放量且突破近 20 根收盘极值
        if i >= 21:
            wv = [rows[j]["volume"] for j in range(i - 20, i)]
            cur_v = rows[i]["volume"] or 0
            wm = sum(wv) / len(wv)
            if wm > 0:
                wvar = sum((x - wm) ** 2 for x in wv) / len(wv)
                wsd = wvar ** 0.5
                if wsd > 0 and (cur_v - wm) / wsd >= 2.0:
                    wh = max(rows[j]["close"] for j in range(i - 20, i))
                    wl = min(rows[j]["close"] for j in range(i - 20, i))
                    if cur_close > wh:
                        hits.append({"k": "vol_bull_break", "v": {"close": cur_close}, "w": 0.6, "role": "event"})
                    elif cur_close < wl:
                        hits.append({"k": "vol_bear_break", "v": {"close": cur_close}, "w": 0.6, "role": "event"})

        sa = ind["ichi_a"][i] if i < len(ind["ichi_a"]) else None
        sb = ind["ichi_b"][i] if i < len(ind["ichi_b"]) else None
        if sa is not None and sb is not None:
            if cur_close > max(sa, sb):
                hits.append({"k": "ichimoku_bull", "v": {"close": cur_close}, "w": 0.5, "role": "context"})
            elif cur_close < min(sa, sb):
                hits.append({"k": "ichimoku_bear", "v": {"close": cur_close}, "w": 0.5, "role": "context"})

        if not hits:
            continue
        # v450: 至少 2 个独立证据族才出信号；状态型证据不再凑数。
        score, family_n, _total_w = _score_from_hits(hits)
        if family_n < W["min_event_families"]:
            continue

        out.append({
            "idx": i, "ts": ts, "close": c,
            "pct_1d": round(pct_1d, 3), "pct_3d": round(pct_3d, 3),
            "vol_ratio": round(vr, 2) if vr is not None else None,
            "score": score, "grade": grade_of(score),
            "trigger_type": _classify(hits),
            "signals": hits, "uses_future": False,
        })

    # 因果转折点：枢轴确认后 + 反向幅度达标的第一根，绝不看未来。
    out.extend(_detect_reversals(rows, ind))
    if ALLOW_FUTURE:
        # 仅审计用（默认关闭）。线上禁止产出 uses_future=1 的信号。
        out.extend(_detect_reversals_lookahead(rows, ind))
    return _merge_same_ts(out)


def _merge_same_ts(out: list[dict]) -> list[dict]:
    """合并同 ts 的信号。

    转折点与因果信号可能落在同一根 bar 上(例如"支撑位破位 + 顶背离")
    两者都有效, 必须合并而不是互相覆盖 —— 保留更高 score, 并把
    另一方的 hits 并入 signals, 同时用副信号记录被合并的类型。
    合并后按证据族重算分数（v450），不再用"条数×0.1"这种拍脑袋加成。
    """
    by_ts: dict[int, dict] = {}
    order: list[int] = []
    for s in out:
        t = s["ts"]
        cur = by_ts.get(t)
        if cur is None:
            by_ts[t] = s
            order.append(t)
            continue
        # 合并 hits(按 k 去重, 保留权重更高者)
        seen = {h["k"]: h for h in cur["signals"]}
        for h in s["signals"]:
            if h["k"] not in seen or h["w"] > seen[h["k"]]["w"]:
                seen[h["k"]] = h
        cur["signals"] = list(seen.values())
        score, _fam_n, _total = _score_from_hits(cur["signals"])
        cur["score"] = score
        cur["grade"] = grade_of(score)
        # 记录被合并掉的类型
        merged = cur.setdefault("merged_types", [])
        if s["trigger_type"] != cur["trigger_type"]:
            merged.append(s["trigger_type"])
        # 未来函数标记: 任一来源用到即置位(保守, 不掩盖)
        cur["uses_future"] = bool(cur["uses_future"] or s["uses_future"])
        # 转折点主类型优先保留, 便于前端突出显示
        if s["trigger_type"] == "reversal" and cur["trigger_type"] != "reversal":
            merged.append(cur["trigger_type"])
            cur["trigger_type"] = "reversal"
        if cur.get("vol_ratio") is None:
            cur["vol_ratio"] = s.get("vol_ratio")
        if not cur.get("grade"):
            cur["grade"] = grade_of(float(cur.get("score") or 0.0))
    out = [by_ts[t] for t in order]
    out.sort(key=lambda x: x["ts"])
    return out


def _detect_reversals(rows: list[dict], ind) -> list[dict]:
    """因果转折点（v450）。

    旧实现用未来 3 根 K 线确认反转，再把信号 ts 回填到枢轴那根 —— 等于在
    i 时刻就已经知道未来，回测和影子账本都会穿透（台账里 9671 条因此被剔除）。
    现在改为纯因果：
      1. 枢轴 i 要等右侧 pivot_window 根走完才成立（i+pw 才"看得见"）；
      2. 从 i+pw 起最多再等 2*pw 根，找第一根相对枢轴反向 ≥ 阈值的 bar k；
      3. 信号落在 k（真正可执行的那根），记录 pivot_ts / lag_bars；
      4. 全程只用 [0..k]，uses_future=False。
    """
    W = CONFIG
    pw = int(W["reversal_pivot_window"])
    look = int(W["reversal_lookback"])
    pivots, confirmed = _confirmed_pivots(rows, pw)
    n = len(rows)
    out: list[dict] = []
    for i in range(n):
        kind = pivots.get(i)
        if kind not in ("high", "low") or not confirmed.get(i):
            continue
        # 枢轴必须是近 look 根的极值：否则每个小摆动都会被当成"转折点"。
        lo_idx = max(0, i - look)
        if kind == "high":
            if rows[i]["high"] < max(rows[j]["high"] for j in range(lo_idx, i + 1)):
                continue
        else:
            if rows[i]["low"] > min(rows[j]["low"] for j in range(lo_idx, i + 1)):
                continue
        start = i + pw
        end = min(i + 2 * pw, n - 1)
        for k in range(start, end + 1):
            c = rows[k]["close"]
            if c <= 0:
                continue
            atr_v = ind["atr"][k]
            atr_pct = (atr_v / c * 100.0) if atr_v else 0.0
            # 低波动品种不能靠固定 2% 触发；阈值随 ATR 抬升。
            thr = max(W["reversal_min_pct"], W["reversal_atr_mult"] * atr_pct)
            if kind == "high":
                base = rows[i]["high"]
                move = (c - base) / base * 100.0 if base > 0 else 0.0
                if move > -thr:
                    continue
                key = "reversal_top"
            else:
                base = rows[i]["low"]
                move = (c - base) / base * 100.0 if base > 0 else 0.0
                if move < thr:
                    continue
                key = "reversal_bottom"
            hits = [{"k": key,
                     "v": {"pivot_ts": rows[i]["ts"], "pivot_price": round(base, 4),
                           "move_pct": round(move, 2), "lag_bars": k - i},
                     "w": 2.0, "role": "event"}]
            vr = ind["vol_ratio"][k]
            if vr is not None and vr >= W["vol_spike_ratio"]:
                hits.append({"k": "vol_spike", "v": round(vr, 2), "w": 0.9, "role": "event"})
            r = ind["rsi"][k]
            if r is not None and (r >= 70 or r <= 30):
                hits.append({"k": "rsi_extreme", "v": round(r, 1), "w": 0.6, "role": "event"})
            score, _fam, _tot = _score_from_hits(hits)
            pc = rows[k - 1]["close"] if k >= 1 else 0.0
            out.append({
                "idx": k, "ts": rows[k]["ts"], "close": c,
                "pct_1d": round((c - pc) / pc * 100, 3) if pc > 0 else 0.0,
                "vol_ratio": round(vr, 2) if vr is not None else None,
                "score": score, "grade": grade_of(score),
                "trigger_type": "reversal",
                "signals": hits,
                "uses_future": False,
            })
            break          # 每个枢轴只产一条，避免整段下跌反复重发
    return out


def _detect_reversals_lookahead(rows: list[dict], ind) -> list[dict]:
    """⚠ 仅审计用的旧版转折点：使用未来 reversal_lookahead 根确认。

    线上禁止调用（CONFIG['enable_reversal_future'] 默认 False）。
    保留它是为了历史对照；产出的行一律 uses_future=True，不参与命中率统计。
    """
    W = CONFIG
    la = W["reversal_lookahead"]
    n = len(rows)
    out: list[dict] = []
    for i in range(n - la):
        c = rows[i]["close"]
        if c <= 0:
            continue
        fut = rows[i + 1:i + 1 + la]
        if not fut:
            continue
        up = rows[i]["high"] >= max(x["high"] for x in fut)
        down = rows[i]["low"] <= min(x["low"] for x in fut)
        if not (up or down):
            continue
        fut_max = max(x["high"] for x in fut)
        fut_min = min(x["low"] for x in fut)
        rev_pct = ((fut_min - c) / c * 100) if up else ((fut_max - c) / c * 100)
        if abs(rev_pct) < W["reversal_min_pct"]:
            continue
        hits = [{"k": "reversal_top" if up else "reversal_bottom",
                 "v": round(rev_pct, 2), "w": 2.0, "role": "event"}]
        vr = ind["vol_ratio"][i]
        if vr is not None and vr >= W["vol_spike_ratio"]:
            hits.append({"k": "vol_spike", "v": round(vr, 2), "w": 0.9, "role": "event"})
        r = ind["rsi"][i]
        if r is not None and (r >= 70 or r <= 30):
            hits.append({"k": "rsi_extreme", "v": round(r, 1), "w": 0.6, "role": "event"})
        score, _fam, _tot = _score_from_hits(hits)
        pc = rows[i - 1]["close"] if i >= 1 else 0.0
        out.append({
            "idx": i, "ts": rows[i]["ts"], "close": c,
            "pct_1d": round((c - pc) / pc * 100, 3) if pc > 0 else 0.0,
            "vol_ratio": round(vr, 2) if vr is not None else None,
            "score": score, "grade": grade_of(score),
            "trigger_type": "reversal",
            "signals": hits,
            "uses_future": True,
        })
    return out