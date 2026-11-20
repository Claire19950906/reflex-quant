# -*- coding: utf-8 -*-
"""平台新闻抓取助手（从 api_server.py 下沉，2026-11-17）。

这批 11 个函数原来散在 api_server.py 里，被 5 个 `/api/news/<平台>` 端点和另外
4 处内部调用共用（预热、日历、聚合），所以不能只搬端点。整簇下沉到这里，
api_server 侧改为 `from feeds.platform_news import ...` 拿回同名符号，
既有 1200 多条扁平引用不受影响。

抓取全走 requests（带 Session 复用），TechFlow 那一路有 20s 进程内缓存
（_TECHFLOW_CACHE），jin10 走 jin10_feed / jin10_mcp 两个既有封装。
"""
from __future__ import annotations

import html
import json
import re
import time
import urllib.parse
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import parse_qs, urlencode, urlsplit, urlunsplit
from xml.etree.ElementTree import fromstring

import requests

import config
from feeds import jin10_feed
from feeds import jin10_mcp
from feeds import source_status


# 各家平台抓取的进程内缓存（20s），挡前端轮询洪峰
_JIN10_CACHE: Dict[str, Any] = {"expires": 0.0, "value": None}
_JIN10_CALENDAR_CACHE: Dict[str, Any] = {"expires": 0.0, "value": None}
_TECHFLOW_CACHE: Dict[str, Any] = {"expires": 0.0, "value": None}
_EASTMONEY_CACHE: Dict[str, Any] = {"expires": 0.0, "value": None}
_BLOCKBEATS_CACHE: Dict[str, Any] = {"expires": 0.0, "value": None}
_NEWS_CACHE_TTL = 20.0

# 加密相关过滤词（用于把英文源里与币圈无关的条目筛掉）
_CRYPTO_KEYWORDS = (
    "比特币", "btc", "以太坊", "eth", "sol", "加密", "crypto", "代币", "token", "币",
    "etf", "现货", "合约", "爆仓", "清算", "稳定币", "usdt", "usdc", "币安", "binance",
    "coinbase", "okx", "交易所", "监管", "sec", "矿工", "挖矿", "减半", "halving",
    "defi", "web3", "nft", "资金费率", "杠杆", "多头", "空头", "巨鲸", "链上", "钱包",
    "质押", "staking", "山寨", "行情", "市值", "meme", "狗狗币", "doge", "突破", "跌破",
    "blockchain", "加密货币",
)



def _techflow_value(item: Dict[str, Any], *keys: str) -> Any:
    for key in keys:
        if item.get(key) not in (None, ""):
            return item[key]
    return ""


def _paged_url(url: str, page: int, page_keys: tuple[str, ...] = ("page", "page_index")) -> str:
    parsed = urllib.parse.urlsplit(url)
    query = urllib.parse.parse_qs(parsed.query, keep_blank_values=True)
    for key in page_keys:
        if key in query:
            query[key] = [str(page)]
    if not any(key in query for key in page_keys):
        query["page"] = [str(page)]
    return urllib.parse.urlunsplit((
        parsed.scheme, parsed.netloc, parsed.path,
        urllib.parse.urlencode(query, doseq=True), parsed.fragment,
    ))


def _extract_news_rows(payload: Any, list_keys: tuple[str, ...]) -> list:
    container = payload.get("data", payload) if isinstance(payload, dict) else payload
    if isinstance(container, dict):
        nested = container.get("data")
        if isinstance(nested, dict):
            container = nested
        rows = next((container[k] for k in list_keys if isinstance(container.get(k), list)), [])
        return rows if isinstance(rows, list) else []
    return container if isinstance(container, list) else []


def _fetch_paged_json(
    url: str,
    *,
    referer: str,
    timeout: int,
    pages: int,
    use_proxy: bool = False,
    extra_headers: Optional[Dict[str, str]] = None,
) -> Any:
    # use_proxy=True 走 env HTTP_PROXY/HTTPS_PROXY（配合 config.MARKET_USE_PROXY，给被墙源用）；
    # 默认 False 直连（trust_env=False，忽略系统残留代理变量）。
    session = requests.Session()
    session.trust_env = use_proxy
    headers = {
        "User-Agent": "Mozilla/5.0 (compatible; TridentNewsReader/1.0)",
        "Referer": referer,
        "Accept": "application/json, text/plain, */*",
    }
    if extra_headers:
        headers.update(extra_headers)
    for page in range(1, max(1, pages) + 1):
        response = session.get(
            _paged_url(url, page),
            timeout=timeout,
            headers=headers,
        )
        response.raise_for_status()
        content_type = str(response.headers.get("content-type") or "").lower()
        if "html" in content_type:
            raise ValueError("non-json response")
        yield response.json()


def _is_crypto_relevant(text: str) -> bool:
    lowered = (text or "").lower()
    return any(kw in lowered for kw in _CRYPTO_KEYWORDS)


def _fetch_techflow_sync() -> Dict[str, Any]:
    now = time.monotonic()
    if _TECHFLOW_CACHE["value"] is not None and now < _TECHFLOW_CACHE["expires"]:
        return _TECHFLOW_CACHE["value"]
    try:
        items = []
        seen: set[str] = set()
        for payload in _fetch_paged_json(
            config.TECHFLOW_NEWS_URL,
            referer="https://www.techflowpost.com/",
            timeout=4,
            pages=config.NEWS_SOURCE_PAGES,
        ):
            if isinstance(payload, dict) and payload.get("data") is not None:
                content_hint = str(payload.get("message") or "")
                if "html" in content_hint.lower():
                    raise ValueError("non-json response")
            for row in _extract_news_rows(payload, ("list", "items", "data", "newsflashes")):
                if not isinstance(row, dict):
                    continue
                title = str(_techflow_value(row, "title", "name", "content") or "").strip()
                if not title:
                    continue
                # 深潮相关性过滤：只保留含加密/行情关键词的新闻，
                # 砍掉纯科技/AI/芯片/公司类内容（降采集量，避免积压）。
                if not _is_crypto_relevant(title):
                    continue
                item_id = str(_techflow_value(row, "id", "newsflash_id", "uuid") or title)
                if item_id in seen:
                    continue
                seen.add(item_id)
                summary = str(_techflow_value(row, "summary", "description", "digest", "content") or "").strip()
                url = str(_techflow_value(row, "url", "link", "share_url") or "").strip()
                if url.startswith("/"):
                    url = "https://www.techflowpost.com" + url
                items.append({
                    "id": item_id,
                    "title": title,
                    "summary": summary if summary != title else "",
                    "url": url,
                    "published_at": str(_techflow_value(row, "published_at", "publish_time", "created_at", "createdAt") or ""),
                    "source": "TechFlow 深潮",
                })
        value = {"status": "ok", "items": items, "error": ""}
    except Exception as exc:
        value = {"status": "unavailable", "items": [], "error": f"TechFlow unavailable: {type(exc).__name__}"}
    _TECHFLOW_CACHE.update({"value": value, "expires": now + _NEWS_CACHE_TTL})
    return value


def _fetch_eastmoney_news_sync() -> Dict[str, Any]:
    now = time.monotonic()
    if _EASTMONEY_CACHE["value"] is not None and now < _EASTMONEY_CACHE["expires"]:
        return _EASTMONEY_CACHE["value"]
    try:
        items = []
        seen: set[str] = set()
        eastmoney_url = config.EASTMONEY_NEWS_URL
        if "req_trace" not in eastmoney_url:
            sep = "&" if "?" in eastmoney_url else "?"
            eastmoney_url = f"{eastmoney_url}{sep}req_trace={int(time.time() * 1000)}"
        for payload in _fetch_paged_json(
            eastmoney_url,
            referer="https://www.eastmoney.com/",
            timeout=5,
            pages=config.NEWS_SOURCE_PAGES,
        ):
            for row in _extract_news_rows(payload, ("list", "items", "data", "result")):
                if not isinstance(row, dict):
                    continue
                title = str(_techflow_value(row, "title", "newsTitle", "Art_Title", "content", "digest") or "").strip()
                if not title:
                    continue
                item_id = str(_techflow_value(row, "id", "newsId", "code", "uniqueUrl", "Art_UniqueUrl") or title)
                if item_id in seen:
                    continue
                seen.add(item_id)
                summary = str(_techflow_value(row, "summary", "digest", "description", "Art_Summary", "content") or "").strip()
                url = str(_techflow_value(row, "url", "uniqueUrl", "link", "newsUrl", "Art_Url") or "").strip()
                if url.startswith("/"):
                    url = "https://www.eastmoney.com" + url
                items.append({
                    "id": item_id,
                    "title": title,
                    "summary": summary if summary != title else "",
                    "url": url,
                    "published_at": str(_techflow_value(row, "published_at", "showTime", "publishTime", "Art_ShowTime", "date") or ""),
                    "source": "东方财富",
                })
        value = {"status": "ok", "items": items, "error": ""}
    except Exception as exc:
        value = {"status": "unavailable", "items": [], "error": f"EastMoney unavailable: {type(exc).__name__}"}
    _EASTMONEY_CACHE.update({"value": value, "expires": now + _NEWS_CACHE_TTL})
    return value


def _fetch_blockbeats_sync() -> Dict[str, Any]:
    now = time.monotonic()
    if _BLOCKBEATS_CACHE["value"] is not None and now < _BLOCKBEATS_CACHE["expires"]:
        return _BLOCKBEATS_CACHE["value"]
    if not config.BLOCKBEATS_API_KEY:
        # v176：fallback — 没配 BlockBeats API Key 时尝试 decrypt / coindesk RSS 兜底
        return _fetch_blockbeats_fallback_sync()
    try:
        items = []
        seen: set[str] = set()
        for payload in _fetch_paged_json(
            config.BLOCKBEATS_NEWS_URL,
            referer="https://www.theblockbeats.info/",
            timeout=5,
            pages=config.NEWS_SOURCE_PAGES,
            use_proxy=config.MARKET_USE_PROXY,
            extra_headers={"api-key": config.BLOCKBEATS_API_KEY},
        ):
            if isinstance(payload, dict) and payload.get("status") not in (0, "0", None, "ok", 200):
                continue
            for row in _extract_news_rows(payload, ("data", "list", "items")):
                if not isinstance(row, dict):
                    continue
                title = str(_techflow_value(row, "title", "name", "content") or "").strip()
                if not title:
                    continue
                item_id = str(_techflow_value(row, "id", "flash_id", "uuid") or title)
                if item_id in seen:
                    continue
                seen.add(item_id)
                # content 为 HTML（如 "<p>BlockBeats 消息…</p>"），去标签 + 还原实体后作摘要。
                summary = html.unescape(re.sub(r"<[^>]+>", "", str(
                    _techflow_value(row, "content", "summary", "description", "digest") or ""
                ))).strip()
                url = str(_techflow_value(row, "link", "url", "share_url") or "").strip()
                items.append({
                    "id": item_id,
                    "title": title,
                    "summary": summary if summary != title else "",
                    "url": url,
                    "published_at": str(_techflow_value(row, "create_time", "published_at", "created_at", "add_time") or ""),
                    "source": "律动 BlockBeats",
                })
        if items:
            value = {"status": "ok", "items": items, "error": ""}
        else:
            # v176：主源 0 条 → 降级 fallback（防 dashboard "BlockBeats 不可用"显示）
            value = _fetch_blockbeats_fallback_sync()
            value["fallback"] = True
    except Exception as exc:
        # v176：API 异常也降级 fallback
        value = _fetch_blockbeats_fallback_sync()
        value["fallback"] = True
        value["error"] = f"BlockBeats 主源失败({type(exc).__name__})，已降级 fallback"
    _BLOCKBEATS_CACHE.update({"value": value, "expires": now + _NEWS_CACHE_TTL})
    return value


def _fetch_blockbeats_fallback_sync() -> Dict[str, Any]:
    """v176.7 BlockBeats 备用源：主 API 失败/未配置/0 条 → 降级到多源 RSS 兜底。

    选源沿革：
      * v176 初版：Coindesk + Decrypt（英文加密主流）—— 但 decrypt/coindesk 在 Python 3.14 下
        与 feedparser/urllib 的 TLS 握手不兼容（SSL EOF）；且它们是英文源，与 BlockBeats
        中文定位不符。
      * v176.7 改造：换成 4 个加密 RSS（中文+英文混合）+ requests+ElementTree（同
        rss_sources.py 成功模式，绕开 feedparser SSL 问题）。

    来源（按优先级）：Foresight News → Cointelegraph → Decrypt → Coindesk
    返回: {"status": "ok"|"unavailable", "items": [...], "error": "..."}
    """
    import xml.etree.ElementTree as ET
    import requests as _rq

    items: list = []
    seen: set[str] = set()
    sources_tried: list = []

    _fallback_sources = (
        ("https://foresightnews.pro/rss", "Foresight News (BlockBeats 备用源)"),
        ("https://cointelegraph.com/rss", "Cointelegraph (BlockBeats 备用源)"),
        ("https://decrypt.co/feed", "Decrypt (BlockBeats 备用源)"),
        ("https://www.coindesk.com/arc/outboundfeeds/rss/", "Coindesk (BlockBeats 备用源)"),
    )

    for url, source_label in _fallback_sources:
        if len(items) >= 30:
            break
        try:
            sources_tried.append(url)
            resp = _rq.get(url, timeout=10, headers={"User-Agent": "Mozilla/5.0 Trident"})
            if resp.status_code != 200 or not resp.content:
                continue
            root = ET.fromstring(resp.content)
            for item_el in root.findall(".//item"):
                title = (item_el.findtext("title") or "").strip()
                if not title or title in seen:
                    continue
                seen.add(title)
                summary = html.unescape(re.sub(r"<[^>]+>", "", str(
                    item_el.findtext("description") or ""
                ))).strip()[:400]
                pub = (item_el.findtext("pubDate") or "").strip()
                items.append({
                    "id": title,
                    "title": title,
                    "summary": summary if summary != title else "",
                    "url": (item_el.findtext("link") or "").strip(),
                    "published_at": pub,
                    "source": source_label,
                })
                if len(items) >= 30:
                    break
        except Exception as exc:
            print(f"[BLOCKBEATS-FB] {url} 失败: {type(exc).__name__}: {str(exc)[:80]}", flush=True)
            continue

    if items:
        return {"status": "ok", "items": items, "error": ""}
    return {
        "status": "unavailable",
        "items": [],
        "error": f"BlockBeats 主源+备用源均失败（已试 {'; '.join(sources_tried) or '无'}）",
    }


def _fetch_jin10_sync() -> Dict[str, Any]:
    now = time.monotonic()
    if _JIN10_CACHE["value"] is not None and now < _JIN10_CACHE["expires"]:
        return _JIN10_CACHE["value"]
    try:
        payload = jin10_feed.fetch_jin10_flash()
        value = {
            "status": payload.get("status", "unavailable"),
            "items": payload.get("items", []),
            "error": payload.get("error", ""),
        }
    except Exception as exc:
        value = {"status": "unavailable", "items": [], "error": f"Jin10 unavailable: {type(exc).__name__}"}
    # 信息源面板：金十 MCP 在线/离线/配额超限实时标色
    source_status.report(
        "jin10_mcp",
        value.get("status") == "ok",
        error=value.get("error", "") or "",
        detail=f"items={len(value.get('items') or [])}",
    )
    # 节流：默认 10s（金十独立 watcher 高频采集；免费 token 有频率限制，
    # 429 时可用 .env 的 JIN10_MCP_POLL_SECONDS 调大）
    _JIN10_CACHE.update({"value": value, "expires": now + config.JIN10_MCP_POLL_SECONDS})
    return value


def _fetch_jin10_calendar_sync() -> Dict[str, Any]:
    """金十财经日历（list_calendar）— 缓存节流：日历数据变化慢，
    缓存 TTL 取 max(JIN10_MCP_POLL_SECONDS, 60)，避免高频轮询打爆 MCP 配额。
    """
    now = time.monotonic()
    if _JIN10_CALENDAR_CACHE["value"] is not None and now < _JIN10_CALENDAR_CACHE["expires"]:
        return _JIN10_CALENDAR_CACHE["value"]
    try:
        payload = jin10_mcp.fetch_jin10_calendar()
        value = {
            "status": payload.get("status", "unavailable"),
            "items": payload.get("items", []),
            "error": payload.get("error", ""),
        }
    except Exception as exc:
        value = {"status": "unavailable", "items": [], "error": f"Jin10 calendar unavailable: {type(exc).__name__}"}
    ttl = max(config.JIN10_MCP_POLL_SECONDS, 60.0)
    _JIN10_CALENDAR_CACHE.update({"value": value, "expires": now + ttl})
    return value
