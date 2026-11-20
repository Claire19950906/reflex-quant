#!/usr/bin/env python3
"""EIA (U.S. Energy Information Administration) 周度石油库存接入。

EIA 免费 API Key 申请：https://www.eia.gov/opendata/register.php
数据集：petroleum/stoc/wstk（美国周度库存，单位千桶 MBBL，每周三发布）

系列：
  WCESTUS1 = 美国原油商业库存（剔除 SPR）—— 市场最关注的 EIA 周度原油库存
  WGTSTUS1 = 美国汽油总库存（成品油需求代理）

用法：fetch_snapshot() 拉最新库存 + 周环比，喂给 macro_context 的 eia 层；
      失败标 unavailable、禁止造数。周度数据带 freshness=weekly，渲染时自动
      标「周度」，提醒 LLM 只作背景趋势、勿作即时方向依据。
"""

from __future__ import annotations

import json
import time
import urllib.request
from typing import Any, Callable, Dict, List, Optional, Tuple

import config
from feeds import source_status

HttpGet = Callable[[str, int], Any]
_TIMEOUT = 15

# EIA v2 API 在部分网络下偶发 SSL EOF / URLError，做有限重试再放弃（不影响其它源）。
_RETRIES = 3

# (series_id, metric_key, 显示名, 单位)
SERIES: Tuple[Tuple[str, str, str, str], ...] = (
    ("WCESTUS1", "oil.crude_stocks",     "美国原油商业库存", "千桶"),
    ("WGTSTUS1", "oil.gasoline_stocks",  "美国汽油总库存",   "千桶"),
    # v460：原油"供需"不能只有库存 —— 库存是结果，产量与开工率才是供给端的
    # 主动变量。此前只有 2 条序列，"库存降了"无法区分是"需求好"还是"炼厂少炼"。
    ("WCRFPUS2", "oil.us_crude_production", "美国原油产量",  "千桶/日"),
    ("WCRRIUS2", "oil.refinery_inputs",     "美国炼厂原油投入", "千桶/日"),
    ("WDISTUS1", "oil.distillate_stocks",   "美国馏分油库存", "千桶"),
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
            source_status.report("eia", True)
            return data
        except Exception as exc:
            last_exc = exc
            time.sleep(0.6 * (attempt + 1))
    source_status.report("eia", False, error=f"{type(last_exc).__name__}: {last_exc}")
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
        "category": "eia",
        "source": "eia",
        "metric_key": metric_key,
        "value": value,
        "unit": unit,
        "status": "ok" if value is not None else "unavailable",
        "payload": payload,
    }


# v460：产量/炼厂/馏分油不在 stoc/wstk 这条路由下（那里只有库存），必须走
# 通用 seriesid 路由（PET.<id>.W）。已验证 WCRFPUS2=14032 千桶/日（2026-11-13）。
_SERIESID_ROUTE = {
    "WCRFPUS2": "PET.WCRFPUS2.W",
    "WCRRIUS2": "PET.WCRRIUS2.W",
    "WDISTUS1": "PET.WDISTUS1.W",
}


# v460：一次取 52 周而不是 3 周，用作"历史分位"的样本（否则新序列要等半年
# 才凑够 30 个观测，期间基本面分量一直是不可得）。payload 里只放精简的
# [{"period","value"}]，不改变库表结构。
_POINTS = 52


def _series_url(series_id: str, length: int = _POINTS) -> str:
    sid = _SERIESID_ROUTE[series_id]
    return (
        f"{config.EIA_BASE_URL}/seriesid/{sid}"
        f"?api_key={config.EIA_API_KEY}&frequency=weekly&data[0]=value"
        f"&sort[0][column]=period&sort[0][direction]=desc&length={length}"
    )


def _data_url(series_id: str, length: int = _POINTS) -> str:
    return (
        f"{config.EIA_BASE_URL}/petroleum/stoc/wstk/data/"
        f"?api_key={config.EIA_API_KEY}&frequency=weekly&data[0]=value"
        f"&facets[series][]={series_id}"
        f"&sort[0][column]=period&sort[0][direction]=desc&length={length}"
    )


def _clean_points(body: Any) -> List[Dict[str, Any]]:
    if not isinstance(body, dict):
        return []
    resp = body.get("response")
    data = resp.get("data") if isinstance(resp, dict) else None
    if not isinstance(data, list):
        return []
    out: List[Dict[str, Any]] = []
    for row in data:
        if not isinstance(row, dict):
            continue
        value = _num(row.get("value"))
        period = str(row.get("period") or "")
        if value is None or not period:
            continue
        out.append({"period": period, "value": value})
    return out


def fetch_snapshot(get: HttpGet = _default_get) -> List[Dict[str, Any]]:
    """拉最新原油/汽油库存 + 周环比。无 Key 或失败 → unavailable（禁止造数）。"""
    api_key = config.EIA_API_KEY
    if not api_key:
        source_status.report("eia", False, error="未配置 EIA_API_KEY")
        return [_metric("eia_unconfigured", None, "", label="EIA", extra={"note": "未配置 EIA_API_KEY"})]

    rows: List[Dict[str, Any]] = []
    for series_id, metric_key, label, unit in SERIES:
        try:
            url = (_series_url(series_id) if series_id in _SERIESID_ROUTE
                   else _data_url(series_id))
            points = _clean_points(get(url, _TIMEOUT))
            if not points:
                unav = {"note": "无观测", "freshness": "weekly"}
                rows.append(_metric(metric_key, None, unit, label=label, extra=unav))
                rows.append(_metric(f"{metric_key}.chg", None, unit, label=label, extra=unav))
                continue
            latest = points[0]
            value = latest["value"]
            date = latest["period"]
            prev_value = points[1]["value"] if len(points) >= 2 else None
            chg = (value - prev_value) if (value is not None and prev_value is not None) else None
            extra: Dict[str, Any] = {"report_date": date, "freshness": "weekly",
                                     "history": points[:_POINTS]}
            rows.append(_metric(metric_key, value, unit, label=label, extra=extra))
            rows.append(_metric(f"{metric_key}.chg", chg, unit, label=label, extra=extra))
        except Exception as exc:
            unav = {"note": f"{type(exc).__name__}", "freshness": "weekly"}
            rows.append(_metric(metric_key, None, unit, label=label, extra=unav))
            rows.append(_metric(f"{metric_key}.chg", None, unit, label=label, extra=unav))
    return rows