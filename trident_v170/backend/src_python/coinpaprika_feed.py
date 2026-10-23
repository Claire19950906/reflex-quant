#!/usr/bin/env python3
"""CoinPaprika 加密市场全局指标接入（免费、免 key、直连）。

端点：https://api.coinpaprika.com/v1/global

用途：资金轮动维度 —— BTC 市占率（dominance）上升=资金回流 BTC / 山寨走弱，
      下降=风险偏好激进取向山寨；加密总市值 + 24h 成交量 = 市场活跃度与流动性。
实时快照（分钟级更新），非滞后数据，故不标「周度/日度」。

法宝：无 key；CoinPaprika 免费限速约每秒多次，本层仅在宏观上下文构建时拉一次，
      远低于限速。
"""

from __future__ import annotations

import json
import time
import urllib.request
from typing import Any, Callable, Dict, List, Optional

import source_status

HttpGet = Callable[[str, int], Any]
_TIMEOUT = 15
_RETRIES = 3  # 部分网络下偶发 SSL EOF，做有限重试（详见 eia_feed 同款处理）

_GLOBAL_URL = "https://api.coinpaprika.com/v1/global"


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
            source_status.report("coinpaprika", True)
            return data
        except Exception as exc:
            last_exc = exc
            time.sleep(0.6 * (attempt + 1))
    source_status.report("coinpaprika", False, error=f"{type(last_exc).__name__}: {last_exc}")
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
        "category": "rotation",
        "source": "coinpaprika",
        "metric_key": metric_key,
        "value": value,
        "unit": unit,
        "status": "ok" if value is not None else "unavailable",
        "payload": payload,
    }


def fetch_snapshot(get: HttpGet = _default_get) -> List[Dict[str, Any]]:
    """拉加密全局指标（BTC 市占率 / 总市值 / 24h 量）。失败标 unavailable。"""
    try:
        body = get(_GLOBAL_URL, _TIMEOUT)
        if not isinstance(body, dict):
            raise ValueError("非 JSON 对象")
        mcap = _num(body.get("market_cap_usd"))
        vol = _num(body.get("volume_24h_usd"))
        dom = _num(body.get("bitcoin_dominance_percentage"))
        extra: Dict[str, Any] = {"label": "加密"}
        return [
            _metric("crypto.btc_dominance", dom, "%", label="加密", extra=extra),
            _metric("crypto.total_mcap", mcap, "usd", label="加密", extra=extra),
            _metric("crypto.vol_24h", vol, "usd", label="加密", extra=extra),
        ]
    except Exception as exc:
        note = f"{type(exc).__name__}"
        return [
            _metric("crypto.btc_dominance", None, "%", label="加密", extra={"note": note}),
            _metric("crypto.total_mcap", None, "usd", label="加密", extra={"note": note}),
            _metric("crypto.vol_24h", None, "usd", label="加密", extra={"note": note}),
        ]