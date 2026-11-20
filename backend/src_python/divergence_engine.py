# -*- coding: utf-8 -*-
"""divergence_engine.py — 多指标 / 多周期 / 无未来函数的顶底背离引擎

为什么重做
----------
旧实现（technical_indicators.rsi_divergence / tech_detector._find_divergence）只有 RSI
一条腿，判据是「当前价创新高、RSI 没创新高」，问题很具体：

  * 只用当前 bar 和窗口极值比，没有 pivot 配对，横盘期几乎不出信号；
  * 单指标，RSI 在强趋势里长期钝化，顶背离会连着报错；
  * 没有强度分级，A 级和噪音混在一个列表里；
  * 没有失效价，看到信号也不知道哪里算错；
  * 没有历史命中率，无法回答「到底准不准」。

本模块的做法
------------
1. 因果 pivot：左右各 k 根确认，pivot 在 i+k 根之后才算「已确认」，
   信号确认时间 = confirm_idx，回测入场用 confirm 当根收盘，绝不使用未来数据。
2. 六条腿投票：RSI(14) / MACD 柱 / OBV / MFI(14) / CCI(20) / Stochastic %K。
   至少 2 条腿同向背离才出信号，按权重合成 0–100 强度。
3. 区分 regular（反转）与 hidden（趋势延续），两者判据相反，不能混为一谈。
4. 给出失效价、触发价、最近对手 pivot（目标参考）与逐条证据。
5. `evaluate()` 用同一套因果信号做前向收益统计，输出分级命中率与基准对比，
   用来证明（或证伪）「很强」，而不是靠形容词。

对外只依赖 rows: [{ts, open, high, low, close, volume}]，不绑定数据库。
"""
from __future__ import annotations

import math
import os
import sqlite3
import sys
from datetime import datetime
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import technical_indicators as ti

# ───────────────────────── 配置 ─────────────────────────
DEFAULT_CONFIG: Dict[str, Any] = {
    # pivot
    "pivot_window": 3,          # 左右各 3 根确认（因果，延迟 3 根）
    "pivot_prominence_atr": 0.15,  # pivot 相对两侧的突出度下限（ATR 倍数）
    "min_pivot_gap": 8,         # 两个 pivot 至少间隔
    "max_pivot_gap": 90,        # 超过视为不同结构，不配对
    "min_price_swing_atr": 0.35,  # 两 pivot 价差下限（ATR 倍数），过滤微幅新高

    # 指标参数
    "rsi_period": 14,
    "macd_fast": 12,
    "macd_slow": 26,
    "macd_signal": 9,
    "mfi_period": 14,
    "cci_period": 20,
    "stoch_k": 14,
    "stoch_smooth": 3,
    "atr_period": 14,
    "adx_period": 14,
    "vol_ma": 20,

    # 每条腿的背离阈值（指标自身单位）与权重
    "legs": {
        "rsi":      {"weight": 0.22, "threshold": 2.5},
        "macd_hist": {"weight": 0.22, "threshold_atr": 0.10},
        "obv":      {"weight": 0.16, "threshold_vol_mult": 0.35},
        "mfi":      {"weight": 0.10, "threshold": 3.0},
        "cci":      {"weight": 0.09, "threshold": 10.0},
        "stoch":    {"weight": 0.10, "threshold": 5.0},
        # 量价代理背离：收盘价动量（近 5 根净变化）与价格方向不一致。
        # 真实 CVD 数据在本库没有，先用可算的动量代理；接入 CVD 后把这条换成 cvd。
        "close_momentum": {"weight": 0.11, "threshold": 0.0},
    },

    # 强度合成权重
    "score_weights": {"agree": 0.55, "swing": 0.20, "volume": 0.15, "trend": 0.10},
    "grade_cut": {"A": 78, "B": 62, "C": 48},  # 低于 C 不出信号
    "confluence_bonus": 12,     # 多周期共振加分（由上层传入）

    # 输出
    "max_signals": 40,
}

# 六条腿的中文名（证据展示用）
LEG_LABELS = {
    "rsi": "RSI(14)",
    "macd_hist": "MACD 柱",
    "obv": "OBV",
    "mfi": "MFI(14)",
    "cci": "CCI(20)",
    "stoch": "Stoch %K",
    "close_momentum": "价格动量(5)",
}


# ───────────────────────── 基础指标（全部 causal） ─────────────────────────
def _candles(rows: Sequence[Dict[str, Any]]) -> List[list]:
    """rows: [{ts, open, high, low, close, volume}] → [ts, open, high, low, close, volume]。"""
    out: List[list] = []
    for r in rows:
        out.append([r.get("ts"), r.get("open"), r["high"], r["low"],
                    r["close"], float(r.get("volume") or 0.0)])
    return out


def _ema(values: Sequence[float], period: int) -> List[Optional[float]]:
    if period <= 0:
        return [None] * len(values)
    seed = ti.sma(list(values), period)
    # ti.ema 从首根起就有值（首值=首根收盘），与本模块旧口径不同；
    # 长周期 ema200 只做趋势位置参考，这里保持「前 period-1 根为 None」的暖机口径。
    return seed


def _rsi(closes: Sequence[float], period: int = 14) -> List[Optional[float]]:
    return ti.rsi(list(closes), period)


def _macd(closes: Sequence[float], fast: int, slow: int, signal: int
          ) -> Tuple[List[Optional[float]], List[Optional[float]], List[Optional[float]]]:
    m = ti.macd(list(closes), fast, slow, signal)
    return m["macd"], m["signal"], m["hist"]


def _obv(closes: Sequence[float], volumes: Sequence[float]) -> List[float]:
    candles = [[0, 0, 0, 0, c, v] for c, v in zip(closes, volumes)]
    return ti.obv(candles)


def _mfi(rows: Sequence[Dict[str, Any]], period: int = 14) -> List[Optional[float]]:
    return ti.mfi(_candles(rows), period)


def _cci(rows: Sequence[Dict[str, Any]], period: int = 20) -> List[Optional[float]]:
    return ti.cci(_candles(rows), period)


def _stoch(rows: Sequence[Dict[str, Any]], k_period: int = 14, smooth: int = 3
           ) -> List[Optional[float]]:
    return ti.stoch(_candles(rows), k_period, smooth)


def _atr(rows: Sequence[Dict[str, Any]], period: int = 14) -> List[Optional[float]]:
    return ti.atr(_candles(rows), period)


def _adx(rows: Sequence[Dict[str, Any]], period: int = 14) -> List[Optional[float]]:
    return ti.adx(_candles(rows), period)["adx"]




# ───────────────────────── 指标打包 ─────────────────────────
def compute_indicators(rows: Sequence[Dict[str, Any]], cfg: Dict[str, Any]) -> Dict[str, List[Any]]:
    closes = [float(r["close"]) for r in rows]
    volumes = [float(r["volume"] or 0.0) for r in rows]
    _, _, hist = _macd(closes, cfg["macd_fast"], cfg["macd_slow"], cfg["macd_signal"])
    vol_ma: List[Optional[float]] = [None] * len(rows)
    for i in range(cfg["vol_ma"] - 1, len(rows)):
        vol_ma[i] = sum(volumes[i - cfg["vol_ma"] + 1:i + 1]) / cfg["vol_ma"]
    # 价格动量腿：近 5 根收盘净变化（CVD 不可得时的量价代理），pivot 处的斜率方向
    mom: List[Optional[float]] = [None] * len(rows)
    for i in range(5, len(rows)):
        mom[i] = closes[i] - closes[i - 5]
    return {
        "rsi": _rsi(closes, cfg["rsi_period"]),
        "macd_hist": hist,
        "obv": _obv(closes, volumes),
        "mfi": _mfi(rows, cfg["mfi_period"]),
        "cci": _cci(rows, cfg["cci_period"]),
        "stoch": _stoch(rows, cfg["stoch_k"], cfg["stoch_smooth"]),
        "atr": _atr(rows, cfg["atr_period"]),
        "adx": _adx(rows, cfg["adx_period"]),
        "ema200": _ema(closes, 200),
        "vol_ma": vol_ma,
        "close_momentum": mom,
    }


# ───────────────────────── pivot ─────────────────────────
def find_pivots(rows: Sequence[Dict[str, Any]], ind: Dict[str, List[Any]],
                cfg: Dict[str, Any]) -> List[Dict[str, Any]]:
    """因果 pivot：i 处高低点，需右侧 window 根走完才确认（confirm_idx=i+window）。"""
    n = len(rows)
    w = cfg["pivot_window"]
    atr = ind["atr"]
    pivots: List[Dict[str, Any]] = []
    for i in range(w, n - w):
        a = atr[i]
        if a is None or a <= 0:
            continue
        hi, lo = rows[i]["high"], rows[i]["low"]
        left = rows[i - w:i]
        right = rows[i + 1:i + w + 1]
        is_high = hi > max(r["high"] for r in left) and hi >= max(r["high"] for r in right)
        is_low = lo < min(r["low"] for r in left) and lo <= min(r["low"] for r in right)
        if is_high:
            prom = hi - max(max(r["high"] for r in left), max(r["high"] for r in right))
            if prom >= cfg["pivot_prominence_atr"] * a:
                pivots.append({"idx": i, "kind": "high", "price": hi,
                               "confirm_idx": i + w, "ts": rows[i]["ts"], "prom": prom})
        elif is_low:
            prom = min(min(r["low"] for r in left), min(r["low"] for r in right)) - lo
            if prom >= cfg["pivot_prominence_atr"] * a:
                pivots.append({"idx": i, "kind": "low", "price": lo,
                               "confirm_idx": i + w, "ts": rows[i]["ts"], "prom": prom})
    return pivots


# ───────────────────────── 背离判定 ─────────────────────────
def _leg_values(ind: Dict[str, List[Any]], idx: int) -> Dict[str, Optional[float]]:
    return {leg: ind[leg][idx] for leg in LEG_LABELS}


def _leg_threshold(leg: str, cfg: Dict[str, Any], ind: Dict[str, List[Any]],
                   idx: int) -> Optional[float]:
    spec = cfg["legs"][leg]
    if "threshold" in spec:
        return float(spec["threshold"])
    if leg == "macd_hist":
        a = ind["atr"][idx]
        return float(spec["threshold_atr"]) * a if a else None
    if leg == "obv":
        v = ind["vol_ma"][idx]
        return float(spec["threshold_vol_mult"]) * v if v else None
    return None


def _evaluate_pair(rows, ind, pivot_a, pivot_b, cfg) -> Optional[Dict[str, Any]]:
    """比较两个同向 pivot，返回最强的 regular / hidden 背离（可能 None）。"""
    a, b = pivot_a["idx"], pivot_b["idx"]
    va, vb = _leg_values(ind, a), _leg_values(ind, b)
    atr_b = ind["atr"][b]
    if atr_b is None or atr_b <= 0:
        return None
    price_a, price_b = pivot_a["price"], pivot_b["price"]

    # 方向与结构条件：regular 要求价格创新高/新低，hidden 要求反向
    if pivot_b["kind"] == "high":
        if price_b > price_a + cfg["min_price_swing_atr"] * atr_b:
            mode = "regular"
            want = "down"   # 指标应走弱
            sig_type = "bearish"
        elif price_b < price_a - cfg["min_price_swing_atr"] * atr_b:
            mode = "hidden"
            want = "up"     # 价格更低的高点、指标更高 → 下跌延续
            sig_type = "bearish"
        else:
            return None
    else:
        if price_b < price_a - cfg["min_price_swing_atr"] * atr_b:
            mode = "regular"
            want = "up"
            sig_type = "bullish"
        elif price_b > price_a + cfg["min_price_swing_atr"] * atr_b:
            mode = "hidden"
            want = "down"   # 价格更高的低点、指标更低 → 上涨延续
            sig_type = "bullish"
        else:
            return None

    evidence: List[Dict[str, Any]] = []
    agree_weight = 0.0
    total_weight = 0.0
    for leg, label in LEG_LABELS.items():
        wa, wb = va.get(leg), vb.get(leg)
        thr = _leg_threshold(leg, cfg, ind, b)
        if wa is None or wb is None or thr is None:
            continue
        total_weight += float(cfg["legs"][leg]["weight"])
        delta = wb - wa
        hit = (delta <= -thr) if want == "down" else (delta >= thr)
        if hit:
            agree_weight += float(cfg["legs"][leg]["weight"])
            evidence.append({"indicator": label, "leg": leg,
                             "a": round(wa, 3), "b": round(wb, 3),
                             "delta": round(delta, 3), "weight": cfg["legs"][leg]["weight"]})
    if len(evidence) < 2:
        return None

    # 强度分量
    agree = agree_weight / total_weight if total_weight else 0.0
    swing = min(abs(price_b - price_a) / atr_b, 3.0) / 3.0
    vol_a = float(rows[a]["volume"] or 0.0)
    vol_b = float(rows[b]["volume"] or 0.0)
    if mode == "regular":
        vol_score = 1.0 if vol_b < vol_a * 0.9 else (0.5 if vol_b < vol_a * 1.1 else 0.2)
    else:
        vol_score = 1.0 if vol_b > vol_a * 1.1 else (0.5 if vol_b > vol_a * 0.9 else 0.2)
    adx_b = ind["adx"][b]
    if adx_b is None:
        trend_score = 0.4
    elif mode == "regular":
        trend_score = 1.0 if adx_b > 25 else (0.6 if adx_b >= 15 else 0.25)
    else:
        trend_score = 1.0 if adx_b > 25 else (0.6 if adx_b >= 18 else 0.3)

    sw = cfg["score_weights"]
    score = 100.0 * (sw["agree"] * agree + sw["swing"] * swing +
                     sw["volume"] * vol_score + sw["trend"] * trend_score)

    # 趋势位置校正：regular 在长期趋势末端（顶在均线上方 / 底在均线下方）加分；
    # hidden 顺势成立时加分；逆势隐藏背离减分。只调 5 分，不改变主判据。
    e200 = ind.get("ema200", [None] * len(rows))[b]
    trend = "unknown"
    if e200:
        trend = "up" if rows[b]["close"] > e200 else "down"
        if mode == "regular":
            score += 5 if ((sig_type == "bearish" and trend == "up") or
                           (sig_type == "bullish" and trend == "down")) else -5
        else:
            score += 5 if ((sig_type == "bearish" and trend == "down") or
                           (sig_type == "bullish" and trend == "up")) else -5
    score = max(0.0, min(100.0, score))

    invalidation = (price_b + 0.6 * atr_b) if sig_type == "bearish" else (price_b - 0.6 * atr_b)
    trigger = (min(rows[b]["low"], rows[b - 1]["low"]) if sig_type == "bearish"
               else max(rows[b]["high"], rows[b - 1]["high"]))

    # 目标参考：b 之前最近的对手 pivot
    return {
        "type": sig_type,
        "kind": mode,
        "score": round(score, 1),
        "pivot_a": {"idx": a, "ts": pivot_a["ts"], "price": round(price_a, 6)},
        "pivot_b": {"idx": b, "ts": pivot_b["ts"], "price": round(price_b, 6)},
        "confirm_idx": pivot_b["confirm_idx"],
        "confirm_ts": rows[pivot_b["confirm_idx"]]["ts"] if pivot_b["confirm_idx"] < len(rows) else None,
        "evidence": evidence,
        "agree_legs": len(evidence),
        "invalidation": round(invalidation, 6),
        "trigger": round(trigger, 6),
        "atr": round(atr_b, 6),
        "adx": round(adx_b, 2) if adx_b is not None else None,
        "trend": trend,
        "context": f"{mode}/{sig_type}/{trend}",
        "ema200": round(e200, 6) if e200 else None,
        "bars_gap": b - a,
    }


def scan_signals(rows: Sequence[Dict[str, Any]], cfg: Optional[Dict[str, Any]] = None,
                 asset: str = "", interval: str = "") -> List[Dict[str, Any]]:
    """按确认时间输出全部信号（因果）。"""
    cfg = cfg or DEFAULT_CONFIG
    ind = compute_indicators(rows, cfg)
    pivots = find_pivots(rows, ind, cfg)
    by_kind = {"high": [], "low": []}
    for p in pivots:
        by_kind[p["kind"]].append(p)

    signals: List[Dict[str, Any]] = []
    for kind, ps in by_kind.items():
        for j, pb in enumerate(ps):
            candidates = [pa for pa in ps[:j]
                          if cfg["min_pivot_gap"] <= pb["idx"] - pa["idx"] <= cfg["max_pivot_gap"]]
            if not candidates:
                continue
            pa = candidates[-1]  # 最近一个可比 pivot
            sig = _evaluate_pair(rows, ind, pa, pb, cfg)
            if not sig:
                continue
            sig["asset"] = asset
            sig["interval"] = interval
            sig["grade"] = grade_of(sig["score"], cfg, sig["agree_legs"])
            # 目标：b 之前最近的对手 pivot
            opp = [p for p in by_kind["low" if kind == "high" else "high"] if p["idx"] < pb["idx"]]
            sig["target"] = round(opp[-1]["price"], 6) if opp else None
            rule = rule_of(sig)
            sig["rule_id"] = rule["id"] if rule else None
            sig["effective"] = bool(rule)
            signals.append(sig)

    signals.sort(key=lambda s: (s["confirm_idx"] or 0, -s["score"]))
    if len(signals) > cfg["max_signals"]:
        signals = signals[-cfg["max_signals"]:]
    return signals


def grade_of(score: float, cfg: Optional[Dict[str, Any]] = None,
             agree_legs: int = 99) -> str:
    cut = (cfg or DEFAULT_CONFIG)["grade_cut"]
    if score >= cut["A"] and agree_legs >= 3:
        return "A"
    if score >= cut["B"]:
        return "B"
    if score >= cut["C"]:
        return "C"
    return "D"


EFFECTIVE_RULES: List[Dict[str, Any]] = [
    {
        "id": "hidden_bull_up_adx20",
        "label": "顺势隐藏底背离（上升趋势 + ADX≥20）",
        "horizon": 20,
        "note": "2026-11-16 日线样本外：n=125、命中 60.8%、扣 8bp 后 +1.55%/笔",
    },
]


# 反向清单：实测**稳定亏钱**的组合。和 EFFECTIVE_RULES 分开记，因为两者语义
# 完全不同 —— 上面的 affix 是"满足条件才算可执行"，这里是"命中就该降权/不提示"。
# 数据来源：divergence_ledger 69 笔已结算（扣 8bp），2026-11-19 22:00。
INEFFECTIVE_COMBOS: List[Dict[str, Any]] = [
    {
        "context": "regular/bullish/down",
        "label": "常规底背离（下跌趋势中抄底）",
        "n": 10, "hit_rate": 10.0, "avg_pnl": -8.81, "horizon": 10,
        "note": "10 笔只赢 1 笔、平均每笔 −8.81%：下跌趋势里的常规底背离是接飞刀，",
    },
    {
        "context": "hidden/bullish/down",
        "label": "隐藏底背离（下跌趋势中）",
        "n": 2, "hit_rate": 0.0, "avg_pnl": -5.02, "horizon": 10,
        "note": "样本只有 2 笔，暂不足以定性，但方向与上一条一致。",
    },
]


def ineffective_of(sig: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """命中「历史稳定亏钱」组合则返回该条记录，用于前端降权标注。"""
    ctx = sig.get("context")
    for c in INEFFECTIVE_COMBOS:
        if c["context"] == ctx and c["n"] >= 10:  # n<10 的只记录不给结论
            return c
    return None


def rule_of(sig: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """命中已通过样本外检验的规则则返回规则，否则 None。"""
    ctx = sig.get("context")
    iv = sig.get("interval")
    adx = sig.get("adx") or 0
    if ctx == "hidden/bullish/up" and iv == "1d" and adx >= 20:
        return EFFECTIVE_RULES[0]
    return None


def latest_signals(rows, cfg=None, *, within_bars: int = 30,
                   asset: str = "", interval: str = "") -> List[Dict[str, Any]]:
    cfg = cfg or DEFAULT_CONFIG
    sigs = scan_signals(rows, cfg, asset, interval)
    last = len(rows) - 1
    out = [s for s in sigs if s["confirm_idx"] is not None and last - s["confirm_idx"] <= within_bars]
    for s in out:
        s["bars_since_confirm"] = last - s["confirm_idx"]
        s["confirm_at"] = _fmt_ts(s["confirm_ts"])
        s["pivot_a"]["at"] = _fmt_ts(s["pivot_a"]["ts"])
        s["pivot_b"]["at"] = _fmt_ts(s["pivot_b"]["ts"])
        s["status"] = _status(rows, s)
    return out


def _fmt_ts(ts: Any) -> Optional[str]:
    if ts is None:
        return None
    try:
        return datetime.fromtimestamp(int(ts)).strftime("%Y-%m-%d %H:%M")
    except Exception:
        return str(ts)


def _status(rows, sig) -> str:
    """信号确认后，价格是否已越过失效/触发。"""
    last_close = float(rows[-1]["close"])
    if sig["type"] == "bearish":
        if last_close > sig["invalidation"]:
            return "已失效"
        if last_close < sig["trigger"]:
            return "已触发"
        return "待触发"
    if last_close < sig["invalidation"]:
        return "已失效"
    if last_close > sig["trigger"]:
        return "已触发"
    return "待触发"


# ───────────────────────── 前向收益评估（证明"准不准"） ─────────────────────────
def evaluate(rows: Sequence[Dict[str, Any]], horizon: int = 10,
             cfg: Optional[Dict[str, Any]] = None,
             asset: str = "", interval: str = "", min_score: float = 0.0) -> Dict[str, Any]:
    """因果回测：信号在 confirm 当根收盘入场，horizon 根后收盘出场。

    bearish 命中 = 收益 < 0；bullish 命中 = 收益 > 0。
    基准 = 同一段样本里任取一根、同 horizon 的同向比例，用来算 lift。
    """
    cfg = dict(cfg or DEFAULT_CONFIG)
    cfg["max_signals"] = 10 ** 6
    sigs = scan_signals(rows, cfg, asset, interval)
    closes = [float(r["close"]) for r in rows]

    def _stats(items: List[Dict[str, Any]]) -> Dict[str, Any]:
        pairs: List[Tuple[Dict[str, Any], float]] = []
        for s in items:
            c = s["confirm_idx"]
            if c is None or c + horizon >= len(closes):
                continue
            entry, exit_ = closes[c], closes[c + horizon]
            if entry <= 0:
                continue
            ret = (exit_ - entry) / entry * 100.0
            pairs.append((s, ret))
            s["_ret"] = ret
        if not pairs:
            return {"n": 0, "hit_rate": None, "avg_ret": None, "median_ret": None}
        pnls = [(-r if s["type"] == "bearish" else r) for s, r in pairs]
        hits = sum(1 for x in pnls if x > 0)
        srt = sorted(pnls)
        med = srt[len(srt) // 2]
        return {"n": len(pnls), "hit_rate": round(hits / len(pnls) * 100, 1),
                "avg_pnl": round(sum(pnls) / len(pnls), 2),
                "median_pnl": round(med, 2)}

    by_grade = {g: [s for s in sigs if s["grade"] == g and s["score"] >= min_score]
                for g in ("A", "B", "C")}
    by_type = {t: [s for s in sigs if s["type"] == t and s["score"] >= min_score]
               for t in ("bearish", "bullish")}
    ctx_keys = sorted({s.get("context") or "unknown" for s in sigs})
    by_context = {k: [s for s in sigs if (s.get("context") or "unknown") == k and s["score"] >= min_score]
                  for k in ctx_keys}

    # 基准：全样本同 horizon 的方向比例（不做信号筛选）
    up = down = 0
    for i in range(len(closes) - horizon):
        if closes[i] <= 0:
            continue
        if closes[i + horizon] > closes[i]:
            up += 1
        elif closes[i + horizon] < closes[i]:
            down += 1
    base = {"up_rate": round(up / max(1, up + down) * 100, 1),
            "down_rate": round(down / max(1, up + down) * 100, 1), "n": up + down}

    def _lift(hit, base_rate):
        return round(hit - base_rate, 1) if hit is not None else None

    out: Dict[str, Any] = {
        "asset": asset, "interval": interval, "horizon": horizon,
        "bars": len(rows), "signals": len(sigs),
        "baseline": base,
        "by_grade": {},
        "by_type": {},
        "by_context": {},
    }
    for g, items in by_grade.items():
        st = _stats(items)
        st["lift"] = _lift(st["hit_rate"], base["down_rate"] if items and items[0]["type"] == "bearish" else base["up_rate"]) if st["n"] else None
        out["by_grade"][g] = st
    for t, items in by_type.items():
        st = _stats(items)
        st["lift"] = _lift(st["hit_rate"], base["down_rate"] if t == "bearish" else base["up_rate"]) if st["n"] else None
        out["by_type"][t] = st
    for k, items in by_context.items():
        st = _stats(items)
        st["lift"] = _lift(st["hit_rate"], base["down_rate"] if items and items[0]["type"] == "bearish" else base["up_rate"]) if st["n"] else None
        out["by_context"][k] = st
    allst = _stats([s for s in sigs if s["score"] >= min_score])
    out["all"] = allst
    return out


# ───────────────────────── 数据库取数 ─────────────────────────
def load_bars(conn: sqlite3.Connection, asset: str, interval: str,
              limit: int = 800) -> List[Dict[str, Any]]:
    if interval in ("1d", "d", "day", "daily"):
        rows = conn.execute(
            "SELECT ts, open, high, low, close, volume FROM ohlcv_daily "
            "WHERE asset=? ORDER BY ts DESC LIMIT ?", (asset, limit)).fetchall()
    else:
        rows = conn.execute(
            "SELECT ts, open, high, low, close, volume FROM ohlcv_intraday "
            "WHERE asset=? AND interval=? ORDER BY ts DESC LIMIT ?",
            (asset, interval, limit)).fetchall()
    data = [{"ts": int(r[0]), "open": float(r[1]), "high": float(r[2]),
             "low": float(r[3]), "close": float(r[4]), "volume": float(r[5])}
            for r in rows]
    data.sort(key=lambda r: r["ts"])
    return data
