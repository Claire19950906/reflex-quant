# -*- coding: utf-8 -*-
"""missed_signal_taxonomy.py — 漏检信号的错因细分 (需求 #7 / G11 实时侧)。

背景
----
missed_signals 表只有 3 个 pattern_type (A1 强买漏 / A2 强卖漏 / A3 延续漏) ——
全部是"信号长什么样", 没有"**为什么漏了**"。于是漏检面板只能回答"漏了哪些信号",
回答不了"系统在哪一环掉的", 也就没法跟反思侧的三维错因对齐。

本模块用**真实证据**给每个漏检信号定一个错因小类, 并且与反思侧共用
reflection_dimension_map 的同一套词表 (A 买卖信号判断 / B 信息分析方向 /
C 技术错误), 两侧因此可以直接对齐统计。

判据 (按优先级, 都必须有证据支撑):
  1. C3 signal_delay        —— 信号很旧 (>24h) 还没被处理
  2. C1 feed_missing        —— 当时该资产**没有行情数据** (kline 缺失)
  3. B1 news_direction_inverted —— 同期有新闻, 但新闻方向与信号方向相反
  4. B2 event_chain_break   —— 同期有同向新闻, 但事件链没把它转成决策
  5. C2 calibration_drift   —— 置信度很高, 但 5 日后实际几乎没动 (信号本身虚高)
  6. A2 market_regime_misread —— 该资产当时有因果链, 但信号没命中链的触发条件
  7. A1 pattern_recognition —— 以上都不是: 纯形态/强度信号, 无催化无链
"""
from __future__ import annotations

import re
import sqlite3
import time
from datetime import datetime, timezone
from typing import Any, Dict, Optional, Tuple

try:  # v326: 同组联动判定 (A 类细分) 与反思侧共用同一份分组常量
    from cem_constants import CROSS_ASSET_GROUPS as _CROSS_GROUPS
except ImportError:  # pragma: no cover
    _CROSS_GROUPS = {
        "crypto": ["BTC", "ETH", "SOL", "ADA", "DOGE", "XRP", "BNB"],
        "metal": ["XAU"], "energy": ["WTI"],
        "us_tech": ["NVDA", "TSLA", "AAPL", "MSFT", "GOOGL", "AMZN", "META"],
        "memory": ["MU", "SKHYNIX"],
    }

PEER_CO_MOVE_MIN = 2
PEER_WINDOW_SEC = 7200
PEER_MIN_MOVE_PCT = 1.5


def _peer_co_move_count(conn: sqlite3.Connection, asset: str, ts: int,
                        move_sign: int) -> int:
    """同组同向异动资产数 (排除自己)。与 missed_signal_reflection 同一口径。"""
    if not ts or not asset or move_sign == 0:
        return 0
    peers = []
    for members in _CROSS_GROUPS.values():
        if asset in members:
            peers = [a for a in members if a != asset]
            break
    if not peers:
        return 0
    ph = ",".join("?" for _ in peers)
    try:
        rows = conn.execute(
            "SELECT asset FROM critical_price_moves WHERE asset IN (%s) "
            "AND ts BETWEEN ? AND ? AND ABS(COALESCE(move_pct,0)) >= ? "
            "AND ((move_pct > 0 AND ? > 0) OR (move_pct < 0 AND ? < 0)) "
            "GROUP BY asset" % ph,
            (*peers, ts - PEER_WINDOW_SEC, ts + PEER_WINDOW_SEC,
             PEER_MIN_MOVE_PCT, move_sign, move_sign),
        ).fetchall()
    except sqlite3.Error:
        return 0
    return len(rows)

# pattern_type 词表: 与反思侧 (dimension, subcategory) 一一对应
PATTERN_BY_SUBCATEGORY: Dict[Tuple[str, str], str] = {
    ("B", "news_direction_inverted"): "B1_news_direction_inverted_missed",
    ("B", "event_chain_break"): "B2_event_chain_break_missed",
    ("C", "feed_missing"): "C1_feed_missing_missed",
    ("C", "calibration_drift"): "C2_calibration_drift_missed",
    ("C", "signal_delay"): "C3_signal_delay_missed",
    ("A", "market_regime_misread"): "A2_market_regime_misread_missed",
    ("A", "pattern_recognition"): "A4_pattern_recognition_missed",
}

DEFAULT_PATTERN = "A3_continuation_missed"

# 旧的 3 类 (A1/A2/A3) 保留 —— 它们描述"信号形态", 仍是有用的事实维度
LEGACY_PATTERNS = ("A1_strong_buy_missed", "A2_strong_sell_missed",
                   "A3_continuation_missed")


def _norm_action(action: Any) -> str:
    a = str(action or "").strip().upper()
    if a in ("BUY", "LONG", "BULLISH"):
        return "BUY"
    if a in ("SELL", "SHORT", "BEARISH"):
        return "SELL"
    return ""


def _sign_of(value: Optional[float]) -> int:
    """取数值符号。

    v313 P0 (诚实宪法修复): 旧写法是「用 `or` 把 None 短路成 0.0 再转 float」,
    于是"这个资产的涨跌数据缺失"和"涨跌真的是 0%"变成同一件事,
    前端会显示假的 0%。这里改成**显式 None 判断**: 数据缺失就返回 0(=无方向),
    但绝不把 None 当成 0.0 参与运算, 缺失信息由调用方按"不可用"处理。
    """
    if value is None:
        return 0
    try:
        v = float(value)
    except (TypeError, ValueError):
        return 0
    if v > 0:
        return 1
    if v < 0:
        return -1
    return 0


def _out(dimension: str, subcategory: str, reason: str) -> Dict[str, str]:
    return {
        "dimension": dimension,
        "subcategory": subcategory,
        "pattern_type": PATTERN_BY_SUBCATEGORY.get((dimension, subcategory), DEFAULT_PATTERN),
        "reason": reason,
    }



def _ts_from_row(conn: sqlite3.Connection, row: Dict[str, Any]) -> Tuple[int, Optional[float]]:
    """定位这条漏检信号的时刻与幅度。

    signal_id 指向 critical_price_moves.id, 但该表是滚动窗口 (历史行会被清),
    所以大量历史漏检记录已经查不到原 move —— 这时退回 decision_date (按 UTC 当天
    中点取), 幅度从 signal 文本里解析 ("move_pct=-6.26 move_kind=slow_fall")。

    v313 P0 (诚实宪法修复): 幅度**取不到就是取不到**, 返回 None, 不再用 0.0 顶替。
    旧实现把 NULL move_pct / 文本里没写 move_pct 都变成 0.0, 于是
    "幅度未知"和"幅度真的是 0"混为一谈 —— 而 _sign_of 会把这个 0 当成"没有方向",
    把"数据缺失"误判成"市场没动"。
    """
    signal_id = row.get("signal_id")
    if signal_id:
        try:
            mv = conn.execute(
                "SELECT ts, move_pct FROM critical_price_moves WHERE id=?",
                (int(signal_id),),
            ).fetchone()
            if mv and mv["ts"]:
                raw = mv["move_pct"]
                return int(mv["ts"]), (float(raw) if raw is not None else None)
        except (sqlite3.OperationalError, TypeError, ValueError):
            pass
    txt = str(row.get("signal") or "")
    pct: Optional[float] = None
    m = re.search(r"move_pct=(-?\d+(?:\.\d+)?)", txt)
    if m:
        try:
            pct = float(m.group(1))
        except ValueError:
            pct = None
    date_s = str(row.get("decision_date") or "")
    ts = 0
    if date_s:
        try:
            ts = int(datetime.strptime(date_s[:10], "%Y-%m-%d")
                     .replace(tzinfo=timezone.utc).timestamp()) + 43200
        except ValueError:
            ts = 0
    return ts, pct


def compute_outcome(conn: sqlite3.Connection, asset: str, ts: int,
                     days: int = 5) -> Optional[float]:
    """算信号之后 N 天的真实涨跌幅 (%) —— 用真实行情, 不用预测。

    v323 修了两个实测错误（原实现会把"后续涨跌"算成 ~0 或 -300%）：
      1. 取未来价时用 `ts >= 信号时刻 ORDER BY ts ASC LIMIT 1` —— 拿到的其实是
         **信号当天那根** K 线，等于自己减自己，结果永远接近 0，
         漏检面板的 hit_rate 因此全是噪声。
         正确做法：锁到 `ts + N 天` 附近那根（容差 ±2 天）。
      2. base 走 ohlcv_daily 拿不到时回退 kline_bars，但 fut 又可能从另一张表取，
         两张表时间单位不同（秒 vs 毫秒），跨表混算出 -310% 这种不可能的数。
         正确做法：base 和 fut **必须同源**，要么都用 ohlcv_daily，要么都用 kline_bars。
    """
    if not asset or not ts:
        return None
    end_ts = int(ts) + days * 86400
    tol = 2 * 86400

    def _daily(at: int) -> Optional[tuple[int, float]]:
        try:
            row = conn.execute(
                "SELECT ts, close FROM ohlcv_daily WHERE asset=? AND ts<=? "
                "ORDER BY ts DESC LIMIT 1",
                (asset.upper(), at),
            ).fetchone()
            return (int(row[0]), float(row[1])) if row and row[1] else None
        except sqlite3.OperationalError:
            return None

    def _daily_near(at: int) -> Optional[tuple[int, float]]:
        try:
            row = conn.execute(
                "SELECT ts, close FROM ohlcv_daily WHERE asset=? AND ts BETWEEN ? AND ? "
                "ORDER BY ABS(ts - ?) ASC LIMIT 1",
                (asset.upper(), at - tol, at + tol, at),
            ).fetchone()
            return (int(row[0]), float(row[1])) if row and row[1] else None
        except sqlite3.OperationalError:
            return None

    def _kline(at_ms: int) -> Optional[tuple[int, float]]:
        try:
            row = conn.execute(
                "SELECT ts, close FROM kline_bars WHERE symbol=? AND ts<=? "
                "ORDER BY ts DESC LIMIT 1",
                (asset.upper(), at_ms),
            ).fetchone()
            return (int(row[0]), float(row[1])) if row and row[1] else None
        except sqlite3.OperationalError:
            return None

    def _kline_near(at_ms: int) -> Optional[tuple[int, float]]:
        try:
            row = conn.execute(
                "SELECT ts, close FROM kline_bars WHERE symbol=? AND ts BETWEEN ? AND ? "
                "ORDER BY ABS(ts - ?) ASC LIMIT 1",
                (asset.upper(), at_ms - tol * 1000, at_ms + tol * 1000, at_ms),
            ).fetchone()
            return (int(row[0]), float(row[1])) if row and row[1] else None
        except sqlite3.OperationalError:
            return None

    # 同源优先：日线(秒) → 分钟线(毫秒)。不允许跨源相减。
    base, fut = _daily(ts), _daily_near(end_ts)
    if base is None or fut is None:
        base, fut = _kline(int(ts) * 1000), _kline_near(end_ts * 1000)
        span_sec = (fut[0] - base[0]) / 1000.0 if (base and fut) else None
    else:
        span_sec = (fut[0] - base[0]) if (base and fut) else None
    if base is None or fut is None or base[1] <= 0:
        return None
    if span_sec is None:
        return None
    # v343: 关键闸门 —— "N 天后的结果"必须真的跨过 N 天。
    # 原实现允许取 ts+5d ±2 天内的"最近一根"，纪元初期只有 1 根日线时，
    # 它会退化成"用相邻几小时的两次收盘相减"，于是几十个不同事件算出
    # 同一个 1.461%（实测 18 行、跨 6 个时间点全是这个值）。
    # 现在要求实际跨度至少 (days-1) 天，不够就返回 None（=还没到能评判的时候）。
    # days=1 时 (days-1)*86400 = 0，闸门形同虚设（1d 口径照样算出同一个 1.461%），
    # 所以下限取"窗口的一半"与 (days-1) 天中的较大者。
    _min_span = max((days - 1) * 86400, days * 86400 * 0.5)
    if span_sec < _min_span:
        return None
    return round((fut[1] - base[1]) / base[1] * 100.0, 4)


def classify_missed(conn: sqlite3.Connection, row: Dict[str, Any],
                    now_ts: Optional[int] = None) -> Dict[str, str]:
    """给一条漏检信号定错因小类。返回 {dimension, subcategory, pattern_type, reason}。"""
    now = int(now_ts or time.time())
    asset = str(row.get("asset") or "").upper()
    pattern = str(row.get("pattern_type") or "").strip()

    move_ts, move_pct = _ts_from_row(conn, row)
    move_sign = _sign_of(move_pct)
    conf = row.get("confidence_at_time")
    try:
        conf = float(conf) if conf is not None else 0.0
    except (TypeError, ValueError):
        conf = 0.0

    # 1) 行情数据缺失 → C1 feed_missing (宏观序列如 FRED/ICS 本来就没有价格)
    if move_ts and asset:
        has_bar = None
        try:
            has_bar = conn.execute(
                "SELECT 1 FROM kline_bars WHERE symbol=? AND ts BETWEEN ? AND ? LIMIT 1",
                (asset, (move_ts - 1800) * 1000, (move_ts + 1800) * 1000),
            ).fetchone()
        except sqlite3.OperationalError:
            has_bar = None
        if not has_bar:
            try:
                has_bar = conn.execute(
                    "SELECT 1 FROM ohlcv_daily WHERE asset=? AND ts BETWEEN ? AND ? LIMIT 1",
                    (asset, move_ts - 3 * 86400, move_ts + 3 * 86400),
                ).fetchone()
            except sqlite3.OperationalError:
                has_bar = None
        if not has_bar:
            return _out("C", "feed_missing", "异动时刻该资产无任何行情数据 (数据缺失)")

    # 2/3) 同期新闻: 方向相反 → B1; 同向但没转成决策 → B2
    if move_ts and asset:
        news = conn.execute(
            "SELECT id, action, score, ts, headline FROM news_events "
            "WHERE asset=? AND ts BETWEEN ? AND ? AND action IN ('BUY','SELL') "
            "ORDER BY ts DESC LIMIT 3",
            (asset, move_ts - 12 * 3600, move_ts + 12 * 3600),
        ).fetchall()
        same_dir = False
        opposite = False
        for n in news:
            n_sign = 1 if _norm_action(n["action"]) == "BUY" else -1
            if move_sign == 0:
                continue
            if n_sign == move_sign:
                same_dir = True
            else:
                opposite = True
        if opposite:
            return _out("B", "news_direction_inverted",
                        "同期新闻方向与信号方向相反, 系统按反方向理解")
        if same_dir:
            return _out("B", "event_chain_break",
                        "同期有同向新闻, 但事件链没把它转成决策")

    # 4) 高置信但 5 日后几乎没动 → C2 calibration_drift
    outcome = row.get("actual_outcome_5d")
    if outcome is None and move_ts:
        outcome = compute_outcome(conn, asset, move_ts, 5)
    if outcome is not None and conf >= 0.7:
        try:
            if abs(float(outcome)) < 0.3:
                return _out("C", "calibration_drift",
                            "置信度 %.2f 但 5 日实际波动仅 %.2f%% (信号虚高)"
                            % (conf, float(outcome)))
        except (TypeError, ValueError):
            pass

    # 5) 该资产当时有因果链, 但信号没命中触发条件 → A2 market_regime_misread
    #    v326: 有 deny_reason 时先看机器记录的拒单机制 (与反思侧同一口径)
    deny = str(row.get("deny_reason") or "").strip()
    if deny:
        try:
            from review import reflection_dimension_map as _rdm
            mapped = _rdm.classify_deny_reason(deny)
        except Exception:  # pragma: no cover
            mapped = None
        if mapped:
            return _out(mapped[0], mapped[1], "%s (deny_reason=%s)" % (mapped[2], deny[:60]))
    if move_ts and asset:
        try:
            chain = conn.execute(
                "SELECT id FROM causal_chains WHERE asset=? AND enabled=1 LIMIT 1",
                (asset,),
            ).fetchone()
        except sqlite3.OperationalError:
            chain = None
        if chain:
            # v326: 不再一律"市场状态误读"，用同组联动区分板块级 vs 单资产形态
            peers = _peer_co_move_count(conn, asset, move_ts, move_sign)
            if peers >= PEER_CO_MOVE_MIN:
                return _out("A", "market_regime_misread",
                            "链未触发且同组 %d 个资产同窗口同向异动 → 板块级状态没读到" % peers)
            return _out("A", "pattern_recognition",
                        "链未触发、同组无联动 → 单资产形态/强度信号没识别出来")

    # 6) 信号超出检测窗口 (>30 天) → C3 signal_delay
    if move_ts and (now - move_ts) > 30 * 86400:
        return _out("C", "signal_delay",
                    "信号发生在 %d 天前, 超出实时检测窗口"
                    % int((now - move_ts) / 86400))

    # 7) 兜底: 纯形态信号
    return _out("A", "pattern_recognition", "无催化无链, 纯形态/强度信号未被识别")


def ensure_columns(conn: sqlite3.Connection) -> None:
    cols = {r[1] for r in conn.execute("PRAGMA table_info(missed_signals)").fetchall()}
    for name, decl in (("dimension", "TEXT"), ("subcategory", "TEXT"),
                       ("miss_reason", "TEXT")):
        if name not in cols:
            conn.execute("ALTER TABLE missed_signals ADD COLUMN %s %s" % (name, decl))
    conn.commit()


def backfill(conn: sqlite3.Connection) -> Dict[str, Any]:
    ensure_columns(conn)
    rows = conn.execute(
        "SELECT id, asset, signal_id, pattern_type, confidence_at_time, "
        "actual_outcome_5d, ignored_reason FROM missed_signals"
    ).fetchall()
    dist: Dict[str, int] = {}
    updated = 0
    for r in rows:
        row = dict(r)
        res = classify_missed(conn, row)
        conn.execute(
            "UPDATE missed_signals SET dimension=?, subcategory=?, pattern_type=?, "
            "miss_reason=? WHERE id=?",
            (res["dimension"], res["subcategory"], res["pattern_type"],
             res["reason"][:300], int(r["id"])),
        )
        updated += 1
        dist[res["pattern_type"]] = dist.get(res["pattern_type"], 0) + 1
    conn.commit()
    return {"rows": len(rows), "updated": updated, "pattern_types": dist}


__all__ = ["PATTERN_BY_SUBCATEGORY", "DEFAULT_PATTERN", "LEGACY_PATTERNS",
           "classify_missed", "ensure_columns", "backfill"]
