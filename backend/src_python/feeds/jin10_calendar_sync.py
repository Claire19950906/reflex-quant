#!/usr/bin/env python3
"""v323: 金十财经日历同步器 —— 补 ForexFactory 彻底失效的空缺。

背景（2026-11-13 实测）：
    macro_calendar_ff.py 依赖的三个 ForexFactory XML 全部不可用：
        ff_calendar_thisweek.xml        → 000（连接不通）
        ff_calendar_thisweekextended.xml → 404（上游已删）
        ff_calendar_nextweek.xml         → 404（上游已删）
    结果：macro_calendar 747 行全是 source='ff'，最新事件只到 11-15，
    再往后就是空白 —— 宏观日历断供，等于"不知道市场在等什么"，
    非农/CPI/FOMC 这类心理预期最核心的输入直接没了。

可用替代：金十 MCP（mcp.jin10.com）实测可达、有 token、list_calendar 返回
    真实日历（213 条覆盖 11-09→11-14，含 actual/consensus/previous/star），
    只是 jin10_mcp.call_tool 的 data 形状是 {"data": [...]}，调用方要按这个取。

写入口径：与 FF 同步器同一张表 macro_calendar、同一套列，source 标 'jin10'，
    不动历史 ff 行、不覆盖、不造数。
"""
from __future__ import annotations

import hashlib
import sys
import time
from datetime import datetime

COUNTRY_KEYS = (
    ("日本", "JP"), ("中国", "CN"), ("美国", "US"), ("欧元区", "EU"),
    ("德国", "DE"), ("英国", "GB"), ("法国", "FR"), ("加拿大", "CA"),
    ("澳大利亚", "AU"), ("新西兰", "NZ"), ("瑞士", "CH"), ("韩国", "KR"),
)


def _country_of(title: str) -> str:
    for kw, code in COUNTRY_KEYS:
        if kw in title:
            return code
    return ""


def _epoch(pub_time: str) -> int:
    """金十的 pub_time 是北京时间 'YYYY-MM-DD HH:MM' → epoch 秒。"""
    try:
        dt = datetime.strptime(pub_time.strip()[:16], "%Y-%m-%d %H:%M")
        return int(time.mktime(dt.timetuple()))
    except Exception:
        return 0


def _impact_of(star: int) -> str:
    if star >= 4:
        return "high"
    if star == 3:
        return "med"
    return "low"


def fetch_items() -> list[dict]:
    from feeds import jin10_mcp as j

    r = j.call_tool("list_calendar", {})
    if r.get("error"):
        print(f"[J10] list_calendar 失败: {str(r['error'])[:160]}", flush=True)
        return []
    data = r.get("data")
    if isinstance(data, dict):
        data = data.get("data") or data.get("items") or []
    return data if isinstance(data, list) else []


def sync() -> int:
    import db as _db

    items = fetch_items()
    if not items:
        return 0

    rows = []
    for it in items:
        title = str(it.get("title") or "").strip()
        pub = str(it.get("pub_time") or "").strip()
        if not title or not pub:
            continue
        try:
            star = int(it.get("star") or 0)
        except (TypeError, ValueError):
            star = 0
        rows.append({
            "event_id": "j10_" + hashlib.sha1(f"{title}|{pub}".encode()).hexdigest()[:16],
            "country": _country_of(title),
            "title": title[:200],
            "impact": _impact_of(star),
            "star": max(0, min(5, star)),
            "event_time": _epoch(pub),
            "forecast": str(it.get("consensus") or ""),
            "previous": str(it.get("previous") or ""),
            "actual": str(it.get("actual") or ""),
        })

    conn = _db.get_connection()
    written = 0
    try:
        for r in rows:
            if r["event_time"] <= 0:
                continue
            conn.execute(
                """
                INSERT INTO macro_calendar
                    (source, event_id, country, title, impact, star,
                     event_time, forecast, previous, actual)
                VALUES ('jin10', ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(source, event_id) DO UPDATE SET
                    country = excluded.country,
                    title = excluded.title,
                    impact = excluded.impact,
                    star = excluded.star,
                    event_time = excluded.event_time,
                    forecast = excluded.forecast,
                    previous = excluded.previous,
                    actual = excluded.actual,
                    fetched_at = datetime('now')
                """,
                (r["event_id"], r["country"], r["title"], r["impact"], r["star"],
                 r["event_time"], r["forecast"], r["previous"], r["actual"]),
            )
            written += 1
        conn.commit()
    finally:
        conn.close()
    return written


if __name__ == "__main__":
    try:
        n = sync()
        print(f"[J10] 写入 {n} 条宏观日历 @ {time.strftime('%H:%M:%S')}", flush=True)
    except Exception as exc:
        print(f"[J10] 同步失败: {type(exc).__name__}: {exc}", flush=True)
        sys.exit(1)
