#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""推送准度账本：每条推到手机的提醒，事后自动结算"准不准"。

背景（2026-11-19 用户反馈）：
  「鹰派鸽派、多因子共振，都不准，就在那瞎发。」
  查下来真正的病根不是阈值高低，而是**系统从来没有记录过推送后对不对**——
  发了多少条、哪条兑现、哪条打脸，一条都没有落库。没有账本，
  调阈值就只能靠猜，今年调 8.0→5.0、明年再猜一次。

本模块做三件事：
  1. record()：推送成功时记一条（类型/标题/识别出的资产/方向/推送时价）
  2. settle()：过 4 小时（主口径）后按 kline_bars 结算，方向一致 = 命中
  3. report()：按类型汇总命中率 —— 低于 50% 的类型就该关掉，别靠感觉

用法：
    python3 alert_ledger.py --settle         # 结算到期的（可放定时任务）
    python3 alert_ledger.py --report         # 看各类提醒命中率
    python3 alert_ledger.py --report --days 30

设计原则：只记账，不改推送行为；结算失败的记为 unknown，绝不拿未来数据凑。
"""
from __future__ import annotations

import argparse
import os
import re
import sqlite3
import sys
import time
from typing import Dict, List, Optional, Tuple

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import db

LEDGER_DB = os.getenv("ALERT_LEDGER_DB") or os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "alert_ledger.db"
)

# 可结算品种（与 kline_bars.symbol 对齐）
SYMBOLS = ["BTC", "ETH", "SOL", "XAU", "XAG", "WTI"]

MAIN_HORIZON_MIN = int(os.getenv("ALERT_LEDGER_HORIZON_MIN", "240"))   # 4h 主口径

DDL = """
CREATE TABLE IF NOT EXISTS alert_ledger (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  kind TEXT,
  title TEXT,
  asset TEXT,
  direction TEXT,          -- up / down / none
  push_ts INTEGER,
  ref_price REAL,          -- 推送时刻该品种最新收盘价
  horizon_min INTEGER,
  exit_ts INTEGER,
  exit_price REAL,
  move_pct REAL,
  hit INTEGER,             -- 1 命中 / 0 未命中 / NULL 未结算或无法结算
  settle_note TEXT,
  settled_at INTEGER
);
CREATE INDEX IF NOT EXISTS idx_alert_ledger_push ON alert_ledger(push_ts DESC);
CREATE INDEX IF NOT EXISTS idx_alert_ledger_unsettled ON alert_ledger(hit, push_ts);
"""


def _ledger_conn() -> sqlite3.Connection:
    c = sqlite3.connect(LEDGER_DB, timeout=10)
    c.executescript(DDL)
    return c


def _prod_ro() -> sqlite3.Connection:
    return sqlite3.connect(f"file:{db.get_db_path()}?mode=ro", uri=True)


def detect_asset(title: str) -> Optional[str]:
    """从标题里认品种。认不出返回 None（不猜）。"""
    t = str(title or "").upper()
    for sym in SYMBOLS:
        # 边界只按"字母"判：标题是中文时没有空格（"BTC反弹"），
        # 但必须挡住"Solo""SOLAR"这类被前缀命中的假阳性。
        if re.search(rf"(?<![A-Z]){sym}(?![A-Z])", t):
            return sym
    return None


def detect_direction(title: str) -> str:
    """从标题里认方向。认不出返回 none。"""
    t = str(title or "")
    up_kw = ("反弹", "买入", "BUY", "转鸽", "降息", "↑", "利多", "做多")
    down_kw = ("回落", "卖出", "SELL", "转鹰", "加息", "↓", "利空", "做空")
    if any(k in t for k in up_kw) and not any(k in t for k in down_kw):
        return "up"
    if any(k in t for k in down_kw) and not any(k in t for k in up_kw):
        return "down"
    return "none"


def _price_at(ts_sec: int, symbol: str, tolerance_sec: int = 900) -> Optional[float]:
    """取 ts 附近（不超过 tolerance）的最新收盘价。kline_bars.ts 是毫秒。

    优先 1m 周期；1m 断源（WTI 常见）则退到表内该品种最细的可用周期。
    """
    try:
        conn = _prod_ro()
    except Exception:
        return None
    try:
        for tf in ("1m", None):
            sql = "SELECT MAX(ts) FROM kline_bars WHERE symbol=? AND ts<=?"
            args = [symbol, ts_sec * 1000]
            if tf:
                sql += " AND timeframe=?"
                args.append(tf)
            row = conn.execute(sql, args).fetchone()
            last = row[0] if row else None
            if not last or (ts_sec * 1000 - last) > tolerance_sec * 1000:
                continue
            r2 = conn.execute(
                "SELECT close FROM kline_bars WHERE symbol=? AND ts=?", (symbol, last)
            ).fetchone()
            if r2 and r2[0] is not None:
                return float(r2[0])
        return None
    except Exception:
        return None
    finally:
        conn.close()


def record(kind: str, title: str, now_ts: Optional[int] = None) -> Optional[int]:
    """推送成功时调用。认不出资产/方向也记（方向 none 便于统计"无方向提醒"占比）。"""
    try:
        now = int(now_ts or time.time())
        asset = detect_asset(title)
        direction = detect_direction(title)
        # 美联储提醒是多资产方向，标题里同时有 ↑ 和 ↓，按通用规则认不出方向。
        # 单独给一条口径：用风险资产代理结算（默认 BTC，可用 env 换），
        # 转鹰 → 风险资产应跌；转鸽 → 应涨。这样才能统计"Fed 提醒到底准不准"。
        if kind == "fed":
            if asset is None:
                asset = os.getenv("ALERT_LEDGER_FED_PROXY", "BTC")
            if "转鹰" in title:
                direction = "down"
            elif "转鸽" in title:
                direction = "up"
        ref = _price_at(now, asset) if asset else None
        conn = _ledger_conn()
        try:
            cur = conn.execute(
                """INSERT INTO alert_ledger(kind,title,asset,direction,push_ts,ref_price,
                                            horizon_min,settle_note)
                   VALUES(?,?,?,?,?,?,?,?)""",
                (kind, str(title)[:200], asset, direction, now, ref, MAIN_HORIZON_MIN,
                 None if (asset and ref) else "no_price_or_asset"),
            )
            conn.commit()
            return cur.lastrowid
        finally:
            conn.close()
    except Exception:
        return None


def settle(verbose: bool = False) -> Dict[str, int]:
    """结算所有到期（push_ts + horizon 已过）且尚未结算的记录。"""
    conn = _ledger_conn()
    now = int(time.time())
    stats = {"checked": 0, "hit": 0, "miss": 0, "unknown": 0}
    try:
        rows = conn.execute(
            """SELECT id, asset, direction, push_ts, ref_price, horizon_min
               FROM alert_ledger
               WHERE hit IS NULL AND push_ts + horizon_min*60 <= ?""", (now,)
        ).fetchall()
        for rid, asset, direction, push_ts, ref, horizon in rows:
            stats["checked"] += 1
            if not asset or not ref or direction == "none":
                conn.execute(
                    "UPDATE alert_ledger SET settle_note=?, settled_at=? WHERE id=?",
                    ("unsettleable", now, rid))
                stats["unknown"] += 1
                continue
            exit_price = _price_at(int(push_ts) + int(horizon) * 60, asset)
            if exit_price is None:
                conn.execute(
                    "UPDATE alert_ledger SET settle_note=?, settled_at=? WHERE id=?",
                    ("no_exit_price", now, rid))
                stats["unknown"] += 1
                continue
            move = (exit_price - float(ref)) / float(ref) * 100
            hit = 1 if ((direction == "up" and move > 0) or
                        (direction == "down" and move < 0)) else 0
            conn.execute(
                """UPDATE alert_ledger SET exit_price=?, move_pct=?, hit=?, settled_at=?
                   WHERE id=?""",
                (exit_price, round(move, 4), hit, now, rid))
            stats["hit" if hit else "miss"] += 1
            if verbose:
                print(f"  #{rid} {asset} {direction} {move:+.2f}% → {'命中' if hit else '打脸'}")
        conn.commit()
    finally:
        conn.close()
    return stats


def report(days: int = 30) -> None:
    conn = _ledger_conn()
    since = int(time.time()) - days * 86400
    try:
        total = conn.execute("SELECT COUNT(*) FROM alert_ledger WHERE push_ts>=?", (since,)).fetchone()[0]
        print(f"近 {days} 天推送记录 {total} 条")
        if not total:
            print("（还没有可结算的样本 —— 账本从今天开始记，4 小时后才有第一批结果）")
            return
        print(f"\n{'类型':<12}{'已推送':>6}{'可结算':>6}{'命中':>6}{'命中率':>8}")
        print("-" * 44)
        for kind, n, settled, hits in conn.execute(
            """SELECT kind, COUNT(*),
                      SUM(CASE WHEN hit IS NOT NULL THEN 1 ELSE 0 END),
                      SUM(CASE WHEN hit=1 THEN 1 ELSE 0 END)
               FROM alert_ledger WHERE push_ts>=? GROUP BY kind ORDER BY 2 DESC""", (since,)
        ):
            settled = settled or 0
            hits = hits or 0
            r = f"{hits/settled*100:.0f}%" if settled else "—"
            print(f"{str(kind):<12}{n:>6}{settled:>6}{hits:>6}{r:>8}")
        print()
        u = conn.execute(
            "SELECT COUNT(*) FROM alert_ledger WHERE push_ts>=? AND hit IS NULL", (since,)
        ).fetchone()[0]
        note = conn.execute(
            "SELECT COUNT(*) FROM alert_ledger WHERE push_ts>=? AND settle_note IS NOT NULL", (since,)
        ).fetchone()[0]
        print(f"待结算 {u} 条，其中标记无法结算 {note} 条")
    finally:
        conn.close()


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--settle", action="store_true")
    ap.add_argument("--report", action="store_true")
    ap.add_argument("--days", type=int, default=30)
    ap.add_argument("-v", "--verbose", action="store_true")
    a = ap.parse_args()
    if a.settle:
        s = settle(verbose=a.verbose)
        print(f"结算完成：检查 {s['checked']}，命中 {s['hit']}，打脸 {s['miss']}，无法结算 {s['unknown']}")
    if a.report or not a.settle:
        report(days=a.days)


if __name__ == "__main__":
    main()
