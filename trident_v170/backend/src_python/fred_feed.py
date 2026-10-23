#!/usr/bin/env python3
"""FRED (St. Louis Fed) 官方宏观指标接入。

FRED 没有新闻快讯，只有官方统计数据（CPI/非农/利率/M2 等）。两个用途：

1. 宏观背景层（macro_context 的 fred 层）— fetch_snapshot() 拉最新指标，
   供 LLM 分析新闻时参考官方宏观数字；失败标 unavailable，禁止造数。

2. 数据发布事件（新闻流）— release_items() 检测新发布的观测日期，生成
   "数据发布"事件交给新闻管道入库（source='fred'）。事件 id 含 series+日期，
   新闻管道按 [hash:] 前缀去重：同 series 同日期只入库一次，新数据发布
   （观测日期变化）自动生成新事件。

API: GET {FRED_BASE_URL}/fred/series/observations?series_id=...&api_key=...
免费 Key 申请: https://fred.stlouisfed.org/docs/api/api_key.html
未配置 FRED_API_KEY 时：快照标 unavailable，不生成发布事件，不影响其它功能。
"""

from __future__ import annotations

import json
import urllib.error  # noqa: F401  (re-exported style, kept for parity with macro_context)
import urllib.request
from typing import Any, Callable, Dict, List, Optional, Tuple

import config
import source_status

HttpGet = Callable[[str, int], Any]
_TIMEOUT = 12

# (series_id, metric_key, 显示名, 单位, 是否算同比%, impact)
# impact: high=即时影响行情（CPI/非农/利率/美债，发布事件进新闻流并高相关）；
#         medium=滞后/低影响（仅作宏观背景，不生成发布事件，避免刷屏）。
# 同比类取 13 个观测点（当前 + 12 个月前）；日度/水平类只用最新值。
SERIES: Tuple[Tuple[str, str, str, str, bool, str], ...] = (
    ("CPIAUCSL",  "cpi_yoy",        "美国 CPI 同比",           "%",   True,  "high"),
    ("CPILFESL",  "core_cpi_yoy",   "美国核心 CPI 同比",       "%",   True,  "high"),
    ("PAYEMS",    "nonfarm_payrolls", "美国非农就业人数",      "千人", False, "high"),
    ("UNRATE",    "unrate",         "美国失业率",              "%",   False, "high"),
    # v2026-10-08: 加入 ICSA 周度观测供发现页周度观察面板使用
    # medium=仅宏观背景不生成发布事件防刷屏（每周四固定发布，新闻侧已有就业日历）
    ("ICSA",      "icsa",           "美国初请失业金(周度)",    "千人", False, "medium"),
    ("DFF",       "effr_fred",      "美国有效联邦基金利率",    "%",   False, "high"),
    ("DGS10",     "dgs10",          "美国 10Y 美债收益率",     "%",   False, "high"),
    ("T10Y2Y",    "t10y2y",         "美国 10Y-2Y 期限利差",    "%",   False, "high"),
    ("PPIFID",    "ppi_yoy",        "美国 PPI 同比",           "%",   True,  "high"),
    ("M2SL",      "m2_yoy",         "美国 M2 货币供应同比",    "%",   True,  "medium"),
    # ── 跨资产资金/避险关键指标（日度；medium=仅宏观背景，不生成发布事件防刷屏）──
    ("DFII10",    "tips10y",        "美国 10Y TIPS 实际利率",  "%",   False, "medium"),
    ("DTWEXBGS",  "dxy",            "美元名义广义指数(DXY)",   "",    False, "medium"),
    ("VIXCLS",    "vix",            "VIX 恐慌指数",            "",    False, "medium"),
    # ── 流动性/套息叠加（日度/周度；medium=仅宏观背景，不生成发布事件）──
    ("SOFR",         "sofr",      "美国 SOFR 利率",        "%",   False, "medium"),
    ("RRPONTSYD",    "rrp_rate",  "美联储隔夜逆回购利率",  "%",   False, "medium"),
    ("WTREGEN",      "tga",       "美国财政部 TGA 余额",   "百万美元", False, "medium"),
    ("IRLTLT01JPM156N", "jgb10y", "日本 10Y 国债收益率",   "%",   False, "medium"),
    # ── 全网跨资产资金/价格基准（日度；medium=仅宏观背景，不生成发布事件防刷屏）──
    ("NASDAQCOM",    "nasdaq",        "纳斯达克综合指数",        "",     False, "medium"),
    ("SP500",        "sp500",         "标普500指数",             "",     False, "medium"),
    ("DGS2",         "dgs2",          "美国 2Y 国债收益率",       "%",    False, "medium"),
    ("DGS5",         "dgs5",          "美国 5Y 国债收益率",       "%",    False, "medium"),
    ("DGS30",        "dgs30",         "美国 30Y 国债收益率",      "%",    False, "medium"),
    ("BAA10Y",       "baa10y",        "穆迪 Baa 企业债收益率",    "%",    False, "medium"),
    ("BAMLH0A0HYM2", "hy_oas",        "美国高收益债利差(OAS)",    "%",    False, "medium"),
    ("DEXUSEU",      "usdeur",        "美元兑欧元汇率",           "",     False, "medium"),
    ("DEXUSUK",      "usdgbp",        "美元兑英镑汇率",           "",     False, "medium"),
    ("DEXCAUS",      "usdcad",        "美元兑加元汇率",           "",     False, "medium"),
    ("DEXJPUS",      "usdjpy",        "美元兑日元汇率",           "",     False, "medium"),
    ("DCOILWTICO",   "wti_spot",      "WTI 原油现货价",          "美元/桶", False, "medium"),
    ("GOLDAMGBD228NLBM", "gold_fixing", "伦敦金下午定盘价",      "美元/盎司", False, "medium"),
    ("T10YIE",        "t10yie",        "美国 10Y 通胀预期(盈亏平衡)", "%",  False, "medium"),
    # ── 杠杆资金/保证金债务（美联储 Z.1 金融账户「证券商对客保证金贷款」，季度滞后）──
    ("BOGZ1FL663067003Q", "margin_debt", "美股保证金债务(杠杆资金)", "百万美元", False, "medium"),
    # ── 美股现货总市值（美联储 Z.1「国内公开股权负债市值」，季度滞后，作杠杆/现货占比分母）──
    ("BOGZ1LM883164115Q", "us_equity_mcap", "美股总市值(公开股权)", "百万美元", False, "medium"),
)

_OBS_LIMIT = 14  # 月度指标取 14 个点（同比用 obs[0]/obs[12]，趋势对比用 obs[1]/obs[13]）

# ── 趋势检测阈值（即时行情之外的宏观趋势变化） ──
# 这些指标对行情是中长期影响，按"显著变化"触发趋势事件，不随发布刷屏。
_TREND_T10Y2Y_DEEPEN_PCT = 0.10   # 期限利差倒挂加深 ≥ 0.10% 或状态翻转（倒挂↔非倒挂）
_TREND_DGS10_MOVE_PCT = 0.15      # 10Y 美债收益率 5 个交易日变动 ≥ ±0.15%
_TREND_M2_ACCEL_PCT = 0.50        # M2 同比加速/减速 ≥ 0.5%（月度）


def _default_get(url: str, timeout: int = _TIMEOUT) -> Any:
    try:
        req = urllib.request.Request(
            url,
            headers={"User-Agent": "TridentAgentMVP/1.0", "Accept": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read()
        data = json.loads(raw.decode("utf-8"))
        source_status.report("fred", True)
        return data
    except Exception as exc:
        source_status.report("fred", False, error=f"{type(exc).__name__}: {exc}")
        raise


def _num(value: Any) -> Optional[float]:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if number == number else None


def _observations_url(series_id: str, api_key: str) -> str:
    return (
        f"{config.FRED_BASE_URL}/fred/series/observations"
        f"?series_id={series_id}&api_key={api_key}&file_type=json"
        f"&sort_order=desc&limit={_OBS_LIMIT}"
    )


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
        "category": "fred",
        "source": "fred",
        "metric_key": metric_key,
        "value": value,
        "unit": unit,
        "status": "ok" if value is not None else "unavailable",
        "payload": payload,
    }


def _clean_observations(body: Any) -> List[Dict[str, Any]]:
    if not isinstance(body, dict):
        return []
    obs = body.get("observations") or []
    if not isinstance(obs, list):
        return []
    return [
        item for item in obs
        if isinstance(item, dict) and str(item.get("value") or "").strip() not in ("", ".")
    ]


def fetch_snapshot(get: HttpGet = _default_get) -> List[Dict[str, Any]]:
    """拉全部 FRED 指标最新值（同比类自动算 YoY%）。无 Key 或失败 → unavailable。"""
    api_key = config.FRED_API_KEY
    if not api_key:
        source_status.report("fred", False, error="未配置 FRED_API_KEY")
        return [_metric("fred_unconfigured", None, "", label="FRED", extra={"note": "未配置 FRED_API_KEY"})]

    rows: List[Dict[str, Any]] = []
    for series_id, metric_key, label, unit, need_yoy, impact in SERIES:
        try:
            obs = _clean_observations(get(_observations_url(series_id, api_key), _TIMEOUT))
            if not obs:
                rows.append(_metric(metric_key, None, unit, label=label, extra={"note": "无观测"}))
                continue
            latest = obs[0]
            value = _num(latest.get("value"))
            date = str(latest.get("date") or "")
            extra: Dict[str, Any] = {"observation_date": date}
            if need_yoy:
                # 同比只需 13 个点（obs[0] 最新 + obs[12] 12 个月前）
                prev = _num(obs[12].get("value")) if len(obs) >= 13 else None
                if value is not None and prev:
                    value = (value / prev - 1.0) * 100.0
                    extra["yoy_base"] = prev
                else:
                    extra["note"] = "样本不足无法算同比"
                    value = None
            # 季度系列特例：保证金债务的同比基准是 obs[4]（4 个季度前，而非 obs[12]=3 年前），
            # 环比基准是 obs[1]（上一季度）；两者放 payload 供前端展示杠杆扩张/收缩与八段式引用。
            if metric_key == "margin_debt" and value is not None:
                extra["freshness"] = "quarterly"
                qoq = _num(obs[1].get("value")) if len(obs) >= 2 else None
                yoy = _num(obs[4].get("value")) if len(obs) >= 5 else None
                if qoq:
                    extra["prev"] = qoq
                    extra["qoq"] = round((value / qoq - 1.0) * 100.0, 2)
                if yoy:
                    extra["yoy"] = round((value / yoy - 1.0) * 100.0, 2)
            rows.append(_metric(metric_key, value, unit, label=label, extra=extra))
        except Exception as exc:
            rows.append(_metric(metric_key, None, unit, label=label, extra={"note": f"{type(exc).__name__}"}))
    return rows


def _render_release(
    series_id: str,
    label: str,
    unit: str,
    value: float,
    prev_value: Optional[float],
    date: str,
    need_yoy: bool,
) -> Tuple[str, str]:
    """生成数据发布事件的标题与摘要（不含 [hash:] 前缀，由新闻管道统一生成）。"""
    if need_yoy and prev_value is not None:
        change = (value / prev_value - 1.0) * 100.0
        title = f"[FRED发布] {label} 同比 {change:+.1f}%（观测日 {date}）"
        summary = f"FRED 官方数据（series={series_id}）：最新 {value:.1f}，同比 {change:+.1f}%，一年前 {prev_value:.1f}。"
    elif unit == "%":
        title = f"[FRED发布] {label} {value:.2f}%（观测日 {date}）"
        summary = f"FRED 官方数据（series={series_id}）：最新 {value:.2f}%。"
    else:
        title = f"[FRED发布] {label} {value:,.0f} {unit}（观测日 {date}）"
        summary = f"FRED 官方数据（series={series_id}）：最新 {value:,.0f} {unit}。"
    return title, summary


_RELEASE_SERIES = tuple(s[0] for s in SERIES if s[5] == "high")
_TREND_SERIES = ("T10Y2Y", "DGS10", "M2SL")


def _fetch_series_map(get: HttpGet, series_ids, api_key: str) -> Dict[str, List[Dict[str, Any]]]:
    """一次拉取多个 series 的最新观测，返回 {series_id: observations}（失败 → 空列表）。"""
    cache: Dict[str, List[Dict[str, Any]]] = {}
    for sid in series_ids:
        try:
            cache[sid] = _clean_observations(get(_observations_url(sid, api_key), _TIMEOUT))
        except Exception:
            cache[sid] = []
    return cache


def _build_releases(cache: Dict[str, List[Dict[str, Any]]]) -> List[Dict[str, Any]]:
    """从 series→observations 映射构建发布事件（不发起网络请求）。"""
    items: List[Dict[str, Any]] = []
    event_impact = str(getattr(config, "FRED_EVENT_IMPACT", "high") or "high").lower()
    for series_id, metric_key, label, unit, need_yoy, impact in SERIES:
        # 默认只对即时影响行情的指标生成发布事件；medium 指标仅作宏观背景
        if event_impact == "high" and impact != "high":
            continue
        obs = cache.get(series_id) or []
        if not obs:
            continue
        latest = obs[0]
        value = _num(latest.get("value"))
        date = str(latest.get("date") or "")
        if value is None or not date:
            continue
        prev_value = _num(obs[12].get("value")) if need_yoy and len(obs) >= 13 else None
        title, summary = _render_release(series_id, label, unit, value, prev_value, date, need_yoy)
        items.append({
            "id": f"fred-{series_id}-{date}",
            "source": "fred",
            "title": title,
            "summary": summary,
            # 官方发布检测事件：观测到新数据即生成（检测到发布=新鲜）。
            # date 是数据观测日（月度指标天然滞后一个月），非发布时间 →
            # 不能作新鲜度依据，入库时豁免 3h 闸（fresh_event=True）。
            "fresh_event": True,
        })
    return items


def release_items(get: HttpGet = _default_get) -> List[Dict[str, Any]]:
    """数据发布检测（独立调用版，测试/兼容用）：返回新发布的 FRED 指标事件。

    事件 id = f"fred-{series_id}-{observation_date}"，新闻管道据此生成
    [hash:] 前缀去重 —— 同 series 同观测日期只入库一次；观测日期变化
    （新数据发布）自动产生新事件。未配置 Key 或拉取失败 → 空列表。
    """
    api_key = config.FRED_API_KEY
    if not api_key:
        source_status.report("fred", False, error="未配置 FRED_API_KEY")
        return []
    # impact=all 时拉取全部 series（含 M2SL 等 medium）；默认只拉 high
    event_impact = str(getattr(config, "FRED_EVENT_IMPACT", "high") or "high").lower()
    ids = [s[0] for s in SERIES] if event_impact == "all" else list(_RELEASE_SERIES)
    cache = _fetch_series_map(get, ids, api_key)
    return _build_releases(cache)


def _trend_event(series_id: str, metric_key: str, title: str, summary: str, date: str) -> Dict[str, Any]:
    return {
        "id": f"fred-trend-{series_id}-{date}",
        "source": "fred",
        "title": title,
        "summary": summary,
        # 趋势事件同样基于最新观测（date 为观测日，非发布时间）→ 豁免入库新鲜度闸
        "fresh_event": True,
    }


def _build_trends(cache: Dict[str, List[Dict[str, Any]]]) -> List[Dict[str, Any]]:
    """从 series→observations 映射构建趋势事件（不发起网络请求）。

    触发规则：
      T10Y2Y  期限利差状态翻转（倒挂↔非倒挂）或倒挂加深 ≥0.10%
      DGS10   10Y 美债收益率 5 个交易日变动 ≥ ±0.15%
      M2SL    M2 同比加速/减速 ≥0.5%（月度）
    """
    items: List[Dict[str, Any]] = []

    # ── T10Y2Y 期限利差：状态翻转或倒挂加深 ──
    obs = cache.get("T10Y2Y") or []
    if len(obs) >= 3:
        latest = _num(obs[0].get("value"))
        prev = _num(obs[1].get("value"))
        date = str(obs[0].get("date") or "")
        if latest is not None and prev is not None and date:
            crossed = (latest >= 0) != (prev >= 0)
            deepening = latest < 0 and prev < 0 and (prev - latest) >= _TREND_T10Y2Y_DEEPEN_PCT
            if crossed:
                state = "转为倒挂（衰退预警）" if latest < 0 else "由倒挂转正（衰退风险缓解）"
                title = f"[FRED趋势] 美国 10Y-2Y 期限利差{state}：{latest:+.2f}%（前值 {prev:+.2f}%）"
                summary = f"FRED 数据（T10Y2Y）：最新 {latest:+.2f}%，前值 {prev:+.2f}%，期限利差方向反转，对美债/美元/黄金影响中长期。"
                items.append(_trend_event("T10Y2Y", "t10y2y_cross", title, summary, date))
            elif deepening:
                title = f"[FRED趋势] 美国 10Y-2Y 期限利差倒挂加深至 {latest:.2f}%（前值 {prev:.2f}%）"
                summary = f"FRED 数据（T10Y2Y）：倒挂较前值加深，衰退信号增强，对风险资产（BTC/原油）中长期偏空。"
                items.append(_trend_event("T10Y2Y", "t10y2y_deepen", title, summary, date))

    # ── DGS10 10Y 美债收益率：5 个交易日快速移动 ──
    obs = cache.get("DGS10") or []
    if len(obs) >= 6:
        latest = _num(obs[0].get("value"))
        base = _num(obs[5].get("value"))
        date = str(obs[0].get("date") or "")
        if latest is not None and base is not None and date:
            change = latest - base
            if change >= _TREND_DGS10_MOVE_PCT:
                title = f"[FRED趋势] 美国 10Y 美债收益率快速上行 {change:+.2f}% 至 {latest:.2f}%，利率压力上升"
                summary = f"FRED 数据（DGS10）：5 个交易日上行 {change:+.2f}%，压制黄金与风险资产估值。"
                items.append(_trend_event("DGS10", "dgs10_rise", title, summary, date))
            elif change <= -_TREND_DGS10_MOVE_PCT:
                title = f"[FRED趋势] 美国 10Y 美债收益率快速下行 {change:+.2f}% 至 {latest:.2f}%，宽松预期升温"
                summary = f"FRED 数据（DGS10）：5 个交易日下行 {change:+.2f}%，利好黄金与风险资产。"
                items.append(_trend_event("DGS10", "dgs10_fall", title, summary, date))

    # ── M2SL M2 同比：加速/减速（月度） ──
    obs = cache.get("M2SL") or []
    if len(obs) >= _OBS_LIMIT:
        latest_yoy = _num(obs[0].get("value"))
        prev_yoy = _num(obs[1].get("value"))
        date = str(obs[0].get("date") or "")
        if latest_yoy is not None and prev_yoy is not None and date:
            # obs 为月度 index 水平值（十亿美元），obs[12]=12 个月前、obs[13]=13 个月前
            base_now = _num(obs[12].get("value"))
            base_prev = _num(obs[13].get("value"))
            if base_now and base_prev:
                latest_yoy = (latest_yoy / base_now - 1.0) * 100.0
                prev_yoy = (prev_yoy / base_prev - 1.0) * 100.0
                delta = latest_yoy - prev_yoy
                if delta >= _TREND_M2_ACCEL_PCT:
                    title = f"[FRED趋势] 美国 M2 货币供应同比加速至 {latest_yoy:+.1f}%（前值 {prev_yoy:+.1f}%）"
                    summary = f"FRED 数据（M2SL）：流动性扩张加速，对加密与风险资产中长期偏多。"
                    items.append(_trend_event("M2SL", "m2_accel", title, summary, date))
                elif delta <= -_TREND_M2_ACCEL_PCT:
                    title = f"[FRED趋势] 美国 M2 货币供应同比减速至 {latest_yoy:+.1f}%（前值 {prev_yoy:+.1f}%）"
                    summary = f"FRED 数据（M2SL）：流动性扩张放缓，对风险资产中长期偏空。"
                    items.append(_trend_event("M2SL", "m2_slow", title, summary, date))

    return items


def trend_items(get: HttpGet = _default_get) -> List[Dict[str, Any]]:
    """宏观趋势变化检测（独立调用版，测试/兼容用）。

    事件 id 含 series+观测日期，新闻管道按 [hash:] 去重；
    未配置 Key 或拉取失败 → 空列表。
    """
    api_key = config.FRED_API_KEY
    if not api_key:
        source_status.report("fred", False, error="未配置 FRED_API_KEY")
        return []
    cache = _fetch_series_map(get, _TREND_SERIES, api_key)
    return _build_trends(cache)


def check_once(get: HttpGet = _default_get) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    """一次轮询：合并拉取发布+趋势所需的全部 series（T10Y2Y/DGS10 发布与趋势共用），
    返回 (发布事件列表, 趋势事件列表)。供 API 侧 10s watcher 调用，减少重复请求。
    未配置 Key 或拉取失败 → ([], [])。
    """
    api_key = config.FRED_API_KEY
    if not api_key:
        source_status.report("fred", False, error="未配置 FRED_API_KEY")
        return [], []
    ids = list(dict.fromkeys(tuple(_RELEASE_SERIES) + tuple(_TREND_SERIES)))
    cache = _fetch_series_map(get, ids, api_key)
    return _build_releases(cache), _build_trends(cache)
