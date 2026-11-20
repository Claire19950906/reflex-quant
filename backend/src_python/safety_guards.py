#!/usr/bin/env python3
"""v230+ 模拟盘安全护栏：日亏熔断 + 高影响事件窗口过滤。

设计要点：
- 所有检查是幂等的、可重入的——在 open_trades 之前批量调用，无副作用。
- 用 quick_sim_trades 实际成交价计算日累计盈亏，不依赖交易所实时账户。
- 事件窗口默认 ±300 秒 (5 分钟)，通过 config.MACRO_EVENTS_FILE 提供事件列表；
  文件缺失或解析失败 → 静默放行（避免阻塞正常交易）。
- 任一拒绝都会记录详细 metadata，方便 reflection/risk_manager 链路回溯。
"""
from __future__ import annotations

import json
import os
import sqlite3
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime
from typing import Any, Dict, List, Optional

import config


@dataclass
class SafetyDecision:
    """单个护栏的检查结果。"""
    allow: bool
    guard: str
    reason: str
    metadata: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


# ── P0.3 日亏熔断 ─────────────────────────────────────────────────────────
def _today_start_local_ts(now_ts: Optional[int] = None) -> int:
    """返回今天 00:00:00 (本地时区) 的 Unix 秒。"""
    now = datetime.fromtimestamp(int(now_ts if now_ts is not None else time.time()))
    start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    return int(start.timestamp())


def check_daily_drawdown(
    conn: sqlite3.Connection,
    daily_loss_limit_usdt: Optional[float] = None,
    now_ts: Optional[int] = None,
) -> SafetyDecision:
    """P0.3: 当日累计亏损熔断检查。

    用 quick_sim_trades 中今日已结算（settled=1）且 exit_ts >= today_start_local
    的所有 trade.pnl_usdt 求和；若累计亏损 >= daily_loss_limit_usdt，返回拒绝。
    """
    if daily_loss_limit_usdt is None:
        daily_loss_limit_usdt = float(
            getattr(config, "DAILY_LOSS_LIMIT_USDT", None)
            or getattr(config, "QUICK_SIM_NOTIONAL_USDT", 100.0)
        )
    limit = float(daily_loss_limit_usdt)
    today_start = _today_start_local_ts(now_ts=now_ts)
    try:
        row = conn.execute(
            """SELECT COALESCE(SUM(pnl_usdt), 0.0) AS daily_pnl_usdt,
                      COUNT(*) AS settled_today
               FROM quick_sim_trades
               WHERE settled = 1
                 AND exit_ts >= ?""",
            (today_start,),
        ).fetchone()
    except sqlite3.OperationalError as exc:
        return SafetyDecision(
            allow=True, guard="daily_loss", reason="table_unavailable",
            metadata={"error": str(exc)[:120], "limit_usdt": limit},
        )

    if row is None:
        return SafetyDecision(
            allow=True, guard="daily_loss", reason="no_data",
            metadata={"daily_pnl_usdt": 0.0, "limit_usdt": limit,
                      "today_start_ts": today_start},
        )
    daily_pnl = float(row[0] or 0.0)
    settled_today = int(row[1] or 0)
    loss = max(-daily_pnl, 0.0)
    metadata = {
        "daily_pnl_usdt": round(daily_pnl, 4),
        "daily_loss_usdt": round(loss, 4),
        "limit_usdt": limit,
        "settled_today": settled_today,
        "today_start_ts": today_start,
    }
    if loss >= limit:
        return SafetyDecision(
            allow=False, guard="daily_loss",
            reason=f"daily_loss {loss:.2f} >= limit {limit:.2f} USDT",
            metadata=metadata,
        )
    return SafetyDecision(allow=True, guard="daily_loss", reason="ok", metadata=metadata)


# ── P0.4 事件窗口过滤 ──────────────────────────────────────────────────────
def _load_macro_events(file_path: Optional[str] = None) -> List[Dict[str, Any]]:
    """从 JSON 文件加载高影响宏观事件时间表。

    JSON 格式: [{"name": "FOMC", "ts": 1731609600, "window_seconds": 600}, ...]
    文件缺失 / 解析失败 → 返回空列表 (== 静默放行)。
    """
    file_path = file_path or getattr(config, "MACRO_EVENTS_FILE", None)
    if not file_path or not os.path.exists(file_path):
        return []
    try:
        with open(file_path, "r", encoding="utf-8") as fp:
            raw = json.load(fp)
        if not isinstance(raw, list):
            return []
        return [e for e in raw if isinstance(e, dict) and "ts" in e]
    except (OSError, ValueError):
        return []


def check_event_window(
    now_ts: Optional[int] = None,
    window_seconds: Optional[int] = None,
    events: Optional[List[Dict[str, Any]]] = None,
) -> SafetyDecision:
    """P0.4: 高影响事件窗口检查。

    当前时间 ± window_seconds 秒内若有 FOMC/NFP/CPI 等高影响事件，拒绝新建仓。
    经验：事件发布前 5 分钟到发布后 5 分钟内，spread 拉宽、价格跳空，胜率 < 40%。
    """
    if window_seconds is None:
        window_seconds = int(
            getattr(config, "EVENT_WINDOW_SECONDS", None) or 300
        )
    default_window = int(window_seconds)
    now = int(now_ts if now_ts is not None else time.time())
    loaded = events if events is not None else _load_macro_events()

    for ev in loaded:
        try:
            ev_ts = int(ev.get("ts") or 0)
        except (TypeError, ValueError):
            continue
        ev_window = int(ev.get("window_seconds") or default_window)
        distance = abs(now - ev_ts)
        if distance <= ev_window:
            return SafetyDecision(
                allow=False, guard="event_window",
                reason=f"within {ev_window}s of {ev.get('name', 'event')}",
                metadata={
                    "event_name": ev.get("name", ""),
                    "event_ts": ev_ts,
                    "event_window_seconds": ev_window,
                    "now_ts": now,
                    "distance_sec": distance,
                },
            )
    return SafetyDecision(
        allow=True, guard="event_window", reason="no_active_event",
        metadata={"now_ts": now, "events_loaded": len(loaded),
                  "default_window_seconds": default_window},
    )


# ── 一站式 ────────────────────────────────────────────────────────────────
def evaluate_all(
    conn: sqlite3.Connection,
    daily_loss_limit_usdt: Optional[float] = None,
    event_window_seconds: Optional[int] = None,
    *,
    daily_loss_enabled: Optional[bool] = None,
    event_window_enabled: Optional[bool] = None,
    events: Optional[List[Dict[str, Any]]] = None,
    now_ts: Optional[int] = None,
) -> SafetyDecision:
    """一站式检查：先看日亏，再看事件窗口；任一拒绝则整体拒绝。

    任一护栏可通过 daily_loss_enabled / event_window_enabled 显式跳过；
    None 时取 config.DAILY_LOSS_GUARD_ENABLED / config.EVENT_WINDOW_GUARD_ENABLED
    (默认均为 True)。events 可显式传入以跳过文件读取 (单测/演示用)。
    """
    if daily_loss_enabled is None:
        daily_loss_enabled = bool(
            getattr(config, "DAILY_LOSS_GUARD_ENABLED", True)
        )
    if event_window_enabled is None:
        event_window_enabled = bool(
            getattr(config, "EVENT_WINDOW_GUARD_ENABLED", True)
        )

    daily = (check_daily_drawdown(conn, daily_loss_limit_usdt=daily_loss_limit_usdt,
                                   now_ts=now_ts)
             if daily_loss_enabled else
             SafetyDecision(allow=True, guard="daily_loss", reason="disabled",
                            metadata={"enabled": False}))
    if not daily.allow:
        return daily
    event = (check_event_window(now_ts=now_ts, window_seconds=event_window_seconds,
                                events=events)
             if event_window_enabled else
             SafetyDecision(allow=True, guard="event_window", reason="disabled",
                            metadata={"enabled": False}))
    if not event.allow:
        return event
    return SafetyDecision(
        allow=True, guard="ok", reason="all_guards_passed",
        metadata={"daily_loss": daily.metadata, "event_window": event.metadata,
                  "daily_loss_enabled": daily_loss_enabled,
                  "event_window_enabled": event_window_enabled},
    )


__all__ = [
    "SafetyDecision",
    "check_daily_drawdown",
    "check_event_window",
    "evaluate_all",
    "_load_macro_events",
    "_today_start_local_ts",
]
