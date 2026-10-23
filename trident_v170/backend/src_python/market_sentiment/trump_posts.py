"""Truth Social 特朗普发帖数据源 (政策预期驱动信号)。

api_server.market_sentiment_watcher 的 (b) 分支调用:
    fetch_with_retry(limit, ...) -> List[Dict]   拉取最新发帖, 内建重试
    to_ingest_items(posts)       -> List[Dict]   转成 raw_news 入库条目

数据源: Truth Social 公开 statuses API (无需 key, 需伪装浏览器 UA)。
政策类发帖对关税/美联储/地缘的预期冲击强, 是高价值结构化信号。
遵循本项目 feed 模块约定 (参考 fred_feed.py):
    - 可注入 get, 便于单测
    - 永不向调用方抛异常
    - 只产出条目, 不自行入库
"""
from __future__ import annotations

import json
import time
import urllib.request
from datetime import datetime, timezone, timedelta
from typing import Any, Callable, Dict, List, Optional

try:
    from zoneinfo import ZoneInfo

    _BEIJING = ZoneInfo("Asia/Shanghai")
except Exception:  # pragma: no cover
    _BEIJING = timezone(timedelta(hours=8))

API_URL = "https://truthsocial.com/api/v1/accounts/107780257626128497/statuses"
_TIMEOUT = 12
_RETRY_SLEEP = 2.0
_BJ = _BEIJING

SOURCE = "truth_social"

_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/122.0 Safari/537.36"
)

# 政策相关关键词 -> 影响方向 (驱动下游打分/信号)
_TOPIC_KEYWORDS = {
    "tariff": ("关税", "利空"),
    "trade": ("贸易", "中性"),
    "fed": ("美联储", "利多"),
    "rate": ("利率", "利空"),
    "inflation": ("通胀", "利空"),
    "war": ("地缘冲突", "利空"),
    "china": ("对华", "利空"),
    "sanction": ("制裁", "利空"),
    "bitcoin": ("加密", "利多"),
    "crypto": ("加密", "利多"),
}


def _default_get(url: str, timeout: int = _TIMEOUT) -> Any:
    req = urllib.request.Request(url, headers={"User-Agent": _UA, "Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8", errors="replace"))


def _strip_html(raw: str) -> str:
    import re

    text = re.sub(r"<br\s*/?>", " ", raw or "", flags=re.IGNORECASE)
    text = re.sub(r"<[^>]+>", "", text)
    text = text.replace("&amp;", "&").replace("&nbsp;", " ").replace("&quot;", '"')
    return " ".join(text.split())


def _created_at_bj(raw: Any) -> str:
    if not isinstance(raw, str) or not raw:
        return ""
    try:
        dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return ""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(_BJ).strftime("%Y-%m-%d %H:%M:%S")


def _classify(text: str) -> tuple:
    lowered = (text or "").lower()
    hits = []
    impact = "中性"
    for kw, (label, direction) in _TOPIC_KEYWORDS.items():
        if kw in lowered:
            hits.append(label)
            if impact == "中性":
                impact = direction
    return ("、".join(dict.fromkeys(hits)) or "综合表态"), impact


def _normalize(row: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    if not isinstance(row, dict):
        return None
    content = _strip_html(str(row.get("content") or ""))
    if not content:
        return None
    topic, impact = _classify(content)
    created = _created_at_bj(row.get("created_at"))
    post_id = str(row.get("id") or "")
    return {
        "id": post_id,
        "text": content,
        "created_at": created,
        "topic": topic,
        "impact": impact,
        "replies": _int_or_zero(row.get("replies_count")),
        "reblogs": _int_or_zero(row.get("reblogs_count")),
        "favourites": _int_or_zero(row.get("favourites_count")),
        "url": f"https://truthsocial.com/@realDonaldTrump/{post_id}" if post_id else "",
    }


def _int_or_zero(value: Any) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def fetch_with_retry(
    limit: int = 10,
    *,
    retries: int = 2,
    sleep: Callable[[float], None] = time.sleep,
    get: Callable = _default_get,
) -> List[Dict[str, Any]]:
    """拉取最新 limit 条发帖, 失败按 retries 退避重试。全部失败返回 []。"""
    try:
        n = max(1, min(int(limit or 10), 80))
    except (TypeError, ValueError):
        n = 10

    for attempt in range(max(1, retries + 1)):
        try:
            body = get(f"{API_URL}?limit={n}&exclude_replies=true")
        except Exception:
            if attempt >= retries:
                return []
            sleep(_RETRY_SLEEP * (attempt + 1))
            continue

        rows = body if isinstance(body, list) else (body or {}).get("statuses", [])
        if not isinstance(rows, list):
            return []
        out = []
        for row in rows:
            norm = _normalize(row)
            if norm:
                out.append(norm)
        return out
    return []


def to_ingest_items(posts: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """转成 raw_news 入库条目。互动量过低 (纯低噪闲聊) 的帖子直接丢弃。"""
    items: List[Dict[str, Any]] = []
    for post in posts or []:
        text = str(post.get("text") or "").strip()
        if not text:
            continue
        engagement = post.get("replies", 0) + post.get("reblogs", 0) + post.get("favourites", 0)
        if engagement <= 0:
            continue

        topic = post.get("topic") or "综合表态"
        impact = post.get("impact") or "中性"
        snippet = text if len(text) <= 260 else text[:257] + "..."
        title = f"特朗普 Truth Social 表态（{topic}·{impact}）"
        summary = (
            f"{snippet}\n互动 {engagement}（回复{post.get('replies', 0)}/"
            f"转发{post.get('reblogs', 0)}/点赞{post.get('favourites', 0)}）"
        )
        post_id = str(post.get("id") or "").strip()
        if not post_id:
            post_id = f"{abs(hash(text)) & 0xFFFFFF:06x}"

        items.append(
            {
                "id": f"truth:{post_id}",
                "title": title,
                "summary": summary,
                "source": SOURCE,
                "published_at": post.get("created_at") or "",
                "url": post.get("url") or "",
            }
        )
    return items