#!/usr/bin/env python3
"""华尔街见闻 7x24 快讯采集器。

为什么需要：
  · 近 30 天 10,833 条新闻里，金十系（金十 + 飞书金十 + 金十日历）占 49.5%。
    一家占一半，编辑口径一变，整个情报面就跟着变。
  · 华尔街见闻是另一条独立编辑线，对央行、国债、汇率、大宗的口径更细，
    与金十形成交叉验证；同一条消息两家都发，可信度自然更高。
  · 它带 score（1-3 重要度）与 channels 字段，可以直接当门卫的先验分。

写入 raw_news，source='wscn'，status='PENDING'（交给门卫判定）。
去重：content_fingerprint（正文 md5）+ source。
"""
import hashlib
import os
import sqlite3
import sys
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List

import requests

import flash_gate as _flash_gate

# 本机存在 DOUBAO_* 转发代理变量，requests 默认 trust_env=True 会误命中代理并断连；
# 实测关闭后同一请求稳定返回 200。
requests.Session.trust_env = False

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import db

URL = "https://api-one-wscn.awtmt.com/apiv1/content/lives"
HEADERS = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                         "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0 Safari/537.36",
           "Referer": "https://wallstreetcn.com/live/global"}
TIMEOUT = 12
SOURCE = "wscn"
CST = timezone(timedelta(hours=8))
CHANNELS = ("global-channel", "a-stock-channel")


def _fetch(channel: str, limit: int = 30) -> List[Dict[str, Any]]:
    params = {"channel": channel, "limit": limit, "accept": "live"}
    for attempt in range(3):
        try:
            r = requests.get(URL, params=params, headers=HEADERS, timeout=TIMEOUT)
            r.raise_for_status()
            data = r.json() or {}
            return ((data.get("data") or {}).get("items")) or []
        except Exception as exc:
            print(f"[WARN] WSCN {channel} 拉取失败({attempt+1}/3): "
                  f"{type(exc).__name__}: {exc}", flush=True)
            time.sleep(2 * (attempt + 1))
    return []


def _text_of(item: Dict[str, Any]) -> str:
    for key in ("content_text", "content"):
        v = item.get(key)
        if isinstance(v, str) and v.strip():
            return v.strip()
    return ""


def main() -> int:
    # v341: 自动提交 + 30s timeout —— 原来 SELECT 开读事务后又 INSERT，
    # 撞上别的进程提交就 SQLITE_BUSY（busy_timeout 不解决快照升级冲突）。
    import db_retry
    conn = db_retry.connect_autocommit(db.get_db_path())
    cur = conn.cursor()
    written = skipped = skipped_noise = 0

    for channel in CHANNELS:
        for item in _fetch(channel):
            body = _text_of(item)
            if not body:
                continue
            body = body[:3000]
            try:
                ts = int(item.get("display_time") or 0) or int(time.time())
            except (TypeError, ValueError):
                ts = int(time.time())
            fp = hashlib.md5(body.encode("utf-8")).hexdigest()
            cur.execute("SELECT 1 FROM raw_news WHERE content_fingerprint=? AND source=? LIMIT 1",
                        (fp, SOURCE))
            if cur.fetchone():
                skipped += 1
                continue
            pub_at = datetime.fromtimestamp(ts, tz=CST).strftime("%Y-%m-%dT%H:%M:%S+08:00")
            # v411: 过闸（原实现是裸 INSERT，绕过整条过滤器 → 实测近 3h 非噪音新闻里
            # 57% 没有门卫判定就进了主分析队列）。FLASH_FEEDS_GATE=0 退回旧行为。
            if _flash_gate.enabled():
                gate_cols = _flash_gate.gate_columns(body, published_at=pub_at, ts=ts)
                if gate_cols is None:
                    skipped_noise += 1
                    continue
            else:
                gate_cols = _flash_gate.gate_kind_only()
            # 撞上写锁退避重试，避免整条新闻被丢掉
            ok = db_retry.insert_with_retry(
                cur,
                """INSERT INTO raw_news
                   (source, content, timestamp, ts, published_at, status,
                    gate_kind, content_fingerprint, cross_net_first_seen_ts,
                    cross_net_source,
                    is_noise, relevance_score, gate_verdict, gate_unavailable,
                    gate_strength, gate_main_asset, filter_reason)
                   VALUES (?,?,?,?,?, 'PENDING', 'real_news', ?, 'SINGLE_NET', ?,
                           ?,?,?,?,?,?,?)""",
                (SOURCE, body, ts, ts, pub_at,
                 fp, SOURCE,
                 gate_cols["is_noise"], gate_cols["relevance_score"],
                 gate_cols["gate_verdict"], gate_cols["gate_unavailable"],
                 gate_cols["gate_strength"], gate_cols["gate_main_asset"],
                 gate_cols["filter_reason"]),
            )
            if ok:
                written += 1

    conn.commit()
    conn.close()
    print(f"[{time.strftime('%H:%M:%S')}] 华尔街见闻 新增 {written} 条 / 跳过重复 {skipped} 条"
          f" / 门卫判噪音 {skipped_noise} 条",
          flush=True)
    return 0 if written >= 0 else 1


if __name__ == "__main__":
    sys.exit(main())
