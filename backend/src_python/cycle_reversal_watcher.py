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
# 2026-11-19：0.66 → 0.90（用户口径"只把重要信息发我"）。
# 事实：predict_reversal 的 prob 是 3 票制离散值，实测近 30 天只有两档——
#   0.333（3 票中 1 票）51 次、0.667（3 票中 2 票）51 次，从未出现 1.0（3 票全中）。
# 所以阈值 ≥0.72 在实际效果上等于"这类信号只落库、不推手机"，
# 只有 3 票全中（prob=1.0）这种历史罕见的强共振才会发一封。
PROB_THRESHOLD = float(os.environ.get("REVERSAL_PROB_THRESHOLD", "0.90"))
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
    except Exception as _silent_exc_91:
        print("[cycle_reversal_watcher] ⚠️ 写库/调用失败（原先静默吞掉）: %s: %s"
                  % (type(_silent_exc_91).__name__, str(_silent_exc_91)[:120]), flush=True)


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


REPEAT_WINDOW_SEC = int(os.environ.get("REVERSAL_REPEAT_WINDOW_S", str(12 * 3600)))
PROB_UPGRADE = 0.10  # 概率涨 10 个点以上才当"升级"再推一次


def _last_pushed(db_path: str, asset: str, direction: str) -> Optional[Tuple[float, Optional[str], int]]:
    """查库里最近一次真正推送过的同品种同方向信号（重启不丢）。"""
    try:
        conn = sqlite3.connect(db_path)
        try:
            row = conn.execute(
                "SELECT prob, phase, ts FROM cycle_reversal_signals "
                "WHERE asset=? AND direction=? AND sent=1 ORDER BY ts DESC LIMIT 1",
                (asset, direction)).fetchone()
        finally:
            conn.close()
    except Exception:
        return None
    return (float(row[0]), row[1], int(row[2])) if row else None


def _is_new_information(db_path: str, asset: str, direction: str,
                        prob: float, phase: Optional[str]) -> Tuple[bool, str]:
    """和上次推过的比：没变化就不再打扰。"""
    last = _last_pushed(db_path, asset, direction)
    if not last:
        return True, "first"
    last_prob, last_phase, last_ts = last
    if time.time() - last_ts >= REPEAT_WINDOW_SEC:
        return True, "window_expired"
    if phase and last_phase and phase != last_phase:
        return True, f"phase_change({last_phase}->{phase})"
    if prob - last_prob >= PROB_UPGRADE:
        return True, f"prob_upgrade({last_prob:.2f}->{prob:.2f})"
    return False, f"unchanged(已推过 {int((time.time()-last_ts)//60)} 分钟前)"


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
    pending: list = []

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

        _dbp = db_path or db.get_db_path()
        is_new, why = _is_new_information(_dbp, asset, direction, prob, phase.get("phase"))
        if not is_new:
            skipped_dedup += 1
            results.append({
                "asset": asset, "prob": prob, "votes": votes,
                "direction": direction, "days_to": days_to,
                "phase": phase.get("phase"), "sent": False,
                "skip_reason": f"unchanged:{why}",
            })
            _record_signal(asset, direction, prob, votes, days_to, phase, sent=False)
            continue

        # 先记账，最后合并成一封发出去 —— 三个品种不该拆成三封邮件
        pending.append({
            "asset": asset, "prob": prob, "votes": votes,
            "direction": direction, "days_to": days_to,
            "phase": phase, "reason": why,
        })

    if pending:
        try:
            from engine.alerts import (
                format_reversal_alert_message, send_important_news,
            )
            if len(pending) == 1:
                p0 = pending[0]
                extra = {"phase": p0["phase"].get("phase"), "votes": p0["votes"],
                         "hurst": p0["phase"].get("hurst"), "rsi": p0["phase"].get("rsi")}
                title = format_reversal_alert_message(
                    prob=p0["prob"], days_to=p0["days_to"], direction=p0["direction"],
                    asset=p0["asset"], title_only=True, extra=extra)
                body = format_reversal_alert_message(
                    prob=p0["prob"], days_to=p0["days_to"], direction=p0["direction"],
                    asset=p0["asset"], title_only=False, extra=extra)
            else:
                names = "/".join(str(x["asset"]) for x in pending)
                dirs = {str(x["direction"]) for x in pending}
                d_cn = "反弹" if dirs == {"up"} else ("回落" if dirs == {"down"} else "方向不一")
                title = f"[周期反转] {len(pending)} 个品种同时出现{d_cn}信号（{names}）"[:60]
                lines = [f"共 {len(pending)} 个品种同时触发周期反转，合并成一封，不再逐个刷屏。", ""]
                for x in pending:
                    lines.append(format_reversal_alert_message(
                        prob=x["prob"], days_to=x["days_to"], direction=x["direction"],
                        asset=x["asset"], title_only=False,
                        extra={"phase": x["phase"].get("phase"), "votes": x["votes"],
                               "hurst": x["phase"].get("hurst"), "rsi": x["phase"].get("rsi")}))
                body = "\n\n".join(lines)
            ok = bool(send_important_news(title, body))
            if ok:
                sent = len(pending)
            # 闸门拦下时不能记"已推送" —— 否则库里记已发、手机没收到，
            # 之后 12h 都不会再试（黑洞）。拦下就记 sent=False，下轮还能重试。
            for x in pending:
                _record_signal(x["asset"], x["direction"], x["prob"], x["votes"],
                               x["days_to"], x["phase"], sent=ok)
                results.append({
                    "asset": x["asset"], "prob": x["prob"], "votes": x["votes"],
                    "direction": x["direction"], "days_to": x["days_to"],
                    "phase": x["phase"].get("phase"), "sent": ok,
                    "skip_reason": None if ok else "alert_gate_suppressed",
                    "trigger": x["reason"],
                })
        except Exception as exc:
            for x in pending:
                _record_signal(x["asset"], x["direction"], x["prob"], x["votes"],
                               x["days_to"], x["phase"], sent=False)
                results.append({
                    "asset": x["asset"], "prob": x["prob"], "votes": x["votes"],
                    "direction": x["direction"], "days_to": x["days_to"],
                    "phase": x["phase"].get("phase"), "sent": False,
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
