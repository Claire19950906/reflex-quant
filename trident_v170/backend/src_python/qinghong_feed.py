# -*- coding: utf-8 -*-
"""晴红·标卡（QingHong）第三方聚合资讯 API —— 试用评估源。

聚合 50+ 平台热榜/财经快讯。此处仅取个别财经快讯子源做质量/时效评估：

    GET {base}/api/s?id={source}&latest=true     请求头 X-API-Key: <key>

返回：
    {"status": "success", "id": "...", "updatedTime": <ms>,
     "items": [{"id", "title", "url", "mobileUrl", "pubDate"(ms), "extra": {"hover","date","info"}}]}

设计要点：
- 单次拉取全部子源合并成 ingest 条目；source 用中文平台名，便于前端展示与去重。
- 到点自动停服：超过 config.QINGHONG_EXPIRES_AT 后 is_expired()=True，fetch 返回
  status='expired' 空 items，watcher 据此停止采集，不再对已过期 key 发请求。
- 部分子源同一条目重复返回（如汇通中英双版），按 (source, id) 去重。
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

import requests

import config

_TIMEOUT_S = 8
_MAX_ITEMS_PER_SOURCE = 60
_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
       "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36")

# 子源 id → 中文展示名（用于 raw_news.source，避开既有 16 源归一化）
_SUB_SOURCES: Dict[str, str] = {
    "cls-telegraph": "财联社电报",
    "wallstreetcn-quick": "华尔街见闻快讯",
    "fastbull-express": "汇通财经快讯",
}


def _parse_expires_at() -> Optional[datetime]:
    s = (config.QINGHONG_EXPIRES_AT or "").strip()
    if not s:
        return None
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d"):
        try:
            return datetime.strptime(s, fmt).replace(tzinfo=config.TZ_SHANGHAI)
        except ValueError:
            continue
    return None


def is_expired() -> bool:
    """是否已到自动关闭时刻（本地统一上海时区）。"""
    dt = _parse_expires_at()
    if dt is None:
        return False
    return datetime.now(config.TZ_SHANGHAI) >= dt


def _label(sub_id: str) -> str:
    return _SUB_SOURCES.get(sub_id, sub_id)


def _parse_source(sub_id: str, headers: Dict[str, str]) -> Tuple[List[Dict[str, Any]], str]:
    """拉单个子源 → (ingest 条目列表, error 文本)。异常/非 200 → 空列表 + error。"""
    base = (config.QINGHONG_NEWS_BASE or "").strip().rstrip("/")
    label = _label(sub_id)
    try:
        resp = requests.get(
            f"{base}/api/s",
            params={"id": sub_id, "latest": "true"},
            headers=headers,
            timeout=_TIMEOUT_S,
        )
        if resp.status_code == 401:
            return [], f"{sub_id}: 无效 X-API-Key（401）"
        if resp.status_code == 402:
            return [], f"{sub_id}: key 已过期（402）"
        if resp.status_code == 429:
            return [], f"{sub_id}: 请求过频/额度用尽（429）"
        if resp.status_code != 200:
            return [], f"{sub_id}: HTTP {resp.status_code}"
        data = resp.json()
        if not isinstance(data, dict) or data.get("status") != "success":
            return [], f"{sub_id}: {data.get('error') or data.get('message') or '非 success 响应'}"
        out: List[Dict[str, Any]] = []
        for row in (data.get("items") or []):
            if not isinstance(row, dict):
                continue
            title = str(row.get("title") or "").strip()
            if not title:
                continue
            item_id = str(row.get("id") or title)
            extra = row.get("extra") if isinstance(row.get("extra"), dict) else {}
            pub_ms = row.get("pubDate") or extra.get("date")
            published_at = config.fmt_bj(pub_ms) if pub_ms else ""
            url = str(row.get("url") or row.get("mobileUrl") or "").strip()
            hover = str(extra.get("hover") or "").strip() if extra.get("hover") else ""
            summary_parts = []
            if hover and hover != title:
                summary_parts.append(hover)
            if url:
                summary_parts.append(f"原文: {url}")
            out.append({
                "id": item_id,
                "title": title,
                "summary": " ".join(summary_parts).strip(),
                "url": url,
                "published_at": published_at,
                "source": label,
            })
            if len(out) >= _MAX_ITEMS_PER_SOURCE:
                break
        return out, ""
    except Exception as exc:  # noqa: BLE001
        return [], f"{sub_id}: {type(exc).__name__}: {str(exc)[:100]}"


def fetch_qinghong_flash() -> Dict[str, Any]:
    """拉全部子源并合并（按 source:id 去重）。返回 {status, items, error}。

    status 取值：ok / unavailable / expired（到期自动关闭）。
    """
    if not config.QINGHONG_API_KEY:
        return {"status": "unavailable", "items": [], "error": "QINGHONG_API_KEY 未配置"}
    exp = _parse_expires_at()
    if exp is not None and datetime.now(config.TZ_SHANGHAI) >= exp:
        return {
            "status": "expired",
            "items": [],
            "error": f"晴红试用 key 已于 {config.QINGHONG_EXPIRES_AT} 到期，自动关闭",
        }
    headers = {"X-API-Key": config.QINGHONG_API_KEY, "User-Agent": _UA}
    items: List[Dict[str, Any]] = []
    errors: List[str] = []
    seen: set = set()
    for sub_id in config.QINGHONG_SOURCES:
        sub_items, err = _parse_source(sub_id, headers)
        if err:
            errors.append(err[:120])
        for it in sub_items:
            key = (it["source"], it["id"])
            if key in seen:
                continue
            seen.add(key)
            items.append(it)
    status = "unavailable" if (errors and not items) else "ok"
    return {"status": status, "items": items, "error": "; ".join(errors)}