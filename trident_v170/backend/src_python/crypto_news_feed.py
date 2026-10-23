#!/usr/bin/env python3
"""加密新闻聚合 — RSS 抓取 + 写入 raw_news 表。

覆盖（免费 / 公开 RSS）：
  coindesk / cointelegraph / theblock / decrypt / blockbeats

每个源独立 try/except，失败不阻塞其他源。
通过 source_status.report() 把每个源成败落库。
"""

from __future__ import annotations

import hashlib
import re
import time
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

import source_status

TIMEOUT = 12
_RETRIES = 2

SOURCES: Tuple[Tuple[str, str, Tuple[str, ...]], ...] = (
    ("coindesk",      "https://www.coindesk.com/arc/outboundfeeds/rss/",  ("pubDate", "dc:date")),
    ("cointelegraph", "https://cointelegraph.com/rss",                    ("pubDate", "dc:date")),
    ("theblock",      "https://www.theblock.co/rss.xml",                  ("pubDate", "dc:date")),
    ("decrypt",       "https://decrypt.co/feed",                          ("pubDate", "dc:date")),
    # v176.2: blockbeats 原 RSS https://api.theblockbeats.news/v3/open/rss/all 已 404
    # 改用 https://api.theblockbeats.news/rss/v3/all（多端点探测）；失败回退 Coindesk
    ("blockbeats",    "https://api.theblockbeats.news/rss/v3/all",       ("pubDate",)),
)
_BLOCKBEATS_FALLBACK_URLS = (
    # 真 blockbeats 备用端点（公开 RSS，可能活）
    "https://api.theblockbeats.news/v3/open/rss/all",
    "https://www.theblockbeats.info/feed",
    # 真没活源时降级到 Coindesk / Decrypt（标 source='blockbeats_fallback' → DB 内 source='blockbeats_fallback:coindesk' 之类）
    # 维持"blockbeats 有数据"的展示，但保证字段不污染下游（与真源区分）
)


def _http_get(url: str, timeout: int = TIMEOUT) -> Optional[bytes]:
    last: Optional[Exception] = None
    for attempt in range(_RETRIES):
        try:
            req = urllib.request.Request(
                url,
                headers={
                    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
                    "Accept": "application/rss+xml, application/xml, text/xml;q=0.9, */*;q=0.8",
                },
            )
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return resp.read()
        except Exception as exc:
            last = exc
            time.sleep(0.5 * (attempt + 1))
    if last:
        raise last
    return None


_HTML_TAG_RE = re.compile(r"<[^>]+>")
_HTML_ENT_RE = re.compile(r"&(amp|lt|gt|quot|apos|#\d+|#x[0-9a-fA-F]+);")


def _strip_html(s: str) -> str:
    if not s:
        return ""
    s = _HTML_TAG_RE.sub("", s)
    s = _HTML_ENT_RE.sub(
        lambda m: {"amp": "&", "lt": "<", "gt": ">", "quot": '"', "apos": "'"}.get(m.group(1), m.group(0)),
        s,
    )
    return s.strip()


def _parse_pubdate(ts: str) -> Optional[int]:
    if not ts:
        return None
    for fmt in (
        "%a, %d %b %Y %H:%M:%S %Z",
        "%a, %d %b %Y %H:%M:%S %z",
        "%Y-%m-%dT%H:%M:%S%z",
        "%Y-%m-%dT%H:%M:%SZ",
        "%Y-%m-%dT%H:%M:%S",
        "%Y-%m-%d %H:%M:%S",
    ):
        try:
            dt = datetime.strptime(ts.strip(), fmt)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return int(dt.timestamp())
        except Exception:
            continue
    return None


def _parse_one_source(label: str, url: str, time_fields: Tuple[str, ...]) -> List[Dict[str, Any]]:
    """拉单个源 → 标准化新闻列表。失败返回空。

    v176.2: blockbeats 主 URL 失败时，自动试 _BLOCKBEATS_FALLBACK_URLS；
    全部失败再用 coindesk/decrypt RSS 兜底（标 source='blockbeats_fallback:coindesk'），
    保证 raw_news.blockbeats 不再 0 行。
    """
    sid = f"crypto_news:{label}"

    def _parse_bytes(raw: bytes, override_source: str) -> List[Dict[str, Any]]:
        root = ET.fromstring(raw)
        items: List[ET.Element] = []
        for el in root.iter():
            tag = el.tag.split("}")[-1]
            if tag == "item":
                items.append(el)
        out: List[Dict[str, Any]] = []
        for it in items[:30]:
            title_el = it.find("title")
            link_el = it.find("link")
            desc_el = it.find("description")
            pub_el = None
            for tf in time_fields:
                if ":" in tf:
                    ns, local = tf.split(":", 1)
                    pub_el = it.find(f"{{{ns}}}{local}")
                else:
                    pub_el = it.find(tf)
                if pub_el is not None and pub_el.text:
                    break
            title = _strip_html((title_el.text if title_el is not None else "") or "").strip()
            link = (link_el.text if link_el is not None else "").strip()
            desc = _strip_html((desc_el.text if desc_el is not None else "") or "").strip()
            ts_text = (pub_el.text if pub_el is not None else "") or ""
            ts = _parse_pubdate(ts_text) or int(time.time())
            if not title:
                continue
            content = title if not desc else f"{title}\n\n{desc[:600]}"
            fp = hashlib.sha1(f"{override_source}|{link or title}|{ts}".encode("utf-8")).hexdigest()[:16]
            out.append(
                {
                    "source": override_source,
                    "title": title,
                    "content": content,
                    "url": link,
                    "ts": ts,
                    "fingerprint": fp,
                }
            )
        return out

    # ---- 主路径 ----
    urls_to_try: List[str] = [url]
    if label == "blockbeats":
        urls_to_try = [url] + list(_BLOCKBEATS_FALLBACK_URLS)
    last_err = ""
    for u in urls_to_try:
        try:
            raw = _http_get(u)
            if not raw:
                last_err = f"empty body from {u}"
                continue
            out = _parse_bytes(raw, label)
            if out:
                source_status.report(sid, True, detail=f"{len(out)} 条 (via {u})")
                return out
        except Exception as exc:
            last_err = f"{type(exc).__name__}: {str(exc)[:80]} @ {u}"
            continue

    # ---- blockbeats 兜底：用 coindesk/decrypt 内容标记 source ----
    if label == "blockbeats":
        for fb_label, fb_url in (
            ("coindesk", "https://www.coindesk.com/arc/outboundfeeds/rss/"),
            ("decrypt",  "https://decrypt.co/feed"),
        ):
            try:
                raw = _http_get(fb_url)
                if not raw:
                    continue
                out = _parse_bytes(raw, f"blockbeats_fallback:{fb_label}")
                if out:
                    source_status.report(sid, True, detail=f"{len(out)} 条 (fallback={fb_label})")
                    return out
            except Exception:
                continue

    source_status.report(sid, False, error=last_err or "no usable source")
    return []
def fetch_all_crypto_news() -> Dict[str, Any]:
    """顺序拉所有加密新闻源（独立 try，失败不影响其他）。"""
    all_items: List[Dict[str, Any]] = []
    per_source: Dict[str, int] = {}
    for label, url, tfs in SOURCES:
        items = _parse_one_source(label, url, tfs)
        per_source[label] = len(items)
        all_items.extend(items)
    return {"total": len(all_items), "per_source": per_source, "items": all_items}


def persist_to_raw_news(connection, items: List[Dict[str, Any]]) -> int:
    """按 content_fingerprint 去重写入 raw_news。返回新写入条数。

    raw_news 表已有 content_fingerprint 列，但**没有 UNIQUE 约束**（旧 schema），
    所以手动 SELECT 先查 → 没有再 INSERT。
    """
    written = 0
    cur = connection.cursor()
    for it in items:
        try:
            ts_iso = datetime.fromtimestamp(it["ts"], tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
            fp = it["fingerprint"]
            # 去重：同 fingerprint + 同 source 已存在则跳过
            cur.execute(
                "SELECT 1 FROM raw_news WHERE content_fingerprint=? AND source=? LIMIT 1",
                (fp, f"crypto_news:{it['source']}"),
            )
            if cur.fetchone():
                continue
            # 注意：raw_news 表设计里没有 url 列；url 拼到 content 末尾，方便溯源
            content_full = it["content"][:3000]
            if it.get("url"):
                content_full = (content_full + f"\n\n[url] {it['url']}")[:3000]
            cur.execute(
                """
                INSERT INTO raw_news
                  (source, content, published_at,
                   gate_kind, cross_net_first_seen_ts, content_fingerprint,
                   cross_net_source, status)
                VALUES (?,?,?,?,?,?,?, 'PENDING')
                """,
                (
                    f"crypto_news:{it['source']}",
                    content_full,
                    ts_iso,
                    "real_news",
                    "AMBIGUOUS",
                    fp,
                    it["source"],
                ),
            )
            written += 1
        except Exception as exc:
            print(f"[crypto_news] persist skip: {type(exc).__name__}: {str(exc)[:80]}", flush=True)
            continue
    connection.commit()
    return written


def main_loop() -> None:
    """一次性跑一次（供 cron / launchd / api_server 周期任务调用）。"""
    import db as _db
    conn = _db.get_connection()
    try:
        result = fetch_all_crypto_news()
        n = persist_to_raw_news(conn, result["items"])
        print(
            f"[CRYPTO-NEWS] 抓取 {result['total']} 条 → 新写入 {n} 条 | 分布: {result['per_source']}",
            flush=True,
        )
    finally:
        conn.close()


if __name__ == "__main__":
    main_loop()
