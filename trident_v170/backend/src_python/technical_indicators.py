#!/usr/bin/env python3
"""技术指标纯函数库 —— 行情归因 Phase 1。

设计约束（对齐 abnormal_detector.py）：
  - 纯函数、无 I/O、无第三方依赖，便于离线单测；
  - 蜡烛升序：[ts_ms, open, high, low, close, volume]；
  - 样本不足处用 None 占位，绝不抛异常、绝不造数；
  - 所有阈值/周期均为可配置参数（默认值为行业惯例），便于后续调参。

指标一览：
  rsi / rsi_divergence          相对强弱指标 + 背离
  ema / sma / ma                指数/简单/均线
  macd / macd_cross             趋势动能 + 金叉死叉
  bollinger / bollinger_state   波动区间
  atr                           波动率
  volume_regime                 量价关系标签
  oi_change_pct                 持仓量变化率
  relative_strength             相对强弱（资产 vs 基准）
  snapshot                      技术面事实快照（聚合，供 ai_worker 注入）
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional, Sequence

TS, OPEN, HIGH, LOW, CLOSE, VOL = range(6)


# --------------------------------------------------------------------------
# 数值工具
# --------------------------------------------------------------------------

def _f(v: Any) -> Optional[float]:
    if v is None or v == "":
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _clean(values: Sequence[Any]) -> List[float]:
    """过滤 None/非法值，返回连续数值序列（K 线通常连续，此处不插值填充）。"""
    out: List[float] = []
    for v in values:
        f = _f(v)
        if f is not None:
            out.append(f)
    return out


def closes(candles: Sequence[Any]) -> List[float]:
    return _clean([c[CLOSE] if isinstance(c, (list, tuple)) and len(c) > CLOSE else None for c in candles])


def highs(candles: Sequence[Any]) -> List[float]:
    return _clean([c[HIGH] if isinstance(c, (list, tuple)) and len(c) > HIGH else None for c in candles])


def lows(candles: Sequence[Any]) -> List[float]:
    return _clean([c[LOW] if isinstance(c, (list, tuple)) and len(c) > LOW else None for c in candles])


def volumes(candles: Sequence[Any]) -> List[float]:
    return _clean([c[VOL] if isinstance(c, (list, tuple)) and len(c) > VOL else None for c in candles])


# --------------------------------------------------------------------------
# 均线 / 指数平滑
# --------------------------------------------------------------------------

def ema(values: Sequence[Any], period: int = 12) -> List[float]:
    """指数移动平均。返回等长序列（首值 = values[0]），空序列返回 []。"""
    values = _clean(values)
    if not values:
        return []
    k = 2.0 / (period + 1)
    out = [values[0]]
    for v in values[1:]:
        out.append(v * k + out[-1] * (1 - k))
    return out


def sma(values: Sequence[Any], period: int = 20) -> List[Optional[float]]:
    """简单移动平均。前 period-1 根为 None（样本不足）。"""
    values = _clean(values)
    n = len(values)
    out: List[Optional[float]] = [None] * n
    if period <= 0 or n < period:
        return out
    running = sum(values[:period])
    out[period - 1] = running / period
    for i in range(period, n):
        running += values[i] - values[i - period]
        out[i] = running / period
    return out


def ma(values: Sequence[Any], period: int = 20) -> List[Optional[float]]:
    """移动平均（SMA 别名）。"""
    return sma(values, period)


def ma_stack(closes: Sequence[Any]) -> Dict[str, Any]:
    """常用均线组合 + 多头排列判断。"""
    cs = _clean(closes)
    out: Dict[str, Any] = {}
    for p in (5, 10, 20, 60):
        s = sma(cs, p)
        out[f"ma{p}"] = s[-1] if s else None
    m5, m10, m20, m60 = out.get("ma5"), out.get("ma10"), out.get("ma20"), out.get("ma60")
    if None not in (m5, m10, m20, m60):
        out["bull_aligned"] = m5 > m10 > m20 > m60
        out["bear_aligned"] = m5 < m10 < m20 < m60
    else:
        out["bull_aligned"] = out["bear_aligned"] = None
    return out


# --------------------------------------------------------------------------
# RSI
# --------------------------------------------------------------------------

def _rsi_from_avg(avg_gain: float, avg_loss: float) -> float:
    if avg_gain == 0 and avg_loss == 0:
        return 50.0  # 平盘无动量 → 中性，避免误判为超买（100）
    if avg_loss == 0:
        return 100.0
    rs = avg_gain / avg_loss
    return 100.0 - 100.0 / (1.0 + rs)


def rsi_divergence(closes: Sequence[Any], period: int = 14, lookback: int = 30) -> str:
    """RSI 顶/底背离检测（简化口径）。

    顶背离（bearish）：当前价格=窗口新高，但当前 RSI 低于窗口历史峰值 → 上涨动能衰竭。
    底背离（bullish）：当前价格=窗口新低，但当前 RSI 高于窗口历史谷值 → 下跌动能衰竭。
    返回 "bearish" / "bullish" / "none"。
    """
    cs = _clean(closes)
    r = rsi(cs, period)
    valid = [(c, ri) for c, ri in zip(cs[-lookback:], r[-lookback:]) if ri is not None]
    if len(valid) < 3:
        return "none"
    prices = [p for p, _ in valid]
    rsis = [ri for _, ri in valid]
    if prices[-1] == max(prices) and rsis[-1] < max(rsis):
        return "bearish"
    if prices[-1] == min(prices) and rsis[-1] > min(rsis):
        return "bullish"
    return "none"


# --------------------------------------------------------------------------
# MACD
# --------------------------------------------------------------------------

def macd(values: Sequence[Any], fast: int = 12, slow: int = 26, signal_period: int = 9) -> Dict[str, List[float]]:
    """MACD：EMA(fast)-EMA(slow)，signal=EMA(macd,9)，hist=macd-signal。均等长。接受 raw candles 或 close 序列。"""
    values = _clean(_auto_values(values))
    ema_fast = ema(values, fast)
    ema_slow = ema(values, slow)
    macd_line = [f - s for f, s in zip(ema_fast, ema_slow)]
    signal_line = ema(macd_line, signal_period)
    hist = [m - s for m, s in zip(macd_line, signal_line)]
    return {"macd": macd_line, "signal": signal_line, "hist": hist}


def macd_cross(values: Sequence[Any], fast: int = 12, slow: int = 26, signal_period: int = 9) -> Optional[str]:
    """最新一根是否发生金叉/死叉。返回 "golden" / "death" / None。"""
    m = macd(values, fast, slow, signal_period)
    ml, sl = m["macd"], m["signal"]
    if len(ml) < 2 or len(sl) < 2:
        return None
    prev, cur = ml[-2] - sl[-2], ml[-1] - sl[-1]
    if prev <= 0 < cur:
        return "golden"
    if prev >= 0 > cur:
        return "death"
    return None


# --------------------------------------------------------------------------
# BOLL
# --------------------------------------------------------------------------

def bollinger(values: Sequence[Any], period: int = 20, num_std: float = 2.0) -> Dict[str, List[Optional[float]]]:
    """布林带（总体标准差）。前 period-1 根为 None。"""
    values = _clean(values)
    n = len(values)
    upper: List[Optional[float]] = [None] * n
    mid: List[Optional[float]] = [None] * n
    lower: List[Optional[float]] = [None] * n
    for i in range(period - 1, n):
        window = values[i - period + 1:i + 1]
        m = sum(window) / period
        var = sum((x - m) ** 2 for x in window) / period
        sd = var ** 0.5
        mid[i] = m
        upper[i] = m + num_std * sd
        lower[i] = m - num_std * sd
    return {"upper": upper, "mid": mid, "lower": lower}


def bollinger_state(values: Sequence[Any], period: int = 20, num_std: float = 2.0) -> Optional[Dict[str, Any]]:
    """最新一根布林带状态：三轨 + %B + 带宽。样本不足返回 None。"""
    cs = _clean(values)
    b = bollinger(cs, period, num_std)
    if not cs or b["upper"][-1] is None:
        return None
    c = cs[-1]
    up, lo, mid = b["upper"][-1], b["lower"][-1], b["mid"][-1]
    pct_b = (c - lo) / (up - lo) if up != lo else 0.5
    bandwidth = (up - lo) / mid if mid else 0.0
    return {"upper": up, "mid": mid, "lower": lo, "percent_b": pct_b, "bandwidth": bandwidth}


# --------------------------------------------------------------------------
# ATR
# --------------------------------------------------------------------------

def atr(candles: Sequence[Any], period: int = 14) -> List[Optional[float]]:
    """Wilder ATR（平均真实波幅）。前 period-1 根为 None。"""
    hs, ls, cs = highs(candles), lows(candles), closes(candles)
    n = min(len(hs), len(ls), len(cs))
    if n < 2:
        return [None] * n
    trs = [hs[0] - ls[0]]
    prev_c = cs[0]
    for i in range(1, n):
        trs.append(max(hs[i] - ls[i], abs(hs[i] - prev_c), abs(ls[i] - prev_c)))
        prev_c = cs[i]
    out: List[Optional[float]] = [None] * n
    if n <= period:
        return out
    avg = sum(trs[:period]) / period
    out[period - 1] = avg
    for i in range(period, n):
        avg = (avg * (period - 1) + trs[i]) / period
        out[i] = avg
    return out


# --------------------------------------------------------------------------
# 量价关系
# --------------------------------------------------------------------------

def volume_regime(candles: Sequence[Any], lookback: int = 5,
                  vol_ratio: float = 1.5, vol_shrink: float = 0.7) -> Dict[str, Any]:
    """最新一根量价关系标签（口径对齐用户定义）。

    以近 lookback 根均量为基准：量 / 均量 ≥ vol_ratio → 放量；≤ vol_shrink → 缩量。
    divergence：价格上涨但量能萎缩 → sell（动能不足）；价格下跌但量能萎缩 → buy（卖压衰竭）。
    """
    cs = closes(candles)
    vs = volumes(candles)
    base: Dict[str, Any] = {"regime": "样本不足", "price_up": None, "vol_ratio": None, "divergence": None}
    if len(cs) < 2 or len(vs) < 2:
        return base
    prev_close, cur_close = cs[-2], cs[-1]
    price_up = cur_close > prev_close
    price_down = cur_close < prev_close
    if len(vs) >= lookback + 1:
        denom = sum(vs[-(lookback + 1):-1]) / lookback
    else:
        denom = sum(vs[:-1]) / max(1, len(vs) - 1)
    cur_vol = vs[-1]
    ratio = None
    vol_dir = None
    if denom > 0:
        ratio = cur_vol / denom
        if ratio >= vol_ratio:
            vol_dir = "up"
        elif ratio <= vol_shrink:
            vol_dir = "down"
        else:
            vol_dir = "flat"
    if vol_dir == "up" and price_up:
        regime = "放量上涨"
    elif vol_dir == "up" and price_down:
        regime = "放量下跌"
    elif vol_dir == "down" and price_up:
        regime = "缩量上涨"
    elif vol_dir == "down" and price_down:
        regime = "缩量下跌"
    elif vol_dir == "flat" and price_up:
        regime = "温和上涨"
    elif vol_dir == "flat" and price_down:
        regime = "温和下跌"
    else:
        regime = "盘整"
    divergence = None
    if price_up and vol_dir == "down":
        divergence = "sell"   # 顶背离：价格涨、量能跟不上
    elif price_down and vol_dir == "down":
        divergence = "buy"    # 底背离：价格跌、卖压衰竭
    return {
        "regime": regime,
        "price_up": price_up,
        "vol_ratio": round(ratio, 4) if ratio is not None else None,
        "divergence": divergence,
    }


# --------------------------------------------------------------------------
# 持仓量 / 相对强弱
# --------------------------------------------------------------------------

def oi_change_pct(current: Any, previous: Any) -> Optional[float]:
    """持仓量变化率 %。任一缺失或 previous=0 返回 None。"""
    cur, prev = _f(current), _f(previous)
    if cur is None or prev is None or prev == 0:
        return None
    return round((cur - prev) / prev * 100.0, 4)


def relative_strength(asset_price: Any, base_price: Any,
                      prev_asset: Any = None, prev_base: Any = None) -> Dict[str, Optional[float]]:
    """相对强弱：asset / base（如 ETH/BTC）。rs_pct 为比值变化率 %。"""
    a, b = _f(asset_price), _f(base_price)
    if not a or not b or b == 0:
        return {"rs": None, "rs_pct": None}
    rs = a / b
    rs_pct = None
    pa, pb = _f(prev_asset), _f(prev_base)
    if pa and pb and pb != 0:
        prev_rs = pa / pb
        if prev_rs:
            rs_pct = round((rs - prev_rs) / prev_rs * 100.0, 4)
    return {"rs": round(rs, 6), "rs_pct": rs_pct}


# --------------------------------------------------------------------------
# v182.3 新增异动指标 —— 提高 Phase 2 多因子识别精度
# --------------------------------------------------------------------------
# 设计原则：
#   1. 全部纯函数、零依赖、样本不足返回 None 绝不抛异常；
#   2. 新指标用于「替换/补充」abnormal_detector 现有硬阈值（1.5×量、70/30 RSI）；
#   3. 新指标覆盖：KDJ 短期反转、MFI 量价加权 RSI、布林带突破、
#      量能 Z-score（自适应阈值）、MA 金叉死叉、单根 K 线形态、ATR 突破（波动率扩张）。
#
# 接入点：abnormal_detector.multi_factor() 通过 technical_indicators.<name>() 调用。
# 这些函数不进 snapshot() 聚合，保持 backward-compatible（旧代码不受影响）。


def kdj(closes: Sequence[Any], highs: Sequence[Any], lows: Sequence[Any],
        n: int = 9, m1: int = 3, m2: int = 3) -> Dict[str, List[Optional[float]]]:
    """KDJ 随机指标（A股/港股常用，3m K 也适用）。

    K = RSV 的 m1 日 SMA；D = K 的 m2 日 SMA；J = 3K - 2D。
    返回 {k: [], d: [], j: []}，样本不足处填 None。
    """
    cs = _clean(closes); hs = _clean(highs); ls = _clean(lows)
    nlen = min(len(cs), len(hs), len(ls))
    out = {"k": [None] * nlen, "d": [None] * nlen, "j": [None] * nlen}
    if nlen < n:
        return out
    # RSV = (close - lowest_low_n) / (highest_high_n - lowest_low_n) * 100
    rsv = [None] * nlen
    for i in range(n - 1, nlen):
        win_h = max(hs[i - n + 1: i + 1])
        win_l = min(ls[i - n + 1: i + 1])
        if win_h == win_l:
            rsv[i] = 50.0
        else:
            rsv[i] = (cs[i] - win_l) / (win_h - win_l) * 100.0
    # K = SMA(RSV, m1)，初始值 = 50
    kvals: List[Optional[float]] = [None] * nlen
    prev_k = 50.0
    for i in range(n - 1, nlen):
        if rsv[i] is None:
            continue
        cur_k = (prev_k * (m1 - 1) + rsv[i]) / m1
        kvals[i] = cur_k
        prev_k = cur_k
    # D = SMA(K, m2)，初始值 = 50
    dvals: List[Optional[float]] = [None] * nlen
    prev_d = 50.0
    for i in range(n - 1, nlen):
        if kvals[i] is None:
            continue
        cur_d = (prev_d * (m2 - 1) + kvals[i]) / m2
        dvals[i] = cur_d
        prev_d = cur_d
    # J = 3K - 2D
    jvals: List[Optional[float]] = []
    for i in range(nlen):
        if kvals[i] is None or dvals[i] is None:
            jvals.append(None)
        else:
            jvals.append(3 * kvals[i] - 2 * dvals[i])
    out["k"], out["d"], out["j"] = kvals, dvals, jvals
    return out


def kdj_trip(kdj_vals: Dict[str, List[Optional[float]]],
             overbought: float = 80.0, oversold: float = 20.0) -> Dict[str, Any]:
    """KDJ 超买超卖触发。"""
    j = kdj_vals.get("j") or []
    k = kdj_vals.get("k") or []
    if not j or j[-1] is None:
        return {"triggered": False, "j": None, "k": None, "direction": None}
    jv = round(j[-1], 2)
    kv = round(k[-1], 2) if k and k[-1] is not None else None
    if jv >= overbought or (kv is not None and kv >= overbought):
        return {"triggered": True, "j": jv, "k": kv, "direction": "bearish"}
    if jv <= oversold or (kv is not None and kv <= oversold):
        return {"triggered": True, "j": jv, "k": kv, "direction": "bullish"}
    return {"triggered": False, "j": jv, "k": kv, "direction": None}


def mfi(candles: Sequence[Any], period: int = 14) -> List[Optional[float]]:
    """MFI 资金流量指标（Volume-weighted RSI）。

    Typical Price = (high + low + close) / 3；Raw Money Flow = TP * volume。
    若 TP 上升 → 正向流量；下降 → 负向流量。
    MFI = 100 - 100 / (1 + 正向流量 / 负向流量)。
    """
    parsed: List[Optional[tuple]] = []
    for c in candles:
        if not isinstance(c, (list, tuple)) or len(c) < 6:
            parsed.append(None); continue
        h, lo, cl, vol = _f(c[HIGH]), _f(c[LOW]), _f(c[CLOSE]), _f(c[VOL])
        if None in (h, lo, cl, vol):
            parsed.append(None); continue
        parsed.append(((h + lo + cl) / 3.0, vol))
    out: List[Optional[float]] = [None] * len(parsed)
    if len(parsed) < period + 1:
        return out
    for i in range(period, len(parsed)):
        window = parsed[i - period + 1: i + 1]
        if any(x is None for x in window):
            continue
        pos = 0.0; neg = 0.0
        for j in range(1, len(window)):
            cur_tp, cur_v = window[j]
            prev_tp = window[j - 1][0]
            if cur_tp > prev_tp:
                pos += cur_tp * cur_v
            elif cur_tp < prev_tp:
                neg += cur_tp * cur_v
        if neg <= 0:
            out[i] = 100.0
        else:
            mf_ratio = pos / neg
            out[i] = 100.0 - 100.0 / (1.0 + mf_ratio)
    return out


def mfi_trip(candles: Sequence[Any], *, overbought: float = 80.0,
             oversold: float = 20.0, period: int = 14) -> Dict[str, Any]:
    """MFI 超买超卖触发（量价加权 RSI，对做市/出货更敏感）。"""
    vals = mfi(candles, period)
    if not vals or vals[-1] is None:
        return {"triggered": False, "mfi": None, "direction": None}
    v = round(vals[-1], 2)
    if v >= overbought:
        return {"triggered": True, "mfi": v, "direction": "bearish"}
    if v <= oversold:
        return {"triggered": True, "mfi": v, "direction": "bullish"}
    return {"triggered": False, "mfi": v, "direction": None}


def bollinger_break(closes: Sequence[Any], *, period: int = 20,
                    num_std: float = 2.0) -> Optional[str]:
    """布林带突破检测：最新一根突破外轨。

    close > mid + num_std*std → 'upper_break'（bearish 嫌疑，价超买）；
    close < mid - num_std*std → 'lower_break'（bullish 嫌疑，价超卖）；
    其他 → None。
    """
    cs = _clean(closes)
    if len(cs) < period:
        return None
    window = cs[-period:]
    mid = sum(window) / period
    var = sum((x - mid) ** 2 for x in window) / period
    std = var ** 0.5
    upper = mid + num_std * std
    lower = mid - num_std * std
    last = cs[-1]
    if last > upper:
        return "upper_break"
    if last < lower:
        return "lower_break"
    return None


def vol_zscore(candles: Sequence[Any], lookback: int = 20) -> Optional[float]:
    """量能 Z-score：当前成交量相对 lookback 窗口均值的标准差倍数。

    替代 abnormal_detector 固定 1.5× 阈值（自适应市场波动）。
    Z > 3.0 视为极端放量；Z > 2.0 显著放量；Z < -2.0 极端缩量。
    """
    vols = _clean([c[VOL] if isinstance(c, (list, tuple)) and len(c) > VOL else None
                   for c in candles])
    if len(vols) < lookback + 1:
        return None
    window = vols[-(lookback + 1):-1]
    cur = vols[-1]
    if not window or cur is None:
        return None
    mean = sum(window) / len(window)
    if mean <= 0:
        return None
    var = sum((v - mean) ** 2 for v in window) / len(window)
    std = var ** 0.5
    if std <= 0:
        return None
    return round((cur - mean) / std, 4)


def ma_cross(closes: Sequence[Any], fast: int = 5, slow: int = 20) -> Optional[str]:
    """MA 金叉死叉检测（最末两根 cross）。

    fast SMA 上穿 slow SMA → 'golden'（bullish）；
    fast SMA 下穿 slow SMA → 'death'（bearish）。
    """
    cs = _clean(closes)
    if len(cs) < slow + fast - 1:
        return None

    def _avg(arr):
        return sum(arr) / len(arr)
    fast_prev = _avg(cs[-(slow + fast):-(slow)])
    slow_prev = _avg(cs[-(slow):])
    fast_cur = _avg(cs[-(fast):])
    slow_cur = _avg(cs[-slow:])
    if fast_prev <= slow_prev and fast_cur > slow_cur:
        return "golden"
    if fast_prev >= slow_prev and fast_cur < slow_cur:
        return "death"
    return None


def candle_pattern(candle: Any) -> Optional[str]:
    """单根 K 线形态识别（反转信号）。

    返回 'hammer' / 'shooting_star' / 'doji' / None。
    位置/趋势判断由调用方结合上下文（hammer 在下跌末端=强反转，
    在上涨末端=诱多）。
    """
    if not isinstance(candle, (list, tuple)) or len(candle) < 5:
        return None
    o, h, l, c = (_f(candle[i]) for i in (OPEN, HIGH, LOW, CLOSE))
    if None in (o, h, l, c) or h == l:
        return None
    rng = h - l
    body = abs(c - o)
    upper_wick = h - max(o, c)
    lower_wick = min(o, c) - l
    if rng <= 0:
        return None
    body_pct = body / rng
    if body_pct <= 0.1:
        return "doji"
    if lower_wick >= 2 * body and upper_wick <= body:
        return "hammer"
    if upper_wick >= 2 * body and lower_wick <= body:
        return "shooting_star"
    return None


def atr_break(candles: Sequence[Any], period: int = 14, lookback: int = 20,
              threshold: float = 1.5) -> bool:
    """ATR 突破检测：当前 ATR > 历史均值的 threshold 倍。

    用于识别波动率突然扩张（黑天鹅/重大事件前夕）。
    """
    a = atr(candles, period)
    if len(a) < lookback + 1 or a[-1] is None:
        return False
    history = [x for x in a[-(lookback + 1):-1] if x is not None]
    if not history:
        return False
    mean = sum(history) / len(history)
    return mean > 0 and a[-1] / mean >= threshold


# --------------------------------------------------------------------------
# Q5 新增 4 个异常识别（macd_cross 已存在，未接入 voting chain 见 abnormal_detector）
# --------------------------------------------------------------------------

def macd_zero_cross(values: Sequence[Any], fast: int = 12, slow: int = 26,
                    signal_period: int = 9) -> Optional[str]:
    """MACD 主线穿越零轴检测（最新一根）。

    macd_line 由负转正 → 'above_zero'（多头占优，看涨）；
    macd_line 由正转负 → 'below_zero'（空头占优，看跌）；
    否则 None。
    """
    m = macd(values, fast, slow, signal_period)
    ml = m["macd"]
    if len(ml) < 2:
        return None
    prev, cur = ml[-2], ml[-1]
    if prev <= 0 < cur:
        return "above_zero"
    if prev >= 0 > cur:
        return "below_zero"
    return None


def rsi_blunting(values: Sequence[Any], *, period: int = 14, lookback: int = 5,
                 overbought: float = 70.0, oversold: float = 30.0) -> Optional[str]:
    """RSI 钝化检测（连续 overbought/oversold → 动能衰竭）。

    最近 lookback 根 RSI 全部 >= overbought → 'bearish'（超买钝化，涨势衰竭）；
    最近 lookback 根 RSI 全部 <= oversold → 'bullish'（超卖钝化，跌势衰竭）；
    否则 None。

    用于识别 RSI 长期高位/低位横盘但价格仍创新高/新低（典型顶/底背离前奏）。
    """
    cs = _clean(values)
    r = rsi(cs, period)
    valid = [(c, ri) for c, ri in zip(cs[-lookback:], r[-lookback:]) if ri is not None]
    if len(valid) < lookback:
        return None
    rsis = [ri for _, ri in valid]
    if all(ri >= overbought for ri in rsis):
        return "bearish"
    if all(ri <= oversold for ri in rsis):
        return "bullish"
    return None


def vol_break(candles: Sequence[Any], *, z_threshold: float = 2.0,
              lookback: int = 20) -> Optional[str]:
    """量能突破检测：当前成交量 Z-score ≥ 阈值 且 收盘突破近 lookback 根极值。

    'bullish_break'：量能显著放大 + 收盘 > 近 lookback 根最高；
    'bearish_break'：量能显著放大 + 收盘 < 近 lookback 根最低；
    'volume_surge_only'：量能显著放大但价格未突破（保留 directional=None 场景）→ 返回 None
    （调用方仅凭量能不足以判方向）。
    """
    if len(candles) < lookback + 1:
        return None
    vs = volumes(candles)
    if len(vs) < lookback + 1 or vs[-1] is None:
        return None
    window = vs[-(lookback + 1):-1]
    cur = vs[-1]
    if not window or cur is None:
        return None
    mean = sum(window) / len(window)
    if mean <= 0:
        return None
    var = sum((v - mean) ** 2 for v in window) / len(window)
    std = var ** 0.5
    if std <= 0:
        return None
    z = (cur - mean) / std
    if z < z_threshold:
        return None
    cs = closes(candles)
    if len(cs) < lookback + 1:
        return None
    high_max = max(cs[-(lookback + 1):-1])
    low_min = min(cs[-(lookback + 1):-1])
    cur_close = cs[-1]
    if cur_close > high_max:
        return "bullish_break"
    if cur_close < low_min:
        return "bearish_break"
    return None


def engulfing(candles: Sequence[Any]) -> Optional[str]:
    """吞没形态检测（两 K 线）。

    当前 K 实体完全包覆前一根 K 实体（且方向相反）→
      'bullish'：当前阳线包覆前一根阴线（看涨吞没）
      'bearish'：当前阴线包覆前一根阳线（看跌吞没）
    否则 None。

    仅看实体（开-收），忽略影线。包覆定义为：当前实体范围包含前一根实体范围。
    """
    if len(candles) < 2:
        return None
    cur = candles[-1]
    prev = candles[-2]
    if not isinstance(cur, (list, tuple)) or not isinstance(prev, (list, tuple)):
        return None
    if len(cur) < 5 or len(prev) < 5:
        return None
    co, cc = _f(cur[OPEN]), _f(cur[CLOSE])
    po, pc = _f(prev[OPEN]), _f(prev[CLOSE])
    if None in (co, cc, po, pc):
        return None
    # 当前实体范围（min/max of open,close）
    cur_body_lo, cur_body_hi = min(co, cc), max(co, cc)
    prev_body_lo, prev_body_hi = min(po, pc), max(po, pc)
    # 当前必须有实体（doji 跳过）
    if cur_body_hi == cur_body_lo:
        return None
    # 前一根必须有实体
    if prev_body_hi == prev_body_lo:
        return None
    # 包覆：当前实体严格大于前一根实体（相等不算吞没）
    cur_is_bull = cc > co
    prev_is_bull = pc > po
    if cur_is_bull == prev_is_bull:
        # 方向相同不构成吞没
        return None
    if cur_body_lo < prev_body_lo and cur_body_hi > prev_body_hi:
        return "bullish" if cur_is_bull else "bearish"
    return None


# --------------------------------------------------------------------------
# 技术面事实快照（聚合入口）
# --------------------------------------------------------------------------

def snapshot(candles: Sequence[Any], *, oi_now: Any = None, oi_prev: Any = None,
             asset_price: Any = None, base_price: Any = None,
             prev_asset: Any = None, prev_base: Any = None) -> Dict[str, Any]:
    """把各类指标收敛成一份结构化「技术面事实快照」，供 ai_worker / causal_graph 注入。"""
    cs = closes(candles)
    r = rsi(cs)
    b = bollinger_state(cs)
    a = atr(candles)
    macd_last = macd(cs)["hist"][-1] if cs else None
    return {
        "rsi": r[-1] if r else None,
        "rsi_divergence": rsi_divergence(cs),
        "macd_hist": macd_last,
        "macd_cross": macd_cross(cs),
        "bollinger": b,
        "atr": a[-1] if a else None,
        "ma_stack": ma_stack(cs),
        "volume_regime": volume_regime(candles),
        "oi_change_pct": oi_change_pct(oi_now, oi_prev),
        "relative_strength": relative_strength(asset_price, base_price, prev_asset, prev_base),
    }


# ──────────────────────────────────────────────────────────────────────────────
# v189-3: 新增 4 个指标 (OBV / VWAP / Ichimoku / Stochastic RSI)
# 全部纯函数, 注册到 INDICATORS 后即可被 IndicatorPipeline 链式调用。
# ──────────────────────────────────────────────────────────────────────────────


def obv(candles: Sequence[Any]) -> List[float]:
    """On-Balance Volume — 量价配合: 价格涨 + 量 → OBV 涨; 价格跌 + 量 → OBV 跌。

    用途: 趋势确认(OBV 与价格背离 = 趋势减弱信号)。
    返回 OBV 序列(从 0 开始累加),长度与 candles 一致。
    """
    if not candles:
        return []
    out: List[float] = [0.0]
    cur = 0.0
    cs = closes(candles)
    vs = volumes(candles)
    for i in range(1, len(candles)):
        if cs[i] > cs[i - 1]:
            cur += vs[i]
        elif cs[i] < cs[i - 1]:
            cur -= vs[i]
        out.append(cur)
    return out


def _auto_values(values: Sequence[Any]) -> Sequence[float]:
    """把 raw candles 自动转 closes 序列(若检测到 candle 形态)。

    Pipeline / 用户传 raw candles 时, 这层把 [ts, o, h, l, c, v] → close 序列。
    已经是纯数值序列时不动(向后兼容)。
    """
    if not values:
        return values
    head = values[0]
    if isinstance(head, (list, tuple)) and len(head) >= 6:
        return closes(values)
    return values


def _ohlcv(c: Any) -> tuple:
    """统一从 list/tuple/dict/对象 中拿 (high, low, close, volume)。

    支持:
      - list/tuple (本项目标准: [ts, open, high, low, close, volume])
      - dict ({"high":..., "low":..., "close":..., "volume":...})
      - 对象 (.high / .low / .close / .volume 属性)
    缺字段则返回 None。
    """
    if isinstance(c, (list, tuple)) and len(c) >= 6:
        return (_f(c[HIGH]), _f(c[LOW]), _f(c[CLOSE]), _f(c[VOL]))
    if isinstance(c, dict):
        return (_f(c.get("high")), _f(c.get("low")), _f(c.get("close")), _f(c.get("volume")))
    h = _f(getattr(c, "high", None))
    l = _f(getattr(c, "low", None))
    cl = _f(getattr(c, "close", None))
    v = _f(getattr(c, "volume", None))
    return (h, l, cl, v)


def rsi(values: Sequence[Any], period: int = 14) -> List[Optional[float]]:
    """Wilder RSI。前 period 根为 None。接受 raw candles 或 close 数值序列。"""
    values = _clean(_auto_values(values))
    n = len(values)
    out: List[Optional[float]] = [None] * n
    if n <= period:
        return out
    gains = 0.0
    losses = 0.0
    for i in range(1, period + 1):
        diff = values[i] - values[i - 1]
        if diff >= 0:
            gains += diff
        else:
            losses -= diff
    avg_gain = gains / period
    avg_loss = losses / period
    out[period] = _rsi_from_avg(avg_gain, avg_loss)
    for i in range(period + 1, n):
        diff = values[i] - values[i - 1]
        gain = diff if diff > 0 else 0.0
        loss = -diff if diff < 0 else 0.0
        avg_gain = (avg_gain * (period - 1) + gain) / period
        avg_loss = (avg_loss * (period - 1) + loss) / period
        out[i] = _rsi_from_avg(avg_gain, avg_loss)
    return out
    """统一从 list/tuple/dict/对象 中拿 (high, low, close, volume)。

    支持:
      - list/tuple (本项目标准: [ts, open, high, low, close, volume])
      - dict ({"high":..., "low":..., "close":..., "volume":...})
      - 对象 (.high / .low / .close / .volume 属性)
    缺字段则返回 None。
    """
    if isinstance(c, (list, tuple)) and len(c) >= 6:
        return (_f(c[HIGH]), _f(c[LOW]), _f(c[CLOSE]), _f(c[VOL]))
    if isinstance(c, dict):
        return (_f(c.get("high")), _f(c.get("low")), _f(c.get("close")), _f(c.get("volume")))
    h = _f(getattr(c, "high", None))
    l = _f(getattr(c, "low", None))
    cl = _f(getattr(c, "close", None))
    v = _f(getattr(c, "volume", None))
    return (h, l, cl, v)


def vwap(candles: Sequence[Any]) -> List[Optional[float]]:
    """Volume-Weighted Average Price — 累计典型价 × 量的加权均价。

    typical_price = (high + low + close) / 3
    VWAP = Σ(typical × vol) / Σ(vol)

    用途: 机构进场参考价, 价格 > VWAP = 多头占优。
    """
    if not candles:
        return []
    out: List[Optional[float]] = []
    cum_tp_vol = 0.0
    cum_vol = 0.0
    for c in candles:
        h, l, cl, v = _ohlcv(c)
        if h is None or l is None or cl is None or v is None or v == 0:
            out.append(None)
            continue
        tp = (h + l + cl) / 3.0
        cum_tp_vol += tp * v
        cum_vol += v
        out.append(round(cum_tp_vol / cum_vol, 4) if cum_vol else None)
    return out


def ichimoku(candles: Sequence[Any], *,
             tenkan: int = 9, kijun: int = 26, senkou_b: int = 52, displacement: int = 26
             ) -> Dict[str, List[Optional[float]]]:
    """一目均衡表 (Ichimoku Cloud) — 5 条线。

    返回:
      tenkan:  转换线 (9 周期中位价)
      kijun:   基准线 (26 周期中位价)
      senkou_a: 先行带 A (=(tenkan+kijun)/2, 前移 26)
      senkou_b: 先行带 B (52 周期中位价, 前移 26)
      chikou:  迟行线 (收盘价后移 26)
    价格 > 云上沿 = 多头趋势; 价格 < 云下沿 = 空头趋势; 云内 = 震荡。
    """
    if not candles:
        return {"tenkan": [], "kijun": [], "senkou_a": [], "senkou_b": [], "chikou": []}
    hs = highs(candles)
    ls = lows(candles)
    cs = closes(candles)

    def _mid(period: int, idx: int) -> Optional[float]:
        if idx < period - 1:
            return None
        h_max = max(hs[idx - period + 1:idx + 1])
        l_min = min(ls[idx - period + 1:idx + 1])
        return (h_max + l_min) / 2.0

    tenkan_s: List[Optional[float]] = [_mid(tenkan, i) for i in range(len(candles))]
    kijun_s:  List[Optional[float]] = [_mid(kijun, i)  for i in range(len(candles))]
    senkou_b_s: List[Optional[float]] = [_mid(senkou_b, i) for i in range(len(candles))]
    # senkou_a = (tenkan + kijun)/2, 前移 displacement
    senkou_a_raw: List[Optional[float]] = []
    for i in range(len(candles)):
        if tenkan_s[i] is not None and kijun_s[i] is not None:
            senkou_a_raw.append((tenkan_s[i] + kijun_s[i]) / 2.0)
        else:
            senkou_a_raw.append(None)
    senkou_a: List[Optional[float]] = [None] * displacement + senkou_a_raw[:len(candles) - displacement]
    senkou_b: List[Optional[float]] = [None] * displacement + senkou_b_s[:len(candles) - displacement]
    chikou:   List[Optional[float]] = cs + [None] * displacement
    chikou = chikou[displacement:]
    return {
        "tenkan": tenkan_s,
        "kijun": kijun_s,
        "senkou_a": senkou_a,
        "senkou_b": senkou_b,
        "chikou": chikou,
    }


def stoch_rsi(closes: Sequence[Any], period: int = 14, smooth_k: int = 3, smooth_d: int = 3
              ) -> Dict[str, List[Optional[float]]]:
    """Stochastic RSI — 在 RSI 上再做一次随机指标。

    返回 {k, d}: %K 是 Stoch(RSI, period) 的 smooth_k SMA,
                  %D 是 %K 的 smooth_d SMA。
    比纯 RSI 更敏感(0-100 范围), 默认 <20 超卖, >80 超买。
    """
    rsi_s = rsi(closes, period=period)
    if not rsi_s:
        return {"k": [], "d": []}
    # 把 RSI 序列当 close, 高低 = 自身
    k_raw: List[Optional[float]] = []
    for i in range(len(rsi_s)):
        if i < period - 1 or rsi_s[i] is None:
            k_raw.append(None)
            continue
        window = [rsi_s[j] for j in range(i - period + 1, i + 1) if rsi_s[j] is not None]
        if len(window) < period:
            k_raw.append(None)
            continue
        lo = min(window)
        hi = max(window)
        if hi == lo:
            k_raw.append(50.0)
        else:
            k_raw.append(round((rsi_s[i] - lo) / (hi - lo) * 100, 2))
    # 平滑 K (SMA) 与 D
    def _sma(seq: Sequence[Optional[float]], n: int) -> List[Optional[float]]:
        out: List[Optional[float]] = []
        for i in range(len(seq)):
            if i < n - 1:
                out.append(None)
                continue
            window = [seq[j] for j in range(i - n + 1, i + 1) if seq[j] is not None]
            out.append(round(sum(window) / len(window), 2) if window else None)
        return out
    k_s = _sma(k_raw, smooth_k)
    d_s = _sma(k_s, smooth_d)
    return {"k": k_s, "d": d_s}


# ──────────────────────────────────────────────────────────────────────────────
# v188+: 技术指标注册表 + IndicatorPipeline (零破坏, 旧 `from technical_indicators import rsi` 仍可用)
# 设计:
#   - `INDICATORS` 是 {name: callable} dict, 程序化遍历所有指标用;
#   - `register(name, category)` 装饰器可让新指标声明后自动入表;
#   - `IndicatorPipeline().add(name).add(name2).run(candles)` 链式计算。
# ──────────────────────────────────────────────────────────────────────────────
from typing import Callable


class _IndicatorMeta:
    """单个指标的元数据(name + category + func + params 元信息)。"""
    __slots__ = ("name", "category", "func", "default_params")

    def __init__(self, name: str, category: str, func: Callable, default_params: Optional[Dict[str, Any]] = None):
        self.name = name
        self.category = category
        self.func = func
        self.default_params = default_params or {}


INDICATORS: Dict[str, _IndicatorMeta] = {}


def register(name: str, category: str = "misc", **default_params) -> Callable:
    """装饰器: 把函数注册到 INDICATORS[name]。
    示例:
        @register("rsi", category="momentum", period=14)
        def rsi(values, period=14): ...
    """
    def deco(fn: Callable) -> Callable:
        INDICATORS[name] = _IndicatorMeta(name, category, fn, dict(default_params))
        return fn
    return deco


# 批量注册现有 30+ 指标(全部为纯函数, 无破坏性)。
def _bootstrap_registry() -> None:
    """扫描模块级函数, 自动归类入 INDICATORS。运行期只调一次。"""
    if INDICATORS:
        return
    category_map = {
        "ema": "trend", "sma": "trend", "ma": "trend", "ma_stack": "trend", "ma_cross": "trend",
        "ichimoku": "trend",
        "rsi": "momentum", "rsi_divergence": "momentum", "macd": "momentum",
        "macd_cross": "momentum", "kdj": "momentum", "kdj_trip": "momentum",
        "mfi": "momentum", "mfi_trip": "momentum", "stoch_rsi": "momentum",
        "atr": "volatility", "bollinger": "volatility", "bollinger_state": "volatility",
        "bollinger_break": "volatility",
        "volume_regime": "volume", "vol_zscore": "volume", "obv": "volume", "vwap": "volume",
        "candle_pattern": "pattern", "atr_break": "pattern",
        "oi_change_pct": "derivatives", "relative_strength": "relative",
    }
    g = globals()
    for fname, cat in category_map.items():
        fn = g.get(fname)
        if callable(fn):
            INDICATORS[fname] = _IndicatorMeta(fname, cat, fn, {})


_bootstrap_registry()


class IndicatorPipeline:
    """链式指标计算管线 — 一次遍历多 timeframe 蜡烛, 按顺序计算若干指标。"""
    def __init__(self):
        self._chain: List[_IndicatorMeta] = []

    def add(self, name: str, **override_params) -> "IndicatorPipeline":
        if name not in INDICATORS:
            raise KeyError(f"unknown indicator: {name}")
        meta = INDICATORS[name]
        meta2 = _IndicatorMeta(meta.name, meta.category, meta.func, {**meta.default_params, **override_params})
        self._chain.append(meta2)
        return self

    def run(self, *args: Any, **kwargs: Any) -> Dict[str, Any]:
        """按链顺序计算所有指标。返回 {name: value} dict。
        多 timeframe 时把 candles 改为 dict[timeframe, list] 自动逐 tf 计算。
        """
        results: Dict[str, Any] = {}
        first = args[0] if args else None
        is_multi_tf = isinstance(first, dict)
        for meta in self._chain:
            try:
                if is_multi_tf:
                    out_tf: Dict[str, Any] = {}
                    for tf, cndls in first.items():
                        v = meta.func(cndls, *args[1:], **meta.default_params, **kwargs)
                        out_tf[tf] = v
                    results[meta.name] = out_tf
                else:
                    results[meta.name] = meta.func(*args, **meta.default_params, **kwargs)
            except Exception as exc:
                results[meta.name] = {"error": f"{type(exc).__name__}: {exc}"}
        return results


def list_indicators(category: Optional[str] = None) -> List[Dict[str, Any]]:
    """列出全部指标元信息; 可按 category 过滤。"""
    if category is None:
        return [{"name": m.name, "category": m.category} for m in INDICATORS.values()]
    return [{"name": m.name, "category": m.category}
            for m in INDICATORS.values() if m.category == category]