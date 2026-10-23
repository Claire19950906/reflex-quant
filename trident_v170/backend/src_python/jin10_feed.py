#!/usr/bin/env python3
"""金十数据 (jin10.com) 快讯采集 — MCP 优先，SSR 首页 HTML 解析降级。

首选官方 MCP 端点（jin10_mcp 模块，token 在 .env 的 JIN10_MCP_TOKEN）：
快讯 list_flash / 财经日历 list_calendar / 行情 get_quote。
MCP 不可用（无 token/网络失败）时降级为 SSR 首页 HTML 解析（jin10.com 已改 CSR，
此路径可能拿不到数据，仅作兜底）。返回结构与 market_feeds/_fetch_techflow_sync 对齐。
"""

from __future__ import annotations

import re
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Dict, List

import requests

import jin10_mcp

JIN10_URL = "https://www.jin10.com/"
UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
    " (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)
TZ = timezone(timedelta(hours=8))

# 快讯条目容器：id="flash20260825122608465800" class="jin-flash-item-container ..."
_ITEM_RE = re.compile(
    r'<div[^>]*id="(flash20\d{10,})"[^>]*class="jin-flash-item-container[^"]*"(.*?)</div>\s*</div>\s*</div>',
    re.S,
)
# 条目标题里的时间：<span>12:26:08</span>
_TIME_RE = re.compile(r'class="item-time"[^>]*>.*?<span[^>]*>([^<]+)</span>', re.S)
# 兜底：直接从块内找 HH:MM:SS
_CLOCK_RE = re.compile(r"(\d{2}:\d{2}:\d{2})")
_TAG_RE = re.compile(r"<[^>]+>")

# 策略相关关键词白名单：只保留与 BTC/XAU/WTI + 宏观相关的快讯，
# 过滤掉 A股/港股/美股/公司公告等无关内容（金十每轮 28 条 80% 无关，
# 全量入库会拖垮 AI 分析吞吐）。
_RELEVANT_KEYWORDS = jin10_mcp._RELEVANT_KEYWORDS


def _is_relevant(text: str) -> bool:
    lowered = text.lower()
    return any(kw in lowered for kw in _RELEVANT_KEYWORDS)


def _clean(text: str) -> str:
    text = _TAG_RE.sub(" ", text)
    return re.sub(r"\s+", " ", text).strip()


def fetch_jin10_flash(get: Callable[..., Any] = requests.get) -> Dict[str, Any]:
    """抓取金十最新快讯。

    优先走官方 MCP（jin10_mcp.fetch_jin10_flash，token 在 .env）；
    MCP 不可用时降级为 SSR 首页 HTML 解析。返回 {"status", "items", "error"}。
    """
    # ── 首选：官方 MCP ──
    if jin10_mcp.config.JIN10_MCP_TOKEN:
        try:
            mcp_result = jin10_mcp.fetch_jin10_flash()
            if mcp_result.get("status") == "ok":
                return mcp_result
            # MCP 失败（401/限流/无相关条目）— 打印一次后走 SSR 兜底
            err = mcp_result.get("error") or ""
            print(f"[JIN10] MCP 不可用（{err}），降级 SSR 解析")
        except Exception as exc:
            print(f"[JIN10] MCP 异常 {type(exc).__name__}: {exc}，降级 SSR 解析")

    # ── 降级：SSR 首页 HTML 解析（jin10.com 已改 CSR，可能拿不到数据）──
    try:
        resp = get(JIN10_URL, headers={"User-Agent": UA}, timeout=8)
        resp.raise_for_status()
        # 金十 HTML 无 Content-Type charset（实测 apparent_encoding 误判导致 r.text 乱码），
        # 强制按 UTF-8 解码（页面 <meta charset=utf-8>）。
        html = resp.content.decode("utf-8", errors="replace")
    except Exception as exc:
        return {"status": "unavailable", "items": [], "error": f"{type(exc).__name__}"}

    items: List[Dict[str, Any]] = []
    today = datetime.now(TZ).strftime("%Y-%m-%d")
    seen: set[str] = set()
    for m in _ITEM_RE.finditer(html):
        fid, block = m.group(1), m.group(2)
        if fid in seen:
            continue
        seen.add(fid)
        tm = _TIME_RE.search(block)
        clock = tm.group(1).strip() if tm else ""
        if not clock:
            clock_match = _CLOCK_RE.search(block)
            clock = clock_match.group(1) if clock_match else ""
        text = _clean(block)
        # 去掉开头的 ">" 装饰符和时间前缀
        text = re.sub(r"^>\s*", "", text).strip()
        if len(text) < 12:
            continue
        if clock and text.startswith(clock):
            text = text[len(clock):].strip()
        # 只保留与策略相关的快讯（过滤 A股/公司公告等无关内容）
        if not _is_relevant(text):
            continue
        published_at = f"{today} {clock}" if clock else f"{today} 00:00:00"
        items.append({
            "id": fid,
            "title": text[:500],
            "summary": "",
            "source": "jin10",
            "published_at": published_at,
            "url": "",
        })
    status = "ok" if items else "unavailable"
    return {"status": status, "items": items, "error": ""}


if __name__ == "__main__":
    t0 = time.time()
    result = fetch_jin10_flash()
    print(f"status={result['status']} items={len(result['items'])} in {time.time()-t0:.1f}s")
    for item in result["items"][:5]:
        print(f"  [{item['id']}] {item['published_at']} | {item['title'][:60]}")
