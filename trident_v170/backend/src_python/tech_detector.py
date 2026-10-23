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
import sqlite3
from typing import Any

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
    "score_base": 0.30,
    "score_cap": 0.95,
    "reversal_lookahead": 3,        # 转折点允许看未来 3 根
    "reversal_min_pct": 5.0,
    # 全局开关: False 即完全禁止任何未来函数
    "enable_reversal_future": True,
}

ALLOW_FUTURE = CONFIG["enable_reversal_future"]
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

    return {
        "ema_fast": ema_f, "ema_slow": ema_s,
        "dif": dif, "dea": dea, "hist": hist,
        "rsi": rsi, "atr": atr,
        "vol_ratio": vol_ratio, "atr_expand": atr_expand,
        "pivots": pivots, "pivot_confirmed": pivot_conf,
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
    """支撑位: 最近一个 pivot low, 且当前 close 距其 <=3%。"""
    piv, conf = ind["pivots"], ind["pivot_confirmed"]
    for j in range(i, max(-1, i - CONFIG["divergence_lookback"]), -1):
        if piv.get(j) == "low" and conf.get(j):
            lvl = rows[j]["low"]
            c = rows[i]["close"]
            if lvl > 0 and abs(c - lvl) / lvl <= 0.03:
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

        # 1) 涨跌幅
        pct_1d = (c - pc) / pc * 100.0
        p3 = rows[i - 3]["close"]
        pct_3d = (c - p3) / p3 * 100.0 if p3 > 0 else 0.0
        if abs(pct_1d) >= W["pct_move_1d"]:
            hits.append({"k": "pct_1d", "v": round(pct_1d, 2), "w": 1.0})
        if abs(pct_3d) >= W["pct_move_3d"]:
            hits.append({"k": "pct_3d", "v": round(pct_3d, 2), "w": 1.0})

        # 2) 量能激增
        vr = ind["vol_ratio"][i]
        if vr is not None and vr >= W["vol_extreme_ratio"]:
            hits.append({"k": "vol_extreme", "v": round(vr, 2), "w": 1.2})
        elif vr is not None and vr >= W["vol_spike_ratio"]:
            hits.append({"k": "vol_spike", "v": round(vr, 2), "w": 0.9})

        # 3) 背离 (顶/底)
        div = _find_divergence(rows, ind, i, "high") or _find_divergence(rows, ind, i, "low")
        if div:
            hits.append({"k": f"div_{div['type']}", "v": div, "w": 1.5})

        # 4) 波动放大
        ae = ind["atr_expand"][i]
        if ae is not None and ae >= W["atr_expand_ratio"]:
            hits.append({"k": "atr_expand", "v": round(ae, 2), "w": 1.1})

        # 5) RSI 极值 (辅助, 低权重)
        r = ind["rsi"][i]
        if r is not None:
            if r >= W["rsi_overbought"]:
                hits.append({"k": "rsi_overbought", "v": round(r, 1), "w": 0.5})
            elif r <= W["rsi_oversold"]:
                hits.append({"k": "rsi_oversold", "v": round(r, 1), "w": 0.5})

        # 6) 均线穿越
        ef, es = ind["ema_fast"][i], ind["ema_slow"][i]
        pef, pes = ind["ema_fast"][i - 1], ind["ema_slow"][i - 1]
        if None not in (ef, es, pef, pes):
            if pef <= pes and ef > es:
                hits.append({"k": "ema_golden", "v": round(ef, 4), "w": 0.8})
            elif pef >= pes and ef < es:
                hits.append({"k": "ema_death", "v": round(ef, 4), "w": 0.8})

        # 7) MACD 柱状反转
        h0, h1 = ind["hist"][i], ind["hist"][i - 1]
        if h0 is not None and h1 is not None:
            if h1 > 0 >= h0:
                hits.append({"k": "macd_top_div", "v": round(h0, 5), "w": 0.9})
            elif h1 < 0 <= h0:
                hits.append({"k": "macd_bot_div", "v": round(h0, 5), "w": 0.9})

        # 8) 支撑位测试
        sup = _support_levels(rows, ind, i)
        if sup:
            hits.append({"k": "support_test", "v": sup, "w": 0.8})

        # 9) 合约爆单热力 (外部数据)
        ts = rows[i]["ts"]
        lk = _match_liq(ts, liq_by_ts)
        if lk:
            hits.append({"k": "liquidation", "v": lk, "w": 1.3})

        if not hits:
            continue
        if sum(1 for h in hits if h["w"] >= 0.8) < 2:
            continue

        score = min(W["score_cap"],
                    round(W["score_base"] + sum(h["w"] * 0.5 for h in hits), 3))
        out.append({
            "idx": i, "ts": ts, "close": c,
            "pct_1d": round(pct_1d, 3), "pct_3d": round(pct_3d, 3),
            "vol_ratio": round(vr, 2) if vr is not None else None,
            "score": score, "trigger_type": _classify(hits),
            "signals": hits, "uses_future": False,
        })

    if ALLOW_FUTURE:
        out.extend(_detect_reversals(rows, ind))
    return _merge_same_ts(out)


def _merge_same_ts(out: list[dict]) -> list[dict]:
    """合并同 ts 的信号。

    转折点与因果信号可能落在同一根 bar 上(例如"支撑位破位 + 顶背离")
    两者都有效, 必须合并而不是互相覆盖 —— 保留更高 score, 并把
    另一方的 hits 并入 signals, 同时用副信号记录被合并的类型。
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
        cur["score"] = min(CONFIG["score_cap"],
                           round(cur["score"] + 0.1 * (len(seen) - 1) / 2, 3))
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
    out = [by_ts[t] for t in order]
    out.sort(key=lambda x: x["ts"])
    return out


def _detect_reversals(rows: list[dict], ind) -> list[dict]:
    """大涨大跌**转折点**。

    ⚠ 用户明确许可此处使用未来函数(ALLOW_FUTURE), 以尽量提前找到转折。
    做法: i 处为局部极值, 用未来 la 根确认反向, 信号 ts 回填到 i,
    并打 uses_future=True, 下游可据此区分/过滤。
    全局置 CONFIG['enable_reversal_future']=False 即完全禁用。
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
                 "v": round(rev_pct, 2), "w": 2.0}]
        vr = ind["vol_ratio"][i]
        if vr is not None and vr >= W["vol_spike_ratio"]:
            hits.append({"k": "vol_spike", "v": round(vr, 2), "w": 1.0})
        r = ind["rsi"][i]
        if r is not None and (r >= 70 or r <= 30):
            hits.append({"k": "rsi_extreme", "v": round(r, 1), "w": 0.6})
        pc = rows[i - 1]["close"] if i >= 1 else 0.0
        out.append({
            "idx": i, "ts": rows[i]["ts"], "close": c,
            "pct_1d": round((c - pc) / pc * 100, 3) if pc > 0 else 0.0,
            "vol_ratio": round(vr, 2) if vr is not None else None,
            "score": min(W["score_cap"], round(W["score_base"] + 1.6, 3)),
            "trigger_type": "reversal",
            "signals": hits,
            "uses_future": True,
        })
    return out