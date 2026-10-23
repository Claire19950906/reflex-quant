#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""v226+ P1-2 wire-in：周期反转信号推送守护。

职责：
  · 周期调 cycle_analyst.predict_reversal() 对每个 kline_bars symbol 检测；
  · 反转概率 ≥ THRESHOLD 且 votes ≥ MIN_VOTES → 调 engine.alerts.send_important_news；
  · 同一 (asset, direction, 4h_bucket) 5h 内不重复推（去重窗口）。

数据源：kline_bars（>= 30 根才参与）。
推送渠道：format_reversal_alert_message → send_important_news（与 Fed 异动推送一致）。
"""
from __future__ import annotations

import json
import os
import sqlite3
import time
from typing import Any, Dict, Optional, Tuple

import db


# 推送门槛
PROB_THRESHOLD = float(os.environ.get("REVERSAL_PROB_THRESHOLD", "0.66"))
MIN_VOTES = int(os.environ.get("REVERSAL_MIN_VOTES", "2"))
DEDUP_WINDOW_SEC = 5 * 3600  # 5h
# kline_bars 表里实际存的 symbol 名(短名,不带 -USD 后缀)。
# v276+ 增加 XAU/XAG/WTI 贵金属+原油(它们有充足的 1d 数据)
DEFAULT_SYMBOLS = ["BTC", "ETH", "SOL", "XAU", "XAG", "WTI"]


# 去重缓存（in-process；重启后重新推）
_seen: Dict[Tuple[str, str, int], float] = {}


# v276: 信号落库(便于 cron digest + 仪表盘查询)
_SIGNALS_TABLE_DDL = """
CREATE TABLE IF NOT EXISTS cycle_reversal_signals (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    asset TEXT NOT NULL,
    direction TEXT NOT NULL,
    prob REAL NOT NULL,
    votes INTEGER NOT NULL,
    days_to INTEGER,
    hurst REAL,
    rsi REAL,
    phase TEXT,
    sent INTEGER DEFAULT 0,
    ts INTEGER NOT NULL,
    UNIQUE (asset, direction, prob, ts)
)
"""
_SIGNALS_TABLE_DDL_INDEX = (
    "CREATE INDEX IF NOT EXISTS idx_cycle_reversal_ts ON cycle_reversal_signals(ts DESC)"
)


def _ensure_signals_table() -> None:
    conn = sqlite3.connect(db.get_db_path())
    try:
        conn.execute(_SIGNALS_TABLE_DDL)
        conn.execute(_SIGNALS_TABLE_DDL_INDEX)
        conn.commit()
    finally:
        try: conn.close()
        except Exception: pass


def _record_signal(asset: str, direction: str, prob: float, votes: int,
                   days_to: Optional[int], phase: Dict[str, Any],
                   sent: bool) -> None:
    """v276: 写入 cycle_reversal_signals 表(便于 cron digest 查询)。"""
    try:
        conn = sqlite3.connect(db.get_db_path())
        try:
            conn.execute(
                """
                INSERT OR IGNORE INTO cycle_reversal_signals
                  (asset, direction, prob, votes, days_to, hurst, rsi, phase, sent, ts)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (asset, direction, float(prob), int(votes), days_to,
                 phase.get("hurst"), phase.get("rsi"), phase.get("phase"),
                 1 if sent else 0, int(time.time())),
            )
            conn.commit()
        finally:
            try: conn.close()
            except Exception: pass
    except Exception:
        pass


def _asset_to_yf_symbol(asset: str) -> str:
    """v276+: kline_bars 表里实际存的是 'BTC'/'ETH'/'XAU' 等短名(不带 -USD)。

    之前错误地映射为 'BTC-USD',导致 _fetch_prices 永远查不到数据,
    cycle_reversal_signals 表永远为空,watcher 形同虚设。
    现在直接返回短名,与 kline_bars 对齐。
    """
    return asset.upper()


def _fetch_prices(symbol: str, db_path: Optional[str] = None,
                 limit: int = 60) -> list:
    p = db_path or db.get_db_path()
    conn = sqlite3.connect(p)
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(
            "SELECT close FROM kline_bars "
            "WHERE symbol=? AND timeframe='1d' AND close IS NOT NULL "
            "ORDER BY ts DESC LIMIT ?",
            (symbol, limit),
        ).fetchall()
    finally:
        conn.close()
    return [float(r['close']) for r in reversed(rows)]


def _dedup_key(asset: str, direction: str) -> Tuple[str, str, int]:
    """去重键（asset + direction + 4h bucket）。"""
    bucket = int(time.time() // (4 * 3600))
    return (asset, direction, bucket)


def _should_send(asset: str, direction: str, prob: float) -> bool:
    """门槛 + 去重判定。"""
    if prob < PROB_THRESHOLD:
        return False
    key = _dedup_key(asset, direction)
    last_ts = _seen.get(key, 0.0)
    if (time.time() - last_ts) < DEDUP_WINDOW_SEC:
        return False
    _seen[key] = time.time()
    return True


def scan_once(symbols: list = None,
              db_path: Optional[str] = None) -> Dict[str, Any]:
    """单次扫描 + 推送。"""
    from cycle_analyst import detect_phase, predict_reversal
    symbols = symbols or DEFAULT_SYMBOLS

    scanned = 0
    sent = 0
    skipped_low_prob = 0
    skipped_dedup = 0
    skipped_no_data = 0
    results: list = []

    for asset in symbols:
        yf_sym = _asset_to_yf_symbol(asset)
        prices = _fetch_prices(yf_sym, db_path=db_path, limit=60)
        if len(prices) < 30:
            skipped_no_data += 1
            continue
        scanned += 1

        try:
            phase = detect_phase(prices, window=20)
            reversal = predict_reversal(prices, reversal_threshold_pct=3.0)
        except Exception:
            continue

        prob = float(reversal.get("prob", 0) or 0)
        votes = int(reversal.get("votes", 0) or 0)
        direction = reversal.get("direction", "neutral")
        days_to = reversal.get("days_to")

        if votes < MIN_VOTES or prob < PROB_THRESHOLD:
            skipped_low_prob += 1
            results.append({
                "asset": asset, "prob": prob, "votes": votes,
                "direction": direction, "days_to": days_to,
                "phase": phase.get("phase"), "sent": False,
                "skip_reason": "low_prob" if prob < PROB_THRESHOLD else "low_votes",
            })
            # v276: 即使概率不够,也落库(用于回看「差一点点就达标的信号」)
            _record_signal(asset, direction, prob, votes, days_to, phase, sent=False)
            continue

        if not _should_send(asset, direction, prob):
            skipped_dedup += 1
            results.append({
                "asset": asset, "prob": prob, "votes": votes,
                "direction": direction, "days_to": days_to,
                "phase": phase.get("phase"), "sent": False,
                "skip_reason": "dedup",
            })
            _record_signal(asset, direction, prob, votes, days_to, phase, sent=False)
            continue

        try:
            from engine.alerts import (
                format_reversal_alert_message, send_important_news,
            )
            extra = {"phase": phase.get("phase"), "votes": votes,
                     "hurst": phase.get("hurst"), "rsi": phase.get("rsi")}
            title = format_reversal_alert_message(
                prob=prob, days_to=days_to, direction=direction,
                asset=asset, title_only=True, extra=extra,
            )
            body = format_reversal_alert_message(
                prob=prob, days_to=days_to, direction=direction,
                asset=asset, title_only=False, extra=extra,
            )
            send_important_news(title, body)
            sent += 1
            _record_signal(asset, direction, prob, votes, days_to, phase, sent=True)
            results.append({
                "asset": asset, "prob": prob, "votes": votes,
                "direction": direction, "days_to": days_to,
                "phase": phase.get("phase"), "sent": True,
                "skip_reason": None,
            })
        except Exception as exc:
            _record_signal(asset, direction, prob, votes, days_to, phase, sent=False)
            results.append({
                "asset": asset, "prob": prob, "votes": votes,
                "direction": direction, "days_to": days_to,
                "phase": phase.get("phase"), "sent": False,
                "skip_reason": f"push_failed:{type(exc).__name__}",
            })

    return {
        "scanned": scanned, "sent": sent,
        "skipped_low_prob": skipped_low_prob,
        "skipped_dedup": skipped_dedup,
        "skipped_no_data": skipped_no_data,
        "results": results,
        "ts": int(time.time()),
    }


def daemon_loop(interval_s: int = 300, symbols: list = None) -> None:
    """周期守护（默认 5 分钟）。"""
    interval_s = max(60, int(os.environ.get("REVERSAL_INTERVAL_S", interval_s)))
    _ensure_signals_table()  # v276: 启动时确保表存在
    print(f"[REVERSAL] daemon started | interval={interval_s}s | "
          f"prob_threshold={PROB_THRESHOLD} | min_votes={MIN_VOTES}", flush=True)
    while True:
        try:
            r = scan_once(symbols=symbols)
            if r["sent"] > 0 or r["skipped_low_prob"] > 0:
                print(f"[REVERSAL] @ {r['ts']:.0f} scanned={r['scanned']} "
                      f"sent={r['sent']} low_prob={r['skipped_low_prob']} "
                      f"dedup={r['skipped_dedup']} no_data={r['skipped_no_data']}",
                      flush=True)
        except KeyboardInterrupt:
            raise
        except Exception as exc:
            print(f"[REVERSAL] loop err: {type(exc).__name__}: {exc}", flush=True)
        time.sleep(interval_s)


def reset_dedup() -> None:
    """测试 / 手动重置去重缓存。"""
    _seen.clear()


if __name__ == "__main__":
    import argparse
    p = argparse.ArgumentParser()
    p.add_argument("--once", action="store_true")
    p.add_argument("--daemon", action="store_true")
    p.add_argument("--interval", type=int, default=300)
    args = p.parse_args()
    if args.daemon:
        daemon_loop(interval_s=args.interval)
    else:
        r = scan_once()
        print(json.dumps({k: v for k, v in r.items() if k != "results"},
                          ensure_ascii=False, indent=2))
        for x in r["results"]:
            print(x)