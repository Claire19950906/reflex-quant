# -*- coding: utf-8 -*-
"""BLS (U.S. Bureau of Labor Statistics) 美国劳工统计局官方数据接入。

与 FRED 同模式、但数据更接近发布时刻（CPI/非农/失业率一手官方源）：

1. 宏观背景层 — fetch_snapshot() 拉最新官方指标（macro_context 可选挂 fred 同级）；
2. 数据发布事件（新闻流）— check_once() 检测最新 (year, period) 变化，生成
   "[BLS发布]" 事件交给新闻管道入库（source='bls'）。事件 id 含 series+年月，
   新闻管道按 [hash:] 前缀去重：同 series 同 period 只入库一次。

API: POST {BLS_BASE_URL}/publicAPI/v2/timeseries/data/
Body: {"seriesid": [...], "startyear": ..., "endyear": ..., "registrationkey": ...}
免费 Key 申请: https://data.bls.gov/registrationEngine/ （格式 XXXX-XXXX-XXXX-XXXX）
v2 免费 25 次请求/日（每请求可含最多 50 series）→ 轮询默认 1h，防烧配额。
未配置 BLS_API_KEY 时：标 unavailable、不生成事件，不影响其它功能。
"""

from __future__ import annotations

import json
import time
from typing import Any, Callable, Dict, List, Optional, Tuple

import config
import source_status

HttpPost = Callable[[Dict[str, Any], int], Any]
_TIMEOUT = 15

# (series_id, metric_key, 显示名, 单位, 是否算同比%)
# CUUR0000SA0 = 所有城市 CPI 指数（同比需 13 个月）；CES0000000001 = 非农就业人数
# LNS14000000 = 失业率
SERIES: Tuple[Tuple[str, str, str, str, bool], ...] = (
    ("CUUR0000SA0",   "cpi_yoy",         "美国 CPI 同比",          "%",   True),
    ("CES0000000001", "nonfarm_payrolls", "美国非农就业人数",       "千人", False),
    ("LNS14000000",   "unrate",           "美国失业率",             "%",   False),
)

_MONTH_CN = {1: "1月", 2: "2月", 3: "3月", 4: "4月", 5: "5月", 6: "6月",
             7: "7月", 8: "8月", 9: "9月", 10: "10月", 11: "11月", 12: "12月"}


def _num(value: Any) -> Optional[float]:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if number == number else None


def _default_post(body: Dict[str, Any], timeout: int = _TIMEOUT) -> Any:
    import requests
    resp = requests.post(
        f"{config.BLS_BASE_URL}/publicAPI/v2/timeseries/data/",
        json=body,
        headers={"Content-Type": "application/json",
                 "User-Agent": "TridentAgentMVP/1.0"},
        timeout=timeout,
    )
    resp.raise_for_status()
    return resp.json()


def _api_body(series_ids: List[str]) -> Dict[str, Any]:
    """BLS API 请求体：最近 13 个月（同比需 12 个月前对照；月度发布滞后 1 期）。"""
    now = time.localtime()
    start_year = now.tm_year - 2
    end_year = now.tm_year
    body: Dict[str, Any] = {
        "seriesid": series_ids,
        "startyear": str(start_year),
        "endyear": str(end_year),
    }
    if config.BLS_API_KEY:
        body["registrationkey"] = config.BLS_API_KEY
    return body


def _fetch_all(post: HttpPost = _default_post) -> Dict[str, List[Dict[str, Any]]]:
    """一次请求拉全部 series → {series_id: data 数组(按 year/period 升序)}。失败 → {}。"""
    if not config.BLS_API_KEY:
        return {}
    ids = [s[0] for s in SERIES]
    try:
        body = _api_body(ids)
        result = post(body, _TIMEOUT)
        series_out: Dict[str, List[Dict[str, Any]]] = {}
        if not isinstance(result, dict):
            source_status.report("bls", False, error="BLS 响应非 JSON 对象")
            return {}
        sers = ((result.get("Results") or {}).get("series")) or []
        for s in sers:
            sid = str((s or {}).get("seriesID") or "")
            data = s.get("data") or []
            if not isinstance(data, list):
                continue
            # 按 (year, period) 排序（period M01..M12；S01..S06 半年报跳过）
            def _sort_key(d: Dict[str, Any]) -> tuple:
                p = str(d.get("period") or "")
                if not p.startswith("M"):
                    return (0, 0)
                return (int(str(d.get("year") or "0") or 0), int(p[1:]))
            rows = [d for d in data if str(d.get("period") or "").startswith("M")]
            rows.sort(key=_sort_key)
            series_out[sid] = rows
        source_status.report("bls", True, detail=f"{len(series_out)} series")
        return series_out
    except Exception as exc:
        print(f"  [BLS] fetch fail: {type(exc).__name__}: {str(exc)[:100]}")
        source_status.report("bls", False, error=f"{type(exc).__name__}")
        return {}


def fetch_snapshot(post: HttpPost = _default_post) -> List[Dict[str, Any]]:
    """拉全部 BLS 指标最新值（同比类自动算 YoY%）。无 Key 或失败 → unavailable 标记。"""
    if not config.BLS_API_KEY:
        source_status.report("bls", False, error="未配置 BLS_API_KEY")
        return [{
            "category": "bls", "source": "bls", "metric_key": "bls_unconfigured",
            "value": None, "unit": "", "status": "unavailable",
            "payload": {"note": "未配置 BLS_API_KEY"},
        }]
    cache = _fetch_all(post)
    rows: List[Dict[str, Any]] = []
    for series_id, metric_key, label, unit, need_yoy in SERIES:
        data = cache.get(series_id) or []
        if not data:
            rows.append(_metric(series_id, metric_key, label, unit, None,
                                extra={"note": "无观测"}))
            continue
        latest = data[-1]
        value = _num(latest.get("value"))
        period = f"{latest.get('year')}-{latest.get('period')}"
        extra: Dict[str, Any] = {"observation_period": period}
        if need_yoy and value is not None and len(data) >= 13:
            prev = _num(data[-13].get("value"))
            if prev:
                value = (value / prev - 1.0) * 100.0
                extra["yoy_base"] = prev
            else:
                value = None
                extra["note"] = "样本不足无法算同比"
        rows.append(_metric(series_id, metric_key, label, unit, value, extra=extra))
    return rows


def _metric(series_id: str, metric_key: str, label: str, unit: str,
            value: Optional[float], *, extra: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    payload = extra or {}
    payload["series_id"] = series_id
    payload["label"] = label
    return {
        "category": "bls",
        "source": "bls",
        "metric_key": metric_key,
        "value": value,
        "unit": unit,
        "status": "ok" if value is not None else "unavailable",
        "payload": payload,
    }


def _render_release(series_id: str, label: str, unit: str, value: float,
                    prev_value: Optional[float], period: str, period_name: str) -> str:
    """生成发布事件标题（不含 [hash:] 前缀，由新闻管道统一生成）。

    口径说明：
      CPI 同比（CUUR0000SA0）→ 直接显示 yoy%（调用方已算好 value）；
      非农 CES0000000001 是就业水平（千人）→ 新闻口径是"当月净增"（= 本期 − 上期）；
      失业率 LNS14000000 → 显示水平 + 环比 bp 变化。
    """
    year = period.split("-")[0]
    month_key = period.split("-")[1]  # M07
    month_cn = _MONTH_CN.get(int(month_key[1:]), month_key)
    if series_id == "CUUR0000SA0":
        return f"[BLS发布] 美国 CPI 同比 {value:+.1f}%（{year}年{month_cn}）"
    if series_id == "LNS14000000":
        if prev_value is not None:
            delta_bp = round((value - prev_value) * 100, 1)
            delta = f"（环比 {delta_bp:+.1f}bp）" if delta_bp != 0 else "（环比持平）"
        else:
            delta = ""
        return f"[BLS发布] 美国 {year}年{month_cn}失业率 {value:.1f}%{delta}"
    if unit == "千人" and prev_value is not None:
        # 非农：显示当月净增（水平值差），这才是新闻与交易市场关注的口径
        change = round(value - prev_value, 0)
        sign = "+" if change >= 0 else ""
        return f"[BLS发布] 美国 {year}年{month_cn}非农就业 {sign}{change:,.0f} 千人（前值 {prev_value:,.0f} 千人）"
    return f"[BLS发布] {label} {value:,.0f} {unit}（{year}年{month_cn}）"


def _build_releases(cache: Dict[str, List[Dict[str, Any]]]) -> List[Dict[str, Any]]:
    """从 series→data 映射构建发布事件（不发起网络请求）。"""
    items: List[Dict[str, Any]] = []
    for series_id, metric_key, label, unit, need_yoy in SERIES:
        data = cache.get(series_id) or []
        if not data:
            continue
        latest = data[-1]
        value = _num(latest.get("value"))
        period = f"{latest.get('year')}-{latest.get('period')}"
        period_name = str(latest.get("periodName") or "")
        if value is None:
            continue
        prev_value: Optional[float] = None
        if need_yoy:
            if len(data) < 13:
                continue
            prev_yoy_base = _num(data[-13].get("value"))
            if not prev_yoy_base:
                continue
            value = (value / prev_yoy_base - 1.0) * 100.0
        elif len(data) >= 2:
            # 非农/失业率：环比上一期（水平值差 / 百分点差）
            prev_value = _num(data[-2].get("value"))
        title = _render_release(series_id, label, unit, value, prev_value, period, period_name)
        items.append({
            "id": f"bls-{series_id}-{period}",
            "source": "bls",
            "title": title,
            "summary": f"BLS 官方数据（series={series_id}，{period}）：{title}",
            # BLS period 是数据所属月份（月度发布天然滞后 1 期），非发布时间 →
            # 不能作新鲜度依据，入库时豁免 3h 闸（fresh_event=True）
            "fresh_event": True,
        })
    return items


def check_once(post: HttpPost = _default_post) -> List[Dict[str, Any]]:
    """一次轮询：拉全部 series → 发布事件列表。未配置 Key 或拉取失败 → []。"""
    if not config.BLS_API_KEY:
        source_status.report("bls", False, error="未配置 BLS_API_KEY")
        return []
    cache = _fetch_all(post)
    if not cache:
        return []
    return _build_releases(cache)
