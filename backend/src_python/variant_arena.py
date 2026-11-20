#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""策略版本赛马场（v476，离线 / 只读生产库）。

要回答的问题
------------
系统现在能产出方向决策，但**没有任何机制能在同一批信号上公平比较两个策略版本**：
算法一换代就清库重来，旧结论全部作废，最后只能看“这一版感觉对不对”。

本模块把「同一新闻流 → 多个策略变体 → 各自影子成交」做成可复现的实验：

  1. 从 `ai_decisions` 取同一批 BUY/SELL 信号（按 news_id × asset 去重）；
  2. 每个变体只允许改两件事：**入场闸**（要不要做）和**出场规则**（怎么平）；
  3. 入场价统一用决策后第一根 1m K 线的 open，出场走同一份 1m 路径；
  4. 成本统一走 `cost_model.round_trip_cost_bps`，净收益 = 毛收益 − 往返成本；
  5. 结果写**独立库** `runtime/eval/variant_arena.db`，不碰生产账本。

默认五匹马（保留 2×2 因子设计，另加一条严格闸做对照）：

  baseline       不过闸 + 固定出场（TP1 0.6% / TP2 1.0% / SL 0.5% / 到期）
  dynamic        不过闸 + 动态出场（保本、移动止损、20 分钟反应窗口）
  gate           **软成本闸**（当前生产口径：样本 <6 放行并标 unverified）+ 固定出场
  gate_dynamic   软成本闸 + 动态出场
  gate_strict    **硬成本闸**（样本 <6 也跳过）+ 固定出场

成本闸的口径：**只用决策之前已经发生的样本**（生产的已结算 MFE + 本场 baseline
在此之前产生的 MFE），中位数必须 ≥ 1.5×往返成本；软闸在样本不足时放行、
硬闸在样本不足时也拒绝。这样回测不会偷看未来，也能看清“当前生产口径”和
“严格口径”差多少。

晋升门槛（写死在 `promotion_gate()`，不允许调用方放宽）：
  - 已结算样本 ≥60 笔；
  - 净合计 > 0 且净均值 > 0；
  - 平均 MFE ≥ 1.5×平均成本；
  - 最大回撤 ≤10 个百分点；
  - t 值 ≥1.5。
任一条不满足 → `eligible=False` 并给出原因。样本不足时只输出“观察”，
绝不把 8 笔、20 笔的差异说成“版本更优”。

用法
----
    cd backend/src_python
    python3 variant_arena.py --days 1
    python3 variant_arena.py --db ../runtime/backup/pre_prune_20261119_154634.db --days 1
    python3 variant_arena.py --since 2026-11-19T13:00:00+08:00 --until 2026-11-19T16:00:00+08:00

产物：
    backend/reports/variant_arena/latest.json
    backend/reports/variant_arena/latest.md
    backend/runtime/eval/variant_arena.db
"""
from __future__ import annotations

import argparse
import bisect
import json
import os
import sqlite3
import statistics
import sys
import time
from collections import Counter, defaultdict
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

# 允许 `python variant_arena.py` 直接跑（同目录裸导入项目约定）
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import cost_model

BACKEND = Path(__file__).resolve().parents[1]
DEFAULT_DB = Path(os.getenv("TRIDENT_DB_PATH") or (BACKEND / "trident_event_bus.db"))
DEFAULT_OUT = BACKEND / "reports" / "variant_arena"
DEFAULT_ARENA_DB = BACKEND / "runtime" / "eval" / "variant_arena.db"

# 与 quick_sim / config 保持同一口径的默认值（不在本模块自造第二套阈值）
DEFAULT_TP1_MULT = 0.6     # %
DEFAULT_TP2_MULT = 1.0     # %
DEFAULT_SL_MULT = 0.5      # %
DEFAULT_TP1_RATIO = 0.5
MIN_SAMPLES = 6
GATE_MULT = 1.5
HORIZON_TIERS = {"high": 60, "medium": 40, "low": 20}
HORIZON_EARLY_MULT = 2.0
HORIZON_LATE_MULT = 0.5
HORIZON_MIN = 20
HORIZON_MAX = 240


# ──────────────────────────────────────────────────────────────────────────
# 数据结构
# ──────────────────────────────────────────────────────────────────────────
@dataclass(frozen=True)
class Bar:
    ts: int          # 毫秒
    open: float
    high: float
    low: float
    close: float


@dataclass
class Signal:
    decision_id: int
    news_id: Optional[int]
    asset: str
    action: str      # BUY / SELL
    ts: int          # 秒
    created_at: str
    score: Optional[float] = None
    confidence: Optional[float] = None
    horizon_minutes: int = 40
    event_strength: str = ""
    event_phase: str = ""


@dataclass(frozen=True)
class VariantConfig:
    """一个策略变体只允许改入场闸和出场规则，不允许改价格/成本口径。"""

    name: str
    exit_mode: str = "fixed"          # fixed | dynamic
    cost_gate: bool = False
    cost_gate_mode: str = "soft"      # soft=样本不足放行（当前生产口径） / strict=样本不足也跳过
    min_samples: int = MIN_SAMPLES
    gate_mult: float = GATE_MULT

    tp1_mult: float = DEFAULT_TP1_MULT
    tp2_mult: float = DEFAULT_TP2_MULT
    sl_mult: float = DEFAULT_SL_MULT
    tp1_ratio: float = DEFAULT_TP1_RATIO

    be_trigger_pct: float = 0.20      # MFE≥0.20% → 止损抬到“成本后保本”
    trail_trigger_pct: float = 0.30   # MFE≥0.30% → 按峰值回撤 50% 移动止损
    trail_giveback: float = 0.50
    reaction_minutes: int = 20        # 20 分钟还没走出来 → 提前撤
    reaction_min_mfe: float = 0.10


DEFAULT_VARIANTS: Tuple[VariantConfig, ...] = (
    VariantConfig(name="baseline", exit_mode="fixed", cost_gate=False),
    VariantConfig(name="dynamic", exit_mode="dynamic", cost_gate=False),
    VariantConfig(name="gate", exit_mode="fixed", cost_gate=True, cost_gate_mode="soft"),
    VariantConfig(name="gate_dynamic", exit_mode="dynamic", cost_gate=True, cost_gate_mode="soft"),
    VariantConfig(name="gate_strict", exit_mode="fixed", cost_gate=True, cost_gate_mode="strict"),
)


# ──────────────────────────────────────────────────────────────────────────
# 时间 / 数据读取
# ──────────────────────────────────────────────────────────────────────────
def parse_ts(value: Any) -> int:
    """把 DB 里的 ts / created_at 统一成 epoch 秒。

    - 整数 >0 → 直接当 epoch 秒（项目里 ts 列都是秒；0/负值视为缺失）；
    - 字符串 → 先试整数，再按 ISO 解析；naive 一律按 **UTC**
      （SQLite datetime('now') 是 UTC）。
    """
    if value is None or value == "":
        return 0
    if isinstance(value, (int, float)):
        n = int(value)
        return n if n > 0 else 0
    s = str(value).strip()
    if s.isdigit():
        n = int(s)
        return n if n > 0 else 0
    try:
        dt = datetime.fromisoformat(s)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return int(dt.timestamp())
    except Exception:
        return 0


def _horizon_for(row: Dict[str, Any]) -> int:
    """优先用决策已落库的持有期；缺失才按 event_strength/phase 推导。"""
    raw = row.get("holding_horizon_minutes")
    try:
        h = int(raw)
        if HORIZON_MIN <= h <= HORIZON_MAX:
            return h
    except (TypeError, ValueError):
        pass
    base = HORIZON_TIERS.get(str(row.get("event_strength") or "").strip().lower(), 40)
    phase = str(row.get("event_phase") or "").strip().lower()
    if phase == "early":
        base = int(base * HORIZON_EARLY_MULT)
    elif phase == "late":
        base = int(base * HORIZON_LATE_MULT)
    return max(HORIZON_MIN, min(HORIZON_MAX, base))


def _table_columns(conn: sqlite3.Connection, table: str) -> set[str]:
    try:
        return {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}
    except sqlite3.Error:
        return set()


def load_signals(
    conn: sqlite3.Connection,
    since_ts: int,
    until_ts: int,
    *,
    exclude_degraded: bool = True,
    dedupe_window_sec: int = 300,
) -> List[Signal]:
    """取同一批方向信号，做两层去重，保留最早一条。

    1) (news_id, asset)：同一条新闻同品种的重复分析直接折叠；
    2) (asset, action, 5 分钟桶)：多源转载/同一事件的不同新闻，5 分钟内同向只算一次。
       这是为了避免把“13 笔仓位其实只有 3 注”当成 13 个独立样本（v449 的教训）。
    """
    cols = _table_columns(conn, "ai_decisions")
    where = [
        "suggested_action IN ('BUY','SELL')",
        "target_asset IS NOT NULL",
        "target_asset != ''",
        "target_asset != 'NONE'",
        "ts >= ?",
        "ts <= ?",
    ]
    params: List[Any] = [int(since_ts), int(until_ts)]
    if "quarantined_at" in cols:
        where.append("COALESCE(quarantined_at, '') = ''")
    if exclude_degraded and "analysis_degraded" in cols:
        where.append("COALESCE(analysis_degraded, 0) = 0")
    if "decision_kind" in cols:
        where.append("COALESCE(decision_kind, 'analysis') = 'analysis'")

    sql = f"""
        SELECT id, news_id, ts, created_at, target_asset, suggested_action,
               sentiment_score, evidence_confidence, holding_horizon_minutes,
               event_strength, event_phase
        FROM ai_decisions
        WHERE {' AND '.join(where)}
        ORDER BY ts ASC, id ASC
    """
    rows: List[Dict[str, Any]] = []
    cur = conn.execute(sql, params)
    cols = [d[0] for d in cur.description]
    for r in cur.fetchall():
        # sqlite3.Row / tuple 两种连接都可能出现；统一转 dict
        rows.append(dict(r) if isinstance(r, sqlite3.Row) else dict(zip(cols, r)))
    out: List[Signal] = []
    seen: set[Tuple[Any, str]] = set()
    seen_bucket: set[Tuple[str, str, int]] = set()
    window = max(1, int(dedupe_window_sec))
    for r in rows:
        asset = str(r.get("target_asset") or "").upper().strip()
        action = str(r.get("suggested_action") or "").upper().strip()
        ts = parse_ts(r.get("ts")) or parse_ts(r.get("created_at"))
        if ts <= 0 or not asset or action not in ("BUY", "SELL"):
            continue
        key = (r.get("news_id") if r.get("news_id") is not None else f"id:{r['id']}", asset)
        if key in seen:
            continue
        seen.add(key)
        bucket_key = (asset, action, ts // window)
        if bucket_key in seen_bucket:
            continue
        seen_bucket.add(bucket_key)
        out.append(Signal(
            decision_id=int(r["id"]),
            news_id=r.get("news_id"),
            asset=asset,
            action=action,
            ts=ts,
            created_at=str(r.get("created_at") or ""),
            score=_float_or_none(r.get("sentiment_score")),
            confidence=_float_or_none(r.get("evidence_confidence")),
            horizon_minutes=_horizon_for(r),
            event_strength=str(r.get("event_strength") or ""),
            event_phase=str(r.get("event_phase") or ""),
        ))
    return out


def _float_or_none(v: Any) -> Optional[float]:
    try:
        if v is None or v == "":
            return None
        return float(v)
    except (TypeError, ValueError):
        return None


def load_bars(
    conn: sqlite3.Connection,
    symbols: Iterable[str],
    start_ms: int,
    end_ms: int,
    timeframe: str = "1m",
) -> Dict[str, List[Bar]]:
    """一次把需要的 1m K 线读进内存；读不到就是没有，不补假数据。"""
    syms = sorted({str(s).upper() for s in symbols if s})
    if not syms:
        return {}
    out: Dict[str, List[Bar]] = defaultdict(list)
    # 分批 IN，避免 SQLite 变量上限；本项目品种数量很小，一次就够
    placeholders = ",".join("?" for _ in syms)
    sql = f"""
        SELECT symbol, ts, open, high, low, close
        FROM kline_bars
        WHERE timeframe = ?
          AND ts >= ? AND ts <= ?
          AND symbol IN ({placeholders})
        ORDER BY symbol, ts ASC
    """
    for r in conn.execute(sql, [timeframe, int(start_ms), int(end_ms), *syms]):
        try:
            o, h, l, c = float(r[2]), float(r[3]), float(r[4]), float(r[5])
        except (TypeError, ValueError):
            continue
        if min(o, h, l, c) <= 0:
            continue
        out[str(r[0]).upper()].append(Bar(int(r[1]), o, h, l, c))
    return dict(out)


def load_production_mfe(conn: sqlite3.Connection) -> Dict[Tuple[str, int], List[Tuple[int, float]]]:
    """生产已结算的 MFE，作为成本闸的“出道前样本”。

    只用 settled=1 且 mfe_pct 非空的行；没有就返回空，绝不补 0。
    """
    out: Dict[Tuple[str, int], List[Tuple[int, float]]] = defaultdict(list)
    try:
        rows = conn.execute(
            """SELECT asset, horizon_minutes, entry_ts, exit_ts, mfe_pct
               FROM quick_sim_trades
               WHERE settled = 1 AND mfe_pct IS NOT NULL AND mfe_pct > 0"""
        ).fetchall()
    except sqlite3.Error:
        return {}
    for asset, horizon, entry_ts, exit_ts, mfe in rows:
        try:
            h = int(horizon)
            m = float(mfe)
        except (TypeError, ValueError):
            continue
        # 用**结算时间**入池；没有 exit_ts 才退回 entry_ts + horizon。
        ts = parse_ts(exit_ts)
        if ts <= 0:
            ts = parse_ts(entry_ts) + h * 60
        if ts <= 0:
            continue
        out[(str(asset).upper(), h)].append((ts, m))
    for v in out.values():
        v.sort()
    return dict(out)


# ──────────────────────────────────────────────────────────────────────────
# 单笔模拟
# ──────────────────────────────────────────────────────────────────────────
def simulate_trade(
    sig: Signal,
    bars: Sequence[Bar],
    cfg: VariantConfig,
    *,
    prior_mfe: Optional[Sequence[float]] = None,
) -> Dict[str, Any]:
    """按一个变体的规则模拟单笔；返回 taken / skipped / no_price。

    `bars` 必须按 ts 升序，允许包含决策之前/之后的 bar；函数自己找入场 bar。
    入场 = 决策后第一根 1m 的 open（不用决策当根收盘，避免同一根内的未来函数）。
    """
    base = {
        "decision_id": sig.decision_id,
        "news_id": sig.news_id,
        "asset": sig.asset,
        "action": sig.action,
        "signal_ts": sig.ts,
        "horizon_minutes": sig.horizon_minutes,
    }
    if not bars:
        return {**base, "status": "no_price", "skip_reason": "no_bars"}

    cost_bps = float(cost_model.round_trip_cost_bps(sig.asset))
    cost_pct = cost_bps / 100.0

    # ── 成本闸：只用决策之前的样本 ──────────────────────────────────────
    if cfg.cost_gate:
        prior = [float(x) for x in (prior_mfe or []) if x is not None]
        if len(prior) < cfg.min_samples:
            if cfg.cost_gate_mode == "strict":
                return {
                    **base, "status": "skipped", "skip_reason": "gate_unverified",
                    "prior_n": len(prior), "prior_median_mfe_pct": None,
                    "need_mfe_pct": round(cfg.gate_mult * cost_pct, 4),
                    "cost_bps": cost_bps,
                }
            # soft：样本不足放行并标明 unverified（与当前生产口径一致）
            gate_status = "unverified_pass"
        else:
            med = statistics.median(prior)
            need = cfg.gate_mult * cost_pct
            if med < need:
                return {
                    **base, "status": "skipped", "skip_reason": "gate_cost",
                    "prior_n": len(prior), "prior_median_mfe_pct": round(med, 4),
                    "need_mfe_pct": round(need, 4), "cost_bps": cost_bps,
                }
            gate_status = "verified_pass"
    else:
        gate_status = "no_gate"

    # ── 入场 bar ────────────────────────────────────────────────────────
    ts_ms = [b.ts for b in bars]
    entry_ms = sig.ts * 1000
    idx = bisect.bisect_left(ts_ms, entry_ms)
    if idx >= len(bars):
        return {**base, "status": "no_price", "skip_reason": "no_bar_after_signal"}
    entry_bar = bars[idx]
    entry_ts = entry_bar.ts
    entry = float(entry_bar.open)
    if entry <= 0:
        return {**base, "status": "no_price", "skip_reason": "bad_entry_price"}
    end_ms = entry_ts + sig.horizon_minutes * 60_000
    path = [b for b in bars[idx:] if b.ts <= end_ms]
    if len(path) < 2:
        return {**base, "status": "no_price", "skip_reason": "path_too_short"}

    side = 1 if sig.action == "BUY" else -1
    tp1 = entry * (1.0 + cfg.tp1_mult / 100.0 * side)
    tp2 = entry * (1.0 + cfg.tp2_mult / 100.0 * side)
    sl = entry * (1.0 - cfg.sl_mult / 100.0 * side)

    remaining = 1.0
    gross_pct = 0.0
    tp1_hit = False
    mfe = 0.0
    mae = 0.0
    peak = entry
    trough = entry
    stop = sl
    exit_ts = None
    exit_price = None
    exit_reason = None
    be_moved = False
    trail_moved = False

    def realize(price: float, ratio: float) -> None:
        nonlocal remaining, gross_pct
        ratio = max(0.0, min(remaining, ratio))
        gross_pct += ratio * side * (price - entry) / entry * 100.0
        remaining -= ratio

    for bar in path:
        minutes = (bar.ts - entry_ts) / 60_000.0

        # 1) 先判止损（同根内止损优先，取保守口径）
        if remaining > 0:
            if side == 1 and bar.low <= stop:
                fill = min(stop, bar.open)   # 跳空低开按更差的 open
                realize(fill, remaining)
                exit_price, exit_ts = fill, bar.ts
                exit_reason = "TRAIL" if trail_moved else ("BE" if be_moved else "SL")
                break
            if side == -1 and bar.high >= stop:
                fill = max(stop, bar.open)
                realize(fill, remaining)
                exit_price, exit_ts = fill, bar.ts
                exit_reason = "TRAIL" if trail_moved else ("BE" if be_moved else "SL")
                break

        # 2) 再判止盈（TP1 部分、TP2 全平）
        if remaining > 0 and not tp1_hit:
            if (side == 1 and bar.high >= tp1) or (side == -1 and bar.low <= tp1):
                realize(tp1, cfg.tp1_ratio)
                tp1_hit = True
                if remaining <= 1e-9:
                    exit_price, exit_ts, exit_reason = tp1, bar.ts, "TP1"
                    break
        if remaining > 0 and ((side == 1 and bar.high >= tp2) or (side == -1 and bar.low <= tp2)):
            realize(tp2, remaining)
            exit_price, exit_ts = tp2, bar.ts
            exit_reason = "TP2" if tp1_hit else "TP2_DIRECT"
            break

        # 3) 动态出场的“反应窗口”：20 分钟还没走出来就撤
        if cfg.exit_mode == "dynamic" and remaining > 0:
            if minutes >= cfg.reaction_minutes and mfe < cfg.reaction_min_mfe:
                realize(bar.close, remaining)
                exit_price, exit_ts, exit_reason = bar.close, bar.ts, "REACTION"
                break

        # 4) 更新极值 / 动态止损（用本根 high/low 更新，供下一根判断）
        if side == 1:
            peak = max(peak, bar.high)
            trough = min(trough, bar.low)
            mfe = max(mfe, (bar.high - entry) / entry * 100.0)
            mae = max(mae, (entry - bar.low) / entry * 100.0)
        else:
            peak = max(peak, bar.high)
            trough = min(trough, bar.low)
            mfe = max(mfe, (entry - bar.low) / entry * 100.0)
            mae = max(mae, (bar.high - entry) / entry * 100.0)

        if cfg.exit_mode == "dynamic":
            # 保本：MFE 必须先覆盖成本，否则把止损抬到 entry+cost 会高于现价，
            # 下一根直接被当成“卖出止损”打掉（这是回测里最常见的假动作）。
            be_floor = max(cfg.be_trigger_pct, cost_pct + 0.05)
            if mfe >= be_floor:
                be_stop = entry * (1.0 + cost_pct / 100.0 * side)
                stop = max(stop, be_stop) if side == 1 else min(stop, be_stop)
                be_moved = True
            # 移动止损：MFE ≥ trigger 后，按峰值回撤 giveback 比例
            if mfe >= cfg.trail_trigger_pct:
                if side == 1:
                    trail = peak * (1.0 - cfg.trail_giveback * mfe / 100.0)
                    stop = max(stop, trail)
                else:
                    trail = trough * (1.0 + cfg.trail_giveback * mfe / 100.0)
                    stop = min(stop, trail)
                trail_moved = True

    # 到期兜底
    if remaining > 0:
        last = path[-1]
        realize(last.close, remaining)
        exit_price, exit_ts, exit_reason = last.close, last.ts, "HORIZON"

    hold_min = round(((exit_ts or path[-1].ts) - entry_ts) / 60_000.0, 2)
    net_pct = gross_pct - cost_pct
    return {
        **base,
        "status": "taken",
        "skip_reason": None,
        "entry_ts": entry_ts // 1000,
        "entry_price": round(entry, 8),
        "exit_ts": (exit_ts or path[-1].ts) // 1000,
        "exit_price": round(float(exit_price or path[-1].close), 8),
        "hold_minutes": hold_min,
        "gross_pnl_pct": round(gross_pct, 4),
        "cost_bps": round(cost_bps, 2),
        "net_pnl_pct": round(net_pct, 4),
        "mfe_pct": round(mfe, 4),
        "mae_pct": round(mae, 4),
        "exit_reason": exit_reason,
        "gate_status": gate_status,
        "tp1_hit": tp1_hit,
        "be_moved": be_moved,
        "trail_moved": trail_moved,
        "prior_n": len(prior_mfe or []),
    }


# ──────────────────────────────────────────────────────────────────────────
# 变体 / 指标
# ──────────────────────────────────────────────────────────────────────────
def _mfe_history_add(
    history: Dict[Tuple[str, int], List[Tuple[int, float]]],
    trade: Dict[str, Any],
) -> None:
    if trade.get("status") != "taken":
        return
    key = (trade["asset"], int(trade["horizon_minutes"]))
    # 只有**已经结算**的交易才能进入闸的样本池；用 exit_ts 而不是 entry_ts，
    # 否则会拿一笔还没走完的交易的未来 MFE 去决定当下是否开火（未来函数）。
    history.setdefault(key, []).append((int(trade.get("exit_ts") or trade["entry_ts"]), float(trade["mfe_pct"])))


def _prior_mfe(
    history: Dict[Tuple[str, int], List[Tuple[int, float]]],
    sig: Signal,
) -> List[float]:
    key = (sig.asset, int(sig.horizon_minutes))
    return [m for ts, m in history.get(key, []) if ts < sig.ts]


def run_variant(
    signals: Sequence[Signal],
    bars_by_symbol: Dict[str, List[Bar]],
    cfg: VariantConfig,
    *,
    history: Optional[Dict[Tuple[str, int], List[Tuple[int, float]]]] = None,
) -> Dict[str, Any]:
    """跑一个变体。`history` 只在 cost_gate 变体里使用，且不会被本变体反向污染。"""
    history = history or {}
    records: List[Dict[str, Any]] = []
    for sig in sorted(signals, key=lambda s: (s.ts, s.decision_id)):
        rec = simulate_trade(
            sig,
            bars_by_symbol.get(sig.asset, []),
            cfg,
            prior_mfe=_prior_mfe(history, sig) if cfg.cost_gate else None,
        )
        records.append(rec)
    metrics = compute_metrics(records, cfg)
    return {"config": asdict(cfg), "metrics": metrics, "records": records}


def compute_metrics(records: Sequence[Dict[str, Any]], cfg: VariantConfig) -> Dict[str, Any]:
    taken = [r for r in records if r.get("status") == "taken"]
    skipped = [r for r in records if r.get("status") == "skipped"]
    no_price = [r for r in records if r.get("status") == "no_price"]
    n = len(taken)
    if n == 0:
        return {
            "variant": cfg.name,
            "n_signals": len(records),
            "n_taken": 0,
            "n_skipped": len(skipped),
            "n_no_price": len(no_price),
            "n_unverified_pass": 0,
            "coverage": 0.0,
            "win_rate": None,
            "gross_avg_pct": None,
            "cost_avg_bps": None,
            "net_avg_pct": None,
            "net_total_pct": None,
            "profit_factor": None,
            "avg_mfe_pct": None,
            "avg_mae_pct": None,
            "mfe_capture": None,
            "max_drawdown_pct": None,
            "t_stat": None,
            "avg_hold_minutes": None,
            "skip_reasons": dict(Counter(r.get("skip_reason") or "unknown" for r in skipped)),
            "sample_status": "no_trades",
            "eligible": False,
            "reasons": ["没有可结算成交"],
        }

    taken.sort(key=lambda r: (r["entry_ts"], r["decision_id"]))
    nets = [float(r["net_pnl_pct"]) for r in taken]
    gross = [float(r["gross_pnl_pct"]) for r in taken]
    costs = [float(r["cost_bps"]) for r in taken]
    mfes = [float(r["mfe_pct"]) for r in taken]
    maes = [float(r["mae_pct"]) for r in taken]
    holds = [float(r["hold_minutes"]) for r in taken]

    wins = sum(1 for x in nets if x > 0)
    losses = sum(1 for x in nets if x < 0)
    pos_sum = sum(x for x in nets if x > 0)
    neg_sum = abs(sum(x for x in nets if x < 0))
    profit_factor = round(pos_sum / neg_sum, 3) if neg_sum > 0 else (None if pos_sum <= 0 else 99.0)

    # 等权 1 单位/笔的净值曲线 + 最大回撤（百分点）
    equity = 0.0
    peak_eq = 0.0
    max_dd = 0.0
    for x in nets:
        equity += x
        peak_eq = max(peak_eq, equity)
        max_dd = max(max_dd, peak_eq - equity)

    mean = statistics.mean(nets)
    std = statistics.stdev(nets) if n > 1 else 0.0
    t_stat = round(mean / std * (n ** 0.5), 3) if std > 0 else None
    avg_mfe = statistics.mean(mfes)
    cost_avg = statistics.mean(costs)
    net_avg = mean
    mfe_capture = round(net_avg / avg_mfe, 3) if avg_mfe > 0 else None

    reasons: List[str] = []
    if n < 60:
        reasons.append(f"样本不足（{n}<60）")
    if sum(nets) <= 0:
        reasons.append("净合计≤0")
    if net_avg <= 0:
        reasons.append("净均值为负")
    if avg_mfe < 1.5 * cost_avg / 100.0:
        reasons.append("平均 MFE 未覆盖 1.5×成本")
    if max_dd > 10.0:
        reasons.append(f"最大回撤 {max_dd:.2f}% >10%")
    if t_stat is not None and t_stat < 1.5:
        reasons.append(f"t 值 {t_stat} <1.5")

    return {
        "variant": cfg.name,
        "n_signals": len(records),
        "n_taken": n,
        "n_skipped": len(skipped),
        "n_no_price": len(no_price),
        "n_unverified_pass": sum(1 for r in taken if r.get("gate_status") == "unverified_pass"),
        "coverage": round(n / len(records), 4) if records else 0.0,
        "win_rate": round(wins / n, 4),
        "n_win": wins,
        "n_loss": losses,
        "gross_avg_pct": round(statistics.mean(gross), 4),
        "cost_avg_bps": round(cost_avg, 2),
        "net_avg_pct": round(net_avg, 4),
        "net_total_pct": round(sum(nets), 4),
        "profit_factor": profit_factor,
        "avg_mfe_pct": round(avg_mfe, 4),
        "avg_mae_pct": round(statistics.mean(maes), 4),
        "mfe_capture": mfe_capture,
        "max_drawdown_pct": round(max_dd, 4),
        "t_stat": t_stat,
        "avg_hold_minutes": round(statistics.mean(holds), 2),
        "skip_reasons": dict(Counter(r.get("skip_reason") or "unknown" for r in skipped)),
        "sample_status": "ok" if n >= 60 else "insufficient",
        "eligible": not reasons,
        "reasons": reasons,
    }


# ──────────────────────────────────────────────────────────────────────────
# 报告 / 独立库
# ──────────────────────────────────────────────────────────────────────────
def _fmt(v: Any, nd: int = 3, suffix: str = "") -> str:
    if v is None:
        return "—"
    if isinstance(v, float):
        return f"{v:.{nd}f}{suffix}"
    return f"{v}{suffix}"


def build_report(
    *,
    run_id: str,
    db_path: str,
    since_ts: int,
    until_ts: int,
    signals: Sequence[Signal],
    variants: Sequence[Dict[str, Any]],
    dedupe_window_sec: int = 300,
) -> Dict[str, Any]:
    ranked = sorted(
        variants,
        key=lambda v: (v["metrics"].get("net_total_pct") is not None,
                       v["metrics"].get("net_total_pct") or -1e9),
        reverse=True,
    )
    best = ranked[0]["config"]["name"] if ranked else None
    eligible = [v["config"]["name"] for v in ranked if v["metrics"].get("eligible")]
    sig_min = min((s.ts for s in signals), default=0)
    sig_max = max((s.ts for s in signals), default=0)
    return {
        "run_id": run_id,
        "generated_at": datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds"),
        "db_path": db_path,
        "window": {
            "since": datetime.fromtimestamp(since_ts, timezone.utc).astimezone().isoformat(timespec="seconds"),
            "until": datetime.fromtimestamp(until_ts, timezone.utc).astimezone().isoformat(timespec="seconds"),
        },
        "signal_window": {
            "first": datetime.fromtimestamp(sig_min, timezone.utc).astimezone().isoformat(timespec="seconds") if sig_min else None,
            "last": datetime.fromtimestamp(sig_max, timezone.utc).astimezone().isoformat(timespec="seconds") if sig_max else None,
        },
        "n_signals": len(signals),
        "dedupe_window_sec": dedupe_window_sec,
        "signals_by_asset": dict(Counter(s.asset for s in signals)),
        "signals_by_action": dict(Counter(s.action for s in signals)),
        "variants": variants,
        "ranking": [v["config"]["name"] for v in ranked],
        "best_by_net_total": best,
        "eligible_for_promotion": eligible,
        "verdict": (
            "样本不足，全部变体只作观察，不允许晋升"
            if not eligible else
            f"可晋升候选：{', '.join(eligible)}（仍需人工复核口径）"
        ),
    }


def report_markdown(report: Dict[str, Any]) -> str:
    lines: List[str] = []
    lines.append(f"# 策略版本赛马场 — {report['run_id']}")
    lines.append("")
    lines.append(f"- 信号窗口：{report.get('signal_window', {}).get('first')} → {report.get('signal_window', {}).get('last')}")
    lines.append(f"- 查询窗口：{report['window']['since']} → {report['window']['until']}")
    lines.append(f"- 数据源：`{report['db_path']}`（只读）")
    lines.append(f"- 方向信号：**{report['n_signals']}** 条（news_id×asset + 同品种同向时间桶，两层去重）")
    lines.append(f"- 同品种同方向去重窗口：{report.get('dedupe_window_sec', 300)} 秒（防多源转载重复计样本）")
    lines.append(f"- 品种分布：{json.dumps(report['signals_by_asset'], ensure_ascii=False)}")
    lines.append(f"- 方向分布：{json.dumps(report['signals_by_action'], ensure_ascii=False)}")
    lines.append("")
    lines.append("## 一、五匹马对比")
    lines.append("")
    lines.append("| 变体 | 信号 | 成交 | 未验证放行 | 跳过 | 无行情 | 胜率 | 毛均% | 成本bps | 净均% | 净合计% | MFE均% | 捕获率 | 最大回撤% | t | 判定 |")
    lines.append("|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|")
    for v in report["variants"]:
        m = v["metrics"]
        c = v["config"]
        verdict = "可晋升" if m.get("eligible") else ("观察" if m.get("sample_status") != "ok" else "淘汰")
        lines.append(
            f"| {c['name']} | {m['n_signals']} | {m['n_taken']} | {m.get('n_unverified_pass', 0)} | {m['n_skipped']} | {m['n_no_price']} | "
            f"{_fmt(m['win_rate'], 3)} | {_fmt(m['gross_avg_pct'], 3)} | {_fmt(m['cost_avg_bps'], 1)} | "
            f"{_fmt(m['net_avg_pct'], 3)} | {_fmt(m['net_total_pct'], 3)} | {_fmt(m['avg_mfe_pct'], 3)} | "
            f"{_fmt(m['mfe_capture'], 3)} | {_fmt(m['max_drawdown_pct'], 3)} | {_fmt(m['t_stat'], 2)} | {verdict} |"
        )
    lines.append("")
    lines.append("## 二、结论")
    lines.append("")
    lines.append(f"- 净合计排名：{' > '.join(report['ranking'])}")
    lines.append(f"- 最佳（仅按净合计，不等于可晋升）：`{report['best_by_net_total']}`")
    lines.append(f"- 晋升判定：{report['verdict']}")

    # 自动补两句“人话结论”，避免读者把 0 成交误当成模块坏了
    by_name = {v["config"]["name"]: v["metrics"] for v in report["variants"]}
    gate_soft = by_name.get("gate")
    if gate_soft and gate_soft.get("n_taken") and gate_soft.get("n_unverified_pass") == gate_soft.get("n_taken"):
        lines.append(
            f"- **软成本闸的冷启动**：`gate` 的 {gate_soft['n_taken']} 笔全部是 unverified 放行；"
            "赛马窗口太短，前 6 笔还没结算，闸没样本可验。生产上要跨天跑才有意义。"
        )
    strict = by_name.get("gate_strict")
    if strict and strict.get("n_taken") == 0 and strict.get("skip_reasons", {}).get("gate_cost"):
        lines.append(
            f"- **严格成本闸**：样本足够后拒绝了 {strict['skip_reasons']['gate_cost']} 条信号，"
            "说明这批信号的 MFE 中位数覆盖不了 1.5×成本；不是数据缺失，是入场质量不够。"
        )
    base = by_name.get("baseline")
    dyn = by_name.get("dynamic")
    if base and dyn and base.get("net_total_pct") is not None and dyn.get("net_total_pct") is not None:
        delta = dyn["net_total_pct"] - base["net_total_pct"]
        lines.append(
            f"- **动态出场 vs 固定出场**：净合计差 {delta:+.3f} 个百分点；"
            "如果差距很小，说明救不回来的是入场，不是出场。"
        )
    lines.append("")
    lines.append("## 三、每个变体的拒绝原因")
    lines.append("")
    for v in report["variants"]:
        m = v["metrics"]
        name = v["config"]["name"]
        if m.get("skip_reasons"):
            lines.append(f"- **{name}**：{json.dumps(m['skip_reasons'], ensure_ascii=False)}")
        else:
            lines.append(f"- **{name}**")
        if m.get("reasons"):
            lines.append(f"  - 不能晋升：{'；'.join(m['reasons'])}")
    lines.append("")
    lines.append("## 四、口径（不可偷看未来）")
    lines.append("")
    lines.append("- 入场价 = 决策时间之后第一根 1m K 线的 `open`，不用决策当根收盘。")
    lines.append("- 出场只用同一份 1m K 线路径；同一根内止损优先于止盈（保守口径）。")
    lines.append("- 成本 = `cost_model.round_trip_cost_bps(asset)`，净收益 = 毛收益 − 成本。")
    lines.append("- 成本闸只用**决策之前**的生产已结算 MFE + 本场 baseline 在此之前产生的 MFE；样本 <6 时软闸放行、硬闸拒绝。")
    lines.append("- 样本 <60 笔一律不判“版本更优”，只输出观察结论。")
    lines.append("")
    lines.append("## 五、成交明细（最多 20 笔/变体）")
    lines.append("")
    for v in report["variants"]:
        trades = [r for r in v["records"] if r.get("status") == "taken"][:20]
        if not trades:
            continue
        lines.append(f"### {v['config']['name']}")
        lines.append("")
        lines.append("| decision | 品种 | 方向 | 入场 | 出场 | 持有m | 毛% | 成本bps | 净% | MFE% | 出场原因 |")
        lines.append("|---|---|---|---|---|---|---|---|---|---|---|")
        for t in trades:
            lines.append(
                f"| {t['decision_id']} | {t['asset']} | {t['action']} | {t['entry_price']} | "
                f"{t['exit_price']} | {t['hold_minutes']} | {t['gross_pnl_pct']} | {t['cost_bps']} | "
                f"{t['net_pnl_pct']} | {t['mfe_pct']} | {t['exit_reason']} |"
            )
        lines.append("")
    lines.append("---")
    lines.append("")
    lines.append("*生成：`variant_arena.py`；本报告只读生产库，写入独立 `runtime/eval/variant_arena.db`。*")
    return "\n".join(lines)


def write_arena_db(
    arena_db: Path,
    report: Dict[str, Any],
) -> None:
    arena_db.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(arena_db), timeout=30)
    try:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS arena_runs (
                run_id TEXT PRIMARY KEY,
                generated_at TEXT,
                db_path TEXT,
                since_ts INTEGER,
                until_ts INTEGER,
                n_signals INTEGER,
                report_json TEXT
            );
            CREATE TABLE IF NOT EXISTS arena_variants (
                run_id TEXT,
                variant TEXT,
                n_signals INTEGER,
                n_taken INTEGER,
                n_skipped INTEGER,
                n_no_price INTEGER,
                win_rate REAL,
                gross_avg_pct REAL,
                cost_avg_bps REAL,
                net_avg_pct REAL,
                net_total_pct REAL,
                avg_mfe_pct REAL,
                mfe_capture REAL,
                max_drawdown_pct REAL,
                t_stat REAL,
                sample_status TEXT,
                eligible INTEGER,
                reasons_json TEXT,
                metrics_json TEXT,
                PRIMARY KEY (run_id, variant)
            );
            CREATE TABLE IF NOT EXISTS arena_trades (
                run_id TEXT,
                variant TEXT,
                decision_id INTEGER,
                news_id INTEGER,
                asset TEXT,
                action TEXT,
                status TEXT,
                skip_reason TEXT,
                entry_ts INTEGER,
                entry_price REAL,
                exit_ts INTEGER,
                exit_price REAL,
                hold_minutes REAL,
                gross_pnl_pct REAL,
                cost_bps REAL,
                net_pnl_pct REAL,
                mfe_pct REAL,
                mae_pct REAL,
                exit_reason TEXT,
                gate_status TEXT,
                PRIMARY KEY (run_id, variant, decision_id)
            );
            """
        )
        # 旧库补列（CREATE IF NOT EXISTS 不会自动加新列）
        _cols = {r[1] for r in conn.execute("PRAGMA table_info(arena_trades)")}
        if "gate_status" not in _cols:
            conn.execute("ALTER TABLE arena_trades ADD COLUMN gate_status TEXT")
        conn.execute(
            """INSERT OR REPLACE INTO arena_runs
               (run_id, generated_at, db_path, since_ts, until_ts, n_signals, report_json)
               VALUES (?,?,?,?,?,?,?)""",
            (
                report["run_id"], report["generated_at"], report["db_path"],
                int(parse_ts(report["window"]["since"])),
                int(parse_ts(report["window"]["until"])),
                report["n_signals"], json.dumps(report, ensure_ascii=False),
            ),
        )
        for v in report["variants"]:
            m = v["metrics"]
            conn.execute(
                """INSERT OR REPLACE INTO arena_variants
                   (run_id, variant, n_signals, n_taken, n_skipped, n_no_price,
                    win_rate, gross_avg_pct, cost_avg_bps, net_avg_pct, net_total_pct,
                    avg_mfe_pct, mfe_capture, max_drawdown_pct, t_stat, sample_status,
                    eligible, reasons_json, metrics_json)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    report["run_id"], v["config"]["name"], m["n_signals"], m["n_taken"],
                    m["n_skipped"], m["n_no_price"], m["win_rate"], m["gross_avg_pct"],
                    m["cost_avg_bps"], m["net_avg_pct"], m["net_total_pct"],
                    m["avg_mfe_pct"], m["mfe_capture"], m["max_drawdown_pct"],
                    m["t_stat"], m["sample_status"], 1 if m["eligible"] else 0,
                    json.dumps(m.get("reasons", []), ensure_ascii=False),
                    json.dumps(m, ensure_ascii=False),
                ),
            )
            for r in v["records"]:
                conn.execute(
                    """INSERT OR REPLACE INTO arena_trades
                       (run_id, variant, decision_id, news_id, asset, action, status,
                        skip_reason, entry_ts, entry_price, exit_ts, exit_price,
                        hold_minutes, gross_pnl_pct, cost_bps, net_pnl_pct,
                        mfe_pct, mae_pct, exit_reason, gate_status)
                       VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (
                        report["run_id"], v["config"]["name"], r.get("decision_id"),
                        r.get("news_id"), r.get("asset"), r.get("action"), r.get("status"),
                        r.get("skip_reason"), r.get("entry_ts"), r.get("entry_price"),
                        r.get("exit_ts"), r.get("exit_price"), r.get("hold_minutes"),
                        r.get("gross_pnl_pct"), r.get("cost_bps"), r.get("net_pnl_pct"),
                        r.get("mfe_pct"), r.get("mae_pct"), r.get("exit_reason"),
                        r.get("gate_status"),
                    ),
                )
        conn.commit()
    finally:
        conn.close()


# ──────────────────────────────────────────────────────────────────────────
# 入口
# ──────────────────────────────────────────────────────────────────────────
def run_arena(
    db_path: str,
    *,
    since_ts: int,
    until_ts: int,
    variants: Sequence[VariantConfig] = DEFAULT_VARIANTS,
    min_samples: int = MIN_SAMPLES,
    dedupe_window_sec: int = 300,
) -> Dict[str, Any]:
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=30)
    conn.row_factory = sqlite3.Row
    try:
        signals = load_signals(conn, since_ts, until_ts, dedupe_window_sec=dedupe_window_sec)
        if not signals:
            raise SystemExit("没有可用的方向信号（BUY/SELL + 非 NONE 标的）")
        max_h = max(s.horizon_minutes for s in signals)
        start_ms = min(s.ts for s in signals) * 1000 - 15 * 60_000
        end_ms = (max(s.ts for s in signals) + max_h * 60) * 1000 + 5 * 60_000
        bars = load_bars(conn, {s.asset for s in signals}, start_ms, end_ms)
        prod_mfe = load_production_mfe(conn)
    finally:
        conn.close()

    # baseline 先跑：成本闸要用它在此之前产生的 MFE 做“出道前样本”
    base_cfg = next((v for v in variants if not v.cost_gate and v.exit_mode == "fixed"),
                    VariantConfig(name="baseline", exit_mode="fixed", cost_gate=False))
    base = run_variant(signals, bars, base_cfg)

    history: Dict[Tuple[str, int], List[Tuple[int, float]]] = defaultdict(list)
    for k, vals in prod_mfe.items():
        history[k].extend(vals)
    for r in base["records"]:
        _mfe_history_add(history, r)
    for vals in history.values():
        vals.sort()

    results: List[Dict[str, Any]] = []
    for cfg in variants:
        cfg2 = VariantConfig(**{**asdict(cfg), "min_samples": int(min_samples)})
        if cfg2.name == base_cfg.name:
            results.append(base)
            continue
        # gate 变体用 baseline 的历史；非 gate 变体不需要
        results.append(run_variant(signals, bars, cfg2, history=history if cfg2.cost_gate else None))

    run_id = "arena_" + datetime.now().strftime("%Y%m%d_%H%M%S")
    report = build_report(
        run_id=run_id,
        db_path=str(db_path),
        since_ts=since_ts,
        until_ts=until_ts,
        signals=signals,
        variants=results,
        dedupe_window_sec=dedupe_window_sec,
    )
    return report


def main(argv: Optional[Sequence[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="策略版本赛马场（离线、只读生产库）")
    ap.add_argument("--db", default=str(DEFAULT_DB), help="生产库路径（只读打开）")
    ap.add_argument("--days", type=float, default=1.0, help="回看天数（默认 1）")
    ap.add_argument("--since", default="", help="起始时间 ISO，优先于 --days")
    ap.add_argument("--until", default="", help="结束时间 ISO，默认现在")
    ap.add_argument("--variants", default="baseline,dynamic,gate,gate_dynamic,gate_strict",
                    help="逗号分隔：baseline,dynamic,gate,gate_dynamic,gate_strict")
    ap.add_argument("--min-samples", type=int, default=MIN_SAMPLES)
    ap.add_argument("--dedupe-window", type=int, default=300,
                    help="同品种同方向多少秒内只算一次（默认 300，防多源转载重复计样本）")
    ap.add_argument("--out", default=str(DEFAULT_OUT))
    ap.add_argument("--arena-db", default=str(DEFAULT_ARENA_DB))
    ap.add_argument("--no-write", action="store_true", help="只打印，不写报告/库")
    args = ap.parse_args(argv)

    until_ts = parse_ts(args.until) or int(time.time())
    if args.since:
        since_ts = parse_ts(args.since)
        if since_ts <= 0:
            ap.error(f"无法解析 --since: {args.since}")
    else:
        since_ts = int(until_ts - args.days * 86400)

    all_cfgs = {v.name: v for v in DEFAULT_VARIANTS}
    names = [x.strip() for x in args.variants.split(",") if x.strip()]
    unknown = [x for x in names if x not in all_cfgs]
    if unknown:
        ap.error(f"未知变体: {unknown}；可选 {list(all_cfgs)}")
    cfgs = [all_cfgs[n] for n in names]

    report = run_arena(args.db, since_ts=since_ts, until_ts=until_ts,
                       variants=cfgs, min_samples=args.min_samples,
                       dedupe_window_sec=args.dedupe_window)

    if args.no_write:
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return 0

    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "latest.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    (out_dir / "latest.md").write_text(report_markdown(report), encoding="utf-8")
    write_arena_db(Path(args.arena_db), report)

    print(f"[variant_arena] run={report['run_id']} signals={report['n_signals']}")
    for v in report["variants"]:
        m = v["metrics"]
        print(
            f"  {v['config']['name']:<13} taken={m['n_taken']:>3} "
            f"win={_fmt(m['win_rate'],3)} net_avg={_fmt(m['net_avg_pct'],3)}% "
            f"net_total={_fmt(m['net_total_pct'],3)}% dd={_fmt(m['max_drawdown_pct'],3)}% "
            f"{'ELIGIBLE' if m['eligible'] else 'observe'}"
        )
    print(f"  report: {out_dir / 'latest.md'}")
    print(f"  arena db: {args.arena_db}")
    print(f"  verdict: {report['verdict']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
