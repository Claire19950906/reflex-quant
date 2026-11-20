#!/usr/bin/env python3
"""intraday_risk_drivers.py —— 日内逆风到底跟什么有关（v415）。

背景：v414 证明"ATR 绝对档位"在日线有区分力（1.72~3.54 倍），但在 4h 上失效
（0.80 倍、样本不足）。于是问题变成：**日内该用哪个风险变量**？

做法（严格因果）：
  1. 每个采样时点用 `asof_ts` 截断历史重算 `build_state`，取出所有候选特征；
  2. 只看该时点之后 5 根的 |MAE|（以及 10 根）；
  3. 对每个特征算：秩相关（Spearman）＋ 三分位分离比（高档中位 ÷ 低档中位）；
  4. 只有 |corr| ≥ 0.20 且分离比 ≥ 1.3 才标"可用"，其余如实写"无区分力"。

注意：ATR、已实现波动、带宽高度同源，报告里必须标出来，否则会以为"发现很多因子"。

用法：
    python3 scripts/intraday_risk_drivers.py --asset BTC --timeframe 1h --samples 400
    python3 scripts/intraday_risk_drivers.py --all --samples 400
"""
from __future__ import annotations

import argparse
import json
import math
import os
import sqlite3
import sys
import time
from typing import Any, Dict, List, Optional, Tuple

BACKEND = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(BACKEND, "src_python"))
sys.path.insert(0, os.path.join(BACKEND, "scripts"))

import technical_state as st
from state_bias_validation import WARMUP, data_quality, load_series

REPORT_DIR = os.path.join(BACKEND, "reports", "intraday_risk")
DB_PATH = os.path.join(BACKEND, "trident_event_bus.db")
HORIZON = 5
CORR_MIN = 0.15   # 下限（日内多是阈值效应，线性相关天然偏低；但不设下限会放进噪声）
SEP_MIN = 1.3     # 真正判据：上三分位 |MAE| 中位 ÷ 下三分位
MIN_N = 100
DEFAULT_SET = [("BTC", "1h"), ("XAU", "1h"), ("ETH", "1h")]

# 同源标记：这些特征本质是同一个"波动尺度"，不能当成多个独立证据
FAMILY = {
    "atr_pct": "波动尺度",
    "atr_pctile": "波动尺度",
    "rv_pct": "波动尺度",
    "bb_width_pct": "波动尺度",
    "bb_width_pctile": "波动尺度",
    "atr_ratio": "波动尺度",
    "adx": "趋势强度",
    "adx_delta5": "趋势强度",
    "di_spread": "趋势强度",
    "donchian": "位置",
    "rsi": "位置",
    "va_dist_poc": "位置",
    "va_dist_edge": "位置",
    "bar_range_atr": "单根冲击",
    "bar_body_atr": "单根冲击",
    "same_dir_streak": "单根冲击",
    "vol_ratio_last": "单根冲击",
}


def _median(vals: List[float]) -> Optional[float]:
    if not vals:
        return None
    vals = sorted(vals)
    n = len(vals)
    mid = n // 2
    return vals[mid] if n % 2 else (vals[mid - 1] + vals[mid]) / 2.0


def _tertile(vals: List[float]) -> Tuple[float, float]:
    vals = sorted(vals)
    n = len(vals)
    return vals[n // 3], vals[2 * n // 3]


def _rank(vals: List[float]) -> List[float]:
    """把值换成秩（并列取平均秩）——Spearman 用。"""
    order = sorted(range(len(vals)), key=lambda i: vals[i])
    ranks = [0.0] * len(vals)
    i = 0
    while i < len(order):
        j = i
        while j + 1 < len(order) and vals[order[j + 1]] == vals[order[i]]:
            j += 1
        avg = (i + j) / 2.0 + 1
        for k in range(i, j + 1):
            ranks[order[k]] = avg
        i = j + 1
    return ranks


def spearman(xs: List[float], ys: List[float]) -> Optional[float]:
    n = len(xs)
    if n < 3:
        return None
    rx, ry = _rank(xs), _rank(ys)
    mx, my = sum(rx) / n, sum(ry) / n
    num = sum((a - mx) * (b - my) for a, b in zip(rx, ry))
    den = math.sqrt(sum((a - mx) ** 2 for a in rx) * sum((b - my) ** 2 for b in ry))
    if not den:
        return None
    return num / den


def features_of(state: Dict[str, Any], candles: List[Any], i: int) -> Dict[str, Optional[float]]:
    """从一个状态读数 + 当前 K 线位置，抽出候选特征（全部只用截至 i 的数据）。"""
    vo = state.get("volatility") or {}
    tr = state.get("trend") or {}
    va = state.get("value_area") or {}
    atr = vo.get("atr_pct")
    c = candles[i]
    o, h, l, cl = float(c[1]), float(c[2]), float(c[3]), float(c[4])
    rng = (h - l) / cl * 100.0 if cl else None
    body = abs(cl - o) / cl * 100.0 if cl else None
    feats: Dict[str, Optional[float]] = {
        "atr_pct": atr,
        "atr_pctile": vo.get("atr_pct_percentile"),
        "rv_pct": vo.get("realized_vol_pct"),
        "bb_width_pct": vo.get("bb_width_pct"),
        "bb_width_pctile": vo.get("bb_width_percentile"),
        "atr_ratio": (rng / atr) if (rng and atr) else None,
        "adx": tr.get("adx"),
        "adx_delta5": tr.get("adx_delta_5b"),
        "di_spread": (abs((tr.get("plus_di") or 0) - (tr.get("minus_di") or 0))
                      if tr.get("plus_di") is not None and tr.get("minus_di") is not None else None),
        "donchian": tr.get("donchian_position"),
        "rsi": tr.get("rsi"),
        "va_dist_poc": va.get("distance_poc_pct") if "distance_poc_pct" in va else None,
        "va_dist_edge": None,
        "bar_range_atr": (rng / atr) if (rng and atr) else None,
        "bar_body_atr": (body / atr) if (body and atr) else None,
        "vol_ratio_last": None,
        "same_dir_streak": None,
    }
    # 距价值区最近边缘的距离（VAH 之上 / VAL 之下 / 内部）
    poc, vah, val = va.get("poc"), va.get("vah"), va.get("val")
    if poc and cl:
        feats["va_dist_poc"] = (cl - poc) / poc * 100.0
    if vah and val and cl:
        edge = vah if cl >= vah else (val if cl <= val else None)
        if edge:
            feats["va_dist_edge"] = (cl - edge) / edge * 100.0
    # 成交量比（有量的品种才算，贵金属日线 volume 缺失 → None，不补 0）
    vols = [float(x[5]) for x in candles[max(0, i - 20):i + 1]
            if len(x) > 5 and x[5] is not None]
    if len(vols) >= 5 and vols[-1]:
        base = sum(vols[:-1]) / max(len(vols) - 1, 1)
        if base:
            feats["vol_ratio_last"] = vols[-1] / base
    # 连续同向根数
    streak = 0
    direction = None
    for k in range(i, max(i - 6, 0), -1):
        d = 1 if candles[k][4] >= candles[k][1] else -1
        if direction is None:
            direction = d
        elif d == direction:
            streak += 1
        else:
            break
    feats["same_dir_streak"] = float(streak)
    return feats


def evaluate(conn: sqlite3.Connection, asset: str, timeframe: str,
             samples: int, step: int, bars: int,
             horizon: int = HORIZON, shift_back: int = 0) -> Dict[str, Any]:
    series = load_series(conn, asset, timeframe)
    dq = data_quality(series)
    payload: Dict[str, Any] = {"asset": asset, "timeframe": timeframe,
                               "data_quality": dq, "series_len": len(series),
                               "horizon": horizon, "features": [], "window": None}
    if not dq.get("ok"):
        payload["verdict"] = "数据不可用"
        return payload
    if len(series) < WARMUP + horizon + 5:
        payload["verdict"] = "样本不足，不给结论"
        return payload

    closes = [c for _, c in series]
    # candles 形态：state 读取端是 (ts, o, h, l, c, v) 的平铺元组；load_series 只给 (ts, close)。
    # 为了拿到 OHLC，这里单独查一次原始行（顺序与 load_series 一致）。
    candles = _load_ohlcv(conn, asset, timeframe)
    if len(candles) != len(series):
        payload["verdict"] = "序列不一致，跳过（不做拼接猜测）"
        return payload

    end = len(closes) - horizon - max(shift_back, 0)
    if end <= WARMUP:
        payload["verdict"] = "样本不足（窗口前移过量），不给结论"
        return payload
    eligible = list(range(WARMUP, end, max(step, 1)))
    if samples and samples > 0:
        eligible = eligible[-samples:]
    rows: List[Dict[str, Any]] = []
    skipped = 0
    for i in eligible:
        state = st.build_state(conn, asset, timeframe, bars=bars, asof_ts=series[i][0])
        if state.get("error"):
            skipped += 1
            continue
        feats = features_of(state, candles, i)
        entry = closes[i]
        seg = closes[i:i + horizon + 1]
        ret = (closes[i + horizon] - entry) / entry * 100.0
        mae = abs((min(seg) - entry) / entry * 100.0)
        # v418：顺风侧（MFE）—— 只有逆风没有顺风，算不出"这笔值不值得挨"
        mfe = (max(seg) - entry) / entry * 100.0
        rows.append({"i": i, "ts": series[i][0], "mae": mae, "mfe": mfe,
                     "ret": ret, "odds": (mfe / mae) if mae else None, **feats})

    payload["window"] = {
        "from": time.strftime("%Y-%m-%d %H:%M", time.localtime(rows[0]["ts"])) if rows else None,
        "to": time.strftime("%Y-%m-%d %H:%M", time.localtime(rows[-1]["ts"])) if rows else None,
        "points": len(rows), "skipped": skipped,
    }
    if not rows:
        payload["verdict"] = "没有可算时点"
        return payload

    base_mae = _median([r["mae"] for r in rows])
    base_mfe = _median([r["mfe"] for r in rows if r.get("mfe") is not None])
    payload["baseline"] = {
        "n": len(rows),
        "mae_median": round(base_mae or 0.0, 4),
        "mfe_median": round(base_mfe, 4) if base_mfe else None,
        # 赔率 = 顺风中位 ÷ 逆风中位（不用比值中位数，避免单边行情把分布拉歪）
        "odds_median": (round(base_mfe / base_mae, 2) if (base_mfe and base_mae) else None),
    }

    for name in FAMILY:
        pairs = [(r[name], r["mae"]) for r in rows if r.get(name) is not None and r["mae"] is not None]
        if len(pairs) < MIN_N:
            payload["features"].append({"name": name, "family": FAMILY[name],
                                        "n": len(pairs), "usable": False,
                                        "why": f"可用样本 {len(pairs)} < {MIN_N}"})
            continue
        xs = [p[0] for p in pairs]
        ys = [p[1] for p in pairs]
        corr = spearman(xs, ys)
        lo, hi = _tertile(xs)
        low_mae = _median([y for x, y in pairs if x <= lo])
        high_mae = _median([y for x, y in pairs if x > hi])
        ratio = (high_mae / low_mae) if (low_mae and high_mae) else None
        # 与 ATR 的相关：>0.5 说明只是"波动"的另一种说法，不能算独立发现
        atr_pairs = [(r[name], r["atr_pct"]) for r in rows
                     if r.get(name) is not None and r.get("atr_pct")]
        c_atr = (spearman([q[0] for q in atr_pairs], [q[1] for q in atr_pairs])
                 if len(atr_pairs) >= MIN_N else None)
        independent = FAMILY[name] == "波动尺度" or (c_atr is not None and abs(c_atr) < 0.5)
        usable = (ratio is not None and ratio >= SEP_MIN and independent
                  and corr is not None and abs(corr) >= CORR_MIN)
        if usable:
            why = "分离比与相关都过线"
        elif ratio is None or ratio < SEP_MIN:
            why = f"分离比 {ratio:.2f} < {SEP_MIN}"
        elif not independent:
            why = f"与 ATR 高相关（{abs(c_atr):.2f}），同源不算独立发现"
        else:
            why = f"秩相关 {abs(corr):.3f} < {CORR_MIN}（过弱，视为噪声）"
        payload["features"].append({
            "name": name, "family": FAMILY[name], "n": len(pairs),
            "corr": round(corr, 4) if corr is not None else None,
            "corr_with_atr": round(c_atr, 3) if c_atr is not None else None,
            "independent": bool(independent),
            "low_mae": round(low_mae, 4) if low_mae else None,
            "high_mae": round(high_mae, 4) if high_mae else None,
            "sep": round(ratio, 3) if ratio else None,
            "usable": bool(usable),
            "why": why,
        })

    payload["stop"] = stop_table(rows)
    payload["odds_by_trend"] = odds_by_trend(rows, series)
    payload["odds_curve"] = odds_curve(rows, series)
    payload["features"].sort(key=lambda f: -(abs(f.get("corr") or 0)))
    usable = [f["name"] for f in payload["features"] if f.get("usable")]
    payload["usable_features"] = usable
    payload["verdict"] = (
        f"可用特征：{'、'.join(usable)}" if usable
        else "没有任何特征过线（分离比<1.3 / 相关<0.15 / 同源于 ATR）—— 这些变量无法提前定位日内逆风"
    )
    return payload


# 只从 3 根起算：h=1 时"逆风"只由两个收盘价决定，下一根收盘更高就让 MAE=0，
# 赔率会算出 4.01 这类假值（ETH 4h 实测踩到）。单根内的高低点不在收盘序列里，不给 1 根。
ODDS_HORIZONS = (3, 5, 10, 20)


def odds_curve(rows: List[Dict[str, Any]], series: List[Any],
               horizons: Tuple[int, ...] = ODDS_HORIZONS) -> Dict[str, Any]:
    """赔率随「持有多久」的变化：回答「这周期不该做，还是不该做短打」。

    v418 的赔率固定在 5 根前瞻。但 1h 赔率 <1 有两种解释：
      a) 这个周期本身随机漂移为负（那就别做）；
      b) 5 根太短，噪音占主导，拿久一点就能盖过（那该改持有期）。
    用同一批入场点跑 1/3/5/10/20 根，看赔率在哪一根转正。
    """
    out: Dict[str, Any] = {"available": False, "horizons": list(horizons), "rows": []}
    idx = [r["i"] for r in rows]
    for h in horizons:
        maes, mfes, rets = [], [], []
        for i in idx:
            if i + h >= len(series):
                continue
            entry = series[i][1]
            if not entry:
                continue
            seg = [c for _, c in series[i:i + h + 1]]
            maes.append(abs((min(seg) - entry) / entry * 100.0))
            mfes.append((max(seg) - entry) / entry * 100.0)
            rets.append((series[i + h][1] - entry) / entry * 100.0)
        if len(maes) < 30:
            out["rows"].append({"horizon": h, "n": len(maes)})
            continue
        mae = _median(maes)
        mfe = _median(mfes)
        if not mae or mae < 0.01:
            out["rows"].append({"horizon": h, "n": len(maes)})
            continue
        out["rows"].append({
            "horizon": h, "n": len(maes),
            "mae_median": round(mae or 0.0, 4),
            "mfe_median": round(mfe or 0.0, 4),
            "odds": (round(mfe / mae, 2) if (mae and mfe) else None),
            "ret_median": round(_median(rets) or 0.0, 4),
            "up_share": round(sum(1 for x in rets if x > 0) / len(rets) * 100.0, 1),
        })
    good = [r for r in out["rows"] if r.get("odds") is not None and r["odds"] >= 1.0]
    out["min_hold_bars"] = good[0]["horizon"] if good else None
    out["available"] = any(r.get("odds") is not None for r in out["rows"])
    return out


def odds_by_trend(rows: List[Dict[str, Any]], series: List[Any], lookback: int = 20) -> Dict[str, Any]:
    """按「入场前 lookback 根的涨跌」分组看赔率。

    动机：全样本赔率在 1h/4h 上 <1、在日线上 >1，差异不像「状态」，更像「这段窗口在跌」。
    如果赔率只是窗口属性，它就不能当静态门槛——牛市里会把所有单子判成 HOLD。
    """
    groups: Dict[str, List[Dict[str, Any]]] = {"前段上涨": [], "前段震荡": [], "前段下跌": []}
    for r in rows:
        i = r["i"]
        if i < lookback:
            continue
        base = series[i - lookback][1]
        if not base:
            continue
        move = (series[i][1] - base) / base * 100.0
        key = "前段上涨" if move >= 1.0 else ("前段下跌" if move <= -1.0 else "前段震荡")
        groups[key].append(r)
    out: Dict[str, Any] = {"available": False, "rows": [], "lookback": lookback}
    # 这一节按走势分组，单组样本天然更少；门槛放到 30（与档位验证同口径），
    # 低于 30 一律报「样本不足」，不硬凑结论。
    for key, rs in groups.items():
        if len(rs) < 30:
            out["rows"].append({"bucket": key, "n": len(rs)})
            continue
        mae = _median([r["mae"] for r in rs])
        mfe = _median([r["mfe"] for r in rs])
        ret = _median([r["ret"] for r in rs])
        up = sum(1 for r in rs if r["ret"] > 0) / len(rs) * 100.0
        out["rows"].append({
            "bucket": key, "n": len(rs),
            "mae_median": round(mae or 0.0, 4),
            "mfe_median": round(mfe or 0.0, 4),
            "odds_median": (round(mfe / mae, 2) if (mae and mfe) else None),
            "ret_median": round(ret or 0.0, 4),
            "up_share": round(up, 1),
        })
    out["available"] = any(r.get("odds_median") for r in out["rows"])
    return out


def stop_table(rows: List[Dict[str, Any]]) -> Dict[str, Any]:
    """按当时波动三分位，测'若把止损设在 k×ATR，5 根内被打掉的概率'。

    这是把 |MAE| 结论变成可执行参数：止损倍数要选到"高波动时也不容易被打"。
    """
    ks = (1.0, 1.5, 2.0, 2.5, 3.0)
    atrs = [r["atr_pct"] for r in rows if r.get("atr_pct")]
    if not atrs:
        return {"available": False}
    lo, hi = _tertile(atrs)
    buckets = {"低": [], "中": [], "高": []}
    for r in rows:
        atr = r.get("atr_pct")
        if not atr:
            continue
        key = "低" if atr <= lo else ("中" if atr <= hi else "高")
        buckets[key].append(r["mae"] / atr)
    out: Dict[str, Any] = {"available": True, "thresholds": {"lo": round(lo, 4), "hi": round(hi, 4)},
                           "rows": []}
    for key, vals in buckets.items():
        if not vals:
            out["rows"].append({"bucket": key, "n": 0})
            continue
        entry: Dict[str, Any] = {"bucket": key, "n": len(vals),
                                 "median_atr": round(_median(vals) or 0.0, 3)}
        # v418：同一批样本的顺风侧 —— 只有逆风没有顺风，算不出「这笔值不值得挨」
        # 注意：不报「比值的中位数」。单边下跌时 MFE≈0、单边上涨时 MAE≈0，
        # 比值分布极度偏斜，中位数会失真；这里报**两个中位数之比**，可解释、可复核。
        same = [r for r in rows if r.get('atr_pct')
                and ('低' if r['atr_pct'] <= lo else ('中' if r['atr_pct'] <= hi else '高')) == key]
        mae_vals = [r['mae'] for r in same if r.get('mae') is not None]
        mfe_vals = [r['mfe'] for r in same if r.get('mfe') is not None]
        if mae_vals:
            entry['mae_median'] = round(_median(mae_vals) or 0.0, 4)
        if mfe_vals:
            entry['mfe_median'] = round(_median(mfe_vals) or 0.0, 4)
        if entry.get('mae_median') and entry.get('mfe_median'):
            entry['odds_median'] = round(entry['mfe_median'] / entry['mae_median'], 2)
        for k in ks:
            entry[f"p{k}"] = round(sum(1 for v in vals if v >= k) / len(vals) * 100.0, 1)
        out["rows"].append(entry)
    out["ks"] = ks
    return out


def _load_ohlcv(conn: sqlite3.Connection, asset: str, timeframe: str) -> List[Tuple]:
    """与 `load_series` 完全同口径取 OHLCV：按 ts 去重（同一 ts 取最后写入的那行）。

    为什么必须同口径：`load_series` 用 ROW_NUMBER() PARTITION BY ts 去重，而这里如果
    直接 SELECT 会把重复 ts 的行一起捞出来，两边长度就对不上——XAU 15m 曾因此
    "序列不一致"被跳过。宁可显式对齐，也不做"差不多的拼接"。
    """
    cur = conn.execute(
        "SELECT ts, open, high, low, close, volume FROM ("
        "  SELECT ts, open, high, low, close, volume,"
        "         ROW_NUMBER() OVER (PARTITION BY ts ORDER BY rowid DESC) AS rn"
        "  FROM kline_bars WHERE symbol=? AND timeframe=? AND close IS NOT NULL)"
        " WHERE rn=1 ORDER BY ts",
        (asset, timeframe),
    )
    return [tuple(r) for r in cur.fetchall()]


def _fmt(v: Any, nd: int = 2) -> str:
    if v is None:
        return "—"
    if isinstance(v, float):
        return f"{v:.{nd}f}"
    return str(v)


def _markdown(payload: Dict[str, Any]) -> str:
    lines = [f"# 日内逆风驱动因素 · {payload['asset']} {payload['timeframe']}", ""]
    win = payload.get("window") or {}
    lines += [f"- 窗口：{win.get('from')} → {win.get('to')}，采样 {win.get('points')} 点",
              f"- 前瞻：{payload.get('horizon')} 根；判据：分离比≥{SEP_MIN} 且 |秩相关|≥{CORR_MIN} 且 不与 ATR 同源",
              "", f"**结论：{payload.get('verdict')}**", ""]
    base = payload.get("baseline") or {}
    _b = base
    lines += ["全样本 |MAE%d| 中位：%s%%｜顺风 MFE 中位：%s%%｜赔率（顺风中位÷逆风中位）：%s（n=%s）"
              % (HORIZON, _fmt(_b.get("mae_median")), _fmt(_b.get("mfe_median")),
                 _fmt(_b.get("odds_median"), 2), _b.get("n")), ""]
    lines += ["| 特征 | 同源组 | n | 秩相关 | 与ATR相关 | 低三分位 MAE | 高三分位 MAE | 分离比 | 判定 | 可用 |",
              "|---|---|---|---|---|---|---|---|---|---|"]
    for f in payload["features"]:
        lines.append(
            f"| {f['name']} | {f['family']} | {f.get('n')} | {_fmt(f.get('corr'), 3)} | "
            f"{_fmt(f.get('corr_with_atr'), 2)} | {_fmt(f.get('low_mae'))}% | "
            f"{_fmt(f.get('high_mae'))}% | {_fmt(f.get('sep'), 2)} | {f.get('why')} | "
            f"{'✅' if f.get('usable') else '—'} |"
        )
    stbl = payload.get("stop") or {}
    if stbl.get("available"):
        ks = stbl["ks"]
        lines += ["", f"### 若止损设在 k×ATR，{HORIZON} 根内被打掉的概率（按当时波动三分位）", "",
                  "| 波动档 | n | 中位 MAE÷ATR | 逆风 MAE | 顺风 MFE | 赔率(中位之比) | " + " | ".join(f"k={k}" for k in ks) + " |",
                  "|---|---|---|---|---|---|" + "---|" * len(ks)]
        for r in stbl["rows"]:
            if not r.get("n"):
                lines.append(f"| {r['bucket']} | 0 | — | " + " | ".join("—" for _ in ks) + " |")
                continue
            lines.append(f"| {r['bucket']} | {r['n']} | {r['median_atr']} | "
                         f"{_fmt(r.get('mae_median'))}% | {_fmt(r.get('mfe_median'))}% | "
                         f"{_fmt(r.get('odds_median'), 2)} | " +
                         " | ".join(f"{r.get(f'p{k}')}%" for k in ks) + " |")
    obt = payload.get("odds_by_trend") or {}
    if obt.get("available"):
        lines += ["", "### 赔率是不是「窗口属性」：按入场前 %s 根涨跌分组" % obt.get("lookback"), "",
                  "| 前段走势 | n | 逆风 MAE | 顺风 MFE | 赔率 | 未来5根中位收益 | 上涨占比 |",
                  "|---|---|---|---|---|---|---|"]
        for r in obt["rows"]:
            if not r.get("n") or r.get("odds_median") is None:
                lines.append("| %s | %s | 样本不足 | — | — | — | — |" % (r["bucket"], r.get("n")))
                continue
            lines.append("| %s | %s | %s%% | %s%% | %s | %s%% | %s%% |"
                         % (r["bucket"], r["n"], r["mae_median"], r["mfe_median"],
                            r["odds_median"], r["ret_median"], r["up_share"]))
        lines += ["", "> 若「前段上涨」组赔率 >1、「前段下跌」组 <1，说明赔率跟着窗口走，不能当静态门槛用。"]

    oc = payload.get("odds_curve") or {}
    if oc.get("available"):
        mh = oc.get("min_hold_bars")
        lines += ["", "### 赔率随持有期变化：这周期不该做，还是不该做短打？", "",
                  "| 持有根数 | n | 逆风 MAE | 顺风 MFE | 赔率 | 中位收益 | 上涨占比 |",
                  "|---|---|---|---|---|---|---|"]
        for r in oc["rows"]:
            if r.get("odds") is None:
                lines.append("| %s | %s | 样本不足 | — | — | — | — |" % (r["horizon"], r.get("n")))
                continue
            lines.append("| %s | %s | %s%% | %s%% | %s | %s%% | %s%% |"
                         % (r["horizon"], r["n"], r["mae_median"], r["mfe_median"],
                            r["odds"], r["ret_median"], r["up_share"]))
        lines += ["", ("> 赔率在 **%d 根** 处首次 ≥1：说明这周期不是不能做，是**别做更短的**。"
                       % mh if mh else
                       "> 1~20 根内赔率始终 <1：这个周期**随机顺风打不过逆风**，只适合按逆风预算卡仓位、不做方向。"),
                  ""]

    lines += ["", "> 注意：`atr_pct` / `atr_pctile` / `rv_pct` / `bb_width_pct` 属于同一个"
                  "「波动尺度」家族，它们同时过线只说明**波动大→逆风大**，不是多个独立发现。", ""]
    return "\n".join(lines)


def _write_index(results: List[Dict[str, Any]]) -> None:
    # 索引要覆盖**磁盘上所有已经跑过的组合**，不能只写本次结果——
    # 否则跑单个资产就把别的行覆盖掉（v418 踩过：汇总表只剩 ETH 4h 一行）。
    merged: Dict[Any, Dict[str, Any]] = {}
    for name in sorted(os.listdir(REPORT_DIR)) if os.path.isdir(REPORT_DIR) else []:
        if not name.endswith(".json") or name == "latest.json":
            continue
        try:
            with open(os.path.join(REPORT_DIR, name), encoding="utf-8") as fh:
                d = json.load(fh)
        except (OSError, ValueError):
            continue
        if d.get("asset") and d.get("timeframe"):
            merged[(d["asset"], d["timeframe"])] = d
    for r in results:
        if r.get("asset") and r.get("timeframe"):
            merged[(r["asset"], r["timeframe"])] = r
    lines = ["# 日内逆风驱动因素 · 汇总", "",
             "| 资产 | 周期 | n | 基线 |MAE5| | 可用特征 | 结论 |",
             "|---|---|---|---|---|---|"]
    for key in sorted(merged):
        r = merged[key]
        if r.get("error"):
            lines.append(f"| {r['asset']} | {r['timeframe']} | — | — | — | 失败：{r['error']} |")
            continue
        base = (r.get("baseline") or {}).get("mae_median")
        usable = "、".join(r.get("usable_features") or []) or "无"
        lines.append(f"| {r['asset']} | {r['timeframe']} | {(r.get('window') or {}).get('points')} | "
                     f"{_fmt(base)}% | {usable} | {r.get('verdict')} |")
    lines += ["", f"生成时间：{time.strftime('%Y-%m-%d %H:%M')}", ""]
    os.makedirs(REPORT_DIR, exist_ok=True)
    with open(os.path.join(REPORT_DIR, "latest.md"), "w", encoding="utf-8") as fh:
        fh.write("\n".join(lines))


def _run_one(conn: sqlite3.Connection, asset: str, timeframe: str, args) -> Dict[str, Any]:
    try:
        payload = evaluate(conn, asset, timeframe, args.samples, args.step, args.bars,
                           args.horizon, args.shift_back)
    except Exception as exc:
        payload = {"asset": asset, "timeframe": timeframe,
                   "error": f"{type(exc).__name__}: {str(exc)[:120]}"}
    if not args.no_write:
        os.makedirs(REPORT_DIR, exist_ok=True)
        stem = f"{asset}_{timeframe}"
        with open(os.path.join(REPORT_DIR, f"{stem}.md"), "w", encoding="utf-8") as fh:
            fh.write(_markdown(payload))
        with open(os.path.join(REPORT_DIR, f"{stem}.json"), "w", encoding="utf-8") as fh:
            json.dump(payload, fh, ensure_ascii=False, indent=2)
    return payload


def main() -> int:
    ap = argparse.ArgumentParser(description="日内逆风驱动因素研究")
    ap.add_argument("--asset", default="BTC")
    ap.add_argument("--timeframe", default="1h")
    ap.add_argument("--samples", type=int, default=400, help="采样点数；<=0 走全序列")
    ap.add_argument("--step", type=int, default=2)
    ap.add_argument("--horizon", type=int, default=HORIZON, help="前瞻根数，默认 5")
    ap.add_argument("--shift-back", type=int, default=0, help="窗口整体前移 N 根（稳健性复核）")
    ap.add_argument("--bars", type=int, default=240)
    ap.add_argument("--db", default=DB_PATH)
    ap.add_argument("--no-write", action="store_true")
    ap.add_argument("--all", action="store_true")
    ap.add_argument("--index-only", action="store_true")
    args = ap.parse_args()

    if not os.path.exists(args.db):
        print(f"数据库不存在：{args.db}", file=sys.stderr)
        return 2
    conn = sqlite3.connect(f"file:{args.db}?mode=ro", uri=True)
    try:
        if args.index_only:
            results = []
        elif args.all:
            results = [_run_one(conn, a, tf, args) for a, tf in DEFAULT_SET]
        else:
            results = [_run_one(conn, args.asset.upper(), args.timeframe, args)]
    finally:
        conn.close()

    if (results or args.index_only) and not args.no_write:
        _write_index(results)
    for r in results:
        if r.get("error"):
            print(f"{r['asset']} {r['timeframe']}: 失败 {r['error']}")
            continue
        print(f"{r['asset']} {r['timeframe']}: {r.get('verdict')} "
              f"(n={(r.get('window') or {}).get('points')}, 基线|MAE{r.get('horizon')}|"
              f"={(r.get('baseline') or {}).get('mae_median')}%)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
