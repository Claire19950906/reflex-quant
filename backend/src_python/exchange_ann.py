#!/usr/bin/env python3
"""交易所一手公告（Bybit / OKX）。

为什么需要：
  · 系统里关于"某币要上/要下架、合约参数要改"的信息，全靠新闻站转述，
    但这类事件的一手公告就在交易所自己的接口上，比新闻早、且不会转述错。
  · 上币/下架/杠杆与保证金调整，是少数能直接影响价格与流动性的确定性事件。

只保留"结构性变动"类公告（上币、下架、合约参数、杠杆、维护），
营销活动（空投、返佣、比赛、理财）一律丢弃 —— 那些对交易没有信号价值。
写入 raw_news，source='exchange_ann'，走 evaluate_news 正规入口。
"""
import hashlib
import json
import os
import sqlite3
import sys
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List

import requests

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import db
import realtime_filter

requests.Session.trust_env = False
HEADERS = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                         "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0 Safari/537.36"}
TIMEOUT = 15
SOURCE = "exchange_ann"
CST = timezone(timedelta(hours=8))
LOOKBACK_HOURS = 48

# 结构性变动关键词（命中才算有用）
KEEP = ("list", "listing", "delist", "perpetual", "contract", "leverage",
        "margin", "maintenance", "upgrade", "tick size", "suspend", "halt",
        "上线", "下架", "合约", "杠杆", "保证金", "维护")
# 营销噪音关键词（命中直接丢）
DROP = ("campaign", "reward", "airdrop", "giveaway", "competition", "bonus",
        "cashback", "earn", "tournament", "referral", "voucher", "lottery",
        "空投", "返佣", "活动", "抽奖", "理财", "红包")


def _keep(title: str) -> bool:
    t = title.lower()
    if any(w in t for w in DROP):
        return False
    return any(w in t for w in KEEP)


def _bybit() -> List[Dict[str, Any]]:
    url = "https://api.bybit.com/v5/announcements/index?locale=en-US&limit=50"
    items = []
    for attempt in range(3):
        try:
            r = requests.get(url, headers=HEADERS, timeout=TIMEOUT)
            r.raise_for_status()
            items = (r.json().get("result") or {}).get("list") or []
            break
        except Exception as exc:
            if attempt == 2:
                print(f"[WARN] Bybit 公告失败: {type(exc).__name__}: {exc}", flush=True)
                return []
            time.sleep(2 * (attempt + 1))
    out = []
    for it in items:
        title = str(it.get("title") or "")
        if not _keep(title):
            continue
        ts = int(it.get("publishTime") or 0) // 1000
        out.append({"title": title, "ts": ts, "url": it.get("url") or ""})
    return out


def _okx() -> List[Dict[str, Any]]:
    url = "https://www.okx.com/api/v5/support/announcements"
    try:
        r = requests.get(url, headers=HEADERS, timeout=TIMEOUT)
        r.raise_for_status()
        data = r.json().get("data") or []
    except Exception as exc:
        print(f"[WARN] OKX 公告失败: {type(exc).__name__}: {exc}", flush=True)
        return []
    out = []
    for group in data:
        for it in (group.get("details") or []):
            title = str(it.get("title") or "")
            if not _keep(title):
                continue
            p = str(it.get("pTime") or it.get("businessPTime") or "0")
            ts = int(p) // 1000 if len(p) >= 13 else int(p or 0)
            out.append({"title": title, "ts": ts, "url": it.get("url") or ""})
    return out


def main() -> int:
    conn = sqlite3.connect(db.get_db_path(), timeout=30, isolation_level=None)
    conn.execute("PRAGMA busy_timeout=30000")
    cur = conn.cursor()
    written = skipped = 0
    cutoff = time.time() - LOOKBACK_HOURS * 3600

    items = _bybit() + _okx()
    for it in items:
        if it["ts"] < cutoff:
            continue
        body = it["title"][:3000]
        fp = hashlib.md5((it["url"] or body).encode()).hexdigest()
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
                (SOURCE, body, it["ts"], it["ts"],
                 datetime.fromtimestamp(it["ts"], tz=CST).strftime("%Y-%m-%dT%H:%M:%S+08:00"),
                 ev.get("status", "PENDING"), int(ev.get("is_noise") or 0),
                 float(ev.get("relevance_score") or 0),
                 ev.get("kind") or "other", ev.get("gate_verdict") or "",
                 int(ev.get("gate_unavailable") or 0), fp, SOURCE, it["url"] or None),
            )
            written += 1
        except sqlite3.Error as exc:
            print(f"[WARN] 写入失败: {exc}", flush=True)

    conn.close()
    print(f"[{time.strftime('%H:%M:%S')}] 交易所公告 新增 {written} 条 / 跳过重复 {skipped} 条"
          f"（原始 {len(items)} 条，已滤掉营销）", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
