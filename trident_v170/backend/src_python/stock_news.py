# -*- coding: utf-8 -*-
"""美股新闻（双源：Finnhub 优先 + Yahoo 兜底）→ 结构化条目，供 watcher 翻译后入库。

- Finnhub：注册免费 key（finnhub.io）→ /v1/company-news 稳定、限频宽（60 次/分）；
  .env 配 FINNHUB_API_KEY 后启用（无 key 自动走 Yahoo）。
- Yahoo：yfinance 个股新闻（备用；news 接口易限频 429，已加轮内 2s 节流）。
英文标题入库前尝试翻译（webhook 兜底链），失败保留原文——AI 仍可阅读，
且行情注入靠 [美股行情] 上下文不受影响。
"""

from __future__ import annotations

import hashlib
import time
from datetime import date, timedelta
from typing import Any, Dict, List, Optional

import requests

import source_status

# 与 stock_prices 同源的币安永续股票列表（也作为新闻轮询对象）
# v211 P1-A：拆成两层 —— 美股 ticker 走 Finnhub+Yahoo 双源（Finnhub 稳定 / Yahoo 兜底）；
# 非美股 ticker（韩股/台股）Finnhub 不覆盖，仅走 Yahoo。
STOCK_TICKERS: List[str] = [
    "NVDA", "TSLA", "AAPL", "MSTR", "MSFT", "AMZN",
    "GOOGL", "COIN", "PLTR", "META", "SMCI", "MARA", "RIOT",
]

# 非美股 ticker：仅走 Yahoo（Finnhub 不覆盖这些市场）
# 顺序：先 Yahoo ticker（用于 fetch_yahoo_news 取新闻），再 target_asset 标签（用于 summary 展示）
# 字段：ticker, market_label, target_asset_hint
_EXTENDED_STOCK_TICKERS: List[Dict[str, str]] = [
    {"ticker": "000660.KS", "market_label": "韩股", "target_asset_hint": "SKHYNIX"},  # SK海力士
    {"ticker": "005930.KS", "market_label": "韩股", "target_asset_hint": "INDIVIDUAL_STOCK"},  # 三星电子（无单独 target_asset，落 INDIVIDUAL_STOCK）
    {"ticker": "2330.TW",  "market_label": "台股", "target_asset_hint": "INDIVIDUAL_STOCK"},  # 台积电
    {"ticker": "MU",       "market_label": "美股", "target_asset_hint": "INDIVIDUAL_STOCK"},  # 美光（Finnhub 已支持，会双源）
]

# Yahoo news 每个 ticker 返回条数上限
_NEWS_LIMIT_PER_TICKER = 8

# 上一次轮询拿到的最早发布时间（Unix），下一轮只取更新的（去重 + 只进新闻）
_last_min_publish_ts: Dict[str, float] = {}

_FINNHUB = "https://finnhub.io/api/v1/company-news"
_finnhub_key: Optional[str] = None


def _finnhub_key_cached() -> Optional[str]:
    """读取 .env FINNHUB_API_KEY（进程内缓存；空则 Finnhub 不可用）。"""
    global _finnhub_key
    if _finnhub_key is None:
        try:
            import config
            _finnhub_key = str(getattr(config, "FINNHUB_API_KEY", "") or "").strip() or ""
        except Exception:
            _finnhub_key = ""
    return _finnhub_key or None


def fetch_finnhub_news(ticker: str) -> List[Dict[str, Any]]:
    """拉一只股票最近 2 天的 Finnhub 公司新闻。失败返回 []。

    v283+ 任务 5 修复：网络层异常（ConnectionResetError/Timeout/SSLError 等）
    不再笼统归类为「finnhub 坏了」，而是明确告诉面板：
      · "finnhub.io 站点被 GFW 阻断（ConnectionResetError）→ 已切 Yahoo 兜底"
    同时把该次失败标为 detail="untracked"，避免后续 fetcher 调用把
    真实异常消息覆盖成干巴巴的 "SSLError"（原本只输出异常类名）。
    """
    key = _finnhub_key_cached()
    if not key:
        return []
    try:
        today = date.today()
        start = (today - timedelta(days=2)).isoformat()
        resp = requests.get(
            _FINNHUB,
            params={"symbol": ticker, "from": start, "to": today.isoformat(), "token": key},
            timeout=10,
        )
        if resp.status_code != 200:
            source_status.report("finnhub", False, error=f"HTTP {resp.status_code}")
            return []
        items: List[Dict[str, Any]] = []
        for n in (resp.json() or [])[: _NEWS_LIMIT_PER_TICKER]:
            try:
                headline = str(n.get("headline") or "").strip()
                ts = int(n.get("datetime") or 0)
                if headline:
                    items.append({
                        "ticker": ticker,
                        "title": headline,
                        "link": str(n.get("url") or ""),
                        "publish_ts": ts,
                        "publisher": str(n.get("source") or "Finnhub"),
                        "provider": "finnhub",
                    })
            except Exception:
                continue
        source_status.report("finnhub", True, detail=f"{ticker} {len(items)} 条")
        return items
    except Exception as exc:
        # v283+ 任务 5：区分网络层异常 vs 业务错误
        # · ConnectionResetError / Timeout / SSLError / ProxyError → 网络问题（GFW/代理/DNS）
        # · 其它 → 代码 bug 或 API 变更
        import requests as _req
        if isinstance(exc, (_req.exceptions.ConnectionError, _req.exceptions.Timeout,
                            _req.exceptions.SSLError, _req.exceptions.ProxyError)):
            err_name = type(exc).__name__
            # 抓第一条内部异常（如 ConnectionResetError(54, ...)）补充信息
            inner = getattr(exc, "args", [None])[0]
            inner_msg = str(inner)[:80] if inner else ""
            msg = f"finnhub.io 网络层异常 {err_name}（{inner_msg}）→ 已切 Yahoo 兜底"
            # detail 标 untracked：避免后续 fetcher 反复覆盖成 SSLError
            source_status.report("finnhub", False, error=msg, detail="untracked — 站点 GFW 阻断，Yahoo 兜底中")
        else:
            source_status.report("finnhub", False, error=f"{type(exc).__name__}: {str(exc)[:120]}")
        return []


def fetch_yahoo_news(ticker: str) -> List[Dict[str, Any]]:
    """拉一只股票最近的 Yahoo Finance 新闻。失败返回 []（不影响其它股票）。"""
    try:
        import yfinance as yf
        ticker_obj = yf.Ticker(ticker)
        news = ticker_obj.news or []
        items: List[Dict[str, Any]] = []
        for n in news[: _NEWS_LIMIT_PER_TICKER]:
            try:
                title = str(n.get("title") or "").strip()
                ts = int(n.get("providerPublishTime") or 0)
                link = str(n.get("link") or "")
                if title:
                    items.append({
                        "ticker": ticker,
                        "title": title,
                        "link": link,
                        "publish_ts": ts,
                        "publisher": str(n.get("publisher") or ""),
                    })
            except Exception:
                continue
        return items
    except Exception:
        return []


def poll_stock_news(min_publish_ts: Optional[float] = None) -> List[Dict[str, Any]]:
    """轮询全部股票的新新闻（比 min_publish_ts 更新的）。返回结构化条目。

    Finnhub 优先（配了 key：稳定、无需节流）；无 key/失败 → Yahoo（轮内 2s 节流防 429）。
    被限时该轮静默跳过（返回已拿到的）。

    v211 P1-A：扩 ticker 列表 —— 美股走双源；韩股/台股 ticker 走 Yahoo-only
    （Finnhub 不覆盖非美股市场），并把每个 ticker 的市场标签注入 summary。
    """
    import time as _time
    finnhub_ok = _finnhub_key_cached() is not None
    if not finnhub_ok:
        source_status.report("finnhub", False, error="未配置 FINNHUB_API_KEY（走 Yahoo 兜底）")
    out: List[Dict[str, Any]] = []
    total_tickers = len(STOCK_TICKERS) + len(_EXTENDED_STOCK_TICKERS)
    for idx, ticker in enumerate(STOCK_TICKERS):
        min_ts = min_publish_ts
        if min_ts is None:
            min_ts = _last_min_publish_ts.get(ticker, 0.0)
        got: List[Dict[str, Any]] = []
        if finnhub_ok:
            got = fetch_finnhub_news(ticker)
            if not got:
                got = fetch_yahoo_news(ticker)  # Finnhub 空 → Yahoo 兜底
        else:
            got = fetch_yahoo_news(ticker)
        for item in got:
            ts = item.get("publish_ts") or 0
            if ts and ts > min_ts:
                item["news_ts"] = ts
                # v211：注入目标标签，便于后续 STOCK_NEWS 阶段正确归类
                item["target_asset_hint"] = ticker
                item["market_label"] = "美股"
                out.append(item)
        if not finnhub_ok and idx < total_tickers - 1:
            _time.sleep(2.0)  # Yahoo 轮内节流：防 news 接口限频
    # v211 P1-A：扩 ticker —— 韩股/台股 ticker 仅 Yahoo（Finnhub 不覆盖）
    for idx, ext in enumerate(_EXTENDED_STOCK_TICKERS):
        ticker = ext["ticker"]
        min_ts = min_publish_ts
        if min_ts is None:
            min_ts = _last_min_publish_ts.get(ticker, 0.0)
        # Yahoo 直接取（非美股 Finnhub 不支持）
        got = fetch_yahoo_news(ticker)
        for item in got:
            ts = item.get("publish_ts") or 0
            if ts and ts > min_ts:
                item["news_ts"] = ts
                item["target_asset_hint"] = ext["target_asset_hint"]
                item["market_label"] = ext["market_label"]
                out.append(item)
        if not finnhub_ok and (len(STOCK_TICKERS) + idx) < total_tickers - 1:
            _time.sleep(2.0)  # Yahoo 轮内节流：防 news 接口限频
    return out


def mark_polled(items: List[Dict[str, Any]]) -> None:
    """记录本轮已处理条目的时间水位（去重：下轮只取更新的）。"""
    for it in items:
        t = it.get("publish_ts") or it.get("news_ts") or 0
        ticker = str(it.get("ticker") or "")
        if ticker and t:
            _last_min_publish_ts[ticker] = max(_last_min_publish_ts.get(ticker, 0.0), float(t))


def translate_title_sync(title: str) -> str:
    """英文标题 → 中文（复用 webhook 翻译兜底链）；失败/纯中文返回原文。"""
    try:
        from engine.webhook import _translate_title_sync as _tr
        return _tr(title) or title
    except Exception:
        return title


def to_ingest_items(news_items: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """翻译标题并组装 _ingest_external_news_sync 需要的条目结构。"""
    items: List[Dict[str, Any]] = []
    for it in news_items:
        title = translate_title_sync(str(it.get("title") or ""))
        if not title.strip():
            continue
        ticker = str(it.get("ticker") or "")
        # 双源：Finnhub 优先 + Yahoo 兜底——来源名按实际 provider 写，别硬编码误导 AI
        provider = str(it.get("provider") or "").lower()
        src_name = "Finnhub" if provider == "finnhub" else "Yahoo Finance"
        # v211 P1-A：用实际市场标签（韩股/台股/美股），不再硬编码「美股」
        market_label = str(it.get("market_label") or "美股").strip()
        summary = f"[{market_label} {ticker}] 来源:{src_name}"
        pub_name = str(it.get("publisher") or "").strip()
        if pub_name and pub_name.lower() != src_name.lower():
            summary += f" ({pub_name})"
        if it.get("link"):
            summary += f" {it['link']}"
        # 平台发布时间：Finnhub/Yahoo 给的是 epoch 秒(UTC)，转北京时间可读串
        pub = _epoch_to_bj(it.get("publish_ts") or it.get("news_ts") or 0)
        # id 用稳定内容哈希（内置 hash() 受 PYTHONHASHSEED 影响跨进程随机，
        # 会导致跨重启后同一新闻的入库去重 marker 变化而重复入库）
        title_hash = hashlib.sha256(str(it.get("title")).encode("utf-8")).hexdigest()[:12]
        item = {
            "title": title.strip(),
            "summary": summary,
            # 来源按实际 provider 写，别硬编码 yahoo_stock：finnhub 源要能被
            # news_sources._normalize_source / 前端分类正确识别为 finnhub。
            "source": "finnhub" if provider == "finnhub" else "yahoo_stock",
            "id": f"{ticker}:{it.get('publish_ts') or it.get('news_ts')}:{title_hash}",
            "published_at": pub,
        }
        # v211 P1-A：target_asset_hint 透传，供 _ingest_external_news_sync 在写入 raw_news
        # 时附到 gate_main_asset / gate_assets，让后续 AI 分析 LLM 拿到正确主品种提示
        if it.get("target_asset_hint"):
            item["target_asset_hint"] = str(it["target_asset_hint"]).strip()
        items.append(item)
    return items


def _epoch_to_bj(epoch: Any) -> str:
    """epoch 秒(UTC) → 'YYYY-MM-DD HH:MM:SS'(北京时间)；0/无效 → ''。"""
    try:
        ts = float(epoch)
        if ts <= 0:
            return ""
        from datetime import datetime
        import config
        return datetime.fromtimestamp(ts, tz=config.TZ_SHANGHAI).strftime("%Y-%m-%d %H:%M:%S")
    except Exception:
        return ""
