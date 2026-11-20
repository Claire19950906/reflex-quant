# -*- coding: utf-8 -*-
"""多源 RSS 新闻采集（Yahoo Finance 官方 + Google News 聚合 FXStreet / MarketWatch）。

源在当前网络环境（直连海外站点 TLS 握手偶发被重置）下靠 3 次重试吸收抖动：
  - Yahoo Finance ── 官方 RSS（finance.yahoo.com/news/rssindex），实时、分钟级更新。
  - MarketWatch ──── 官方 feeds.content.dowjones.io / www.marketwatch.com 直连不可达，
                      走 Google News RSS `site:marketwatch.com` 聚合（收录滞后 ~2h，与
                      reuters_news 同法；时效对交易无用，但可补新闻量供反事实页时间线展示）。
  - FXStreet ──────── 官方 fxstreet.com 直连不可达，走 Google News RSS `site:fxstreet.com`。
  - Cointelegraph ─── 加密 RSS 直连（cointelegraph.com/rss），补齐加密线条（与深潮并列）。
  - OilPrice ──────── 原油 RSS 直连（oilprice.com/rss/main），补齐原油/能源线条。

条目结构对齐 _ingest_external_news_sync：
  {title, summary(含原文链接), source, id(稳定哈希), published_at(北京时间串)}
英文标题入库前走 webhook 翻译兜底链转中文。

⚠️ 无硬归因：这些源只提供"多源并列"的新闻时间线，不做任何因果断言；
   AI 分析按既有管道独立判定，交易层受 45min 时效闸保护（聚合源滞后自动被过滤）。
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

_UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"

# 源配置：(source_id, 展示名, RSS 地址, 是否 RFC822 时间格式, 兜底标题后缀清洗正则)
_SOURCES: List[Dict[str, Any]] = [
    {
        "source": "yahoo_finance",
        "name": "Yahoo Finance",
        "url": "https://finance.yahoo.com/news/rssindex",
        "rfc822": False,  # Yahoo 用 ISO8601（2026-09-12T11:01:30Z）
        "strip": r"\s*-\s*(Yahoo\s*Finance|Yahoo)\s*$",
    },
    # ── v170+ 数据源加固：ticker-level Yahoo RSS（补 GC=F 黄金/CL=F 原油/BTC-USD/ETH-USD/DX-Y.NYB/^TNX）
    # 比全局索引更精准，每日合计 +120 条与 XAU/WTI/crypto/DXY 强相关结构化新闻
    {
        "source": "yahoo_gold",
        "name": "Yahoo Gold (GC=F)",
        "url": "https://feeds.finance.yahoo.com/rss/2.0/headline?s=GC=F",
        "rfc822": False,
        "strip": r"\s*-\s*Yahoo\s*Finance\s*$",
    },
    {
        "source": "yahoo_oil",
        "name": "Yahoo Oil (CL=F)",
        "url": "https://feeds.finance.yahoo.com/rss/2.0/headline?s=CL=F",
        "rfc822": False,
        "strip": r"\s*-\s*Yahoo\s*Finance\s*$",
    },
    {
        "source": "yahoo_btc",
        "name": "Yahoo BTC (BTC-USD)",
        "url": "https://feeds.finance.yahoo.com/rss/2.0/headline?s=BTC-USD",
        "rfc822": False,
        "strip": r"\s*-\s*Yahoo\s*Finance\s*$",
    },
    {
        "source": "yahoo_eth",
        "name": "Yahoo ETH (ETH-USD)",
        "url": "https://feeds.finance.yahoo.com/rss/2.0/headline?s=ETH-USD",
        "rfc822": False,
        "strip": r"\s*-\s*Yahoo\s*Finance\s*$",
    },
    {
        "source": "yahoo_dxy",
        "name": "Yahoo DXY (DX-Y.NYB)",
        "url": "https://feeds.finance.yahoo.com/rss/2.0/headline?s=DX-Y.NYB",
        "rfc822": False,
        "strip": r"\s*-\s*Yahoo\s*Finance\s*$",
    },
    {
        "source": "yahoo_us10y",
        "name": "Yahoo US10Y (^TNX)",
        "url": "https://feeds.finance.yahoo.com/rss/2.0/headline?s=^TNX",
        "rfc822": False,
        "strip": r"\s*-\s*Yahoo\s*Finance\s*$",
    },
    {
        "source": "marketwatch",
        "name": "MarketWatch",
        "url": "https://news.google.com/rss/search?q=site:marketwatch.com&hl=en-US&gl=US&ceid=US:en",
        "rfc822": True,  # Google News 用 RFC822（Fri, 11 Sep 2026 14:59:29 GMT）
        "strip": r"\s*-\s*MarketWatch\s*$",
    },
    {
        "source": "fxstreet",
        "name": "FXStreet",
        "url": "https://news.google.com/rss/search?q=site:fxstreet.com&hl=en-US&gl=US&ceid=US:en",
        "rfc822": True,
        "strip": r"\s*-\s*FX[Ss]treet\s*$",
    },
    {
        "source": "cointelegraph",
        "name": "Cointelegraph",
        "url": "https://cointelegraph.com/rss",
        "rfc822": True,  # 直连 RSS，pubDate 为 RFC822
        "strip": r"\s*-\s*Cointelegraph.*\s*$",
    },
    {
        "source": "oilprice",
        "name": "OilPrice",
        "url": "https://oilprice.com/rss/main",
        "rfc822": True,  # 直连 RSS，pubDate 为 RFC822（TLS 偶发抖动由 _fetch_one 重试吸收）
        "strip": r"\s*-\s*OilPrice.*\s*$",
    },
    # ── v170+ 数据源加固：路透/彭博/SeekingAlpha（Google News site: 聚合兜底，~2h 收录滞后但补量）
    {
        "source": "reuters_business",
        "name": "Reuters Business",
        "url": "https://news.google.com/rss/search?q=site:reuters.com/business+OR+"
               "site:reuters.com/world&hl=en-US&gl=US&ceid=US:en",
        "rfc822": True,
        "strip": r"\s*-\s*(Reuters)\s*$",
    },
    {
        "source": "reuters_commodities",
        "name": "Reuters Commodities",
        "url": "https://news.google.com/rss/search?q=site:reuters.com+commodities+" \
               "gold+OR+oil+OR+copper&hl=en-US&gl=US&ceid=US:en",
        "rfc822": True,
        "strip": r"\s*-\s*(Reuters)\s*$",
    },
    {
        "source": "bloomberg_markets",
        "name": "Bloomberg Markets",
        "url": "https://news.google.com/rss/search?q=site:bloomberg.com+markets+" \
               "OR+site:bloomberg.com/news/articles&hl=en-US&gl=US&ceid=US:en",
        "rfc822": True,
        "strip": r"\s*-\s*Bloomberg\s*$",
    },
    {
        "source": "seekingalpha",
        "name": "SeekingAlpha Trending",
        "url": "https://news.google.com/rss/search?q=site:seekingalpha.com+trending+" \
               "OR+market+OR+analysis&hl=en-US&gl=US&ceid=US:en",
        "rfc822": True,
        "strip": r"\s*-\s*Seeking\s*Alpha\s*$",
    },
]

_TIMEOUT = 15
_MAX_ITEMS_EACH = 40


def _rfc822_to_bj(value: str) -> str:
    """RFC822 GMT 时间 → 北京时间字符串；解析失败返回 ""。"""
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


def _iso8601_to_bj(value: str) -> str:
    """ISO8601（Yahoo：2026-09-12T11:01:30Z）→ 北京时间字符串；失败返回 ""。"""
    if not value:
        return ""
    try:
        s = value.strip().replace("Z", "+00:00")
        dt = datetime.fromisoformat(s)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        import config
        return dt.astimezone(config.TZ_SHANGHAI).strftime("%Y-%m-%d %H:%M:%S")
    except Exception:
        return ""


def _strip_source_suffix(title: str, item: ET.Element, cfg: Dict[str, Any]) -> str:
    """去标题末尾的 " - 来源名" 后缀（Google News 格式）；Yahoo 标题通常无后缀。"""
    t = (title or "").strip()
    src_tag = (item.findtext("source") or "").strip()
    if src_tag:
        suffix = f" - {src_tag}"
        if t.lower().endswith(suffix.lower()):
            t = t[: -len(suffix)].strip()
    if cfg.get("strip"):
        t = re.sub(cfg["strip"], "", t, flags=re.IGNORECASE).strip()
    return t


def _fetch_one(cfg: Dict[str, Any]) -> List[Dict[str, Any]]:
    """拉取单个 RSS 源并解析为标准条目。失败返回 []。每项含稳定 _id（url 哈希）。

    对偶发 TLS 抖动 / 聚合源瞬时限流做最多 2 次重试（间隔递增 2s/4s），
    吸收网络不稳定（当前环境直连海外站点 TLS 握手偶发被重置）。
    """
    last_exc: Optional[Exception] = None
    for attempt in range(3):
        try:
            resp = requests.get(cfg["url"], headers={"User-Agent": _UA}, timeout=_TIMEOUT)
            if resp.status_code != 200:
                return []
            root = ET.fromstring(resp.content)
            break
        except Exception as exc:
            last_exc = exc
            time.sleep(2 * (attempt + 1))
    else:
        print(f"  [RSS] {cfg['source']} fetch fail: {type(last_exc).__name__}: {str(last_exc)[:100]}")
        return []
    out: List[Dict[str, Any]] = []
    for item in root.findall(".//item"):
        title = _strip_source_suffix(item.findtext("title") or "", item, cfg)
        if not title:
            continue
        link = (item.findtext("link") or "").strip()
        pub_raw = (item.findtext("pubDate") or "").strip()
        pub_bj = _rfc822_to_bj(pub_raw) if cfg["rfc822"] else _iso8601_to_bj(pub_raw)
        # 稳定去重 id：同一文章的 url/标题哈希跨进程不变，入库层 [hash:] 可持久去重
        _id = hashlib.sha256((link or title).encode("utf-8")).hexdigest()[:20]
        out.append({
            "title": title,
            "id": _id,
            "published_at": pub_bj,
            "url": link,
            "feed_source": cfg["source"],
            "name": cfg["name"],
        })
        if len(out) >= _MAX_ITEMS_EACH:
            break
    return out


# 已处理条目去重集合（进程内）；重启后首轮会重取近几天条目，但入库层 [hash:] 稳定
# 哈希去重会挡掉重复（仅多一次翻译成本，可接受）。
_seen_ids: set[str] = set()


def poll_rss_news() -> List[Dict[str, Any]]:
    """轮询三源并合并"未见过的"新条目。失败源静默跳过（不阻塞其它源）。"""
    global _seen_ids
    out: List[Dict[str, Any]] = []
    for cfg in _SOURCES:
        for it in _fetch_one(cfg):
            uniq = f"{cfg['source']}:{it.get('id') or ''}"
            if not uniq or uniq in _seen_ids:
                continue
            _seen_ids.add(uniq)
            it["news_ts"] = time.time()
            out.append(it)
    if len(_seen_ids) > 6000:
        _seen_ids = set(list(_seen_ids)[-3000:])
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
        name = str(it.get("name") or "RSS")
        summary = f"{name} 快讯"
        if url:
            summary += f" | 原文: {url}"
        items.append({
            "title": title,
            "summary": summary,
            "source": str(it.get("feed_source") or "rss"),
            # 稳定哈希 id（不含时间戳）：同一文章跨重启 marker 一致，入库层去重持久生效
            "id": str(it.get("id") or ""),
            "published_at": it.get("published_at") or "",
        })
    return items