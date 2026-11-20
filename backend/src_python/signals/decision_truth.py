# -*- coding: utf-8 -*-
"""decision_truth.py — 决策之后的**真实市场结果** (v313 P1)

背景 (为什么需要这个模块)
------------------------
验收 #2 "准确评估信号权重 (自动学习)" 的判据是:

    决策时点权重 × **可判定真值** ≥ 50 条, 且
    Spearman(权重, |实际涨跌|) ≥ 0.2 (n ≥ 200)

真值这一侧此前只有一个来源: `ai_decisions.forward_pnl`。但它是
**模拟盘这一笔单的净盈亏**, 于是:

  · 只有真正开仓的 BUY/SELL 才有值 —— 实测 4605 条决策里只有 **8** 条;
  · HOLD 决策 (占 94.7%) 永远为 NULL —— 而 HOLD 恰恰是权重表里的大多数;
  · 于是"事前权重 × 真值"只能配出 **5 条**, 判据要求的 ≥50 / n≥200 根本无从谈起。

所以真值这一侧需要另一个来源: **市场自己走了多少**, 与是否开仓无关。
本模块用已落库的**真实 K 线** (kline_bars) 算这个数, 与
`missed_signal_taxonomy.compute_outcome` 用真实行情算漏检结果的做法一致 ——
只认真实数据, 不做任何预测, 数据不足就返回 None (不编造)。

口径
----
  · 起点: 决策时刻 ts 之前最近的一根 K 线收盘价 (不晚于决策, 避免用到未来价)
  · 终点: 决策时刻 + 持有期之后最近的一根 K 线收盘价
  · 持有期: 由 expected_horizon 映射 (intraday→1h, 1-3d→3d, 1w+→7d)
  · 返回: 百分比 (涨为正跌为负); **数据不足返回 None**, 绝不返回 0 冒充"没动"

为什么不用 expected_horizon 的下限: 那是个粗粒度标签 ("1-3d" / "1w+"),
要可判定就得落到具体秒数, 这里取该区间的**上界** (最保守: 至少要走满 3 天才算),
否则"1-3d"在第 1 天就被判定, 等于用 1 天的行情冒充 3 天的结果。
"""
from __future__ import annotations

import bisect
import sqlite3
import time
from typing import Any, Dict, List, Optional, Sequence, Tuple

# expected_horizon → 持有期秒数 (取区间上界, 见模块 docstring)
HORIZON_SECONDS: Dict[str, int] = {
    "intraday": 3600,          # 1 小时
    "1-3d": 3 * 86400,         # 3 天
    "1w+": 7 * 86400,          # 7 天
}
DEFAULT_HORIZON_SECONDS = 3 * 86400

# 允许使用的时间框架 (按精度从高到低), 取第一个"够用"的
_TIMEFRAMES: Tuple[str, ...] = ("15m", "1m", "1h", "4h", "1d")

# 起点/终点允许的最大陈旧度 (毫秒): 找不到足够近的 bar 就判为数据不足
_MAX_STALE_MS = 3 * 3600 * 1000

# v337: 陈旧度必须按时间框架分别设定。
# 原来所有框架共用 3 小时，导致**日线根本不可能被用上**：一根 1d bar 距离
# 任意时刻天然就有 0~24h 的偏差，永远 > 3h。后果是只有日线的品种
# （WTI 在补数据前、AAPL/NVDA/MSFT/SPX…）永远算不出真值，
# S8 卡在 88.2%。日线允许 4 天（覆盖周末 + 一个休市日）。
_STALE_BY_TF: Dict[str, int] = {
    "1m": 3 * 3600 * 1000,
    "5m": 3 * 3600 * 1000,
    "10m": 3 * 3600 * 1000,
    "15m": 3 * 3600 * 1000,
    "30m": 3 * 3600 * 1000,
    "1h": 90 * 60 * 1000,
    "4h": 5 * 3600 * 1000,
    "1d": 4 * 86400 * 1000,
}


class KlineIndex:
    """把 kline_bars 读进内存, 按 (symbol, timeframe) 建有序索引。

    一次装载、全库复用 —— 否则每条决策都去查一次 SQLite, 上千条决策
    会把验收脚本拖到分钟级。
    """

    def __init__(self, conn: sqlite3.Connection) -> None:
        self._series: Dict[Tuple[str, str], Tuple[List[int], List[float]]] = {}
        try:
            rows = conn.execute(
                "SELECT symbol, timeframe, ts, close FROM kline_bars ORDER BY symbol, timeframe, ts"
            ).fetchall()
        except sqlite3.Error:
            rows = []
        for r in rows:
            # 不依赖 row_factory —— db.get_connection() 返回的是 tuple 行,
            # 而 sqlite3.Row 也能按下标取, 所以统一用下标。
            sym = str(r[0] or "").upper()
            tf = str(r[1] or "")
            ts = int(r[2] or 0)
            close = r[3]
            if close is None:
                continue
            key = (sym, tf)
            series = self._series.get(key)
            if series is None:
                series = ([], [])
                self._series[key] = series
            series[0].append(ts)
            series[1].append(float(close))

    def close_at(self, symbol: str, timeframe: str, ts_ms: int) -> Optional[float]:
        """返回 ts_ms 时刻**之前最近**的一根 bar 的收盘价。

        陈旧度超过 _MAX_STALE_MS 返回 None —— 半年前的一根 bar 不能代表"现在的价"。
        """
        series = self._series.get((symbol.upper(), timeframe))
        if not series:
            return None
        times, closes = series
        i = bisect.bisect_right(times, ts_ms) - 1
        if i < 0:
            return None
        max_stale = _STALE_BY_TF.get(timeframe, _MAX_STALE_MS)
        if ts_ms - times[i] > max_stale:
            return None
        return closes[i]

    def price_at(self, symbol: str, ts_ms: int) -> Optional[Tuple[float, str]]:
        """在所有时间框架里找一个能代表 ts_ms 时刻的收盘价。返回 (价, 框架)。"""
        for tf in _TIMEFRAMES:
            px = self.close_at(symbol, tf, ts_ms)
            if px is not None:
                return px, tf
        return None


def horizon_seconds(expected_horizon: Optional[str]) -> int:
    """expected_horizon 标签 → 持有期秒数。未知标签用默认 3 天。"""
    if not expected_horizon:
        return DEFAULT_HORIZON_SECONDS
    return HORIZON_SECONDS.get(str(expected_horizon).strip(), DEFAULT_HORIZON_SECONDS)


def compute_forward_move(
    index: KlineIndex,
    asset: Optional[str],
    decision_ts: Optional[int],
    expected_horizon: Optional[str],
    now_ts: Optional[int] = None,
) -> Optional[float]:
    """算这个决策之后市场实际走了多少 (%)。数据不足返回 None。

    Returns:
        百分比涨跌幅 (正=涨), 或 None (数据不足 / 未到期 / 品种无法定)
    """
    if not asset or not decision_ts:
        return None
    asset = str(asset).upper()
    if asset in ("", "NONE"):
        return None
    now = int(now_ts or time.time())
    horizon = horizon_seconds(expected_horizon)
    t0 = int(decision_ts) * 1000
    t1 = t0 + horizon * 1000
    if t1 > now * 1000:
        return None      # 持有期还没走完 —— 现在判定就是拿未来数据作弊
    entry = index.price_at(asset, t0)
    if entry is None:
        return None
    exit_ = index.price_at(asset, t1)
    if exit_ is None:
        return None
    p0, p1 = entry[0], exit_[0]
    if not p0:
        return None
    return round((p1 - p0) / p0 * 100.0, 4)


def collect_decision_truth(
    conn: sqlite3.Connection,
    index: Optional[KlineIndex] = None,
    now_ts: Optional[int] = None,
    limit: int = 5000,
) -> List[Dict[str, Any]]:
    """扫 ai_decisions, 算出每条决策之后的真实市场涨跌。

    只处理**还没算过**的 (forward_move_pct IS NULL), 幂等。
    """
    idx = index if index is not None else KlineIndex(conn)
    now = int(now_ts or time.time())
    try:
        rows = conn.execute(
            """SELECT id, target_asset, ts, created_at, expected_horizon
               FROM ai_decisions
               WHERE forward_move_pct IS NULL AND target_asset IS NOT NULL
                 AND target_asset NOT IN ('', 'NONE')
               ORDER BY id DESC LIMIT ?""",
            (int(limit),),
        ).fetchall()
    except sqlite3.Error:
        return []
    out: List[Dict[str, Any]] = []
    for r in rows:
        # 下标取值, 不依赖 row_factory (db.get_connection() 给的是 tuple 行)
        aid = int(r[0] or 0)
        asset = r[1]
        ts = r[2]
        created_at = r[3]
        exp_horizon = r[4]
        if not ts:
            ca = str(created_at or "")
            try:
                from datetime import datetime
                ts = int(datetime.fromisoformat(ca).timestamp())
            except Exception:
                continue
        mv = compute_forward_move(idx, asset, ts, exp_horizon, now_ts=now)
        if mv is None:
            continue
        out.append({
            "id": aid,
            "asset": asset,
            "ts": int(ts),
            "expected_horizon": exp_horizon,
            "forward_move_pct": mv,
        })
    return out


def persist_truth(
    conn: sqlite3.Connection,
    truths: Sequence[Dict[str, Any]],
) -> int:
    """把算出来的真实涨跌写回 ai_decisions.forward_move_pct。返回写入行数。"""
    n = 0
    for t in truths:
        try:
            conn.execute(
                "UPDATE ai_decisions SET forward_move_pct=? WHERE id=?",
                (t["forward_move_pct"], t["id"]),
            )
            n += 1
        except sqlite3.Error:
            continue
    if n:
        conn.commit()
    return n


__all__ = [
    "HORIZON_SECONDS", "DEFAULT_HORIZON_SECONDS",
    "KlineIndex", "horizon_seconds", "compute_forward_move",
    "collect_decision_truth", "persist_truth",
    "DEFAULT_BREAKEVEN_PCT", "breakeven_pct", "signed_move", "derive_verdict",
    "collect_truth_verdicts", "persist_truth_verdicts",
]


# ============================================================================
# v315 P0: 把"市场实际涨跌"变成**可结算的胜负** (闭环接线)
# ============================================================================
#
# 背景: 验收 #2 的闭环里, source_stats (按 source×asset 聚合命中率) 只消费
# ai_decisions.forward_pnl —— 那是**模拟盘这一笔单的净盈亏**, 只有真正开仓的
# BUY/SELL 才有值 (实测 4605 条决策里 8 条)。于是:
#   · source_stats 全库只有 4 个 key / 26 个样本, source_mult 几乎恒为 1.0;
#   · 前向反馈 (decision_truth 算出的 forward_move_pct) 有 1495 条, 却**从不**
#     进入结算闭环 —— 权重学到了"信号有多可信", 学不到"这个来源准不准"。
#
# 接线方式: forward_move_pct (市场走了多少) + suggested_action (方向) →
#   WIN / LOSS / BREAKEVEN。纯函数, 不预测, 只做"事后判定"。
# 口径选择:
#   · 只有 BUY/SELL 能判定方向; HOLD 没有方向 → 不参与命中分母 (记 BREAKEVEN
#     会被当成"没打中", 把命中率算低)。实测 HOLD 占 94.7%, 放进来没有信息量。
#   · |涨跌| 在 ±阈值 内算 BREAKEVEN, 不计入命中分母 (与
#     update_source_stats_from_settlement 的 win+loss 口径一致)。
#   · 阈值默认 0.3% —— 3 天持有期上, 小于 0.3% 的波动与噪声无法区分。

DEFAULT_BREAKEVEN_PCT = 0.3


def breakeven_pct(default: float = DEFAULT_BREAKEVEN_PCT) -> float:
    """判定阈值 (%), 可用环境变量 TRUTH_BREAKEVEN_PCT 覆盖。"""
    import os
    raw = os.environ.get("TRUTH_BREAKEVEN_PCT")
    if raw is None:
        return float(default)
    try:
        val = float(raw)
    except (TypeError, ValueError):
        return float(default)
    return val if val >= 0 else float(default)


def signed_move(action, forward_move_pct) -> Optional[float]:
    """把"市场涨跌幅"折算成"这笔信号带方向的结果" (%):

        BUY  → 原值    (涨 = 对)
        SELL → 取反    (跌 = 对)
        其它 → None   (HOLD 没有方向, 不可判定)

    返回的数值同时用作 source_stats 的 avg_forward_pnl (EMA), 语义是
    "该来源该品种上, 带方向的单次收益期望 (%)"。
    """
    if forward_move_pct is None:
        return None
    try:
        mv = float(forward_move_pct)
    except (TypeError, ValueError):
        return None
    act = str(action or "").strip().upper()
    if act == "BUY":
        return mv
    if act == "SELL":
        return -mv
    return None


def derive_verdict(action, forward_move_pct, threshold: Optional[float] = None) -> Optional[str]:
    """方向判定: 'WIN' / 'LOSS' / 'BREAKEVEN'; 无方向 (HOLD) 或缺数据 → None。

    纯函数: 不查库、不预测, 只按"市场走了多少 vs 阈值"判定。
    """
    sm = signed_move(action, forward_move_pct)
    if sm is None:
        return None
    thr = float(threshold) if threshold is not None else breakeven_pct()
    if sm > thr:
        return "WIN"
    if sm < -thr:
        return "LOSS"
    return "BREAKEVEN"


def collect_truth_verdicts(
    conn: sqlite3.Connection,
    threshold: Optional[float] = None,
    now_ts: Optional[int] = None,
    limit: int = 5000,
    recheck: bool = False,
) -> List[Dict[str, Any]]:
    """取出"有真实涨跌 + 有方向"的决策, 算出胜负判定。

    幂等: 默认只取还没判过 (truth_verdict IS NULL) 的行; recheck=True 时
    把全部重新判一遍 (阈值改了要重判时用)。
    """
    thr = float(threshold) if threshold is not None else breakeven_pct()
    sql = ("SELECT id, news_id, target_asset, suggested_action, forward_move_pct, "
           "truth_verdict FROM ai_decisions "
           "WHERE forward_move_pct IS NOT NULL "
           "AND suggested_action IN ('BUY','SELL')")
    if not recheck:
        sql += " AND (truth_verdict IS NULL OR truth_verdict = '')"
    sql += " ORDER BY id DESC LIMIT ?"
    try:
        rows = conn.execute(sql, (int(limit),)).fetchall()
    except sqlite3.Error:
        # 列还没补齐 (旧库) → 返回空, 由调用方按"跳过"处理
        return []
    out: List[Dict[str, Any]] = []
    for r in rows:
        vid = r[0]
        verdict = derive_verdict(r[3], r[4], thr)
        if verdict is None:
            continue
        out.append({
            "id": int(vid),
            "news_id": r[1],
            "asset": r[2],
            "action": r[3],
            "forward_move_pct": r[4],
            "verdict": verdict,
            "signed_move": signed_move(r[3], r[4]),
        })
    return out


def persist_truth_verdicts(
    conn: sqlite3.Connection,
    verdicts: Sequence[Dict[str, Any]],
    now_ts: Optional[int] = None,
    dry_run: bool = False,
) -> int:
    """把胜负判定写回 ai_decisions.truth_verdict (+ truth_verdict_ts)。

    只写 truth_verdict 本身, **不动** is_correct / settled —— 那两个字段是
    "模拟盘这笔单结算了吗"的口径, 由 quick_sim / history_review 维护。
    混用会把"模拟盘没开仓"和"方向没判对"混成一件事。
    """
    now = int(now_ts or time.time())
    n = 0
    for v in verdicts:
        try:
            if dry_run:
                n += 1
                continue
            conn.execute(
                "UPDATE ai_decisions SET truth_verdict=?, truth_verdict_ts=? WHERE id=?",
                (v["verdict"], now, v["id"]),
            )
            n += 1
        except sqlite3.Error:
            continue
    if n and not dry_run:
        conn.commit()
    return n
