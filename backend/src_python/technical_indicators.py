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


# 成交量有效覆盖率下限（v480）。低于此值，所有量价指标一律判「不可用」。
# 为什么必须有这道门：COMEX 日线（XAU/XAG/HG）的 volume 系统性缺失 ——
# 实测 XAU 95.4% 为空、HG 100% 为空。而 `volumes()` 用 `_clean` 把空值过滤掉，
# 返回的是**压缩后的稀疏序列**，长度与 K 线对不上：
#   - OBV 按 `range(1, len(candles))` 索引压缩序列 → 直接 IndexError；
#   - MFI/VWAP/vol_zscore 却会用 120 个跨越十年的稀疏点算出「看起来正常」的数
#     （实测 XAU MFI=24.14，会被下游读成「超卖」）。
# 静默给错数比崩更贵，所以这里宁可不给。
VOLUME_MIN_COVERAGE = 0.80


def volumes_full(candles: Sequence[Any]) -> List[Optional[float]]:
    """**保留长度**的成交量序列（无效位置为 None）。

    与 `volumes()` 的区别：后者压缩空值，适合"只关心有效值"的场景；
    任何需要与 K 线逐根对齐的计算（OBV 累加、VWAP 累计）必须用这个。
    """
    out: List[Optional[float]] = []
    for c in candles:
        v = _f(c[VOL]) if (isinstance(c, (list, tuple)) and len(c) > VOL) else None
        out.append(v if (v is not None and v > 0) else None)
    return out


def volume_availability(candles: Sequence[Any],
                        min_coverage: float = VOLUME_MIN_COVERAGE) -> Dict[str, Any]:
    """成交量可用性体检 —— 量价指标的前置门。

    返回 available（覆盖率是否达标）、coverage_pct、n_valid、n_null、n_zero、n。
    下游拿到 available=False 时应当如实说「该标的没有成交量」，不要用价格类指标替代。
    """
    n = len(candles)
    vs = volumes_full(candles)
    valid = sum(1 for v in vs if v is not None)
    n_null = sum(1 for c in candles
                 if not (isinstance(c, (list, tuple)) and len(c) > VOL) or c[VOL] is None)
    n_zero = n - valid - n_null
    cov = (valid / n) if n else 0.0
    ok = bool(n and valid and cov >= min_coverage)
    return {
        "available": ok,
        "coverage_pct": round(cov * 100.0, 1),
        "n_valid": valid,
        "n_null": max(0, n_null),
        "n_zero": max(0, n_zero),
        "n": n,
        "min_coverage_pct": round(min_coverage * 100.0, 1),
        "note": (None if ok else
                 f"成交量有效覆盖 {round(cov * 100, 1)}% < {round(min_coverage * 100)}%"
                 f"（空 {max(0, n_null)} 行 / 零值 {max(0, n_zero)} 行），"
                 "量价类指标（OBV/MFI/VWAP/量能 Z/量价关系）不计算"),
    }


# 无成交量时必须整体跳过的指标（volume_profile 有 TPO 兜底，故意不在此列）
_VOLUME_GATED = {"volume_regime", "vol_zscore", "obv", "vwap", "mfi", "mfi_trip", "vol_break"}


def pipeline_skip_reason(name: str, candles: Any) -> Optional[Dict[str, Any]]:
    """管线级跳过判据：无量时不要白算一遍再返回 None。

    返回 None 表示"照常计算"；返回 dict 表示"跳过"，dict 会直接进结果，
    下游能读到 `skipped=no_volume` 和覆盖率，而不是拿到一个裸 None 去猜原因。
    """
    if name not in _VOLUME_GATED:
        return None
    if not isinstance(candles, (list, tuple)) or not candles:
        return None
    va = volume_availability(candles)
    if va["available"]:
        return None
    return {"skipped": "no_volume", "coverage_pct": va["coverage_pct"],
            "n_null": va["n_null"], "n_zero": va["n_zero"], "note": va["note"]}


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
    va = volume_availability(candles)
    base: Dict[str, Any] = {"regime": "样本不足", "price_up": None, "vol_ratio": None,
                            "divergence": None, "available": True}
    if len(cs) < 2:
        return base
    if not va["available"]:
        # v480：以前这里会拿压缩后的稀疏量算出「放量/缩量」标签（XAU 实测给出
        # 「温和下跌、vol_ratio 1.1567」），看起来完全正常但口径是错的。
        return {**base, "regime": "成交量不可用", "available": False,
                "coverage_pct": va["coverage_pct"], "note": va["note"]}
    vs = [v for v in volumes_full(candles) if v is not None]
    if len(vs) < 2:
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
        "available": True,
        "coverage_pct": va["coverage_pct"],
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

    v480：成交量覆盖不足时整体返回 None（不再用零散的有效量凑窗口）。
    """
    n = len(candles)
    if not volume_availability(candles)["available"]:
        return [None] * n
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

    v480：改用 `volumes_full()` 逐根对齐 + 覆盖率门。旧版 `_clean` 会压缩空值，
    把「跨越十年的 120 个稀疏点」当成连续窗口算 Z，得出看似正常实则无意义的数。
    """
    if not volume_availability(candles)["available"]:
        return None
    vols = [v for v in volumes_full(candles)[:-1] if v is not None]
    cur = volumes_full(candles)[-1] if candles else None
    if len(vols) < lookback or cur is None:
        return None
    window = vols[-lookback:]
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
    if not volume_availability(candles)["available"]:
        return None
    vs = volumes_full(candles)
    if len(vs) < lookback + 1:
        return None
    win = [v for v in vs[-(lookback + 1):-1] if v is not None]
    cur = vs[-1]
    if len(win) < lookback or cur is None:
        return None
    window = win
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
        "volume_availability": volume_availability(candles),
        "oi_change_pct": oi_change_pct(oi_now, oi_prev),
        "relative_strength": relative_strength(asset_price, base_price, prev_asset, prev_base),
    }


# ──────────────────────────────────────────────────────────────────────────────
# v189-3: 新增 4 个指标 (OBV / VWAP / Ichimoku / Stochastic RSI)
# 全部纯函数, 注册到 INDICATORS 后即可被 IndicatorPipeline 链式调用。
# ──────────────────────────────────────────────────────────────────────────────


def obv(candles: Sequence[Any]) -> List[Optional[float]]:
    """On-Balance Volume — 量价配合: 价格涨 + 量 → OBV 涨; 价格跌 + 量 → OBV 跌。

    用途: 趋势确认(OBV 与价格背离 = 趋势减弱信号)。
    返回 OBV 序列(从 0 开始累加)，长度与 candles 一致；成交量不可用时全 None。

    v480 修两个坑：
      1. 旧版 `vs = volumes(candles)` 拿到的是**压缩后**的序列，再按
         `range(1, len(candles))` 索引 → XAU（2623 根、只有 120 个有效量）直接 IndexError；
      2. 改为 `volumes_full()` 逐根对齐，无效量为 0（不累加），并加覆盖率门。
    """
    if not candles:
        return []
    n = len(candles)
    if not volume_availability(candles)["available"]:
        return [None] * n
    cs = closes(candles)
    vs = volumes_full(candles)
    out: List[Optional[float]] = [0.0]
    cur = 0.0
    for i in range(1, n):
        v = vs[i] or 0.0
        if i < len(cs) and cs[i] > cs[i - 1]:
            cur += v
        elif i < len(cs) and cs[i] < cs[i - 1]:
            cur -= v
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


def vwap(candles: Sequence[Any]) -> List[Optional[float]]:
    """Volume-Weighted Average Price — 累计典型价 × 量的加权均价。

    typical_price = (high + low + close) / 3
    VWAP = Σ(typical × vol) / Σ(vol)

    用途: 机构进场参考价, 价格 > VWAP = 多头占优。

    v480：成交量覆盖不足时返回全 None。旧版逐根跳过缺量 bar 但继续累加，
    输出会变成「只有几个非 None 点、且这些点跨越很久」的伪 VWAP（实测 XAU 末值 4216，
    看着像正常价格，实际是把 120 个稀疏量按十年跨度加权）。
    """
    if not candles:
        return []
    if not volume_availability(candles)["available"]:
        return [None] * len(candles)
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
# v343: 市场状态补全 —— 趋势强度 / 筹码结构 / 博弈结构 / 波动情绪
#
# 补齐此前**完全缺失**的四类能力（原有库只有均线交叉、单根形态，判不出"多强"）：
#   1. 趋势强度：adx / aroon / supertrend / donchian / ma_slope
#   2. 筹码结构：volume_profile / swing_structure
#   3. 博弈结构：percentile_rank / oi_price_quadrant / positioning_divergence /
#                taker_pressure / cvd_price_divergence
#   4. 波动情绪：realized_vol / bollinger_width / rsi_stretch
#   5. 聚合读数：market_state（六维读数 + 一句话结论，缺维度进 missing 不造数）
#
# 约束与全文一致：纯函数、零依赖、样本不足返回 None、绝不抛异常。
# 所有阈值均可配置，默认值为行业惯例，便于后续按资产分组调参。
# ──────────────────────────────────────────────────────────────────────────────


# --------------------------------------------------------------------------
# 趋势强度
# --------------------------------------------------------------------------

def adx(candles: Sequence[Any], period: int = 14) -> Dict[str, List[Optional[float]]]:
    """Wilder ADX / +DI / -DI —— 回答"趋势有多强"，而不只是"往哪走"。

    均线交叉只能给出方向，ADX 给出**强度**：
      ADX < 20  无趋势（震荡，均值回归策略有效）
      20-40     趋势成立
      > 40      强趋势（此时逆势做回归是主要亏损来源）

    +DI > -DI 多头主导，反之空头主导；两者同时走低说明方向在收敛。
    计算：TR/+DM/-DM → Wilder 平滑 → DI → DX → ADX 再平滑，
    ADX 首值落在第 2*period-1 根，之前全部 None（不造数）。
    """
    hs, ls, cs = highs(candles), lows(candles), closes(candles)
    n = min(len(hs), len(ls), len(cs))
    out: Dict[str, List[Optional[float]]] = {
        "adx": [None] * n, "plus_di": [None] * n, "minus_di": [None] * n,
    }
    if n < period + 1:
        return out
    tr: List[float] = [0.0] * n
    pdm: List[float] = [0.0] * n
    ndm: List[float] = [0.0] * n
    for i in range(1, n):
        tr[i] = max(hs[i] - ls[i], abs(hs[i] - cs[i - 1]), abs(ls[i] - cs[i - 1]))
        up = hs[i] - hs[i - 1]
        dn = ls[i - 1] - ls[i]
        pdm[i] = up if (up > dn and up > 0) else 0.0
        ndm[i] = dn if (dn > up and dn > 0) else 0.0
    s_tr = sum(tr[1:period + 1])
    s_pdm = sum(pdm[1:period + 1])
    s_ndm = sum(ndm[1:period + 1])
    dx: List[Optional[float]] = [None] * n
    for i in range(period, n):
        if i > period:
            s_tr = s_tr - s_tr / period + tr[i]
            s_pdm = s_pdm - s_pdm / period + pdm[i]
            s_ndm = s_ndm - s_ndm / period + ndm[i]
        pdi = 100.0 * s_pdm / s_tr if s_tr else 0.0
        ndi = 100.0 * s_ndm / s_tr if s_tr else 0.0
        out["plus_di"][i] = round(pdi, 2)
        out["minus_di"][i] = round(ndi, 2)
        denom = pdi + ndi
        # 双向都无位移（横盘/内包 K 线）时 DX 无定义，记 0 而不是 100——
        # 否则"没有趋势"会被算成"极强趋势"，这是 ADX 最常见的实现错误。
        # 阈值取 0.01（DI 以百分点为单位，真实趋势的 DI 量级是 O(10)）：
        # 首根残留的 ±DM 会按 (period-1)/period 衰减到 1e-6 级别，
        # 用 1e-9 判零会让这点残渣把 DX 顶成 100。
        dx[i] = round(100.0 * abs(pdi - ndi) / denom, 4) if denom > 0.01 else 0.0
    first = 2 * period - 1
    if first >= n:
        return out
    window = [x for x in dx[period:first + 1] if x is not None]
    if not window:
        return out
    avg_dx = sum(window) / len(window)
    out["adx"][first] = round(avg_dx, 2)
    for i in range(first + 1, n):
        d = dx[i]
        if d is None:
            continue
        avg_dx = (avg_dx * (period - 1) + d) / period
        out["adx"][i] = round(avg_dx, 2)
    return out


def aroon(candles: Sequence[Any], period: int = 25) -> Dict[str, List[Optional[float]]]:
    """Aroon —— 用"多久没创新高/新低"量度趋势的新鲜度。

    AroonUp = 100 * (period - 距上次最高价的根数) / period，AroonDown 同理。
    Up 贴近 100 / Down 贴近 0 → 上升趋势仍在推进；
    两者都在 50 附近来回 → 无方向，典型的震荡市。
    """
    hs, ls = highs(candles), lows(candles)
    n = min(len(hs), len(ls))
    out: Dict[str, List[Optional[float]]] = {"up": [None] * n, "down": [None] * n}
    if n < period + 1:
        return out
    for i in range(period, n):
        wh = hs[i - period:i + 1]
        wl = ls[i - period:i + 1]
        hi_idx = len(wh) - 1 - wh[::-1].index(max(wh))   # 取最近一次出现的极值
        lo_idx = len(wl) - 1 - wl[::-1].index(min(wl))
        out["up"][i] = round(100.0 * hi_idx / period, 2)
        out["down"][i] = round(100.0 * lo_idx / period, 2)
    return out


def supertrend(candles: Sequence[Any], period: int = 10,
               multiplier: float = 3.0) -> Dict[str, List[Optional[float]]]:
    """Supertrend —— ATR 自适应止损/趋势轨，回答"这段趋势什么时候算结束"。

    line 为当前有效轨（多头时是下轨，空头时是上轨），direction = 1 / -1。
    与固定百分比止损不同，它随波动自动放宽/收紧，适合跨资产统一口径。
    """
    hs, ls, cs = highs(candles), lows(candles), closes(candles)
    n = min(len(hs), len(ls), len(cs))
    out: Dict[str, List[Optional[float]]] = {"line": [None] * n, "direction": [None] * n}
    if n < period + 1:
        return out
    a = atr(candles, period)
    if len(a) < n:
        a = list(a) + [None] * (n - len(a))
    start = next((i for i in range(n) if a[i] is not None), None)
    if start is None:
        return out
    fub: Optional[float] = None
    flb: Optional[float] = None
    dirn: Optional[int] = None
    for i in range(start, n):
        hl2 = (hs[i] + ls[i]) / 2.0
        bub = hl2 + multiplier * a[i]
        blb = hl2 - multiplier * a[i]
        if fub is None or flb is None:
            fub, flb = bub, blb
            dirn = 1 if cs[i] >= hl2 else -1
        else:
            prev_close = cs[i - 1]
            # 上轨只降不升（除非前收已站上），下轨只升不降
            fub = bub if (bub < fub or prev_close > fub) else fub
            flb = blb if (blb > flb or prev_close < flb) else flb
            if dirn == 1 and cs[i] < flb:
                dirn = -1
            elif dirn == -1 and cs[i] > fub:
                dirn = 1
        out["direction"][i] = dirn
        out["line"][i] = round(flb if dirn == 1 else fub, 6)
    return out


def donchian(candles: Sequence[Any], period: int = 20) -> Dict[str, List[Optional[float]]]:
    """Donchian 通道 + 价格在通道中的位置（0=下轨，1=上轨）。

    position 比"突破没突破"更有信息量：0.9 以上说明价格贴着区间上沿运行，
    在趋势里是持有信号、在震荡里是回落前兆——需要 ADX 来区分。
    """
    hs, ls, cs = highs(candles), lows(candles), closes(candles)
    n = min(len(hs), len(ls), len(cs))
    out: Dict[str, List[Optional[float]]] = {
        "upper": [None] * n, "mid": [None] * n, "lower": [None] * n, "position": [None] * n,
    }
    if n < period:
        return out
    for i in range(period - 1, n):
        wh = max(hs[i - period + 1:i + 1])
        wl = min(ls[i - period + 1:i + 1])
        out["upper"][i] = round(wh, 6)
        out["lower"][i] = round(wl, 6)
        out["mid"][i] = round((wh + wl) / 2.0, 6)
        out["position"][i] = round((cs[i] - wl) / (wh - wl), 4) if wh > wl else 0.5
    return out


def ma_slope(values: Sequence[Any], period: int = 20, lookback: int = 5) -> Optional[float]:
    """均线斜率（% / 根，按均线自身水平归一）。

    回答"趋势陡不陡"。同样 ADX 30，斜率 0.05%/根 和 0.5%/根 是两种完全不同的行情：
    前者是慢牛可以拿，后者是加速段通常伴随回撤风险。
    """
    cs = _clean(_auto_values(values))
    if len(cs) < period + lookback:
        return None
    avg_now = sum(cs[-period:]) / period
    avg_prev = sum(cs[-period - lookback:-lookback]) / period
    if avg_prev == 0:
        return None
    return round((avg_now - avg_prev) / abs(avg_prev) / lookback * 100.0, 4)


# --------------------------------------------------------------------------
# 筹码结构
# --------------------------------------------------------------------------

def volume_profile(candles: Sequence[Any], *, lookback: int = 120, bins: int = 24,
                   value_area: float = 0.70) -> Dict[str, Any]:
    """Volume Profile —— POC / 价值区（VAH-VAL）/ 价格相对价值区位置。

    把 lookback 根 K 线的成交量按价格分箱累加（每根按其 high-low 跨度等比摊到各箱），
    POC = 成交最密集的价位（多空换手成本最集中的地方），
    价值区 = 从 POC 向两侧扩展到累计 70% 成交量的区间。

    读法：价格在价值区上方 = 上方套牢盘少、回踩 VAH 有支撑；
          价格在价值区下方 = 上方全是套牢盘，反弹到 VAL 容易遇抛压。
    """
    bars: List[tuple] = []
    tpo_bars: List[tuple] = []      # 没有成交量、但有完整 OHLC 的 bar（TPO 兜底）
    for c in candles[-lookback:]:
        if not isinstance(c, (list, tuple)) or len(c) < 6:
            continue
        h, lo, cl, v = _f(c[HIGH]), _f(c[LOW]), _f(c[CLOSE]), _f(c[VOL])
        if None in (h, lo, cl):
            continue
        tpo_bars.append((h, lo, cl, 1.0))
        if v is None or v <= 0:
            continue          # 成交量缺失 ≠ 这根 bar 不存在（贵金属日线就没有成交量）
        bars.append((h, lo, cl, v))
    empty = {"poc": None, "vah": None, "val": None, "position": None,
             "poc_dist_pct": None, "bars": 0, "bins": bins, "weight": None}
    weight = "volume"
    if len(bars) < 10:
        # v409：成交量整体缺失（COMEX 日线、外盘指数）时退到 **TPO**（Time Price Opportunity）：
        #   每根 bar 按其 high-low 区间等权计入，问的是"价格在哪些价位被反复交易过"，
        #   而不是"哪些价位成交量大"。两种口径的 POC 都叫 POC，但必须标清楚是哪一种，
        #   否则会被当成"有成交量的价值区"误用。
        if len(tpo_bars) < 10:
            return empty
        bars = tpo_bars
        weight = "tpo"
    hi = max(b[0] for b in bars)
    lo = min(b[1] for b in bars)
    if hi <= lo:
        return empty
    step = (hi - lo) / bins
    vol_bins = [0.0] * bins
    for h, l_, _cl, v in bars:
        b0 = min(bins - 1, max(0, int((l_ - lo) / step)))
        b1 = min(bins - 1, max(0, int((h - lo) / step)))
        span = b1 - b0 + 1
        per = v / span
        for b in range(b0, b1 + 1):
            vol_bins[b] += per
    total = sum(vol_bins)
    if total <= 0:
        return empty
    poc_idx = vol_bins.index(max(vol_bins))
    target = total * value_area
    acc = vol_bins[poc_idx]
    l_idx = r_idx = poc_idx
    while acc < target and (l_idx > 0 or r_idx < bins - 1):
        left = vol_bins[l_idx - 1] if l_idx > 0 else -1.0
        right = vol_bins[r_idx + 1] if r_idx < bins - 1 else -1.0
        if right >= left:
            r_idx += 1
            acc += vol_bins[r_idx]
        else:
            l_idx -= 1
            acc += vol_bins[l_idx]
    poc = lo + (poc_idx + 0.5) * step
    val = lo + l_idx * step
    vah = lo + (r_idx + 1) * step
    last = bars[-1][2]
    if last > vah:
        position = "above_value"
    elif last < val:
        position = "below_value"
    else:
        position = "in_value"
    return {
        "poc": round(poc, 6),
        "vah": round(vah, 6),
        "val": round(val, 6),
        "position": position,
        "poc_dist_pct": round((last - poc) / poc * 100.0, 4) if poc else None,
        "bars": len(bars),
        "bins": bins,
        "weight": weight,
    }


def swing_structure(candles: Sequence[Any], *, left: int = 2, right: int = 2,
                    max_swings: int = 4) -> Dict[str, Any]:
    """摆动结构 —— HH/HL/LH/LL，趋势的"骨架"而不是指标值。

    最近的高点在抬高、低点也在抬高（HH_HL）= 上升结构成立；
    高点降低、低点也降低（LH_LL）= 下降结构；
    高低点不一致（HH_LL / LH_HL）= 扩张或转折中的混乱结构。

    与均线交叉相比，它不会被一根大阳线立刻改写，更适合判断"趋势有没有坏"。
    """
    hs = [_f(c[HIGH]) if isinstance(c, (list, tuple)) and len(c) > HIGH else None for c in candles]
    ls = [_f(c[LOW]) if isinstance(c, (list, tuple)) and len(c) > LOW else None for c in candles]
    n = min(len(hs), len(ls))
    highs_idx: List[float] = []
    lows_idx: List[float] = []
    for i in range(left, n - right):
        lwin_h = hs[i - left:i]
        rwin_h = hs[i + 1:i + right + 1]
        lwin_l = ls[i - left:i]
        rwin_l = ls[i + 1:i + right + 1]
        if any(x is None for x in lwin_h + rwin_h + lwin_l + rwin_l):
            continue
        # 左右分开比较：左侧严格更高、右侧允许等高（平台/双顶时取第一根），
        # 若改用"整窗最大值唯一"这种写法，遇到等高会被整根丢掉，摆动点直接漏检。
        if hs[i] > max(lwin_h) and hs[i] >= max(rwin_h):
            highs_idx.append(round(hs[i], 6))
        if ls[i] < min(lwin_l) and ls[i] <= min(rwin_l):
            lows_idx.append(round(ls[i], 6))
    out_highs = highs_idx[-max_swings:]
    out_lows = lows_idx[-max_swings:]
    result: Dict[str, Any] = {"highs": out_highs, "lows": out_lows,
                              "structure": None, "trend": None}
    if len(out_highs) < 2 or len(out_lows) < 2:
        return result
    # 只用最近 3 个摆动点（两次比较）判定当前结构：更早的摆动属于上一段行情，
    # 混进来会把一段清晰的趋势判成 mixed——实测 BTC 4h 240 根就是这样丢掉类型的。
    use_h = out_highs[-3:]
    use_l = out_lows[-3:]
    hh = all(b > a for a, b in zip(use_h, use_h[1:]))
    hl = all(b > a for a, b in zip(use_l, use_l[1:]))
    lh = all(b < a for a, b in zip(use_h, use_h[1:]))
    ll = all(b < a for a, b in zip(use_l, use_l[1:]))
    if hh and hl:
        result.update(structure="HH_HL", trend="up")
    elif lh and ll:
        result.update(structure="LH_LL", trend="down")
    elif hh and ll:
        result.update(structure="HH_LL", trend="expanding")
    elif lh and hl:
        result.update(structure="LH_HL", trend="contracting")
    else:
        result.update(structure="mixed", trend="range")
    result["swings_used"] = len(use_h) + len(use_l)
    return result


# --------------------------------------------------------------------------
# 博弈结构（持仓 / 主动性 / 分歧）
# --------------------------------------------------------------------------

def percentile_rank(value: Any, history: Sequence[Any]) -> Optional[float]:
    """当前值在历史分布中的分位（0-100，中位平局算 0.5 份）。

    把"资金费率 0.03%"这种绝对值转成"处于历史 92% 分位"，
    才能跨资产、跨时间比较拥挤程度。样本少于 2 条返回 None。
    """
    v = _f(value)
    if v is None:
        return None
    hist = _clean(history or [])
    if len(hist) < 2:
        return None
    if v <= min(hist):
        return 0.0
    if v >= max(hist):
        return 100.0
    below = sum(1 for x in hist if x < v)
    equal = sum(1 for x in hist if x == v)
    return round((below + 0.5 * equal) / len(hist) * 100.0, 2)


def oi_price_quadrant(price_change_pct: Any, oi_change_pct: Any, *,
                      price_eps: float = 0.3, oi_eps: float = 1.0) -> Dict[str, Any]:
    """持仓量 × 价格四象限 —— 判断这波涨跌是"新钱进场"还是"平仓推动"。

      增仓上涨：新多进场，趋势有燃料，回撤通常可接；
      减仓上涨：空头回补，缺新增买盘，涨得快也可能断得快；
      增仓下跌：新空进场，下跌有增量资金支持；
      减仓下跌：多头止损/爆仓离场，抛压来自平仓而非新空，往往跌得急但容易V。

    这是"谁在做"最接近的可量化替代：看不到账户，但看得到钱是在进还是出。
    """
    p = _f(price_change_pct)
    o = _f(oi_change_pct)
    if p is None or o is None:
        return {"label": None, "bias": None, "note": None, "strength": None}
    if abs(p) < price_eps and abs(o) < oi_eps:
        return {"label": "双向观望", "bias": "neutral",
                "note": "价格与持仓都在阈值内波动，没有新增资金方向。", "strength": 0.0}
    if p >= 0 and o >= 0:
        label, bias = "增仓上涨", "bull"
        note = "新多进场推动上涨，趋势有增量资金支撑。"
    elif p >= 0 > o:
        label, bias = "减仓上涨", "bull_weak"
        note = "空头回补推动，缺少新增买盘，持续性通常弱于增仓上涨。"
    elif p < 0 <= o:
        label, bias = "增仓下跌", "bear"
        note = "新空进场推动下跌，下跌获得增量资金支持。"
    else:
        label, bias = "减仓下跌", "bear_weak"
        note = "多头止损/爆仓离场，抛压来自平仓而非新空，常见急跌后反抽。"
    strength = round(min(1.0, (abs(p) / 3.0 + abs(o) / 8.0)), 4)
    return {"label": label, "bias": bias, "note": note, "strength": strength}


def positioning_divergence(top_long_short_ratio: Any, retail_long_short_ratio: Any, *,
                           markdown: float = 0.8) -> Dict[str, Any]:
    """大户 vs 全体账户的多空比分歧 —— 散户在追还是在撤。

    top 是持仓量前 20% 账户的多空比，retail 是全体账户多空比。
    两者同向且都 > 1 = 一致看多（拥挤）；
    retail 明显高于 top = 散户在追、大户相对克制（往往是反向信号）；
    top 明显高于 retail = 筹码集中在大户手里，散户不参与。
    """
    t = _f(top_long_short_ratio)
    r = _f(retail_long_short_ratio)
    if t is None or r is None or r <= 0:
        return {"label": None, "bias": None, "gap_pct": None}
    gap = (r - t) / t * 100.0
    if t >= 1 and r >= 1:
        if r >= t * (1.0 / markdown if markdown else 1.25):
            label, bias = "散户追多、大户相对克制", "bear"
        elif t >= r * (1.0 + (1.0 - markdown)):
            label, bias = "筹码集中在大户多头", "bull"
        else:
            label, bias = "多空一致偏多", "bull_weak"
    elif t < 1 and r < 1:
        if r <= t * (1.0 / markdown if markdown else 1.25):
            label, bias = "散户追空、大户相对克制", "bull"
        elif t <= r * (1.0 + (1.0 - markdown)):
            label, bias = "筹码集中在大户空头", "bear"
        else:
            label, bias = "多空一致偏空", "bear_weak"
    elif t > 1 >= r:
        label, bias = "大户偏多、散户偏空", "bull"
    else:
        label, bias = "大户偏空、散户偏多", "bear"
    return {"label": label, "bias": bias, "gap_pct": round(gap, 2)}


def taker_pressure(buy_volume: Any, sell_volume: Any, *,
                   strong: float = 0.58, mild: float = 0.53) -> Dict[str, Any]:
    """主动成交方向（taker buy vs sell）—— 谁在"吃单"。

    被动挂单看不出意图，主动成交才算数：
      > 0.58 强主动买（追价买入，通常是突破/逼空）
      > 0.53 主动买占优
      0.47-0.53 均衡（挂单博弈，方向未定）
      < 0.47 主动卖占优；< 0.42 强主动卖（砸盘/止损连锁）
    """
    b = _f(buy_volume)
    s = _f(sell_volume)
    if b is None or s is None or b + s <= 0:
        return {"label": None, "bias": None, "buy_ratio": None, "strength": None}
    ratio = b / (b + s)
    if ratio >= strong:
        label, bias = "强主动买", "bull"
    elif ratio >= mild:
        label, bias = "主动买占优", "bull_weak"
    elif ratio <= 1.0 - strong:
        label, bias = "强主动卖", "bear"
    elif ratio <= 1.0 - mild:
        label, bias = "主动卖占优", "bear_weak"
    else:
        label, bias = "主动成交均衡", "neutral"
    return {"label": label, "bias": bias, "buy_ratio": round(ratio, 4),
            "strength": round(abs(ratio - 0.5) * 2.0, 4)}


def cvd_price_divergence(cvd: Sequence[Any], closes: Sequence[Any], *,
                         lookback: int = 20, min_move_pct: float = 0.2) -> Dict[str, Any]:
    """CVD（累计主动买卖差）与价格的背离 —— 涨得虚不虚。

    价涨、CVD 不涨（甚至下降）→ 主动买盘没跟上，上涨靠挂单承接，容易回落；
    价跌、CVD 上升 → 有人在低位主动吸，跌势可能衰竭。
    CVD 序列与收盘序列需等长对齐（按时间戳截取后传入）。
    """
    cv = [_f(x) for x in cvd][-lookback - 1:]
    cs = [_f(x) for x in closes][-lookback - 1:]
    if len(cv) < 2 or len(cs) < 2 or len(cv) != len(cs):
        return {"divergence": None, "price_change_pct": None, "cvd_change": None}
    if any(x is None for x in cv) or any(x is None for x in cs) or cs[0] == 0:
        return {"divergence": None, "price_change_pct": None, "cvd_change": None}
    price_chg = (cs[-1] - cs[0]) / cs[0] * 100.0
    cvd_chg = cv[-1] - cv[0]
    if abs(price_chg) < min_move_pct:
        div = None
    elif price_chg > 0 and cvd_chg < 0:
        div = "bearish"
    elif price_chg < 0 and cvd_chg > 0:
        div = "bullish"
    else:
        div = "confirmed"
    return {"divergence": div, "price_change_pct": round(price_chg, 4),
            "cvd_change": round(cvd_chg, 4)}


# --------------------------------------------------------------------------
# 波动 / 情绪
# --------------------------------------------------------------------------

def realized_vol(candles: Sequence[Any], period: int = 20) -> List[Optional[float]]:
    """滚动已实现波动（收益率的样本标准差，单位 %，未年化）。

    与 ATR 的区别：ATR 含跳空与振幅，已实现波动只看收盘收益率，
    更纯粹地反映"价格走得多快"。用于和自身历史比出波动分位。
    """
    cs = closes(candles)
    n = len(cs)
    out: List[Optional[float]] = [None] * n
    if n < period + 1:
        return out
    rets: List[float] = [0.0] * n
    for i in range(1, n):
        rets[i] = (cs[i] - cs[i - 1]) / cs[i - 1] * 100.0 if cs[i - 1] else 0.0
    for i in range(period, n):
        w = rets[i - period + 1:i + 1]
        mean = sum(w) / len(w)
        var = sum((x - mean) ** 2 for x in w) / len(w)
        out[i] = round(var ** 0.5, 4)
    return out


def bollinger_width(values: Sequence[Any], period: int = 20,
                    num_std: float = 2.0) -> List[Optional[float]]:
    """布林带宽度（(上轨-下轨)/中轨，%）。

    宽度分位是判断"波动收缩/扩张"最直接的量：
    分位 < 25 = 挤压（变盘前夜，方向未定但幅度会放大）
    分位 > 75 = 扩张（趋势或恐慌进行中，此时追单风险最高）
    """
    cs = _clean(_auto_values(values))
    n = len(cs)
    out: List[Optional[float]] = [None] * n
    if n < period:
        return out
    for i in range(period - 1, n):
        w = cs[i - period + 1:i + 1]
        mid = sum(w) / period
        if not mid:
            continue
        var = sum((x - mid) ** 2 for x in w) / period
        std = var ** 0.5
        out[i] = round((2.0 * num_std * std) / abs(mid) * 100.0, 4)
    return out


def cci(candles: Sequence[Any], period: int = 20) -> List[Optional[float]]:
    """CCI（顺势指标）— 典型价偏离其均值的程度，除以 0.015×平均绝对偏差。

    标准算法：TP=(H+L+C)/3；CCI = (TP - MA(TP)) / (0.015 × MD(TP))。
    窗口满 period 后出值，之前为 None。
    """
    out: List[Optional[float]] = [None] * len(candles)
    tps: List[Optional[float]] = []
    for c in candles:
        h, l, cl, _ = _ohlcv(c)
        tps.append(None if h is None or l is None or cl is None else (h + l + cl) / 3.0)
    n = len(tps)
    if n < period:
        return out
    for i in range(period - 1, n):
        win = tps[i - period + 1:i + 1]
        if any(x is None for x in win):
            continue
        ma_tp = sum(win) / period  # type: ignore[arg-type]
        md = sum(abs(x - ma_tp) for x in win) / period  # type: ignore[union-attr]
        out[i] = 0.0 if md == 0 else round((tps[i] - ma_tp) / (0.015 * md), 4)  # type: ignore[operator]
    return out


def stoch(candles: Sequence[Any], k_period: int = 14, smooth: int = 3
          ) -> List[Optional[float]]:
    """Stochastic %K（原始 %K 的移动平均）。

    标准算法：raw = (C - L_low) / (H_high - L_low) × 100；
    输出对最近 smooth 根 raw 取均值。窗口不足 smooth 根时为 None。
    """
    hs, ls, cs = highs(candles), lows(candles), closes(candles)
    n = min(len(hs), len(ls), len(cs))
    raw: List[Optional[float]] = [None] * n
    out: List[Optional[float]] = [None] * n
    if n < k_period:
        return out
    for i in range(k_period - 1, n):
        wh = max(hs[i - k_period + 1:i + 1])
        wl = min(ls[i - k_period + 1:i + 1])
        raw[i] = 50.0 if wh == wl else (cs[i] - wl) / (wh - wl) * 100.0
    for i in range(n):
        vals = [raw[j] for j in range(max(0, i - smooth + 1), i + 1) if raw[j] is not None]
        if len(vals) == smooth:
            out[i] = round(sum(vals) / smooth, 4)
    return out


def rsi_stretch(values: Sequence[Any], *, period: int = 14, overbought: float = 70.0,
                oversold: float = 30.0) -> Dict[str, Any]:
    """RSI 极值驻留根数 —— 心理状态而非数值。

    RSI 78 持续 1 根 = 短期冲刺；持续 8 根 = 多头情绪长期锁定在高位，
    后者一旦破位，回落的幅度通常大于前者。单看 RSI 数值看不出这个差别。
    """
    cs = _clean(_auto_values(values))
    r = rsi(cs, period)
    side = None
    bars = 0
    for v in reversed(r):
        if v is None:
            break
        if v >= overbought:
            if side in (None, "overbought"):
                side, bars = "overbought", bars + 1
                continue
            break
        if v <= oversold:
            if side in (None, "oversold"):
                side, bars = "oversold", bars + 1
                continue
            break
        break
    return {"side": side, "bars": bars,
            "rsi": round(r[-1], 2) if r and r[-1] is not None else None}


# --------------------------------------------------------------------------
# 市场状态聚合（六维读数）
# --------------------------------------------------------------------------

def market_state(candles: Sequence[Any], *, price_change_pct: Any = None,
                 oi_change_pct: Any = None, funding_rate: Any = None,
                 funding_history: Sequence[Any] | None = None,
                 top_long_short_ratio: Any = None, retail_long_short_ratio: Any = None,
                 taker_buy: Any = None, taker_sell: Any = None,
                 cvd: Sequence[Any] | None = None,
                 cvd_closes: Sequence[Any] | None = None,
                 interval_sec: int = 0) -> Dict[str, Any]:
    """把上面所有指标压成「六维读数 + 一句话」，缺数据的维度进 missing，不造数。

    六维：趋势阶段 / 波动状态 / 价值区位置 / 杠杆结构 / 主动性 / 持仓分歧。
    这是给人和 LLM 看的同一份东西——不再需要各自拼 20 个数字。

    interval_sec 只用于给读数标注周期，不参与计算。
    """
    cs = closes(candles)
    if len(cs) < 2:
        return {"error": "样本不足（至少 2 根 K 线）"}

    missing: List[str] = []
    readout_parts: List[str] = []

    # —— 趋势阶段 ——
    a = adx(candles)
    adx_v = a["adx"][-1] if a["adx"] else None
    adx_prev = a["adx"][-6] if len(a["adx"]) >= 6 else None
    pdi = a["plus_di"][-1] if a["plus_di"] else None
    ndi = a["minus_di"][-1] if a["minus_di"] else None
    sw = swing_structure(candles)
    slope = ma_slope(cs, 20, 5)
    dc = donchian(candles)
    dc_pos = dc["position"][-1] if dc["position"] else None
    stretch = rsi_stretch(cs)
    stage = None
    if adx_v is None:
        missing.append("trend")
    else:
        if adx_v < 20:
            stage = "震荡/无趋势"
        else:
            direction = "up" if (slope or 0) > 0 else "down"
            aligned = ((sw.get("structure") == "HH_HL" and direction == "up")
                       or (sw.get("structure") == "LH_LL" and direction == "down"))
            if adx_v >= 40:
                stage = "强趋势"
            elif aligned:
                stage = "趋势中段"
            else:
                stage = "趋势减弱"
            if (adx_prev is not None and adx_v < adx_prev
                    and dc_pos is not None and (dc_pos >= 0.9 or dc_pos <= 0.1)
                    and stretch.get("side") is not None):
                stage = "趋势衰竭"
        trend_dir = sw.get("trend")
        # ADX 只说"多强"，方向必须由 DI 给出；否则"强趋势"这种读数会让人以为在涨
        direction = None
        if pdi is not None and ndi is not None:
            direction = "多头主导" if pdi > ndi else "空头主导"
        readout_parts.append(
            f"{stage}·{direction or '方向不明'}（ADX {adx_v}，斜率 {slope}%/根）"
            if slope is not None else f"{stage}·{direction or '方向不明'}（ADX {adx_v}）")
    trend_block = {
        "stage": stage, "adx": adx_v, "adx_delta_5b": round(adx_v - adx_prev, 2)
        if adx_v is not None and adx_prev is not None else None,
        "plus_di": pdi, "minus_di": ndi,
        "structure": sw.get("structure"), "trend_dir": sw.get("trend"),
        "direction": direction if adx_v is not None else None,
        "ma_slope_pct_per_bar": slope, "donchian_position": dc_pos,
        "rsi": stretch.get("rsi"), "rsi_stretch_bars": stretch.get("bars"),
    }

    # —— 波动状态 ——
    bbw = bollinger_width(cs)
    bbw_valid = [x for x in bbw if x is not None]
    bbw_last = bbw[-1] if bbw else None
    bbw_pct = percentile_rank(bbw_last, bbw_valid) if bbw_valid else None
    rv = realized_vol(candles)
    rv_valid = [x for x in rv if x is not None]
    rv_last = rv[-1] if rv else None
    rv_pct = percentile_rank(rv_last, rv_valid) if rv_valid else None
    atr_series = atr(candles)
    atr_last = atr_series[-1] if atr_series else None
    atr_pct = round(atr_last / cs[-1] * 100.0, 4) if atr_last and cs[-1] else None
    # v414: ATR 的历史分位 —— 风控档位用它做仓位口径（v414 实测：日线 ATR 高/低三分
    # 的 5 根 |MAE| 中位是 2.1~2.5 倍差，所以按波动尺度缩仓位在日线是有依据的；
    # 4h 上这个关系不成立，端口只在 report 里标注，不在代码里假装成立）。
    atr_pct_hist = [x / c * 100.0 for x, c in zip(atr_series, cs) if x and c]
    atr_pctile = percentile_rank(atr_pct, atr_pct_hist) if atr_pct_hist and atr_pct else None
    # v415：近 60 根的 ATR% 中位 —— 用于"当前波动相对自身常态贵不贵"，
    # 也是日内等风险仓位的分母（ATR 突然放大时仓位按比例收，不是拍脑袋缩仓）。
    atr_med = None
    if len(atr_pct_hist) >= 20:
        win = sorted(atr_pct_hist[-60:])
        n = len(win)
        mid = n // 2
        atr_med = round(win[mid] if n % 2 else (win[mid - 1] + win[mid]) / 2.0, 4)
    vol_state = None
    ref_pct = bbw_pct if bbw_pct is not None else rv_pct
    if ref_pct is None:
        missing.append("volatility")
    else:
        vol_state = "收缩" if ref_pct < 25 else ("扩张" if ref_pct > 75 else "常态")
        readout_parts.append(f"波动{vol_state}（宽度 {ref_pct}% 分位）")
    volatility_block = {
        "state": vol_state, "bb_width_pct": bbw_last, "bb_width_percentile": bbw_pct,
        "realized_vol_pct": rv_last, "realized_vol_percentile": rv_pct, "atr_pct": atr_pct,
        "atr_pct_percentile": atr_pctile,
        "atr_pct_median": atr_med,
    }

    # —— 价值区位置 ——
    vp = volume_profile(candles)
    if vp.get("position") is None:
        missing.append("value_area")
        vp_read = None
    else:
        vp_read = {"above_value": "价格在价值区上方", "below_value": "价格在价值区下方",
                   "in_value": "价格在价值区内部"}[vp["position"]]
        readout_parts.append(f"{vp_read}（距 POC {vp['poc_dist_pct']:+.2f}%）")
    value_block = {"position": vp.get("position"), "readout": vp_read,
                   "poc": vp.get("poc"), "vah": vp.get("vah"), "val": vp.get("val"),
                   "poc_dist_pct": vp.get("poc_dist_pct"),
                   # v409：价值区有两种口径 —— "volume"=按成交量加权，"tpo"=按价格被
                   # 反复经过的次数等权（贵金属日线没有成交量时只能用后者）。读数里必须带上，
                   # 否则同样叫 POC 的两个数会被当成一回事。
                   "weight": vp.get("weight")}

    # —— 杠杆结构 ——
    if price_change_pct is None:
        n_bars = min(len(cs) - 1, 5)
        price_change_pct = round((cs[-1] / cs[-1 - n_bars] - 1) * 100.0, 4) if n_bars else None
        price_window = f"{n_bars} 根"
    else:
        price_window = "外部传入"
    quad = oi_price_quadrant(price_change_pct, oi_change_pct)
    funding_pct = percentile_rank(funding_rate, funding_history) if funding_history else None
    crowding = None
    if funding_pct is not None:
        crowding = "多头拥挤" if funding_pct >= 90 else ("空头拥挤" if funding_pct <= 10 else "中性")
    leverage_block = {
        "label": quad["label"], "bias": quad["bias"], "note": quad["note"],
        "strength": quad["strength"],
        "price_change_pct": price_change_pct, "price_change_window": price_window,
        "oi_change_pct": _f(oi_change_pct),
        "funding_rate": _f(funding_rate), "funding_percentile": funding_pct,
        "crowding": crowding,
    }
    if quad["label"] is None:
        missing.append("leverage(OI)")
    else:
        readout_parts.append(f"{quad['label']}（价 {price_change_pct:+.2f}% / OI {'—' if oi_change_pct is None else format(_f(oi_change_pct), '+.2f') + '%'}）")
    if funding_pct is not None and crowding:
        readout_parts.append(f"资金费率 {funding_pct}% 分位（{crowding}）")

    # —— 主动性 ——
    tp = taker_pressure(taker_buy, taker_sell)
    # CVD 与收盘价必须按同一时间桶对齐；调用方传 cvd_closes 时以它为准（长度不一致直接判 None）
    if cvd is not None:
        ref_closes = cvd_closes if cvd_closes is not None else cs
        cvd_div = cvd_price_divergence(cvd, ref_closes)
    else:
        cvd_div = {"divergence": None}
    flow_block = {"taker": tp, "cvd_divergence": cvd_div.get("divergence"),
                  "cvd_price_change_pct": cvd_div.get("price_change_pct"),
                  "cvd_change": cvd_div.get("cvd_change")}
    if tp["label"] is not None:
        readout_parts.append(f"{tp['label']}（买占比 {tp['buy_ratio'] * 100:.1f}%）")
    else:
        missing.append("taker")
    if cvd_div.get("divergence") in ("bearish", "bullish"):
        note = "主动买盘未跟上，涨势偏虚" if cvd_div["divergence"] == "bearish" \
            else "下跌中主动买盘增加，跌势可能衰竭"
        readout_parts.append(f"CVD 背离（{note}）")

    # —— 持仓分歧 ——
    pos = positioning_divergence(top_long_short_ratio, retail_long_short_ratio)
    positioning_block = {"label": pos.get("label"), "bias": pos.get("bias"),
                         "gap_pct": pos.get("gap_pct"),
                         "top_long_short_ratio": _f(top_long_short_ratio),
                         "retail_long_short_ratio": _f(retail_long_short_ratio)}
    if pos.get("label"):
        readout_parts.append(pos["label"])
    elif _f(retail_long_short_ratio) is not None:
        # 只有散户多空比、没有大户对照（黄金这类只有券商客户持仓的标的就是这种情况）：
        # 给读数但明确标注对照缺失，不冒充完整的分歧判断。
        readout_parts.append(f"仅散户多空比 {round(_f(retail_long_short_ratio), 2)}（缺大户对照）")
        missing.append("positioning(top)")
    else:
        missing.append("positioning")

    readout = "｜".join(readout_parts) if readout_parts else None
    return {
        "trend": trend_block,
        "volatility": volatility_block,
        "value_area": value_block,
        "leverage": leverage_block,
        "flow": flow_block,
        "positioning": positioning_block,
        "readout": readout,
        "missing": missing,
        "bars": len(cs),
        "interval_sec": interval_sec,
    }


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
        # v343 补齐：趋势强度 / 筹码结构 / 博弈结构 / 波动情绪
        "adx": "trend", "aroon": "trend", "supertrend": "trend",
        "donchian": "trend", "ma_slope": "trend",
        "volume_profile": "volume", "swing_structure": "structure",
        "realized_vol": "volatility", "bollinger_width": "volatility", "rsi_stretch": "momentum",
        "percentile_rank": "stats", "oi_price_quadrant": "derivatives",
        "positioning_divergence": "derivatives", "taker_pressure": "flow",
        "cvd_price_divergence": "flow", "market_state": "state",
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
                        # v486：无量时直接跳过，不白扫一遍 2600 根再返回 None
                        skip = pipeline_skip_reason(meta.name, cndls)
                        if skip is not None:
                            out_tf[tf] = skip
                        else:
                            out_tf[tf] = meta.func(cndls, *args[1:],
                                                   **meta.default_params, **kwargs)
                    results[meta.name] = out_tf
                else:
                    skip = pipeline_skip_reason(meta.name, first)
                    if skip is not None:
                        results[meta.name] = skip
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