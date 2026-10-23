"""
trend_pivot.py — v303 R13: 纯指标组合的「增长拐点」识别
========================================================
用户要求
--------
"先不用未来函数, 只用指标组合判断拐点趋势
 (如均线交叉 + RSI + 量能背离 同时满足)"

因此本模块**完全不使用未来函数**:
  - 不看 i 之后的任何数据
  - 不做 pivot 波峰波谷确认(那本质是事后数 K 线)
  - 只用 EMA/RSI/MACD/量能/ATR 的**当前值与历史斜率**判断趋势是否正在转向

判定逻辑(顶部)
--------------
    EMA 下穿 + RSI 从超买回落 + 动量衰减 + 量能背离  →  顶部拐点
判定逻辑(底部)
--------------
    EMA 上穿 + RSI 从超卖回升 + 动量增强 + 量能背离  →  底部拐点

关键防过载设计
--------------
上一版的问题: MU 501 根 bar 出了 263 个转折点(73% 密度)。
本模块强制:
  * **趋势过滤**: 只有处在"上升趋势"或"下降趋势"时才判拐点,
    震荡市(EMA 缠绕/斜率平)一律不报 —— 用户说的"增长交接点"
    本质就是趋势的反转, 震荡市不该有拐点
  * **冷却期**: 同方向拐点至少间隔 `cooldown` 根
  * **确认滞后**: 拐点标记在**当根**就出现(实时), 而不是等事后确认
  * **高门槛**: 至少命中 N 个指标组合才输出
"""
from __future__ import annotations

from typing import Any

from tech_detector import CONFIG, _atr_series, _ema_series, _macd_series, _rsi_series

PIVOT = {
    "ema_fast": 12,
    "ema_slow": 26,
    "rsi_period": 14,
    "rsi_overbought": 70.0,
    "rsi_oversold": 30.0,
    # 斜率回看: 判断 EMA 是在"走平"还是"掉头"
    "slope_lookback": 5,
    # 趋势斜率回看(慢均线)
    "trend_slope_lookback": 8,
    # 价格与快慢均线偏离的最小幅度, 低于此值视为缠绕/震荡
    "trend_pos_min": 0.012,
    # 趋势强度门槛: |ema_fast - ema_slow| / close 必须超过这个比例
    "trend_strength_min": 0.004,
    # 冷却期: 同方向拐点最小间隔(bar 数)
    "cooldown": 10,
    # 至少命中几个条件
    "min_confirm": 3,
    # MACD 柱体收缩比例: hist[i] / max(|hist[i-k..i]|) 低于该值视为动量衰竭
    "momentum_decay": 0.35,
    "momentum_lookback": 10,
    # RSI 回撤: 从极值回落多少视为转折
    "rsi_retrace_top": 3.0,
    "rsi_retrace_bottom": 3.0,
    # 量能背离: 突破时缩量(顶部)/放量(底部)
    "vol_ma": 20,
    "vol_shrink_ratio": 0.85,
    "vol_expand_ratio": 1.15,
}


def _slope(series: list[float | None], i: int, look: int) -> float | None:
    """series[i] 相对 i-look 的变化率。None 表示数据不足。"""
    if i - look < 0:
        return None
    a, b = series[i - look], series[i]
    if a is None or b is None or a == 0:
        return None
    return (b - a) / abs(a)


def _momentum_ratio(hist: list[float | None], i: int, look: int) -> float | None:
    """当前 MACD 柱体相对近端最大柱体的比例, 衡量动量是否衰竭。"""
    if i - look < 0:
        return None
    seg = [h for h in hist[max(0, i - look):i + 1] if h is not None]
    if len(seg) < 3:
        return None
    mx = max(abs(x) for x in seg)
    if mx == 0:
        return None
    return abs(hist[i]) / mx if hist[i] is not None else None


def analyze(rows: list[dict]) -> dict[str, Any]:
    """预计算纯因果指标序列。索引 i 只依赖 [0..i]。"""
    closes = [r["close"] for r in rows]
    vols = [float(r["volume"] or 0.0) for r in rows]
    n = len(rows)
    P = PIVOT

    ema_f = _ema_series(closes, P["ema_fast"])
    ema_s = _ema_series(closes, P["ema_slow"])
    rsi = _rsi_series(closes, P["rsi_period"])
    dif, dea, hist = _macd_series(closes, P["ema_fast"], P["ema_slow"], 9)
    atr = _atr_series(rows, P["rsi_period"])

    # 趋势强度: |EMA12 - EMA26| / close
    strength: list[float | None] = [None] * n
    spread: list[float | None] = [None] * n
    for i in range(n):
        a, b, c = ema_f[i], ema_s[i], closes[i]
        if a is not None and b is not None and c:
            spread[i] = (a - b) / c
            strength[i] = abs(a - b) / c

    # 量能均线(不含当前 bar, 避免自我稀释)
    vol_ma: list[float | None] = [None] * n
    w = P["vol_ma"]
    for i in range(n):
        if i >= w:
            vol_ma[i] = sum(vols[i - w:i]) / w

    vol_ratio: list[float | None] = [None] * n
    for i in range(n):
        if vol_ma[i] and vol_ma[i] > 0:
            vol_ratio[i] = vols[i] / vol_ma[i]

    return {"ema_fast": ema_f, "ema_slow": ema_s, "rsi": rsi,
            "dif": dif, "dea": dea, "hist": hist, "atr": atr,
            "strength": strength, "spread": spread,
            "vol_ma": vol_ma, "vol_ratio": vol_ratio, "n": n}


# ─────────────────────── 趋势状态 ───────────────────────
def _trend_state(ind: dict, rows: list[dict], i: int) -> str:
    """判断 i 时刻的趋势: up / down / range(震荡)。纯因果。

    早期版本只看 |EMA12-EMA26|/close 的间距, 但日线上这个值几乎恒定偏正,
    导致"趋势"判定形同虚设(实测准确率 49.5%, 低于抛硬币)。
    现改为**三要素共识**:
      1. 收盘价在 EMA26 上方/下方
      2. EMA12 相对 EMA26 的方向(用差值而非单纯符号)
      3. 近 N 根 EMA26 的斜率方向
    三者一致才算趋势, 否则为震荡。
    """
    P = PIVOT
    c = rows[i]["close"]
    ef, es = ind["ema_fast"][i], ind["ema_slow"][i]
    if ef is None or es is None or not c:
        return "range"

    pos = (c - es) / c                       # 价格相对慢均线的位置
    gap = (ef - es) / c                      # 快慢均线差
    slope_s = _slope(ind["ema_slow"], i, P["trend_slope_lookback"])

    if slope_s is None:
        return "range"

    # 阈值带内视为缠绕/震荡
    thr = P["trend_pos_min"]
    if abs(pos) < thr or abs(gap) < thr * 0.5:
        return "range"

    votes_up = sum([
        pos > 0,
        gap > 0,
        slope_s > 0,
    ])
    if votes_up == 3:
        return "up"
    if votes_up == 0:
        return "down"
    return "range"


def _was_trend(ind: dict, rows: list[dict], i: int, look: int = 10) -> str | None:
    """i 之前 look 根的主导趋势方向, 用于确认"确实发生了交接"。"""
    j = i - look
    if j < 0:
        return None
    return _trend_state(ind, rows, j)


def detect_pivots(rows: list[dict], ind: dict | None = None) -> list[dict]:
    """检测趋势拐点。**纯因果**: 输出中每条只依赖 ts 及之前的数据。

    返回字段与 tech_detector 的信号兼容, 但:
      uses_future 恒为 0
      trigger_type = "pivot_top" / "pivot_bottom"
    """
    if ind is None:
        ind = analyze(rows)
    P = PIVOT
    n = len(rows)
    lb, ml = P["slope_lookback"], P["momentum_lookback"]
    out: list[dict] = []
    last_top = last_bot = -10**9

    for i in range(max(lb, P["ema_slow"], P["rsi_period"]), n):
        ef, es, r = ind["ema_fast"][i], ind["ema_slow"][i], ind["rsi"][i]
        if ef is None or es is None or r is None:
            continue

        prev = _trend_state(ind, rows, i)
        prev_trend = _was_trend(ind, rows, i)
        # 必须在震荡 → 趋势 的交接处, 或者趋势方向发生反转时才继续判断
        slope_f = _slope(ind["ema_fast"], i, lb)
        slope_s = _slope(ind["ema_slow"], i, lb)
        if slope_f is None or slope_s is None:
            continue
        cross_down = slope_f < 0 and slope_s <= 0
        cross_up = slope_f > 0 and slope_s >= 0
        mom = _momentum_ratio(ind["hist"], i, ml)
        vr = ind["vol_ratio"][i]

        # ── 顶部拐点 ──
        if prev == "range" and prev_trend == "up" and cross_down:
            hits = [{"k": "ema_cross_down", "v": round(slope_f, 5), "w": 0.8}]
            if r < P["rsi_overbought"] and r > P["rsi_overbought"] - P["rsi_retrace_top"] * 3:
                hits.append({"k": "rsi_fade", "v": round(r, 2), "w": 0.7})
            if mom is not None and mom < P["momentum_decay"]:
                hits.append({"k": "momentum_decay", "v": round(mom, 3), "w": 0.7})
            if vr is not None and vr < P["vol_shrink_ratio"]:
                hits.append({"k": "vol_diverge", "v": round(vr, 2), "w": 0.6})
            if len(hits) >= P["min_confirm"] and i - last_top >= P["cooldown"]:
                out.append(_mk(rows, i, "pivot_top", hits, "up"))
                last_top = i

        # ── 底部拐点 ──
        elif prev == "range" and prev_trend == "down" and cross_up:
            hits = [{"k": "ema_cross_up", "v": round(slope_f, 5), "w": 0.8}]
            if r > P["rsi_oversold"] and r < P["rsi_oversold"] + P["rsi_retrace_bottom"] * 3:
                hits.append({"k": "rsi_rebound", "v": round(r, 2), "w": 0.7})
            if mom is not None and mom < P["momentum_decay"]:
                hits.append({"k": "momentum_decay", "v": round(mom, 3), "w": 0.7})
            if vr is not None and vr > P["vol_expand_ratio"]:
                hits.append({"k": "vol_surge", "v": round(vr, 2), "w": 0.6})
            if len(hits) >= P["min_confirm"] and i - last_bot >= P["cooldown"]:
                out.append(_mk(rows, i, "pivot_bottom", hits, "down"))
                last_bot = i

    return out


def _mk(rows: list[dict], i: int, kind: str, hits: list[dict],
        from_trend: str) -> dict:
    """构造拐点记录。uses_future 恒为 0。"""
    score = min(CONFIG["score_cap"],
                round(CONFIG["score_base"] + 0.15 * len(hits), 3))
    return {
        "ts": rows[i]["ts"],
        "close": rows[i]["close"],
        "pct_1d": _pct(rows, i, 1),
        "pct_3d": _pct(rows, i, 3),
        "vol_ratio": None,
        "trigger_type": kind,
        "score": score,
        "uses_future": False,
        "signals": hits,
        "merged_types": [],
        "from_trend": from_trend,
    }


def _pct(rows: list[dict], i: int, back: int) -> float | None:
    j = i - back
    if j < 0:
        return None
    a, b = rows[j]["close"], rows[i]["close"]
    if not a:
        return None
    return round((b - a) / a * 100, 2)