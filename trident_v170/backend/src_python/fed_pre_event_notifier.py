#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""v276: Fed 事件事前通知 daemon — Fed 相关日历事件事前 30min / 24h 提醒。

订阅 macro_calendar 中 USD 高影响事件(FOMC/CPI/NFP/PCE/Powell讲话),
在事件发生前:
  · 30min ± 5min → 推一次"X 分钟前提醒"
  · 24h ± 30min  → 推一次"24h 后提醒"

去重:UNIQUE (event_id, window) → 同窗口不重复推。
推到 Server酱/邮件/PushPlus 三通道(走 engine.alerts.send_important_news)。

调用方式:
  python3 fed_pre_event_notifier.py --once          # 跑一次(测试 / cron 用)
  python3 fed_pre_event_notifier.py --interval 300  # 守护循环(默认 5 分钟)
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import sqlite3
import sys
import time
from typing import Any, Dict, List

_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

import db  # noqa: E402


logger = logging.getLogger("fed_pre_event_notifier")

# 触发窗口(秒前)
WINDOW_30MIN = (30 * 60 - 5 * 60, 30 * 60 + 5 * 60)
WINDOW_24H = (24 * 3600 - 30 * 60, 24 * 3600 + 30 * 60)

# Fed 相关事件关键词
_FED_KEYWORDS = (
    "FOMC", "Fed ", "Fed-", "Fed.", "美联储", "鲍尔", "鲍威", "Powell",
    "CPI", "PCE", "NFP", "非农", "失业率", "Unemployment", "Payroll",
    "议息会议", "利率决议", "通胀", "Inflation", "利率", "Rate Decision",
    "FOMC Minutes", "FOMC Press Conference", "FOMC Statement",
    "Fed Chair", "Fed Official",
)


def _ensure_dedup_table(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS fed_pre_event_dedup (
          key TEXT PRIMARY KEY,
          ts INTEGER
        )
        """
    )


def _record_dedup(conn: sqlite3.Connection, key: str) -> bool:
    """返回 True 表示"未推过,可以推";"已推过"返回 False。"""
    row = conn.execute(
        "SELECT 1 FROM fed_pre_event_dedup WHERE key = ?", (key,)
    ).fetchone()
    if row:
        return False
    conn.execute(
        "INSERT INTO fed_pre_event_dedup(key, ts) VALUES (?, ?)",
        (key, int(time.time())),
    )
    conn.execute(
        "DELETE FROM fed_pre_event_dedup WHERE ts < ?",
        (int(time.time()) - 7 * 86400,),
    )
    conn.commit()
    return True


def _load_fed_events(window_seconds: int) -> List[Dict[str, Any]]:
    """从 macro_calendar 加载 Fed 相关事件(未来 window_seconds 内)。"""
    conn = sqlite3.connect(db.get_db_path())
    conn.row_factory = sqlite3.Row
    try:
        now = int(time.time())
        hi = now + window_seconds
        rows = conn.execute(
            """
            SELECT source, event_id, title, event_time, impact, star,
                   forecast, previous
              FROM macro_calendar
             WHERE event_time BETWEEN ? AND ?
               AND country = 'USD'
               AND impact IN ('med', 'high')
             ORDER BY event_time ASC
            """,
            (now, hi),
        ).fetchall()
        out: List[Dict[str, Any]] = []
        for r in rows:
            title = (r["title"] or "").strip()
            if not any(k in title for k in _FED_KEYWORDS):
                continue
            out.append({
                "source": r["source"],
                "event_id": r["event_id"],
                "title": title,
                "event_time": int(r["event_time"]),
                "minutes_to": int((int(r["event_time"]) - now) / 60),
                "impact": r["impact"],
                "star": int(r["star"] or 0),
                "forecast": r["forecast"] or "",
                "previous": r["previous"] or "",
            })
        return out
    finally:
        try:
            conn.close()
        except Exception:
            pass


def _format_title(window_name: str, ev: Dict[str, Any]) -> str:
    severity_emoji = "🚨" if ev["impact"] == "high" else "⚠️"
    return f"{severity_emoji} Fed 事件 {window_name}:{ev['title']}"


def _format_body(window_name: str, ev: Dict[str, Any]) -> str:
    d = time.strftime("%Y-%m-%d %H:%M", time.localtime(ev["event_time"]))
    lines = [
        f"**{window_name}** 提醒",
        "",
        f"📅 事件:**{ev['title']}**",
        f"⏰ 时间:{d}({ev['minutes_to']} 分钟后)",
        f"⭐ 等级:{ev['star']} 星 · {ev['impact']}",
    ]
    if ev.get("forecast"):
        lines.append(f"📊 预测:{ev['forecast']}")
    if ev.get("previous"):
        lines.append(f"📋 前值:{ev['previous']}")
    lines.extend([
        "",
        "💡 影响传导: Fed 利率预期(CPI/NFP/PCE)异动 → 2Y UST yield 移动 → 风险资产重估",
        "→ 详见 /discover → 📡 Fed 全景监控",
    ])
    return "\n".join(lines)


def _send(title: str, body: str) -> bool:
    try:
        from engine.alerts import send_important_news
        send_important_news(title, body)
        return True
    except ImportError:
        pass
    try:
        from engine import alerts as _alerts  # type: ignore
        _alerts.send_important_news(title, body)
        return True
    except Exception as exc:
        logger.warning("推送失败: %s: %s", type(exc).__name__, exc)
        return False


def check_once() -> Dict[str, Any]:
    """扫描一次,推所有匹配的窗口。返回推送统计。"""
    pushed_30min = 0
    pushed_24h = 0
    skipped = 0

    evs = _load_fed_events(24 * 3600 + 30 * 60)
    if not evs:
        return {"ok": True, "pushed_30min": 0, "pushed_24h": 0, "skipped": 0}

    conn = sqlite3.connect(db.get_db_path())
    try:
        _ensure_dedup_table(conn)
        for ev in evs:
            mt = ev["minutes_to"]
            # 30min 窗口:25-35 min
            if 25 <= mt <= 35:
                key = f"30min::{ev['source']}::{ev['event_id']}"
                if _record_dedup(conn, key):
                    title = _format_title("30min 前", ev)
                    body = _format_body("30min 前", ev)
                    if _send(title, body):
                        pushed_30min += 1
                    print(f"[FED-PRE] 30min PUSHED {ev['title']}", flush=True)
                else:
                    skipped += 1
            # 24h 窗口:23.5-24.5h → 1410-1470 min
            elif 1410 <= mt <= 1470:
                key = f"24h::{ev['source']}::{ev['event_id']}"
                if _record_dedup(conn, key):
                    title = _format_title("24h 前", ev)
                    body = _format_body("24h 前", ev)
                    if _send(title, body):
                        pushed_24h += 1
                    print(f"[FED-PRE] 24h PUSHED {ev['title']}", flush=True)
                else:
                    skipped += 1
    finally:
        try:
            conn.close()
        except Exception:
            pass

    return {
        "ok": True,
        "ts": int(time.time()),
        "events_seen": len(evs),
        "pushed_30min": pushed_30min,
        "pushed_24h": pushed_24h,
        "skipped": skipped,
    }


def daemon_loop(interval_s: int = 300) -> None:
    """守护循环(默认 5 分钟)。"""
    interval_s = max(60, int(interval_s))
    print(f"[FED-PRE] daemon started interval={interval_s}s", flush=True)
    while True:
        try:
            r = check_once()
            if r["pushed_30min"] or r["pushed_24h"]:
                print(
                    f"[FED-PRE] pushed_30min={r['pushed_30min']} "
                    f"pushed_24h={r['pushed_24h']}",
                    flush=True,
                )
        except KeyboardInterrupt:
            raise
        except Exception as exc:
            print(f"[FED-PRE] loop err: {type(exc).__name__}: {exc}", flush=True)
        time.sleep(interval_s)


if __name__ == "__main__":
    logging.basicConfig(
        level=os.getenv("FED_PRE_LOG_LEVEL", "INFO"),
        format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
    )
    parser = argparse.ArgumentParser(description="Fed 事件事前通知")
    parser.add_argument("--once", action="store_true", help="跑一次就退出")
    parser.add_argument("--interval", type=int, default=300,
                        help="守护循环间隔秒数(默认 300)")
    args = parser.parse_args()
    if args.once:
        r = check_once()
        print(json.dumps(r, ensure_ascii=False))
    else:
        daemon_loop(args.interval)

