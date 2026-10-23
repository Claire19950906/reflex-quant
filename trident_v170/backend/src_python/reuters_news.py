# -*- coding: utf-8 -*-
"""Reuters 路透社快讯采集（经 Google News RSS 聚合通道）——【已停用】。

⚠️ 停用原因：Google News 收录路透滞后 ~2h（实测平均 118min），对交易信号
时效无价值，且与金十（秒级转发路透一手）内容重复占 AI 配额。
路透时效由金十快讯承担；本模块由 .env REUTERS_NEWS_ENABLED=0 关闭
（watcher 启动即跳过）。代码保留供将来接入更快的路透通道（RSSHub 自建等）。

背景：Reuters 无官方免费 API/RSS（官网反爬 401/DataDome 验证墙）。曾用
Google News RSS 搜索 `site:reuters.com when:1d` —— 返回路透标题（英文原文、
发布时间的 RFC822 格式）。标题为英文 → 入库前走 webhook 翻译兜底链转中文。

条目结构对齐 _ingest_external_news_sync：
  {title, summary(含原链接), source:"reuters", id, published_at(北京时间串)}
"""

from __future__ import annotations

import hashlib
import re
import time
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from typing import Any, Dict, List, Optional

import requests

_GOOGLE_NEWS_RSS = (
    "https://news.google.com/rss/search"
    "?q=site:reuters.com%20when:1d&hl=en-US&gl=US&ceid=US:en"
)
_TIMEOUT = 15
_MAX_ITEMS = 100

# 平台发布时间（RFC822 GMT）→ 北京时间字符串；解析失败 → ""
def _rfc822_to_bj(value: str) -> str:
    if not value:
        return ""
    try:
        dt = parsedate_to_datetime(value)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        import config
        return dt.astimezone(config.TZ_SHANGHAI).strftime("%Y-%m-%d %H:%M:%S")
    except Exception:
        return ""


def _clean_title(title: str) -> str:
    """去掉 Google News 标题末尾的 ' - Reuters' 来源后缀与多余空白。"""
    t = re.sub(r"\s*-\s*Reuters\s*$", "", title or "").strip()
    return t


def fetch_reuters_headlines() -> List[Dict[str, Any]]:
    """拉 Google News RSS 中最近 1 天的路透条目。失败返回 []。

    每项: {title(英文原文), id, published_at(北京时间串), url(Google 跳转链接)}
    发布时间距今超过 24h 的条目丢弃（Google News 偶发旧缓存）。
    """
    try:
        resp = requests.get(
            _GOOGLE_NEWS_RSS,
            headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"},
            timeout=_TIMEOUT,
        )
        if resp.status_code != 200:
            return []
        root = ET.fromstring(resp.content)
        out: List[Dict[str, Any]] = []
        now = time.time()
        for item in root.findall(".//item"):
            title = _clean_title(item.findtext("title") or "")
            if not title:
                continue
            link = (item.findtext("link") or "").strip()
            pub_raw = (item.findtext("pubDate") or "").strip()
            pub_bj = _rfc822_to_bj(pub_raw)
            # 过旧条目丢弃（Google News 缓存可能混入历史）
            try:
                dt = parsedate_to_datetime(pub_raw)
                if dt.tzinfo is None:
                    dt = dt.replace(tzinfo=timezone.utc)
                age_h = (now - dt.timestamp()) / 3600.0
                if age_h > 26:
                    continue
            except Exception:
                pass
            out.append({
                "title": title,
                "id": link or title,
                "published_at": pub_bj,
                "url": link,
            })
            if len(out) >= _MAX_ITEMS:
                break
        return out
    except Exception as exc:
        print(f"  [REUTERS] fetch fail: {type(exc).__name__}: {str(exc)[:100]}")
        return []


# 已处理条目的去重集合（进程内；Google News RSS 每次全量返回近 1 天条目，
# 用"已见 id"做增量，避免时间水位漏掉中间时段条目）。重启后首轮会全量重取，
# 但入库层 [hash:] 去重会挡掉重复（仅多一次翻译成本，可接受）。
_seen_ids: set[str] = set()


def poll_reuters_news() -> List[Dict[str, Any]]:
    """轮询路透新条目（返回 fetch 结果中未处理过的新条目）。"""
    global _seen_ids
    items = fetch_reuters_headlines()
    out: List[Dict[str, Any]] = []
    for it in items:
        item_id = str(it.get("id") or "")
        if not item_id or item_id in _seen_ids:
            continue
        _seen_ids.add(item_id)
        it["news_ts"] = time.time()
        out.append(it)
    # 控制集合大小（防内存无限增长）
    if len(_seen_ids) > 2000:
        _seen_ids = set(list(_seen_ids)[-1000:])
    return out


def translate_title_sync(title: str) -> str:
    """英文标题 → 中文（复用 webhook 翻译兜底链）；失败/纯中文返回原文。"""
    try:
        from engine.webhook import _translate_title_sync as _tr
        return _tr(title) or title
    except Exception:
        return title


def to_ingest_items(news_items: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """翻译标题并组装 _ingest_external_news_sync 需要的条目结构。"""
    items: List[Dict[str, Any]] = []
    for it in news_items:
        title = translate_title_sync(str(it.get("title") or "")).strip()
        if not title:
            continue
        url = str(it.get("url") or "")
        summary = "Reuters 路透社一手快讯"
        if url:
            summary += f" | 原文: {url}"
        items.append({
            "title": title,
            "summary": summary,
            "source": "reuters",
            "id": f"reuters:{it.get('news_ts') or ''}:{hashlib.sha256(str(it.get('title')).encode('utf-8')).hexdigest()[:12]}",
            "published_at": it.get("published_at") or "",
        })
    return items
