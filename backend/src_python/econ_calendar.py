"""宏观日历采集 —— 把"预期 vs 实际"这类确定性信息按周存库。

为什么要单独采:
  v335 用本库新闻流解析"实际/预期", 40 天只有 26 个可交易事件(12 笔可结算),
  样本太小, 无法判断"超预期→反着做"到底有没有 70~80% 命中率。
  ForexFactory 公开周历 (nfs.faireconomy.media) 直接给结构化字段:
    国家 / 指标名 / 星级 / 公布时间 / 实际 / 预期 / 前值
  每周拉一次即可累积, 三个月约 200+ 美国事件, 一年约 800+。

用法:
  python3 econ_calendar.py --fetch --commit     # 拉本周日历入库
  python3 econ_calendar.py --report            # 看库里有多少、缺口在哪
"""
from __future__ import annotations

import argparse
import datetime
import json
import sqlite3
import sys
import urllib.request
from pathlib import Path

DB_PATH = Path(__file__).resolve().parents[1] / "trident_event_bus.db"
FEED = "https://nfs.faireconomy.media/ff_calendar_thisweek.json"
# 只关心美国(USD)与高影响事件; 影响其他货币的宏观对 BTC/黄金的传导太弱
KEEP_COUNTRIES = {"USD"}


def fetch(retries: int = 4) -> list[dict]:
    """该源偶发 SSL 中途断开, 退避重试。"""
    import time
    last: Exception | None = None
    for i in range(retries):
        try:
            req = urllib.request.Request(FEED, headers={"User-Agent": "Mozilla/5.0"})
            with urllib.request.urlopen(req, timeout=25) as r:
                return json.loads(r.read().decode())
        except Exception as e:
            last = e
            time.sleep(1.5 * (i + 1))
    raise last if last else RuntimeError("calendar fetch failed")


def norm_ts(date_str: str) -> int | None:
    """日历里的时间形如 '2026-11-13T21:30:00-05:00' → 本地秒级时间戳。"""
    try:
        dt = datetime.datetime.fromisoformat(date_str)
    except ValueError:
        return None
    return int(dt.timestamp())


def ensure_table(conn: sqlite3.Connection) -> None:
    conn.execute("""CREATE TABLE IF NOT EXISTS econ_events (
        country TEXT, title TEXT, ts INTEGER,
        impact TEXT, actual TEXT, forecast TEXT, previous TEXT,
        fetched_at INTEGER,
        PRIMARY KEY (country, title, ts))""")
    conn.commit()


def store(conn: sqlite3.Connection, items: list[dict]) -> int:
    now = int(datetime.datetime.now().timestamp())
    n = 0
    for it in items:
        if it.get("country") not in KEEP_COUNTRIES:
            continue
        ts = norm_ts(it.get("date") or "")
        if not ts:
            continue
        cur = conn.execute(
            "INSERT OR IGNORE INTO econ_events VALUES(?,?,?,?,?,?,?,?)",
            (it["country"], it.get("title", ""), ts, it.get("impact", ""),
             it.get("actual", ""), it.get("forecast", ""), it.get("previous", ""), now))
        n += cur.rowcount
    conn.commit()
    return n


def report(conn: sqlite3.Connection) -> None:
    try:
        n = conn.execute("SELECT count(*) FROM econ_events").fetchone()[0]
    except sqlite3.OperationalError:
        print("还没有 econ_events 表, 先跑 --fetch --commit")
        return
    if not n:
        print("日历库 0 条")
        return
    have = conn.execute("SELECT count(*) FROM econ_events WHERE actual IS NOT NULL "
                        "AND actual!=''").fetchone()[0]
    rows = conn.execute("SELECT ts, title, impact, actual, forecast FROM econ_events "
                        "WHERE actual!='' AND actual IS NOT NULL ORDER BY ts DESC LIMIT 6").fetchall()
    print(f"日历事件 {n} 条, 其中已出结果 {have} 条")
    for ts, title, impact, actual, forecast in rows:
        print(f"  {datetime.datetime.fromtimestamp(ts):%m-%d %H:%M} [{impact}] "
              f"{title[:34]:34s} 实际 {actual:>7s} 预期 {forecast:>7s}")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--fetch", action="store_true")
    ap.add_argument("--commit", action="store_true")
    ap.add_argument("--report", action="store_true")
    a = ap.parse_args()
    if a.fetch:
        try:
            items = fetch()
        except Exception as e:
            print(f"拉取失败: {e}")
            return 1
        us = [x for x in items if x.get("country") in KEEP_COUNTRIES]
        print(f"拉到 {len(items)} 条, 其中美国 {len(us)} 条")
        if a.commit:
            conn = sqlite3.connect(DB_PATH)
            try:
                ensure_table(conn)
                print(f"新增 {store(conn, items)} 条")
            finally:
                conn.close()
    if a.report:
        conn = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True)
        try:
            report(conn)
        finally:
            conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
