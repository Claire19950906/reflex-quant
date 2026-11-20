#!/usr/bin/env python3
"""新浪财经 7x24 快讯采集器。

为什么需要：
  · 现在新闻主力是金十一家（近 7 天 1,686 条，占 46%）。单一来源意味着
    一条编辑口径或一次接口故障，整个情报面就塌一半。
  · 新浪 7x24 是国内另一条主流通道路径，覆盖宏观、A 股、公司公告，
    与金十的"全球宏观 + 加密"形成互补，且延迟通常在 1 分钟内。
  · 它带 ext.stocks 字段，能直接给出关联标的，比从正文里猜资产更准。

写入 raw_news，source='sina_flash'，status='PENDING'（交给门卫判定）。
去重：按 content_fingerprint（正文 md5）+ source。
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

URL = "https://zhibo.sina.com.cn/api/zhibo/feed"
HEADERS = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                         "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0 Safari/537.36",
           "Referer": "https://finance.sina.com.cn/7x24/"}
TIMEOUT = 12
SOURCE = "sina_flash"
CST = timezone(timedelta(hours=8))


def _fetch(size: int = 50) -> List[Dict[str, Any]]:
    params = {"page": 1, "page_size": size, "zhibo_id": 152, "tag_id": 0}
    for attempt in range(3):
        try:
            r = requests.get(URL, params=params, headers=HEADERS, timeout=TIMEOUT)
            r.raise_for_status()
            data = r.json()
            return (((data.get("result") or {}).get("data") or {}).get("feed") or {}).get("list") or []
        except Exception as exc:
            print(f"[WARN] 新浪快讯拉取失败({attempt+1}/3): {type(exc).__name__}: {exc}", flush=True)
            time.sleep(2 * (attempt + 1))
    return []


def _parse_ts(text: str) -> int:
    try:
        return int(datetime.strptime(text, "%Y-%m-%d %H:%M:%S").replace(tzinfo=CST).timestamp())
    except Exception:
        return int(time.time())


def _related_assets(ext_raw: str) -> List[str]:
    """ext 里有 stocks 列表，尽量取出代码，便于门卫/归因时关联。"""
    import json
    try:
        ext = json.loads(ext_raw or "{}")
    except Exception:
        return []
    out = []
    for s in (ext.get("stocks") or [])[:5]:
        code = str(s.get("symbol") or "").strip()
        if code:
            out.append(code)
    return out


def main() -> int:
    items = _fetch()
    if not items:
        return 1

    # v341: 自动提交 + 30s timeout —— 原来 SELECT 开读事务后又 INSERT，
    # 撞上别的进程提交就 SQLITE_BUSY（busy_timeout 不解决快照升级冲突）。
    import db_retry
    conn = db_retry.connect_autocommit(db.get_db_path())
    cur = conn.cursor()
    written = skipped = skipped_noise = 0

    for it in items:
        body = (it.get("rich_text") or "").strip()
        if not body:
            continue
        body = body[:3000]
        ts = _parse_ts(str(it.get("create_time") or ""))
        fp = hashlib.md5(body.encode("utf-8")).hexdigest()
        cur.execute("SELECT 1 FROM raw_news WHERE content_fingerprint=? AND source=? LIMIT 1",
                    (fp, SOURCE))
        if cur.fetchone():
            skipped += 1
            continue
        pub_at = datetime.fromtimestamp(ts, tz=CST).strftime("%Y-%m-%dT%H:%M:%S+08:00")
        assets = _related_assets(it.get("ext") or "")
        # v411: 过闸（原实现是裸 INSERT，绕过了整条过滤器 → 实测近 3h 非噪音新闻里
        # 57% 没有门卫判定就进了主分析队列，近 1h 的 8 条决策里 6 条来自这批）。
        # FLASH_FEEDS_GATE=0 可一键退回旧行为；过滤链路异常时自动退回（不丢新闻）。
        gate_cols = None
        if _flash_gate.enabled():
            gate_cols = _flash_gate.gate_columns(body, published_at=pub_at, ts=ts)
            if gate_cols is None:
                skipped_noise += 1
                continue
        else:
            gate_cols = _flash_gate.gate_kind_only()
        ok = db_retry.insert_with_retry(
            cur,
            """INSERT INTO raw_news
               (source, content, timestamp, ts, published_at, status,
                gate_kind, content_fingerprint, cross_net_first_seen_ts,
                cross_net_source, gate_assets,
                is_noise, relevance_score, gate_verdict, gate_unavailable,
                gate_strength, gate_main_asset, filter_reason)
               VALUES (?,?,?,?,?, 'PENDING', 'real_news', ?, 'SINGLE_NET', ?, ?,
                       ?,?,?,?,?,?,?)""",
            (SOURCE, body, ts, ts, pub_at,
             fp, SOURCE, ",".join(assets) if assets else None,
             gate_cols["is_noise"], gate_cols["relevance_score"],
             gate_cols["gate_verdict"], gate_cols["gate_unavailable"],
             gate_cols["gate_strength"], gate_cols["gate_main_asset"],
             gate_cols["filter_reason"]),
        )
        if ok:
            written += 1

    conn.commit()
    conn.close()
    print(f"[{time.strftime('%H:%M:%S')}] 新浪快讯 新增 {written} 条 / 跳过重复 {skipped} 条 / 门卫判噪音 {skipped_noise} 条", flush=True)
    return 0 if written >= 0 else 1


if __name__ == "__main__":
    sys.exit(main())
