"""Fear & Greed Index 数据源 (加密市场情绪)。

api_server.market_sentiment_watcher 的 (a) 分支调用:
    fetch_fng(limit)      -> List[Dict]   拉取 FNG 历史序列
    to_ingest_items(rows) -> List[Dict]   转成 raw_news 入库条目

数据源: alternative.me 公开 FNG API (limit/format 参数), 无需 key。
遵循本项目既有 feed 模块约定 (参考 fred_feed.py / bls_feed.py):
    - _default_get 为默认 HTTP 注入点, 便于单测替换
    - 永不抛异常给调用方, 失败返回空列表
    - 不写库, 只产出结构化条目交给 _ingest_external_news_sync
"""
from __future__ import annotations

import json
import urllib.request
from datetime import datetime, timezone, timedelta
from typing import Any, Callable, Dict, List, Optional

try:  # zoneinfo 在部分解释器缺失, 降级到固定偏移
    from zoneinfo import ZoneInfo

    _BEIJING = ZoneInfo("Asia/Shanghai")
except Exception:  # pragma: no cover - 兜底
    _BEIJING = timezone(timedelta(hours=8))

FNG_URL = "https://api.alternative.me/fng/"
_TIMEOUT = 12
_BJ = _BEIJING

SOURCE = "fng"

# 情绪档位 (0=Extreme Fear .. 100=Extreme Greed)
_BANDS = (
    (25, "极度恐慌", "extreme_fear"),
    (45, "恐慌", "fear"),
    (55, "中性", "neutral"),
    (75, "贪婪", "greed"),
    (101, "极度贪婪", "extreme_greed"),
)


def _default_get(url: str, timeout: int = _TIMEOUT) -> Any:
    req = urllib.request.Request(url, headers={"User-Agent": "trident-v170/fng"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8", errors="replace"))


def _num(value: Any) -> Optional[float]:
    try:
        if value is None or value == "":
            return None
        return float(value)
    except (TypeError, ValueError):
        return None


def _band(value: float) -> tuple:
    for edge, label, key in _BANDS:
        if value < edge:
            return label, key
    return "极度贪婪", "extreme_greed"


def _ts_to_bj(unix_ts: Any) -> str:
    """FNG 返回 unix 时间戳, 统一换算成北京时间串。"""
    sec = _num(unix_ts)
    if sec is None:
        return ""
    return datetime.fromtimestamp(sec, tz=timezone.utc).astimezone(_BJ).strftime(
        "%Y-%m-%d %H:%M:%S"
    )


def fetch_fng(limit: int = 5, get: Callable = _default_get) -> List[Dict[str, Any]]:
    """拉取最近 limit 条 FNG。失败返回 [] (watcher 侧只打日志不中断)。"""
    try:
        n = max(1, min(int(limit or 5), 3650))
        body = get(f"{FNG_URL}?limit={n}&format=json")
    except Exception:
        return []
    if not isinstance(body, dict):
        return []
    rows = body.get("data")
    if not isinstance(rows, list):
        return []

    out: List[Dict[str, Any]] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        value = _num(row.get("value"))
        if value is None:
            continue
        out.append(
            {
                "value": value,
                "classification": str(row.get("value_classification") or "").strip(),
                "timestamp": _ts_to_bj(row.get("timestamp")),
                "raw_id": str(row.get("timestamp") or row.get("value") or ""),
            }
        )
    return out


def to_ingest_items(rows: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """转成 raw_news 入库条目。

    只保留档位切换 (相对上一条 classification 变化) 与极值档, 避免每日重复刷同一条。
    raw_news 侧按 [hash:marker] 去重, 这里再做一层语义降噪。
    """
    items: List[Dict[str, Any]] = []
    previous: Optional[str] = None
    for row in rows or []:
        value = row.get("value")
        if value is None:
            continue
        label, key = _band(float(value))
        classification = row.get("classification") or label
        is_fresh_event = True  # FNG 每日一更, 属"观测即公布", 豁免新鲜度闸
        changed = previous is not None and classification != previous
        extreme = key in ("extreme_fear", "extreme_greed")
        previous = classification

        if not (changed or extreme):
            continue

        direction = "转恐慌" if key in ("fear", "extreme_fear") else (
            "转贪婪" if key in ("greed", "extreme_greed") else "转中性"
        )
        title = f"加密恐慌贪婪指数 {int(value)}（{label}）"
        summary = (
            f"Fear & Greed Index = {int(value)}，分类「{classification}」，"
            f"较上一读数{direction}。极端情绪常伴随波动率放大与去杠杆。"
        )
        items.append(
            {
                "id": f"fng:{row.get('raw_id') or int(value)}",
                "title": title,
                "summary": summary,
                "source": SOURCE,
                "published_at": row.get("timestamp") or "",
                "fresh_event": is_fresh_event,
                "url": FNG_URL,
            }
        )
    return items