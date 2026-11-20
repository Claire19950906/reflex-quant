#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""v230 深度反思 — 补全 4 个数据缺口(K线/跨资产/抢跑/运气 vs 信号)

设计动机(用户反馈 4 条):
  1) 反思缺数据 — 当前 trade_reflections_v2 没有 K线上下文、跨资产联动量化、抢跑检测、运气 vs 信号
  2) 反思太表层 — 之前 root_cause 多是关键词匹配(_ANALYSIS_STEPS),没有站在逻辑/因果链
  3) 不看 K 线 / 不联动资产 — 涨/跌核心逻辑不清楚
  4) 不自循环 — meta_reflection_updater 只改 .md, 不实际再反思

提供 6 类深度分析:
  A. compute_kline_context    — 决策时点 1h/15m/1d OHLC + ATR + 区间位置
  B. classify_kline_pattern   — K线形态(突破 / 吞没 / 十字星 / 双顶 ...)
  C. compute_cross_asset_sync — 与关联资产的 1h 同步性 [-1,+1]
  D. compute_front_run_score  — 新闻 vs 价格 谁先动(0=未抢跑, 1=完全抢跑)
  E. compute_luck_vs_signal   — 结果是信号驱动还是纯运气 [-1,+1]
  F. compute_deep_root_cause  — 多因子深度归因(替代浅层 root_cause)

主入口: enrich_reflection(reflection_id) → 写满 trade_reflections_v2 + kline_at_decision_cache
并发入口: enrich_pending_loop()         → daemon 每 5 分钟跑一次
"""
from __future__ import annotations

import json
import math
import os
import re
import sqlite3
import time
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

import db


# ═══════════════════════════════════════════════════════════════════════════
# 关联资产映射(决策时,与主品种联动比较)
# ═══════════════════════════════════════════════════════════════════════════
_CROSS_ASSET_MAP: Dict[str, List[str]] = {
    # 加密 — 内部高度联动
    "BTC":  ["ETH", "SOL"],
    "ETH":  ["BTC", "SOL"],
    "SOL":  ["BTC", "ETH"],
    "BNB":  ["BTC", "ETH"],
    "XRP":  ["BTC", "ETH"],
    "DOGE": ["BTC"],
    # 大宗 — 黄金 ↔ 比特币(避险);原油 ↔ 黄金(通胀)
    "XAU":  ["WTI", "BTC"],
    "WTI":  ["XAU", "BTC"],
    "XAG":  ["XAU", "WTI"],
    "HG":   ["WTI", "XAU"],
    # 外汇 — DXY 反向
    "DXY":  ["XAU", "BTC"],
    # 美股 — 纳指 ↔ BTC(风险情绪)
    "NDX":  ["BTC", "XAU"],
    "SPX":  ["XAU", "BTC"],
    "TSLA": ["NDX", "BTC"],
    # 韩股 — 半导体 ↔ BTC
    "005930": ["NDX", "BTC"],
    "000660": ["NDX", "BTC"],
}


def _related_assets(asset: str) -> List[str]:
    """取关联品种(无关联返回空列表)"""
    a = (asset or "").upper().strip()
    return _CROSS_ASSET_MAP.get(a, [])


# ═══════════════════════════════════════════════════════════════════════════
# A. K线上下文 — 决策时点 OHLC + ATR + 区间位置
# ═══════════════════════════════════════════════════════════════════════════

def _query_ohlc_at(conn, asset: str, ts_ms: int, timeframe: str) -> Dict[str, float]:
    """从 kline_bars 查 ts <= decision_ts 最近一根 bar 的 OHLC."""
    if not asset or not ts_ms:
        return {}
    try:
        row = conn.execute(
            """
            SELECT open, high, low, close, volume FROM kline_bars
             WHERE symbol = ? AND timeframe = ? AND ts <= ?
             ORDER BY ts DESC LIMIT 1
            """,
            (asset.upper(), timeframe, int(ts_ms)),
        ).fetchone()
    except sqlite3.OperationalError:
        return {}
    if not row:
        return {}
    return {
        "o": float(row[0]) if row[0] is not None else None,
        "h": float(row[1]) if row[1] is not None else None,
        "l": float(row[2]) if row[2] is not None else None,
        "c": float(row[3]) if row[3] is not None else None,
        "v": float(row[4]) if row[4] is not None else None,
    }


def _atr_pct(conn, asset: str, ts_ms: int, n: int = 14, timeframe: str = "1h") -> float:
    """近 N 根 K线的 ATR / close(%)。空数据 → 0.0."""
    if not asset or not ts_ms:
        return 0.0
    try:
        rows = conn.execute(
            """
            SELECT high, low, close FROM kline_bars
             WHERE symbol = ? AND timeframe = ? AND ts <= ?
             ORDER BY ts DESC LIMIT ?
            """,
            (asset.upper(), timeframe, int(ts_ms), int(n)),
        ).fetchall()
    except sqlite3.OperationalError:
        return 0.0
    if not rows or len(rows) < 2:
        return 0.0
    trs: List[float] = []
    prev_c = None
    for r in rows:
        h, l, c = r[0], r[1], r[2]
        if h is None or l is None or c is None:
            continue
        if prev_c is None:
            tr = h - l
        else:
            tr = max(h - l, abs(h - prev_c), abs(l - prev_c))
        trs.append(tr)
        prev_c = c
    if not trs:
        return 0.0
    atr = sum(trs) / len(trs)
    last_close = rows[0][2] if rows[0][2] else 0
    if last_close <= 0:
        return 0.0
    return round(atr / last_close * 100.0, 4)


def _range_position_7d(conn, asset: str, ts_ms: int, timeframe: str = "1d") -> Tuple[Optional[float], Optional[float], Optional[float]]:
    """近 7 天价格区间: (pos_pct_7d, range_high_7d, range_low_7d).

    v300.5 P0+：无 K线 / 无 ts / SQL 失败时返回 (None, None, None),
    而非 (0.5, 0.0, 0.0) —— 0.5 会被下游 classify_kline_pattern 误判为
    "价格在 7d 区间中点" → 错误落 range_bound / LLM prompt 被污染。
    hi/lo 真存在但 pos 退化(hi<=lo)时仍返 hi/lo,pos=None。
    """
    if not asset or not ts_ms:
        return (None, None, None)
    try:
        rows = conn.execute(
            """
            SELECT high, low, close FROM kline_bars
             WHERE symbol = ? AND timeframe = ? AND ts <= ? AND ts >= ?
             ORDER BY ts ASC
            """,
            (asset.upper(), timeframe, int(ts_ms), int(ts_ms) - 7 * 86400 * 1000),
        ).fetchall()
    except sqlite3.OperationalError:
        return (None, None, None)
    if not rows:
        return (None, None, None)
    highs = [r[0] for r in rows if r[0] is not None]
    lows = [r[1] for r in rows if r[1] is not None]
    closes = [r[2] for r in rows if r[2] is not None]
    if not highs or not lows or not closes:
        return (None, None, None)
    hi, lo = max(highs), min(lows)
    last_c = closes[-1]
    if hi <= lo:
        return (None, hi, lo)
    pos = (last_c - lo) / (hi - lo)
    return (round(pos, 4), round(hi, 4), round(lo, 4))


def compute_kline_context(conn, asset: str, decision_ts_ms: int) -> Dict[str, Any]:
    """A. 决策时点的 K线上下文."""
    asset_u = (asset or "").upper().strip()
    if not asset_u or not decision_ts_ms:
        return {}
    ohlc_1h = _query_ohlc_at(conn, asset_u, decision_ts_ms, "1h")
    ohlc_15m = _query_ohlc_at(conn, asset_u, decision_ts_ms, "15m")
    ohlc_1d = _query_ohlc_at(conn, asset_u, decision_ts_ms, "1d")
    atr = _atr_pct(conn, asset_u, decision_ts_ms, n=14, timeframe="1h")
    pos, hi, lo = _range_position_7d(conn, asset_u, decision_ts_ms, timeframe="1d")
    return {
        "ohlc_1h": ohlc_1h,
        "ohlc_15m": ohlc_15m,
        "ohlc_1d": ohlc_1d,
        "atr_pct": atr,
        "pos_pct_7d": pos,
        "range_high_7d": hi,
        "range_low_7d": lo,
    }


# ═══════════════════════════════════════════════════════════════════════════
# B. K线形态分类
# ═══════════════════════════════════════════════════════════════════════════

def classify_kline_pattern(kline_ctx: Dict[str, Any]) -> Tuple[str, str]:
    """B. 从 K线上下文判定形态. Returns: (label, detail)

    v300.5 P0+：pos_pct_7d=None 时跳过所有 pos 维度判定
    (breakout_up / breakout_down / range_bound),且 detail 中标 N/A 而非 0.50。
    之前: None → 兜底 0.5 → 错误落 range_bound / LLM prompt 污染。
    """
    if not kline_ctx:
        return ("unknown", "")
    h = kline_ctx.get("ohlc_1h") or {}
    if not h or h.get("c") is None:
        return ("unknown", "")
    op, hi, lo, cl = h.get("o"), h.get("h"), h.get("l"), h.get("c")
    if None in (op, hi, lo, cl) or hi <= lo:
        return ("unknown", "")
    body = abs(cl - op)
    rng = hi - lo
    body_pct = body / rng if rng > 0 else 0
    pos = kline_ctx.get("pos_pct_7d")     # v300.5: None 表示无 7d 区间数据
    atr = kline_ctx.get("atr_pct", 0.0)
    pos_str = f"{pos:.2f}" if pos is not None else "N/A"

    if pos is not None and pos >= 0.85 and cl > op and body_pct > 0.7:
        return ("breakout_up", f"7d_pos={pos_str} 1h_body={body_pct:.2f} cl>op")
    if pos is not None and pos <= 0.15 and cl < op and body_pct > 0.7:
        return ("breakout_down", f"7d_pos={pos_str} 1h_body={body_pct:.2f} cl<op")
    if body_pct < 0.1:
        return ("doji", f"1h_body={body_pct:.2f} 极度犹豫")
    if body_pct > 0.9:
        return ("marubozu_bull" if cl > op else "marubozu_bear",
                f"1h_body={body_pct:.2f} 单边{'多' if cl > op else '空'}")
    if pos is not None and 0.4 <= pos <= 0.6 and atr < 1.5:
        return ("range_bound", f"7d_pos={pos_str} atr={atr:.2f}% 区间震荡")
    return ("unknown", f"7d_pos={pos_str} body_pct={body_pct:.2f} atr={atr:.2f}%")


# ═══════════════════════════════════════════════════════════════════════════
# C. 跨资产同步性
# ═══════════════════════════════════════════════════════════════════════════

def _return_1h(conn, asset: str, ts_ms: int) -> Optional[float]:
    """决策时点前 1h 的涨跌幅(%)。从 kline_bars 当前 1h close 与前 1h close 计算."""
    if not asset or not ts_ms:
        return None
    try:
        rows = conn.execute(
            """
            SELECT close, ts FROM kline_bars
             WHERE symbol = ? AND timeframe = '1h' AND ts <= ?
             ORDER BY ts DESC LIMIT 3
            """,
            (asset.upper(), int(ts_ms)),
        ).fetchall()
    except sqlite3.OperationalError:
        return None
    if len(rows) < 2:
        return None
    cur, prev = rows[0], rows[1]
    if not cur or not prev or cur[0] is None or prev[0] is None or prev[0] == 0:
        return None
    return round((cur[0] - prev[0]) / prev[0] * 100.0, 4)


def compute_cross_asset_sync(conn, asset: str, decision_ts_ms: int) -> Tuple[float, Dict[str, Any]]:
    """C. 跨资产同步性. Returns: (sync_score, detail_json), score ∈ [-1, +1]."""
    asset_u = (asset or "").upper().strip()
    if not asset_u:
        return (0.0, {})
    rels = _related_assets(asset_u)
    if not rels:
        return (0.0, {})
    my_move = _return_1h(conn, asset_u, decision_ts_ms)
    if my_move is None or abs(my_move) < 0.0001:
        return (0.0, {"my_1h_pct": my_move or 0.0, "related": {}})
    my_sign = 1 if my_move > 0 else -1
    detail: Dict[str, Any] = {"my_1h_pct": my_move, "related": {}}
    matches: List[float] = []
    for r in rels:
        r_move = _return_1h(conn, r, decision_ts_ms)
        if r_move is None:
            continue
        r_sign = 1 if r_move > 0 else -1
        direction_match = 1 if r_sign == my_sign else -1
        matches.append(float(direction_match))
        detail["related"][r] = {"1h_pct": r_move, "direction_match": direction_match}
    if not matches:
        return (0.0, detail)
    score = sum(matches) / len(matches)
    return (round(score, 4), detail)


# ═══════════════════════════════════════════════════════════════════════════
# D. 抢跑检测
# ═══════════════════════════════════════════════════════════════════════════

def _decision_ts_ms(decision_row: Dict[str, Any]) -> int:
    """从 ai_decisions 行取 ts(epoch ms). ts 字段为 INTEGER ms."""
    try:
        v = decision_row.get("ts")
        if isinstance(v, (int, float)) and v > 0:
            return int(v)
        s = (decision_row.get("created_at") or "").strip()
        for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%dT%H:%M:%SZ"):
            try:
                dt = datetime.strptime(s, fmt)
                if dt.tzinfo is None:
                    dt = dt.replace(tzinfo=timezone.utc)
                return int(dt.timestamp() * 1000)
            except ValueError:
                continue
    except Exception:
        pass
    return 0


def _news_first_seen_ts(conn, news_id: Optional[int]) -> Optional[int]:
    """从 raw_news 取 first_seen_ts(epoch ms). first_seen_ts 在表里是 TEXT,需解析."""
    if not news_id:
        return None
    try:
        row = conn.execute(
            "SELECT first_seen_ts FROM raw_news WHERE id = ?",
            (int(news_id),),
        ).fetchone()
    except sqlite3.OperationalError:
        return None
    if not row:
        return None
    val = row[0]
    if not val:
        return None
    if isinstance(val, (int, float)):
        return int(val) if val > 10**12 else int(val * 1000)
    s = str(val).strip()
    for fmt in ("%Y-%m-%dT%H:%M:%S.%fZ", "%Y-%m-%dT%H:%M:%SZ",
                "%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S"):
        try:
            dt = datetime.strptime(s, fmt)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return int(dt.timestamp() * 1000)
        except ValueError:
            continue
    try:
        v = int(float(s))
        return v if v > 10**12 else v * 1000
    except (ValueError, TypeError):
        return None


def compute_front_run_score(conn, asset: str, decision_ts_ms: int,
                             news_id: Optional[int]) -> Tuple[float, Dict[str, Any]]:
    """D. 抢跑检测. Returns: (score, detail), score ∈ [0, 1]."""
    asset_u = (asset or "").upper().strip()
    detail: Dict[str, Any] = {"news_id": news_id, "delta_min": None}
    if not asset_u or not decision_ts_ms:
        return (0.0, detail)
    news_ts = _news_first_seen_ts(conn, news_id)
    if not news_ts:
        return (0.0, detail)
    delta_ms = decision_ts_ms - news_ts
    delta_min = delta_ms / 60000.0
    detail["delta_min"] = round(delta_min, 1)
    price_1h_pct = _return_1h(conn, asset_u, decision_ts_ms)
    if price_1h_pct is not None and abs(price_1h_pct) < 0.05:
        detail["note"] = "price_unmoved_score_reset_to_zero"
        return (0.0, detail)
    if delta_min < 0:
        return (0.0, detail)
    if delta_min <= 5:
        return (0.1, detail)
    if delta_min <= 30:
        return (round(0.3 + (delta_min - 5) / 25.0 * 0.4, 4), detail)
    extra = min((delta_min - 30) / 60.0, 1.0)
    return (round(0.7 + 0.3 * extra, 4), detail)


# ═══════════════════════════════════════════════════════════════════════════
# E. 运气 vs 信号
# ═══════════════════════════════════════════════════════════════════════════

def compute_luck_vs_signal(reflection_row: Dict[str, Any],
                            kline_ctx: Dict[str, Any],
                            cross_sync: float,
                            front_run: float) -> Tuple[float, str]:
    """E. 区分运气 vs 信号驱动. Returns: (score, detail), score ∈ [-1, +1]."""
    pnl = float(reflection_row.get("pnl_pct") or 0.0)
    mfe = float(reflection_row.get("mfe_pct") or 0.0)
    mae = float(reflection_row.get("mae_pct") or 0.0)
    event_strength = (reflection_row.get("event_strength") or "").lower()
    market_conf = (reflection_row.get("market_confirmation") or "").lower()

    win = pnl > 0.005
    loss = pnl < -0.005
    if not win and not loss:
        return (0.0, "pnl_neutral")

    base_skill = 0.5 if win else -0.5
    notes: List[str] = [f"base={'+0.5' if win else '-0.5'}({'WIN' if win else 'LOSS'})"]

    if front_run >= 0.5:
        base_skill -= 0.4 if win else 0.2
        notes.append(f"front_run={front_run} 信号弱")

    if cross_sync == 0:
        base_skill -= 0.15 if win else 0.10
        notes.append("cross_sync=0 无联动确认")
    elif cross_sync < 0:
        base_skill -= 0.2 if win else 0.2
        notes.append(f"cross_sync={cross_sync} 跨资产反向")

    pattern = (kline_ctx or {}).get("pattern_label") if kline_ctx else None
    if pattern in ("breakout_up", "marubozu_bull") and win:
        base_skill += 0.15
        notes.append(f"pattern={pattern} 趋势确认")
    elif pattern in ("breakout_down", "marubozu_bear") and loss:
        base_skill += 0.15
        notes.append(f"pattern={pattern} 趋势确认")
    elif pattern in ("doji", "range_bound"):
        if win:
            base_skill -= 0.15
        notes.append(f"pattern={pattern} 震荡易被随机")

    if market_conf in ("no", "negative", "denied"):
        if win:
            base_skill -= 0.3
            notes.append(f"market_conf={market_conf} 盘面否认")
        else:
            base_skill += 0.1
            notes.append(f"market_conf={market_conf} 盘面否认+亏=信号真错")

    if event_strength == "low" and win:
        base_skill -= 0.2
        notes.append("event_strength=low WIN 接近运气")

    atr = (kline_ctx or {}).get("atr_pct") or 0.0
    if atr > 0 and (abs(mfe) + abs(mae)) > 0:
        ratio = (abs(mfe - mae)) / atr
        if ratio < 1.5 and abs(pnl) < atr * 0.5:
            if win:
                base_skill -= 0.1
            notes.append(f"mfe_mae_diff={ratio:.2f} atr 窄震荡")

    score = max(-1.0, min(1.0, round(base_skill, 4)))
    return (score, " | ".join(notes))


# ═══════════════════════════════════════════════════════════════════════════
# F. 深度根因 + 教训
# ═══════════════════════════════════════════════════════════════════════════

_PATTERN_DEEP_HINT = {
    "breakout_up":      "price breaks 7d high + 1h big bullish candle - trend following high EV, loss = true reversal / false breakout",
    "breakout_down":    "price breaks 7d low + 1h big bearish candle - trend following high EV, loss = true bounce / false breakdown",
    "doji":             "1h doji - extreme indecision, no direction, any directional bet is a guess",
    "marubozu_bull":    "1h big bullish marubozu - strong one-way up, loss = catching falling knife / end acceleration",
    "marubozu_bear":    "1h big bearish marubozu - strong one-way down, loss = catching falling knife / end acceleration",
    "range_bound":      "7d mid-range + ATR low - choppy, chasing breakout gets stopped out, mean-reversion better",
    "inside_bar":       "1h inside previous 1h - coiling, direction undetermined",
    "engulfing_bull":   "1h bullish engulfing - reversal signal, needs next candle confirm",
    "engulfing_bear":   "1h bearish engulfing - reversal signal, needs next candle confirm",
    "double_top":       "7d double top - strong resistance, long risky",
    "double_bottom":    "7d double bottom - strong support, short risky",
    "hammer":           "1h hammer - bottom reversal",
    "shooting_star":    "1h shooting star - top reversal",
    "unknown":          "",
}


def compute_deep_root_cause(reflection_row: Dict[str, Any],
                             kline_ctx: Dict[str, Any],
                             kline_pattern: str,
                             cross_sync: float,
                             front_run: float,
                             luck_score: float) -> Tuple[str, str]:
    """F. 深度根因 + 教训. Returns: (deep_root_cause, deep_lesson)"""
    pnl = float(reflection_row.get("pnl_pct") or 0.0)
    mfe = float(reflection_row.get("mfe_pct") or 0.0)
    mae = float(reflection_row.get("mae_pct") or 0.0)
    asset = reflection_row.get("asset") or "?"
    action = reflection_row.get("action") or "HOLD"
    win = pnl > 0.005
    loss = pnl < -0.005

    facts: List[str] = []
    facts.append(f"{asset} {action} pnl={pnl:+.3f}% mfe={mfe:+.2f}% mae={mae:+.2f}%")
    if kline_pattern and kline_pattern != "unknown":
        facts.append(f"K-line pattern={kline_pattern}")
        if _PATTERN_DEEP_HINT.get(kline_pattern):
            facts.append(f"  meaning: {_PATTERN_DEEP_HINT[kline_pattern]}")
    if front_run >= 0.3:
        facts.append(f"front_run score={front_run:.2f} - price moved before news")
    elif front_run > 0:
        facts.append(f"mild front_run score={front_run:.2f}")
    if cross_sync == 0:
        facts.append("cross_asset no data - missing independent verification")
    elif cross_sync > 0.5:
        facts.append(f"cross_asset sync={cross_sync:.2f} - multi-asset confirmed")
    elif cross_sync < -0.5:
        facts.append(f"cross_asset reverse={cross_sync:.2f} - multi-asset denied")

    causes: List[str] = []
    lessons: List[str] = []

    if front_run >= 0.5 and loss:
        causes.append("news->decision time gap too large, price already moved, entry = catching falling knife")
        lessons.append("rule: when first_seen_ts earlier than decision >15min, drop confidence one level")

    if kline_pattern in ("breakout_up", "marubozu_bull") and action == "SELL":
        causes.append("price strong breakout/bullish marubozu, subjective short = counter-trend")
        lessons.append("counter-trend entry must wait for 4h reversal candle")
    if kline_pattern in ("breakout_down", "marubozu_bear") and action == "BUY":
        causes.append("price strong breakdown/bearish marubozu, subjective long = counter-trend")
        lessons.append("same as above in reverse")
    if kline_pattern == "range_bound":
        causes.append("range market, chasing breakout gets stopped out")
        lessons.append("range market: forced HOLD or mean-reversion, do not chase breakout")

    if cross_sync < -0.3 and loss:
        causes.append(f"cross_asset same-direction reverse ({cross_sync:.2f}), single-asset signal invalid")
        lessons.append("single asset BUY/SELL requires >=1 cross_asset same-direction confirm; else HOLD")

    if luck_score < -0.3 and win:
        causes.append(f"luck_score={luck_score:.2f}, this WIN likely random (no cross_asset, market denied)")
        lessons.append("low luck_score WIN should not be promoted to experience, possibly noise")
    if luck_score < -0.3 and loss:
        causes.append(f"luck_score={luck_score:.2f}, this LOSS purely luck")
        lessons.append("luck-induced LOSS should not enter review_insights, avoid polluting rule library")
    if luck_score > 0.3 and loss:
        causes.append(f"signal correct (luck={luck_score:.2f}) but pnl={pnl:+.3f}% - execution/sizing/slippage issue")
        lessons.append("signal-correct but loss: check tp1/tp2/sl triggers vs plan")

    if mfe > 0 and mae < 0 and abs(mfe) > 3 * abs(mae) and loss:
        causes.append(f"MFE={mfe:+.2f}% big floating profit given back, no trail stop at MFE high")
        lessons.append("set trail stop: when MFE > 1xATR, move sl to entry+0.5xATR")
    if mae < 0 and mfe > 0 and abs(mae) > 3 * abs(mfe) and loss:
        causes.append(f"MAE={mae:+.2f}% deep drawdown then small bounce stopped out, entry timing bad")
        lessons.append("before entry check if 15m already moved; if yes, wait for pullback")

    if not causes:
        if win:
            causes.append("decision chain complete: event->reason->action->price consistent")
            lessons.append("keep successful path; can repeat in same regime")
        else:
            causes.append(f"chain multi-segment uncertain: event_strength={reflection_row.get('event_strength','?')}, market_confirm={reflection_row.get('market_confirmation','?')}")
            lessons.append("complete chain required: event_strength >= medium + cross_asset >=1 + counter_consensus keyword; missing one = HOLD")

    deep_root = " | ".join([f"[{i+1}] {c}" for i, c in enumerate(causes)])
    deep_lesson = " | ".join([f"[{i+1}] {l}" for i, l in enumerate(lessons)])
    deep_root = "FACT: " + "; ".join(facts) + " || CAUSE: " + deep_root
    deep_lesson = "LESSON: " + deep_lesson
    return (deep_root[:2000], deep_lesson[:1500])


# ═══════════════════════════════════════════════════════════════════════════
# 主入口: enrich_reflection()
# ═══════════════════════════════════════════════════════════════════════════

def _load_reflection_row(conn, reflection_id: int) -> Optional[Dict[str, Any]]:
    """读 trade_reflections_v2 一行 + JOIN ai_decisions."""
    try:
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            """
            SELECT r.*,
                   a.ts AS a_ts, a.news_id AS a_news_id,
                   a.target_asset AS a_target_asset,
                   a.event_strength AS a_event_strength,
                   a.market_confirmation AS a_market_confirmation,
                   a.completeness_score AS a_completeness_score
              FROM trade_reflections_v2 r
              LEFT JOIN ai_decisions a ON a.id = r.decision_id
             WHERE r.id = ?
            """,
            (int(reflection_id),),
        ).fetchone()
    except sqlite3.OperationalError:
        return None
    if not row:
        return None
    return dict(row)


def _save_kline_cache(conn, decision_id: int, asset: str, decision_ts_ms: int,
                        kline_ctx: Dict[str, Any], cross_detail: Dict[str, Any],
                        pattern_label: str, pattern_detail: str,
                        front_run: float) -> None:
    """写 kline_at_decision_cache."""
    try:
        conn.execute(
            """
            INSERT OR REPLACE INTO kline_at_decision_cache
              (decision_id, asset, decision_ts, ohlc_1h_json, ohlc_15m_json, ohlc_1d_json,
               atr_pct, pos_pct_7d, range_high_7d, range_low_7d, cross_asset_json,
               pattern_label, pattern_detail, front_run_score, computed_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, datetime('now'))
            """,
            (
                int(decision_id), (asset or "")[:16], int(decision_ts_ms),
                json.dumps(kline_ctx.get("ohlc_1h") or {}, ensure_ascii=False),
                json.dumps(kline_ctx.get("ohlc_15m") or {}, ensure_ascii=False),
                json.dumps(kline_ctx.get("ohlc_1d") or {}, ensure_ascii=False),
                float(kline_ctx.get("atr_pct") or 0.0),
                # v300.5 P0+：pos_pct_7d=None 直传,不再 `or 0.5` 兜底 —
                # 0.5 会被下游 read 端误判为"价格中点"。SQLite 接受 None → NULL。
                kline_ctx.get("pos_pct_7d"),
                float(kline_ctx.get("range_high_7d") or 0.0),
                float(kline_ctx.get("range_low_7d") or 0.0),
                json.dumps(cross_detail or {}, ensure_ascii=False),
                (pattern_label or "unknown")[:32],
                (pattern_detail or "")[:200],
                float(front_run or 0.0),
            ),
        )
    except sqlite3.OperationalError as _e:
        print(f"[DEEP] kline cache write failed: {_e}", flush=True)


def enrich_reflection(reflection_id: int, *, write: bool = True) -> Dict[str, Any]:
    """对一条 trade_reflections_v2 做 v230 深度增强.

    Returns: dict 含 13 个新字段值 + enrich_status
    """
    conn = db.get_connection()
    out: Dict[str, Any] = {"reflection_id": int(reflection_id), "ok": False,
                           "enrich_status": "failed", "enrich_error": ""}
    try:
        row = _load_reflection_row(conn, int(reflection_id))
        if not row:
            out["enrich_error"] = "reflection_not_found"
            return out
        decision_id = int(row.get("decision_id") or 0)
        asset = (row.get("asset") or row.get("a_target_asset") or "").upper().strip()
        if not asset:
            out["enrich_error"] = "no_asset"
            return out
        decision_ts_ms = _decision_ts_ms(row) or int(row.get("a_ts") or 0)
        if not decision_ts_ms:
            out["enrich_error"] = "no_decision_ts"
            return out
        news_id = row.get("a_news_id") or row.get("news_id")

        kline_ctx = compute_kline_context(conn, asset, decision_ts_ms)
        if not kline_ctx:
            out["enrich_status"] = "partial"
            out["enrich_error"] = "no_kline_data"
        out["kline_at_decision"] = json.dumps(kline_ctx, ensure_ascii=False)

        pattern_label, pattern_detail = classify_kline_pattern(kline_ctx)
        out["kline_pattern"] = pattern_label

        cross_sync, cross_detail = compute_cross_asset_sync(conn, asset, decision_ts_ms)
        out["cross_asset_sync_score"] = cross_sync

        front_run, front_detail = compute_front_run_score(conn, asset, decision_ts_ms, news_id)
        out["front_run_score"] = front_run

        luck_score, luck_detail = compute_luck_vs_signal(row, kline_ctx, cross_sync, front_run)
        out["luck_vs_signal_score"] = luck_score
        out["_luck_detail"] = luck_detail

        deep_root, deep_lesson = compute_deep_root_cause(
            row, kline_ctx, pattern_label, cross_sync, front_run, luck_score)
        out["deep_root_cause"] = deep_root
        out["deep_lesson"] = deep_lesson

        regime_detail = {
            "atr_pct": kline_ctx.get("atr_pct"),
            "pos_pct_7d": kline_ctx.get("pos_pct_7d"),
            "kline_pattern": pattern_label,
            "front_run_score": front_run,
            "cross_sync_score": cross_sync,
        }
        out["market_regime_detail"] = json.dumps(regime_detail, ensure_ascii=False)
        out["_cross_detail"] = cross_detail
        out["_pattern_detail"] = pattern_detail
        out["_kline_ctx"] = kline_ctx

        if write:
            try:
                conn.execute(
                    """
                    UPDATE trade_reflections_v2
                       SET kline_at_decision = ?, kline_pattern = ?,
                           cross_asset_sync_score = ?, front_run_score = ?,
                           luck_vs_signal_score = ?, market_regime_detail = ?,
                           deep_root_cause = ?, deep_lesson = ?,
                           enriched_at = datetime('now'),
                           enrich_status = ?, enrich_error = ?
                     WHERE id = ?
                    """,
                    (
                        out["kline_at_decision"], out["kline_pattern"],
                        out["cross_asset_sync_score"], out["front_run_score"],
                        out["luck_vs_signal_score"], out["market_regime_detail"],
                        out["deep_root_cause"], out["deep_lesson"],
                        "ok" if out["enrich_status"] != "partial" else "partial",
                        out.get("enrich_error", ""),
                        int(reflection_id),
                    ),
                )
                _save_kline_cache(conn, decision_id, asset, decision_ts_ms,
                                   kline_ctx, cross_detail, pattern_label,
                                   pattern_detail, front_run)
                conn.commit()
                out["ok"] = True
                if out["enrich_status"] != "partial":
                    out["enrich_status"] = "ok"
            except sqlite3.OperationalError as _e:
                out["enrich_error"] = f"write_failed:{_e}"
                out["enrich_status"] = "failed"
                try:
                    conn.rollback()
                except Exception:
                    pass
        return out
    finally:
        try:
            conn.close()
        except Exception:
            pass


def enrich_pending_loop(batch_size: int = 50, *,
                         max_per_run: int = 200,
                         only_enrich_status: str = "pending") -> Dict[str, Any]:
    """daemon 入口 — 扫 pending 的反思做 enrich."""
    conn = db.get_connection()
    out = {"scanned": 0, "ok": 0, "partial": 0, "failed": 0, "skipped": 0, "errors": []}
    try:
        rows = conn.execute(
            """
            SELECT id FROM trade_reflections_v2
             WHERE (enrich_status = ? OR enrich_status = '' OR enrich_status IS NULL)
             ORDER BY id DESC LIMIT ?
            """,
            (only_enrich_status, int(max_per_run)),
        ).fetchall()
    except sqlite3.OperationalError as _e:
        out["errors"].append(str(_e))
        conn.close()
        return out
    for r in rows:
        rid = int(r[0])
        out["scanned"] += 1
        try:
            r_out = enrich_reflection(rid, write=True)
            st = r_out.get("enrich_status") or "failed"
            if st == "ok":
                out["ok"] += 1
            elif st == "partial":
                out["partial"] += 1
            else:
                out["failed"] += 1
                if r_out.get("enrich_error"):
                    out["errors"].append(f"id={rid}: {r_out['enrich_error']}")
        except Exception as _e:
            out["failed"] += 1
            out["errors"].append(f"id={rid}: {type(_e).__name__}: {_e}")
        if out["scanned"] >= batch_size:
            break
    conn.close()
    return out


def get_reflection_deep_stats(window_days: int = 30) -> Dict[str, Any]:
    """统计 v230 深度反思概况(给前端/仪表板)."""
    conn = db.get_connection()
    out: Dict[str, Any] = {"window_days": window_days, "by_pattern": {},
                            "by_luck_band": {}, "by_front_run": {},
                            "by_cross_sync": {}, "enrich_status_breakdown": {},
                            "deep_lessons_top": [], "ok": False}
    try:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            """
            SELECT kline_pattern,
                   SUM(CASE WHEN pnl_pct > 0.005 THEN 1 ELSE 0 END) AS win_n,
                   SUM(CASE WHEN pnl_pct < -0.005 THEN 1 ELSE 0 END) AS loss_n,
                   COUNT(*) AS n
              FROM trade_reflections_v2
             WHERE enrich_status IN ('ok','partial')
               AND enriched_at >= datetime('now', ?)
             GROUP BY kline_pattern
             ORDER BY n DESC
            """,
            (f"-{int(window_days)} days",),
        ).fetchall()
        for r in rows:
            pat = r[0] or "unknown"
            n = int(r[3] or 0)
            win = int(r[1] or 0)
            loss = int(r[2] or 0)
            wr = win / n if n > 0 else 0
            out["by_pattern"][pat] = {"n": n, "win": win, "loss": loss, "win_rate": round(wr, 3)}

        for lo, hi, label in [(-1.0, -0.5, "luck_-1_to_-0.5"),
                               (-0.5, 0.0, "luck_-0.5_to_0"),
                               (0.0, 0.5, "luck_0_to_0.5"),
                               (0.5, 1.01, "luck_0.5_to_1")]:
            row = conn.execute(
                """
                SELECT COUNT(*),
                       SUM(CASE WHEN pnl_pct > 0.005 THEN 1 ELSE 0 END),
                       SUM(CASE WHEN pnl_pct < -0.005 THEN 1 ELSE 0 END)
                  FROM trade_reflections_v2
                 WHERE luck_vs_signal_score >= ? AND luck_vs_signal_score < ?
                   AND enrich_status IN ('ok','partial')
                """,
                (lo, hi),
            ).fetchone()
            n = int(row[0] or 0)
            win = int(row[1] or 0)
            loss = int(row[2] or 0)
            out["by_luck_band"][label] = {"n": n, "win": win, "loss": loss,
                                           "win_rate": round(win/n, 3) if n > 0 else 0}

        for lo, hi, label in [(0.0, 0.3, "front_run_0_to_0.3"),
                               (0.3, 0.7, "front_run_0.3_to_0.7"),
                               (0.7, 1.01, "front_run_0.7_to_1")]:
            row = conn.execute(
                """
                SELECT COUNT(*),
                       SUM(CASE WHEN pnl_pct > 0.005 THEN 1 ELSE 0 END),
                       SUM(CASE WHEN pnl_pct < -0.005 THEN 1 ELSE 0 END)
                  FROM trade_reflections_v2
                 WHERE front_run_score >= ? AND front_run_score < ?
                   AND enrich_status IN ('ok','partial')
                """,
                (lo, hi),
            ).fetchone()
            n = int(row[0] or 0)
            win = int(row[1] or 0)
            loss = int(row[2] or 0)
            out["by_front_run"][label] = {"n": n, "win": win, "loss": loss,
                                           "win_rate": round(win/n, 3) if n > 0 else 0}

        for lo, hi, label in [(-1.01, -0.5, "sync_-1_to_-0.5"),
                               (-0.5, 0.5, "sync_-0.5_to_0.5"),
                               (0.5, 1.01, "sync_0.5_to_1")]:
            row = conn.execute(
                """
                SELECT COUNT(*),
                       SUM(CASE WHEN pnl_pct > 0.005 THEN 1 ELSE 0 END),
                       SUM(CASE WHEN pnl_pct < -0.005 THEN 1 ELSE 0 END)
                  FROM trade_reflections_v2
                 WHERE cross_asset_sync_score >= ? AND cross_asset_sync_score < ?
                   AND enrich_status IN ('ok','partial')
                """,
                (lo, hi),
            ).fetchone()
            n = int(row[0] or 0)
            win = int(row[1] or 0)
            loss = int(row[2] or 0)
            out["by_cross_sync"][label] = {"n": n, "win": win, "loss": loss,
                                            "win_rate": round(win/n, 3) if n > 0 else 0}

        rows = conn.execute(
            """
            SELECT enrich_status, COUNT(*) FROM trade_reflections_v2
             WHERE id > 0 GROUP BY enrich_status
            """,
        ).fetchall()
        for r in rows:
            out["enrich_status_breakdown"][r[0] or "unknown"] = int(r[1] or 0)

        rows = conn.execute(
            """
            SELECT id, asset, action, pnl_pct, deep_root_cause, deep_lesson,
                   kline_pattern, front_run_score, luck_vs_signal_score
              FROM trade_reflections_v2
             WHERE deep_lesson != '' AND enrich_status = 'ok'
             ORDER BY id DESC LIMIT 20
            """,
        ).fetchall()
        out["deep_lessons_top"] = [dict(r) for r in rows]
        out["ok"] = True
    except sqlite3.OperationalError as _e:
        out["error"] = str(_e)
    finally:
        try:
            conn.close()
        except Exception:
            pass
    return out


# CLI 入口
if __name__ == "__main__":
    import sys
    if len(sys.argv) >= 2 and sys.argv[1] == "enrich":
        if len(sys.argv) >= 3:
            rid = int(sys.argv[2])
            print(json.dumps(enrich_reflection(rid, write=True), ensure_ascii=False, indent=2))
        else:
            r = enrich_pending_loop(batch_size=200, max_per_run=200)
            print(json.dumps(r, ensure_ascii=False, indent=2))
    elif len(sys.argv) >= 2 and sys.argv[1] == "stats":
        window = int(sys.argv[2]) if len(sys.argv) >= 3 else 30
        print(json.dumps(get_reflection_deep_stats(window_days=window),
                         ensure_ascii=False, indent=2))
    else:
        print("Usage:")
        print("  python reflection_deep.py enrich [reflection_id]")
        print("  python reflection_deep.py enrich              # all pending")
        print("  python reflection_deep.py stats [window_days]")
