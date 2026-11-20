"""surprise_factor — 预期差（actual vs forecast）进判断链（v342）

背景（v342 判断层 #⑤）：
    `econ_calendar` 早就在存 `actual / forecast / previous`，但判断链从没读过。
    CPI / 非农这类事件的方向**由"超预期幅度"决定**，而新闻标题情绪会把
    超预期 0.1 和超预期 2.0 当成同一件事。

设计取舍（重要）：
    预期差**不给方向**，只给"反方证据"。原因：同一个超预期对资产的方向
    取决于指标与当时的政策反应函数（CPI 热 → 加息预期 ↑ → 风险资产偏空；
    失业率上行 → 宽松预期 ↑ → 风险资产偏多），把符号写死等于制造假精度。
    所以本模块只输出：
        · norm       超预期幅度（-1..+1，按"预期与前值的典型变化"归一）
        · polarity   指标 → 该资产方向（表内已知指标才给，未知一律 None）
        · conflict   预期差方向与新闻方向是否相反
    判断闸门据此**只做减分/降置信**，不新增方向权重。

取不到就不可得：缺 actual / forecast 一律 available=False，不猜。
"""
from __future__ import annotations

import re
import sqlite3
from typing import Any, Dict, Optional

# ── 指标极性表 ──────────────────────────────────────────────────────────
# 含义：actual 高于 forecast 时，该资产类别的方向（+1 偏多 / -1 偏空）
# 只登记口径清楚的指标；不认识的关键词一律 None（不给意见）。
_POLARITY: Dict[str, Dict[str, int]] = {
    # 通胀：超预期 → 加息预期升温 → 风险资产/黄金承压，美元偏强
    "cpi": {"RISK": -1, "XAU": -1, "USD": +1},
    "ppi": {"RISK": -1, "XAU": -1, "USD": +1},
    "pce": {"RISK": -1, "XAU": -1, "USD": +1},
    "通胀": {"RISK": -1, "XAU": -1, "USD": +1},
    "物价": {"RISK": -1, "XAU": -1, "USD": +1},
    # 就业：非农/ADP 超预期 → 紧缩预期 → 风险资产承压
    "非农": {"RISK": -1, "XAU": -1, "USD": +1},
    "nonfarm": {"RISK": -1, "XAU": -1, "USD": +1},
    "payroll": {"RISK": -1, "XAU": -1, "USD": +1},
    "adp": {"RISK": -1, "XAU": -1, "USD": +1},
    # 失业率：超预期 = 就业转弱 → 宽松预期 → 风险资产偏多
    "失业率": {"RISK": +1, "XAU": +1, "USD": -1},
    "unemployment": {"RISK": +1, "XAU": +1, "USD": -1},
    # 增长：超预期 → 风险偏好回升
    "gdp": {"RISK": +1, "XAU": -1, "USD": +1},
    "pmi": {"RISK": +1, "XAU": -1, "USD": +1},
    "零售": {"RISK": +1, "XAU": -1, "USD": +1},
    "retail": {"RISK": +1, "XAU": -1, "USD": +1},
    "工业产出": {"RISK": +1, "XAU": -1, "USD": +1},
}

_ASSET_CLASS = {
    "BTC": "RISK", "ETH": "RISK", "SOL": "RISK", "COIN": "RISK", "MSTR": "RISK",
    "NVDA": "RISK", "TSLA": "RISK", "AAPL": "RISK", "MSFT": "RISK", "AMZN": "RISK",
    "GOOGL": "RISK", "META": "RISK", "PLTR": "RISK", "SMCI": "RISK",
    "CN_STOCK": "RISK", "US_STOCK": "RISK", "HK_STOCK": "RISK",
    "XAU": "XAU", "XAG": "XAU",
    "USD": "USD", "CNY": "USD", "EUR": "USD", "JPY": "USD",
}

_MULT = {"k": 1e3, "m": 1e6, "b": 1e9, "万": 1e4, "亿": 1e8, "万亿": 1e12}

_NUM_RE = re.compile(r"[-+−]?\d+(?:[.,]\d+)*\s*(?:%|k|m|b|万|亿|万亿)?", re.IGNORECASE)


def parse_number(text: Any) -> Optional[float]:
    """把 '2.5%' / '150K' / '1.2M' / '(1.3)' / '—' 解析成 float。

    解析不出（N/A、—、空、纯文本）→ None。括号视为负数（财经表格惯例）。
    """
    if text is None:
        return None
    if isinstance(text, (int, float)):
        try:
            return float(text)
        except (TypeError, ValueError):
            return None
    raw = str(text).strip()
    if not raw or raw in {"—", "-", "N/A", "n/a", "NA", "None", "null"}:
        return None
    neg = False
    if raw.startswith("(") and raw.endswith(")"):
        neg, raw = True, raw[1:-1].strip()
    m = _NUM_RE.search(raw)
    if not m:
        return None
    token = m.group(0).replace(" ", "").replace(",", "").replace("−", "-")
    unit = ""
    for u in ("万亿", "亿", "万", "K", "k", "M", "m", "B", "b"):
        if token.endswith(u):
            unit, token = u, token[: -len(u)]
            break
    if token.endswith("%"):
        token = token[:-1]
    try:
        val = float(token)
    except ValueError:
        return None
    if unit:
        val *= _MULT.get(unit.lower(), 1.0)
    return -val if neg else val


def polarity(indicator: Any, asset: str) -> Optional[int]:
    """指标 + 资产 → 「超预期」对该资产的方向。不认识就 None（不给意见）。"""
    text = str(indicator or "").lower()
    if not text:
        return None
    cls = _ASSET_CLASS.get(str(asset or "").upper())
    if not cls:
        return None
    for key, table in _POLARITY.items():
        if key.lower() in text:
            return table.get(cls)
    return None


def compute(actual: Any, forecast: Any, previous: Any = None,
            *, indicator: str = "", asset: str = "") -> Dict[str, Any]:
    """算预期差。返回 {available, deviation, norm, polarity, detail}。"""
    a = parse_number(actual)
    f = parse_number(forecast)
    if a is None or f is None:
        return {"available": False, "deviation": None, "norm": 0.0,
                "polarity": None, "detail": "缺 actual/forecast，不可得"}
    dev = a - f
    p = parse_number(previous)
    # 归一尺度：优先「预期 vs 前值」的典型变化；退化到 |forecast| 的 5%
    scale = None
    if p is not None and abs(f - p) > 1e-12:
        scale = abs(f - p)
    if scale is None or scale <= 1e-12:
        scale = max(abs(f) * 0.05, 1e-9)
    norm = max(-1.0, min(1.0, dev / scale))
    pol = polarity(indicator, asset)
    detail = (f"actual={a:g} forecast={f:g}"
              + (f" prev={p:g}" if p is not None else "")
              + f" → 偏离 {dev:+g}（normalized {norm:+.2f}）")
    return {"available": True, "deviation": round(dev, 6),
            "norm": round(norm, 4), "polarity": pol,
            "indicator": str(indicator or ""), "detail": detail}


def find_event(conn: sqlite3.Connection, *, when_ts: Optional[int],
               title: str = "", asset: str = "", window_sec: int = 1800
               ) -> Optional[Dict[str, Any]]:
    """在 econ_events 里找与这条新闻最接近的事件（窗口内、按标题关键词）。"""
    if not title:
        return None
    keyword = None
    lower = str(title).lower()
    for key in _POLARITY:
        if key.lower() in lower:
            keyword = key
            break
    if not keyword:
        return None
    try:
        if when_ts:
            rows = conn.execute(
                "SELECT country, title, ts, impact, actual, forecast, previous "
                "FROM econ_events WHERE ts BETWEEN ? AND ? "
                "ORDER BY ABS(ts - ?) ASC LIMIT 12",
                (int(when_ts) - window_sec, int(when_ts) + window_sec, int(when_ts)),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT country, title, ts, impact, actual, forecast, previous "
                "FROM econ_events ORDER BY ts DESC LIMIT 12").fetchall()
    except sqlite3.Error:
        return None
    for r in rows:
        if keyword.lower() in str(r[1] or "").lower():
            return {"country": r[0], "title": r[1], "ts": r[2], "impact": r[3],
                    "actual": r[4], "forecast": r[5], "previous": r[6]}
    return None


def enrich(conn: sqlite3.Connection, row: Dict[str, Any], *,
           window_sec: int = 1800) -> Dict[str, Any]:
    """给一条决策行注入 `surprise`（找不到事件就标不可得，不编造）。

    调用方拿到返回值后自行决定是否放进 decision row 的 `surprise` 字段。
    """
    title = (row.get("news_title") or row.get("title") or "")
    asset = str(row.get("target_asset") or row.get("asset") or "").upper()
    when = row.get("news_ts") or row.get("decision_ts")
    event = find_event(conn, when_ts=when, title=title, asset=asset,
                       window_sec=window_sec)
    if not event:
        return {"available": False, "detail": "窗口内无匹配的宏观事件", "norm": 0.0}
    out = compute(event.get("actual"), event.get("forecast"),
                  event.get("previous"), indicator=event.get("title", ""),
                  asset=asset)
    out["event"] = {"title": event.get("title"), "ts": event.get("ts"),
                    "country": event.get("country")}
    return out


def enrich_by_news_id(conn: sqlite3.Connection, news_id: Any, asset: str,
                      *, window_sec: int = 1800) -> Dict[str, Any]:
    """按新闻 id 从 raw_news 取内容+时间, 再 enrich（给 ai_worker 调用，少传参）。"""
    try:
        nid = int(news_id)
    except (TypeError, ValueError):
        return {"available": False, "detail": "news_id 非法", "norm": 0.0}
    try:
        row = conn.execute(
            "SELECT content, ts FROM raw_news WHERE id=?", (nid,)).fetchone()
    except sqlite3.Error:
        return {"available": False, "detail": "读 raw_news 失败", "norm": 0.0}
    if not row:
        return {"available": False, "detail": "news 不存在", "norm": 0.0}
    return enrich(conn, {"news_title": row[0] or "", "news_ts": row[1],
                         "target_asset": asset}, window_sec=window_sec)


def conflict_with_action(surprise: Dict[str, Any], action: str) -> bool:
    """预期差方向与决策动作是否相反（只在有极性、幅度够大时才判）。"""
    if not surprise or not surprise.get("available"):
        return False
    pol = surprise.get("polarity")
    if pol is None:
        return False
    act = str(action or "").upper()
    if act not in ("BUY", "SELL"):
        return False
    want = 1 if act == "BUY" else -1
    sign = 1 if (surprise.get("norm") or 0) > 0 else -1
    return sign * pol * want < 0
