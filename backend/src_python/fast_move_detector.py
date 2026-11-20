# -*- coding: utf-8 -*-
"""fast_move_detector.py — 用 market_ticks(秒级) 检测"快涨/快跌"临界点。

为什么需要这个模块
------------------
fast move (快涨/快跌) 的判定原本依赖 ``ohlcv_intraday`` 分钟线, 但那张表
**没有任何定时增量任务** —— ``intraday_store`` 只被 API 按需调用, 没有 cron
在后台持续补。结果:

    ohlcv_intraday  XAU 1m  →  最后 2026-10-21 20:37
                    WTI 1m  →  最后 2026-10-21 20:42
                    ETH 1m  →  最后 2026-10-22 10:43
                    BTC 1m  →  最后 2026-10-30 02:07

于是 ``critical_price_moves`` 里 fast_rise / fast_fall 自 10-22 起**再无新增**
(实测: fast_rise 最后 10-21, fast_fall 最后 10-22, 而 slow_rise 到 10-27、
slow_fall 到 10-29 —— 正好印证"只有分钟线断了")。

而 ``market_ticks`` 是**活的**: 49.9 万行秒级 tick, 覆盖
WTI / XAU / BTC / ETH / SOL, 2 秒一采, 一直在写到最新 —— 只是此前
**从未被 fast 检测消费**。本模块把这条断链接上。

口径 (与 slow 互为对照, 阈值沿用 backfill_critical_moves 的既有定义):
    slow: |1d| ≥ 3.0%   duration_min=1440   (external_indicators 日线)
    fast: |1h| ≥ 1.5%   duration_min=60     (market_ticks 秒级聚合)

四道保护 (与 detect_fast_moves 同源, 不放宽):
    · 窗口按**真实时间戳**算, 不按 bar 数 → 1m 粒度口径一致;
    · 窗口跨度不足 80% 目标时跳过 (数据稀疏时不拿残缺窗口冒充);
    · **bar 连续性 ≥ 50%**: 窗口内实际 bar 数 ≥ 该跨度"应有"的一半;
    · |pct| > 90% 判为量纲跳变 (数据污染) → 不产出 move。

市场上下文 (需求 #6 / #14)
--------------------------
每条 fast move 都带上"现在属于什么市场情况":
    · regime       —— market_style_snapshot.regime (volatile_low / mean_revert ...)
    · vol_ratio    —— 成交量相对近期均值的放大倍数 (资金进场/退出的粗口径)
    · change24h    —— market_ticks.change24h (整体资金方向)
    · crowd        —— crowd_topic_daily 的情绪 + 影响力分层 (市场共识)
    · liquidation  —— liquidation_heat (杠杆/强平 → 套利离场可能)
这些信息缺失时**显式标 unavailable**, 绝不编造。

用法:
    python3 fast_move_detector.py --dry-run   # 只算不写
    python3 fast_move_detector.py             # 写入 critical_price_moves
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

BACKEND = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(Path(__file__).resolve().parent))

from contextlib import contextmanager

DB_PATH = BACKEND / "trident_event_bus.db"


@contextmanager
def write_cursor():
    """可写连接。

    与 bridge_server.write_db_cursor 同口径 (WAL + busy_timeout + 失败不吞),
    但**不从 bridge_server 导入** —— 那个模块 import 即启动整个服务,
    脚本化/定时调用会被拖进 FastAPI 生命周期。
    """
    conn = sqlite3.connect(str(DB_PATH), timeout=15)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=10000")
    try:
        yield conn
    except BaseException:
        try:
            conn.rollback()
        except sqlite3.Error:
            pass
        raise
    finally:
        conn.close()

# ── 阈值 (与 backfill_critical_moves 保持一致) ────────────────
THRESH_FAST_1H_PCT = 1.5      # |1h| ≥ 1.5% 判定为 fast
FAST_WINDOW_MIN = 60          # 窗口 60 分钟
BASE_SCORE = {"fast_rise": 0.65, "fast_fall": 0.70}  # 快跌信息量更大
MAX_PLAUSIBLE_PCT = 90.0      # |pct| > 90% 视为量纲污染
MIN_CONTINUITY = 0.5          # 窗口内 bar 数 ≥ 期望值的 50%
MIN_SPAN_RATIO = 0.8          # 窗口跨度 ≥ 目标的 80%

# 回看窗口: 一次跑覆盖最近 N 小时, 避免重复扫全表
LOOKBACK_HOURS = 48


# ── 1. tick → 1m bar ──────────────────────────────────────────
def load_tick_bars(conn: sqlite3.Connection, symbol: str,
                    since_ts: int) -> List[Tuple[int, float]]:
    """把 market_ticks 的秒级 tick 聚合成 1m bar, 返回 [(minute_ts, close)] 升序。

    每根 1m bar 取该分钟内**最后一笔** tick 的价格 (收盘语义)。
    非正价格直接丢弃 —— 量纲错误的 tick 不能进序列。

    注意: key 必须是**分钟对齐的秒级时间戳** (ts//60*60), 不能是分钟桶
    (ts//60)。后者会让下游 span = ts_i - ts_j 算成"分钟差"而当秒用,
    窗口跨度与连续性校验全部失真 (实测会写出 1970 年的时间戳)。
    """
    rows = conn.execute(
        "SELECT ts, price FROM market_ticks "
        "WHERE symbol=? AND ts>=? ORDER BY ts ASC",
        (symbol, int(since_ts)),
    ).fetchall()
    if not rows:
        return []
    bars: Dict[int, float] = {}
    for r in rows:
        try:
            ts = int(r[0])
            px = float(r[1])
        except (TypeError, ValueError):
            continue
        if ts <= 0 or px <= 0:
            continue
        bars[(ts // 60) * 60] = px    # 后来的覆盖先来的 → 保留分钟内最后一笔
    return sorted(bars.items())


# ── 2. fast move 判定 ─────────────────────────────────────────
def detect_fast_moves(series: List[Tuple[int, float]],
                      window_min: int = FAST_WINDOW_MIN,
                      thresh: float = THRESH_FAST_1H_PCT) -> List[Dict[str, Any]]:
    """在 1m bar 序列上检测"短时间内大幅变动"。口径与 detect_fast_moves 一致。"""
    if len(series) < 2:
        return []
    win_sec = window_min * 60
    moves: List[Dict[str, Any]] = []
    n = len(series)
    j = 0
    for i in range(n):
        ts_i, p_i = series[i]
        if p_i <= 0:
            continue
        target = ts_i - win_sec
        # 双指针: 找到时间上最接近 target 的下标 (且在 i 之前)
        while j + 1 < i and abs(series[j + 1][0] - target) <= abs(series[j][0] - target):
            j += 1
        if j >= i:
            continue
        ts_j, p_j = series[j]
        if p_j <= 0:
            continue
        span = ts_i - ts_j
        if span < win_sec * MIN_SPAN_RATIO:
            continue
        # bar 连续性校验: 该跨度内应有的 bar 数 vs 实际 bar 数
        expected = span / 60.0 + 1.0
        got = i - j + 1
        if got < expected * MIN_CONTINUITY:
            continue
        pct = (p_i - p_j) / p_j * 100.0
        if abs(pct) > MAX_PLAUSIBLE_PCT:
            continue
        if abs(pct) < thresh:
            continue
        kind = "fast_rise" if pct > 0 else "fast_fall"
        moves.append({
            "ts": ts_i,
            "start_price": round(p_j, 6),
            "peak_price": round(max(p_j, p_i), 6),
            "end_price": round(p_i, 6),
            "move_pct": round(pct, 4),
            "move_kind": kind,
            "duration_min": window_min,
            "algo_score": round(min(0.95, BASE_SCORE[kind] + min(0.25, abs(pct) / 40.0)), 3),
            "_span_sec": span,
            "_bars": got,
            "_expected_bars": round(expected, 1),
            "_continuity": round(got / expected, 3) if expected else 0.0,
        })
    # 去重: 重叠窗口只留幅度最大的 (否则同一波行情被记几十遍, 虚增 fast 数量)
    moves.sort(key=lambda m: (m["ts"], -abs(m["move_pct"])))
    kept: List[Dict[str, Any]] = []
    for m in moves:
        if kept and m["ts"] - kept[-1]["ts"] < win_sec:
            if abs(m["move_pct"]) > abs(kept[-1]["move_pct"]):
                kept[-1] = m
            continue
        kept.append(m)
    return kept


# ── 3. 市场上下文 (需求 #6 / #14) ──────────────────────────────
def _latest_regime(conn: sqlite3.Connection, asset: str,
                   ts: Optional[int] = None) -> Dict[str, Any]:
    """market_style_snapshot 在**触发时刻及之前**的市场状态。

    表不存在/无数据 → unavailable。

    必须带 ts 上界: 回填历史 move 时若取"当前最新"的 regime, 就把**未来信息**
    写进了历史归因 —— 这会让事后复盘的系统性偏好 (未来上涨的样本被标成
    上涨后的 regime), 直接污染下游的因果学习。
    """
    try:
        if ts is not None:
            row = conn.execute(
                "SELECT regime, layer1_alerts, layer2_alerts, layer3_alerts, ts "
                "FROM market_style_snapshot WHERE asset=? AND ts<=? "
                "ORDER BY ts DESC LIMIT 1", (asset, int(ts)),
            ).fetchone()
        else:
            row = conn.execute(
                "SELECT regime, layer1_alerts, layer2_alerts, layer3_alerts, ts "
                "FROM market_style_snapshot WHERE asset=? ORDER BY ts DESC LIMIT 1",
                (asset,),
            ).fetchone()
    except sqlite3.Error:
        return {"regime": None, "available": False}
    if not row:
        return {"regime": None, "available": False}
    alerts = {}
    for idx, name in ((1, "L1"), (2, "L2"), (3, "L3")):
        try:
            val = row[idx]
        except IndexError:
            continue
        if val:
            alerts[name] = val
    return {"regime": row[0], "alerts": alerts, "ts": row[4], "available": True}


def _volume_context(conn: sqlite3.Connection, asset: str,
                    ts: int) -> Dict[str, Any]:
    """成交量放大倍数 (vol_ratio): 资金进场/退出的粗口径。

    取 kline_bars 里该 symbol 触发时刻之前的最近 20 根 bar 的 volume,
    用最新那根与 20 根均值的比值。取不到就标 unavailable。

    **必须固定 timeframe** —— kline_bars 同一 symbol 同时存 1m/1h/1d,
    不加 timeframe 过滤会把不同周期的 volume 混在一起算均值, 得到的
    vol_ratio 是纯噪音。优先 1h, 不足则退 1d。

    **kline_bars.ts 是毫秒级** (如 1793545200000), 而本模块其余地方用秒 ——
    直接拿秒去比会恒为 false, 静默变成"成交量永远 unavailable"。
    """
    for tf in ("1h", "1d"):
        try:
            rows = conn.execute(
                "SELECT ts, volume FROM kline_bars "
                "WHERE symbol=? AND timeframe=? AND ts<=? ORDER BY ts DESC LIMIT 20",
                (asset, tf, int(ts) * 1000),
            ).fetchall()
        except sqlite3.Error:
            continue
        vols = [float(r[1]) for r in rows
                if r[1] is not None and float(r[1]) > 0]
        if len(vols) < 5:
            continue
        avg = sum(vols) / len(vols)
        if avg <= 0:
            continue
        return {"vol_ratio": round(vols[0] / avg, 3), "volume_last": vols[0],
                "volume_avg20": round(avg, 4), "bars": len(vols),
                "timeframe": tf, "available": True}
    return {"vol_ratio": None, "available": False}


def _crowd_context(conn: sqlite3.Connection, asset: str,
                   ts: Optional[int] = None) -> Dict[str, Any]:
    """市场共识: crowd_topic_daily 的情绪 + 影响力分层。

    与 _latest_regime 同理, 必须带日期上界 —— 否则回填历史 move 时会把
    事后才形成的舆情写进历史归因。
    """
    try:
        if ts is not None:
            row = conn.execute(
                "SELECT sentiment, influence_tier, asset_class, day FROM crowd_topic_daily "
                "WHERE day <= date(?,'unixepoch','localtime') "
                "ORDER BY day DESC, influence_tier ASC LIMIT 1", (int(ts),),
            ).fetchone()
        else:
            row = conn.execute(
                "SELECT sentiment, influence_tier, asset_class, day FROM crowd_topic_daily "
                "WHERE day = (SELECT MAX(day) FROM crowd_topic_daily) "
                "ORDER BY influence_tier ASC LIMIT 1"
            ).fetchone()
    except sqlite3.Error:
        return {"sentiment": None, "available": False}
    if not row:
        return {"sentiment": None, "available": False}
    return {"sentiment": row[0], "influence_tier": row[1],
            "asset_class": row[2], "day": row[3], "available": True}


def _liquidation_context(conn: sqlite3.Connection, asset: str,
                         ts: int) -> Dict[str, Any]:
    """强平热度 → 杠杆过高 / 套利离场的可能性证据。"""
    try:
        row = conn.execute(
            "SELECT total_usd, long_liq_usd, short_liq_usd FROM liquidation_heat "
            "WHERE ts<=? ORDER BY ts DESC LIMIT 1", (int(ts),)
        ).fetchone()
    except sqlite3.Error:
        return {"total_usd": None, "available": False}
    if not row:
        return {"total_usd": None, "available": False}
    return {"total_usd": row[0], "long_liq_usd": row[1],
            "short_liq_usd": row[2], "available": True}


def enrich_context(conn: sqlite3.Connection, asset: str, ts: int,
                   base_ctx: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """给已有 move 的 context 补齐 市场状态/成交量/情绪/强平（v336）。

    为什么要补：slow_rise/slow_fall 由日线回填路径写入，context 里只有
    k_days / window_high / window_low —— 实测 182 条慢异动里
    **regime / vol_ratio / crowd_sentiment 覆盖为 0**（fast 有 61~101/223）。
    于是"需求 #6：结合资金流向、成交量、K线形态判断现在是什么市场情况"
    对慢涨慢跌从来没算过，归因页只能看到"涨了/跌了"。

    三条纪律：
      · 全部带 ts 上界，只取"触发时刻及之前"，杜绝未来函数；
      · 只补空值，不覆盖已有真实值（回填不得篡改原始观测）；
      · 任何一张表缺失都只标 available=False，不抛异常。
    """
    ctx: Dict[str, Any] = dict(base_ctx or {})
    try:
        regime = _latest_regime(conn, asset, ts)
        if ctx.get("regime") is None and regime.get("regime"):
            ctx["regime"] = regime.get("regime")
            ctx["regime_alerts"] = regime.get("alerts") or {}
        if ctx.get("vol_ratio") is None:
            vol = _volume_context(conn, asset, ts)
            if vol.get("vol_ratio") is not None:
                ctx["vol_ratio"] = vol.get("vol_ratio")
                ctx["volume_last"] = vol.get("volume_last")
                ctx["vol_timeframe"] = vol.get("timeframe")
        if ctx.get("crowd_sentiment") is None:
            crowd = _crowd_context(conn, asset, ts)
            if crowd.get("sentiment") is not None:
                ctx["crowd_sentiment"] = crowd.get("sentiment")
                ctx["crowd_influence"] = crowd.get("influence_tier")
                ctx["crowd_asset_class"] = crowd.get("asset_class")
        if ctx.get("liq_total_usd") is None:
            liq = _liquidation_context(conn, asset, ts)
            if liq.get("total_usd") is not None:
                ctx["liq_total_usd"] = liq.get("total_usd")
    except Exception:
        ctx.setdefault("enrich_error", "enrich_failed")

    # 重新跑一遍综合判定（引擎是确定性的，可安全重跑）
    ctx.update(classify_market_context(ctx))
    ctx["context_enrich_source"] = "v336_slow_enrich"
    return ctx


def classify_market_context(ctx: Dict[str, Any]) -> Dict[str, Any]:
    """综合"资金流向 + 成交量 + 市场状态 + 情绪"给出当前市场情况判断。

    规则是**可解释的确定性映射**, 不是黑箱评分 —— 每一条都写进 reasons,
    便于在归因页直接展示"为什么这么判断" (需求 #6: 原因是什么)。
    """
    reasons: List[str] = []
    phase = "unclassified"
    lever_risk = "unknown"

    regime = ctx.get("regime") or ""
    vol_ratio = ctx.get("vol_ratio")
    chg24 = ctx.get("change24h_pct")
    sentiment = ctx.get("crowd_sentiment") or ""
    liq_usd = ctx.get("liq_total_usd")

    if regime:
        reasons.append("市场状态=%s" % regime)
    if vol_ratio is not None:
        if vol_ratio >= 1.8:
            reasons.append("成交量放大 %.2fx (资金集中进场)" % vol_ratio)
        elif vol_ratio <= 0.6:
            reasons.append("成交量萎缩 %.2fx (资金观望/离场)" % vol_ratio)
        else:
            reasons.append("成交量正常 %.2fx" % vol_ratio)
    if chg24 is not None:
        reasons.append("24h 变动 %+.2f%%" % chg24)
    if sentiment and "无方向" not in sentiment:
        reasons.append("舆情=%s" % sentiment)
    if liq_usd:
        reasons.append("强平额 $%s" % format(int(liq_usd), ","))
        if liq_usd > 50_000_000:
            lever_risk = "high"
            reasons.append("→ 杠杆/强平显著, 存在踩踏或套利离场风险")
        else:
            lever_risk = "low"

    # 阶段判定: 先看成交量放大 + 方向, 再叠加市场状态
    if vol_ratio is not None and vol_ratio >= 1.8 and chg24 is not None:
        phase = "accumulation_or_markup" if chg24 > 0 else "distribution_or_dump"
    elif vol_ratio is not None and vol_ratio <= 0.6:
        phase = ("quiet_or_exit" if (chg24 is not None and abs(chg24) > 1)
                 else "consolidation")
    elif regime == "volatile_low":
        phase = "volatile_low_range"
    elif regime == "mean_revert":
        phase = "mean_reverting"
    elif regime:
        phase = "regime_%s" % regime

    return {"market_phase": phase, "lever_risk": lever_risk, "reasons": reasons}


# ── 4. 主流程 ────────────────────────────────────────────────
def _existing_keys(conn: sqlite3.Connection, asset: str,
                   since_ts: int) -> set:
    """已存在的 (ts, move_kind) —— critical_price_moves 没有 UNIQUE 约束,
    去重必须自己做, 否则每次跑都会翻倍。"""
    try:
        rows = conn.execute(
            "SELECT ts, move_kind FROM critical_price_moves "
            "WHERE asset=? AND ts>=?", (asset, int(since_ts))
        ).fetchall()
    except sqlite3.Error:
        return set()
    return {(int(r[0]), r[1]) for r in rows}


def _change24h_at(conn: sqlite3.Connection, symbol: str, ts: int) -> Optional[float]:
    try:
        r = conn.execute(
            "SELECT change24h FROM market_ticks WHERE symbol=? AND ts<=? "
            "ORDER BY ts DESC LIMIT 1", (symbol, int(ts))
        ).fetchone()
        if r and r[0] is not None:
            return float(r[0])
    except sqlite3.Error:
        pass
    return None


def run(conn: sqlite3.Connection, dry_run: bool = False,
        lookback_hours: int = LOOKBACK_HOURS) -> Dict[str, Any]:
    now = int(time.time())
    since = now - lookback_hours * 3600
    # v340 纪元闸门: 48h lookback 会跨过 data_epoch，不定期把清空前的异动重新插回来。
    # 只从纪元之后开始检测（含纪元之后新产生的 bar）。
    try:
        row = conn.execute("SELECT MAX(started_at) FROM data_epoch").fetchone()
        if row and row[0]:
            from datetime import datetime as _dt
            since = max(since, int(_dt.strptime(str(row[0]), "%Y-%m-%d %H:%M:%S").timestamp()))
    except sqlite3.Error:
        pass

    try:
        symbols = [r[0] for r in conn.execute(
            "SELECT DISTINCT symbol FROM market_ticks WHERE ts>=? ORDER BY symbol",
            (since,),
        ).fetchall()]
    except sqlite3.Error:
        symbols = []

    report: List[Dict[str, Any]] = []
    inserts: List[tuple] = []

    for sym in symbols:
        series = load_tick_bars(conn, sym, since)
        if len(series) < 5:
            report.append({"symbol": sym, "status": "skipped_insufficient_bars",
                           "bars": len(series)})
            continue
        moves = detect_fast_moves(series)
        if not moves:
            report.append({"symbol": sym, "status": "no_move", "bars": len(series)})
            continue

        seen = _existing_keys(conn, sym, since)
        for m in moves:
            if (m["ts"], m["move_kind"]) in seen:
                continue
            seen.add((m["ts"], m["move_kind"]))

            chg24 = _change24h_at(conn, sym, m["ts"])
            regime = _latest_regime(conn, sym, m["ts"])
            vol = _volume_context(conn, sym, m["ts"])
            crowd = _crowd_context(conn, sym, m["ts"])
            liq = _liquidation_context(conn, sym, m["ts"])

            ctx: Dict[str, Any] = {
                "source": "market_ticks",
                "mode": "realtime",
                "window_min": m["duration_min"],
                "threshold_pct": THRESH_FAST_1H_PCT,
                "span_sec": m["_span_sec"],
                "bars_in_window": m["_bars"],
                "expected_bars": m["_expected_bars"],
                "continuity": m["_continuity"],
                "change24h_pct": chg24,
                "regime": regime.get("regime"),
                "regime_alerts": regime.get("alerts") or {},
                "vol_ratio": vol.get("vol_ratio"),
                "volume_last": vol.get("volume_last"),
                "crowd_sentiment": crowd.get("sentiment"),
                "crowd_influence": crowd.get("influence_tier"),
                "crowd_asset_class": crowd.get("asset_class"),
                "liq_total_usd": liq.get("total_usd"),
            }
            ctx.update(classify_market_context(ctx))

            # v340: 序列是从 since 起取的，但 K 线时间戳可能落在 since 之前
            # （桶起点对齐），实测于是有 25 行 epoch 之前的异动被写回来。
            # 这里按**异动本身的 ts** 再拦一次。
            if m["ts"] < since:
                continue
            inserts.append((
                sym, m["ts"], m["start_price"], m["peak_price"], m["end_price"],
                m["move_pct"], m["move_kind"], m["duration_min"], m["algo_score"],
                json.dumps(ctx, ensure_ascii=False),
            ))

        report.append({"symbol": sym, "status": "ok", "bars": len(series),
                       "moves": len(moves)})

    if inserts and not dry_run:
        conn.executemany(
            "INSERT INTO critical_price_moves "
            "(asset,ts,start_price,peak_price,end_price,move_pct,move_kind,"
            " duration_min,algo_score,context_json) "
            "VALUES (?,?,?,?,?,?,?,?,?,?)",
            inserts,
        )
        conn.commit()

    return {"symbols": report, "inserted": len(inserts), "dry_run": dry_run,
            "lookback_hours": lookback_hours}


def main() -> int:
    ap = argparse.ArgumentParser(
        description="用 market_ticks 检测 fast_rise/fast_fall 并写入 critical_price_moves")
    ap.add_argument("--dry-run", action="store_true", help="只算不写库")
    ap.add_argument("--lookback-hours", type=int, default=LOOKBACK_HOURS,
                    help="回看小时数 (默认 %d)" % LOOKBACK_HOURS)
    args = ap.parse_args()

    # 写锁冲突重试：引擎批事务可能持续数分钟，10s busy_timeout 不够。
    # 实测 11-12 21:16 整个作业在 INSERT 处抛 database is locked 直接退出，
    # 那一轮异动检测完全没落库（cron 5 分钟一次，失败就是静默丢一轮）。
    res = None
    for _attempt in range(4):
        try:
            with write_cursor() as conn:
                res = run(conn, dry_run=args.dry_run, lookback_hours=args.lookback_hours)
            break
        except sqlite3.OperationalError as exc:
            if "locked" not in str(exc).lower() or _attempt == 3:
                raise
            _wait = 30 * (_attempt + 1)
            print(f"[WARN] 写锁忙，{_wait}s 后重试 ({_attempt+1}/4): {exc}", flush=True)
            time.sleep(_wait)
    if res is None:
        return 1
    for r in res["symbols"]:
        extra = {k: v for k, v in r.items() if k not in ("symbol", "status")}
        print("  %-8s %-26s %s" % (r["symbol"], r["status"], extra))
    print("inserted=%d dry_run=%s" % (res["inserted"], res["dry_run"]))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
