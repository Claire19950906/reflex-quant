#!/usr/bin/env python3
"""美联储官方新闻稿采集器（press_all.xml）。

为什么需要：
  · FOMC 决议、主席讲话、褐皮书这些是美元的源头变量，现在系统里对它们的认知
    只来自新闻站转述（常常滞后几十分钟到几小时，且夹带解读）。
  · federalreserve.gov 的 press_all.xml 是官方 RSS，一手标题 + 一手发布时间，
    不需要 key。对"新闻→分析"这条链来说，它是延迟最低的宏观输入之一。

写入 raw_news，source='fed_press'，status='PENDING'。
去重：content_fingerprint（guid/链接 md5）＋ source。
"""
import hashlib
import os
import re
import sqlite3
import sys
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

import requests

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import db
import realtime_filter

requests.Session.trust_env = False
URL = "https://www.federalreserve.gov/feeds/press_all.xml"
HEADERS = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                         "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0 Safari/537.36"}
TIMEOUT = 15
SOURCE = "fed_press"
CST = timezone(timedelta(hours=8))
LOOKBACK_HOURS = 72


def _fetch() -> Optional[str]:
    last = None
    for attempt in range(4):
        try:
            r = requests.get(URL, headers=HEADERS, timeout=TIMEOUT)
            r.raise_for_status()
            return r.text
        except Exception as exc:
            last = exc
            time.sleep(2 * (attempt + 1))
    print(f"[WARN] 美联储 RSS 拉取失败: {type(last).__name__}: {last}", flush=True)
    return None


def _items(xml: str) -> List[Dict[str, Any]]:
    # 先把 CDATA 包裹剥掉再解析：`<![CDATA[` 里的 `[` 未转义会被正则当成字符集起始，
    # 之前正是这个坑导致标题全部匹配失败、一条都没入库。
    xml = xml.replace("<![CDATA[", "").replace("]]>", "")
    out = []
    for m in re.finditer(r"<item>(.*?)</item>", xml, re.S):
        block = m.group(1)
        title = re.search(r"<title>(.*?)</title>", block, re.S)
        link = re.search(r"<link>(.*?)</link>", block, re.S)
        date = re.search(r"<pubDate>(.*?)</pubDate>", block, re.S)
        if not title:
            continue
        out.append({
            "title": re.sub(r"<[^>]+>", "", title.group(1)).strip(),
            "link": (link.group(1).strip() if link else ""),
            "pub": (date.group(1).strip() if date else ""),
        })
    return out


def _parse_pub(text: str) -> int:
    for fmt in ("%a, %d %b %Y %H:%M:%S %Z", "%a, %d %b %Y %H:%M:%S %z"):
        try:
            dt = datetime.strptime(text.replace("GMT", "+0000"), fmt)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return int(dt.timestamp())
        except ValueError:
            continue
    return int(time.time())


def main() -> int:
    xml = _fetch()
    if not xml:
        return 1

    conn = sqlite3.connect(db.get_db_path(), timeout=30, isolation_level=None)
    conn.execute("PRAGMA busy_timeout=30000")
    cur = conn.cursor()
    written = skipped = 0
    cutoff = time.time() - LOOKBACK_HOURS * 3600

    for it in _items(xml):
        ts = _parse_pub(it["pub"])
        if ts < cutoff:
            continue
        body = it["title"][:3000]
        fp = hashlib.md5((it["link"] or body).encode()).hexdigest()
        cur.execute("SELECT 1 FROM raw_news WHERE content_fingerprint=? AND source=? LIMIT 1",
                    (fp, SOURCE))
        if cur.fetchone():
            skipped += 1
            continue
        ev = realtime_filter.evaluate_news(body, body)
        try:
            cur.execute(
                """INSERT INTO raw_news
                   (source, content, timestamp, ts, published_at, status,
                    is_noise, relevance_score, gate_kind, gate_verdict,
                    gate_unavailable, content_fingerprint,
                    cross_net_first_seen_ts, cross_net_source, url)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?, 'SINGLE_NET', ?, ?)""",
                (SOURCE, body, ts, ts,
                 datetime.fromtimestamp(ts, tz=CST).strftime("%Y-%m-%dT%H:%M:%S+08:00"),
                 ev.get("status", "PENDING"), int(ev.get("is_noise") or 0),
                 float(ev.get("relevance_score") or 0),
                 ev.get("kind") or "other", ev.get("gate_verdict") or "",
                 int(ev.get("gate_unavailable") or 0), fp, SOURCE, it["link"] or None),
            )
            written += 1
        except sqlite3.Error as exc:
            print(f"[WARN] 写入失败: {exc}", flush=True)

    conn.close()
    print(f"[{time.strftime('%H:%M:%S')}] 美联储新闻稿 新增 {written} 条 / 跳过重复 {skipped} 条",
          flush=True)
    return 0 if written >= 0 else 1


if __name__ == "__main__":
    sys.exit(main())
