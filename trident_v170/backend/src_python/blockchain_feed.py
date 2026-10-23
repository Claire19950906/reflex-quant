#!/usr/bin/env python3
"""Blockchain.com Charts API — BTC 链上基本面（免费、免 key、直连）。

端点：https://api.blockchain.info/charts/{chart}?timespan=1week&format=json
  hash-rate   全网哈希率（算力安全），日度
  difficulty  全网挖矿难度（矿工竞争），日度

日度慢变量：仅供背景/趋势交叉验证，标注 freshness=daily → 渲染带「日度」，提醒
LLM 只作长期算力/安全趋势参考，勿当交易方向即时依据。
"""

from __future__ import annotations

import json
import time
import urllib.request
from typing import Any, Callable, Dict, List, Optional, Tuple

import source_status

HttpGet = Callable[[str, int], Any]
_TIMEOUT = 15
_RETRIES = 3  # 部分网络下偶发 SSL EOF，做有限重试（同 eia_feed / coinpaprika_feed）

_BASE = "https://api.blockchain.info/charts"

# (chart, metric_key, label, unit)
SERIES: Tuple[Tuple[str, str, str, str], ...] = (
    ("hash-rate", "btc.hashrate",    "BTC哈希率",   "TH/s"),
    ("difficulty", "btc.difficulty", "BTC挖矿难度", "T"),
)


def _default_get(url: str, timeout: int = _TIMEOUT) -> Any:
    last_exc: Optional[Exception] = None
    for attempt in range(_RETRIES):
        try:
            req = urllib.request.Request(
                url,
                headers={"User-Agent": "TridentAgentMVP/1.0", "Accept": "application/json"},
            )
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                raw = resp.read()
            data = json.loads(raw.decode("utf-8"))
            source_status.report("blockchain", True)
            return data
        except Exception as exc:
            last_exc = exc
            time.sleep(0.6 * (attempt + 1))
    source_status.report("blockchain", False, error=f"{type(last_exc).__name__}: {last_exc}")
    assert last_exc is not None
    raise last_exc


def _num(value: Any) -> Optional[float]:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if number == number else None


def _metric(
    metric_key: str,
    value: Optional[float],
    unit: str,
    *,
    label: str = "",
    extra: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    payload = extra or {}
    if label:
        payload["label"] = label
    return {
        "category": "onchain",
        "source": "blockchain",
        "metric_key": metric_key,
        "value": value,
        "unit": unit,
        "status": "ok" if value is not None else "unavailable",
        "payload": payload,
    }


def fetch_snapshot(get: HttpGet = _default_get) -> List[Dict[str, Any]]:
    """拉最新哈希率 / 难度（日度）。失败标 unavailable。"""
    rows: List[Dict[str, Any]] = []
    for chart, metric_key, label, unit in SERIES:
        url = f"{_BASE}/{chart}?timespan=1week&format=json"
        try:
            body = get(url, _TIMEOUT)
            if not isinstance(body, dict) or body.get("status") != "ok":
                raise ValueError("响应异常")
            value: Optional[float] = None
            report_date = ""
            values = body.get("values")
            if isinstance(values, list) and values:
                last = values[-1]
                if isinstance(last, dict):
                    value = _num(last.get("y"))
                    x = last.get("x")
                    if x:
                        report_date = time.strftime("%Y-%m-%d", time.gmtime(float(x)))
            if value is None:
                extra: Dict[str, Any] = {"note": "无观测", "freshness": "daily", "label": label}
                rows.append(_metric(metric_key, None, unit, label=label, extra=extra))
            else:
                extra = {"report_date": report_date, "freshness": "daily", "label": label}
                rows.append(_metric(metric_key, value, unit, label=label, extra=extra))
        except Exception as exc:
            extra = {"note": f"{type(exc).__name__}", "freshness": "daily", "label": label}
            rows.append(_metric(metric_key, None, unit, label=label, extra=extra))
    return rows