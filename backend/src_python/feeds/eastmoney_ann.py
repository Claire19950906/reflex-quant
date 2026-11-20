#!/usr/bin/env python3
"""东方财富 A 股一手公告（按大宗商品关键词过滤）。

为什么需要：
  · 系统盯的是黄金、原油、铜这些大宗品类，但缺少"国内产业端"的一手信息：
    上市公司公告是强制性披露，比媒体报道早且不会转述失真。
  · 全市场每天几百上千条公告，绝大多数与交易无关。这里只保留标题命中
    贵金属 / 能源 / 工业金属 / 新能源上游 关键词的条目，做窄口径信号源。

写入 raw_news，source='eastmoney_ann'，走 evaluate_news 正规入口。
"""
from ._paths import SRC_DIR, BACKEND_DIR  # P2: 路径锚点
import hashlib
import json
import os
import sqlite3
import sys
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List

import requests

sys.path.insert(0, str(SRC_DIR))
import db
import realtime_filter

requests.Session.trust_env = False
URL_TMPL = ("https://np-anotice-stock.eastmoney.com/api/security/ann"
            "?sr=-1&page_size=100&page_index={page}&ann_type=A&client_source=web&f_node=0")
PAGES = 6  # 一页 100 条，扫 6 页覆盖当日大部分公告
HEADERS = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                         "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0 Safari/537.36",
           "Referer": "https://data.eastmoney.com/notices/"}
TIMEOUT = 15
SOURCE = "eastmoney_ann"
CST = timezone(timedelta(hours=8))
LOOKBACK_HOURS = 24

# 窄口径：只保留与大宗商品/能源相关的产业公告
KEEP = ("黄金", "白银", "铜", "铝", "锌", "镍", "锂", "稀土", "钨", "钼",
        "原油", "石油", "天然气", "油气", "煤炭", "焦炭", "铀",
        "矿产", "矿山", "冶炼", "选矿", "资源储量", "探矿权", "采矿权")


def _fetch() -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    for page in range(1, PAGES + 1):
        for attempt in range(2):
            try:
                r = requests.get(URL_TMPL.format(page=page), headers=HEADERS, timeout=TIMEOUT)
                r.raise_for_status()
                out.extend(((r.json().get("data") or {}).get("list")) or [])
                break
            except Exception as exc:
                if attempt == 1:
                    print(f"[WARN] 东财公告第 {page} 页失败: {type(exc).__name__}", flush=True)
                else:
                    time.sleep(1.5)
        time.sleep(0.4)
    return out


def main() -> int:
    items = _fetch()
    conn = sqlite3.connect(db.get_db_path(), timeout=30, isolation_level=None)
    conn.execute("PRAGMA busy_timeout=30000")
    cur = conn.cursor()
    written = skipped = kept = 0
    cutoff = time.time() - LOOKBACK_HOURS * 3600

    for it in items:
        title = str(it.get("title") or "").strip()
        if not title or not any(k in title for k in KEEP):
            continue
        kept += 1
        code = ""
        for c in (it.get("codes") or []):
            if c.get("stock_code"):
                code = f"{c.get('stock_code')} {c.get('short_name') or ''}".strip()
                break
        try:
            ts = int(datetime.strptime(str(it.get("notice_date"))[:10], "%Y-%m-%d")
                     .replace(tzinfo=CST).timestamp())
        except (ValueError, TypeError):
            continue
        if ts < cutoff:
            continue
        body = f"{code} 公告：{title}" if code else title
        fp = hashlib.md5(str(it.get("art_code") or body).encode()).hexdigest()
        cur.execute("SELECT 1 FROM raw_news WHERE content_fingerprint=? AND source=? LIMIT 1",
                    (fp, SOURCE))
        if cur.fetchone():
            skipped += 1
            continue
        ev = realtime_filter.evaluate_news(body, body)
        art = it.get("art_code") or ""
        url = f"https://data.eastmoney.com/notices/detail/{code.split(' ')[0]}/{art}.html" if art else None
        try:
            cur.execute(
                """INSERT INTO raw_news
                   (source, content, timestamp, ts, published_at, status,
                    is_noise, relevance_score, gate_kind, gate_verdict,
                    gate_unavailable, content_fingerprint,
                    cross_net_first_seen_ts, cross_net_source, url)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?, 'SINGLE_NET', ?, ?)""",
                (SOURCE, body[:3000], ts, ts,
                 datetime.fromtimestamp(ts, tz=CST).strftime("%Y-%m-%dT%H:%M:%S+08:00"),
                 ev.get("status", "PENDING"), int(ev.get("is_noise") or 0),
                 float(ev.get("relevance_score") or 0),
                 ev.get("kind") or "other", ev.get("gate_verdict") or "",
                 int(ev.get("gate_unavailable") or 0), fp, SOURCE, url),
            )
            written += 1
        except sqlite3.Error as exc:
            print(f"[WARN] 写入失败: {exc}", flush=True)

    conn.close()
    print(f"[{time.strftime('%H:%M:%S')}] 东财产业公告 扫描 {len(items)} 条 / 命中关键词 {kept} 条"
          f" / 新增 {written} 条 / 重复 {skipped} 条", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
