# -*- coding: utf-8 -*-
"""美股行情（Yahoo Finance）——供 AI 分析美股相关新闻的价格上下文 / 模拟盘统一取价。

- 数据源：Yahoo Finance（NVDA/TSLA/AAPL/MSFT 等真实美股 ticker）。币安 fapi 无美股
  永续合约（NVDAUSDT 等均不存在），此前映射到币安导致永久 400 静默失败——已纠正。
  模拟盘对美股建仓/结算同样取此价（口径统一，对账一致）。
- 拉不到则返回 None——行情只是上下文，绝不阻塞 AI 分析。
- 限频保护：每只股票 10 分钟缓存（同股高频新闻只拉一次）。
- 迷你快照：除现价外拉 1d K 线算 7d 区间/趋势/ATR（水位博弈段的价格锚点），
  与市场快照同算法同缓存。
"""

from __future__ import annotations

import json
import re
import threading
import time
import urllib.request
from typing import Dict, Optional

import requests

import config
import source_status

# Yahoo Finance 美股行情（yfinance，国内可直连；与 stock_news 的 Finnhub/Yahoo 同源）

# 股票关键词 → 真实美股 ticker（中文名 + 常见写法映射到同一 ticker）。
# 币安 fapi 无美股永续合约 → 已改 Yahoo 美股现货（yfinance）。
_STOCK_SYMBOLS: Dict[str, str] = {
    "NVDA": "NVDA", "NVIDIA": "NVDA", "英伟达": "NVDA", "英伟达公司": "NVDA",
    "TSLA": "TSLA", "TESLA": "TSLA", "特斯拉": "TSLA",
    "AAPL": "AAPL", "APPLE": "AAPL", "苹果": "AAPL", "苹果公司": "AAPL",
    "MSTR": "MSTR", "MICROSTRATEGY": "MSTR", "微策略": "MSTR",
    "MSFT": "MSFT", "MICROSOFT": "MSFT", "微软": "MSFT",
    "AMZN": "AMZN", "AMAZON": "AMZN", "亚马逊": "AMZN",
    "GOOGL": "GOOGL", "GOOG": "GOOGL", "GOOGLE": "GOOGL", "谷歌": "GOOGL", "ALPHABET": "GOOGL",
    "COIN": "COIN", "COINBASE": "COIN",
    "PLTR": "PLTR", "PALANTIR": "PLTR", "帕兰提尔": "PLTR",
    "META": "META", "FACEBOOK": "META", "脸书": "META",
    "SMCI": "SMCI", "SUPER MICRO": "SMCI", "美超微": "SMCI",
    "MARA": "MARA", "MARATHON": "MARA",
    "RIOT": "RIOT", "RIOT PLATFORMS": "RIOT",
}


def _norm_ticker(symbol: str) -> str:
    """规范化美股代码：'NVDAUSDT'→'NVDA'；已是 'NVDA' 原样返回。"""
    s = (symbol or "").strip().upper()
    if s.endswith("USDT"):
        s = s[:-4]
    return s

_cache_lock = threading.Lock()
_price_cache: Dict[str, Dict] = {}  # symbol -> {price, ts}（行情上下文 10 分钟）
_CACHE_TTL_S = 600
_live_cache: Dict[str, Dict] = {}   # symbol -> {price, ts}（交易取价 2 秒）
_LIVE_TTL_S = 2.0

_session = requests.Session()
_session.headers.update({"User-Agent": "Mozilla/5.0"})
# trust_env=True：读 env HTTP_PROXY（fapi 国内需代理）


# ── Polygon.io 前收价（Yahoo 失败兜底）──────────────────────────────────────
_POLYGON_BASE = "https://api.polygon.io"
_POLYGON_TIMEOUT_S = 8.0
_UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
       "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36")


def _polygon_prev_close(symbol: str) -> Optional[float]:
    """Polygon.io 上一交易日收盘价（前收）——Yahoo 拉不到时兜底。

    前收价是延时近似（非盘中实时价），仅在 Yahoo 彻底失败时启用，避免美股
    行情整段缺失。免费额度 5 次/分钟，靠外层 10m/2s 缓存兜住，不会高频烧配额。
    用 urllib 直连（api.polygon.io 国内可直连，不走 env 代理——requests 走代理反会失败）。
    """
    key = getattr(config, "POLYGON_API_KEY", "") or ""
    if not key:
        return None
    try:
        req = urllib.request.Request(
            f"{_POLYGON_BASE}/v2/aggs/ticker/{symbol}/prev?adjusted=true&apiKey={key}",
            headers={"User-Agent": _UA, "Accept": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=_POLYGON_TIMEOUT_S) as resp:
            body = json.loads(resp.read().decode("utf-8", "replace"))
        res = body.get("results") or []
        if res and res[0].get("c") is not None:
            v = float(res[0]["c"])
            if v > 0:
                source_status.report("polygon", True, detail=f"前收 {symbol}={v}")
                return v
    except Exception:
        pass
    source_status.report("polygon", False, error=f"前收 {symbol} 失败")
    return None


def fetch_stock_price(symbol: str) -> Optional[Dict]:
    """拉一只美股当前价（Yahoo Finance 10 分钟缓存；失败 → Polygon 前收兜底）。"""
    symbol = _norm_ticker(symbol)
    if not symbol:
        return None
    with _cache_lock:
        hit = _price_cache.get(symbol)
        if hit and time.time() - hit["ts"] < _CACHE_TTL_S:
            return {"price": hit["price"], "source": hit.get("source", "Yahoo Finance")}

    price: Optional[float] = None
    source = "Yahoo Finance"
    try:
        import yfinance as yf
        p = yf.Ticker(symbol).fast_info.last_price
        if p is not None and float(p) > 0:
            price = float(p)
    except Exception:
        price = None
    if price is None:
        # Yahoo 失败 → Polygon.io 前收价兜底（延时近似，非盘中）
        price = _polygon_prev_close(symbol)
        source = "Polygon.io(前收兜底)"
    if price is None:
        return None
    with _cache_lock:
        _price_cache[symbol] = {"price": price, "ts": time.time(), "source": source}
    return {"price": price, "source": source}


def _fetch_klines(symbol: str, interval: str, limit: int) -> Optional[list]:
    """拉 Yahoo 美股 K 线 → [open, high, low, close, volume] 列表。失败 → None。

    保留成交量：RSI/ADX/VWAP/量能比里的 VWAP 与量能比需要它。
    yfinance 不支持 4h → 用 60m 近似（1 小时 K 线）。
    """
    symbol = _norm_ticker(symbol)
    if not symbol:
        return None
    try:
        import yfinance as yf
        yf_interval = "60m" if interval == "4h" else interval
        period = {"1d": "3mo", "60m": "2mo"}.get(yf_interval, "3mo")
        df = yf.Ticker(symbol).history(period=period, interval=yf_interval, auto_adjust=False)
        if df is None or len(df) < 2:
            return None
        rows = []
        for _, row in df.iterrows():
            o = float(row["Open"]); h = float(row["High"])
            lo = float(row["Low"]); c = float(row["Close"]); v = float(row["Volume"])
            if min(o, h, lo, c) <= 0:
                continue
            rows.append([o, h, lo, c, v])
        return rows[-limit:]
    except Exception:
        return None


def stock_technicals(symbol: str) -> Dict[str, Optional[float]]:
    """股票技术指标：RSI14 / ADX14 / VWAP 位置 / 量能比。

    直接复用 market_snapshot 的算法（_rsi14 / _adx14 / _window_vwap），保证股票与
    加密品种口径一致 —— 八段式「水位博弈」段要求引用 RSI/ADX/VWAP/量能比，
    股票此前一个都没有，模型只能判 NONE。
    样本不足 → 对应字段为 None（不造数），由调用方写 N/A。
    1H × 60 根 ≈ 2.5 天：满足 RSI(≥15根) 与 ADX(≥29根) 的最小样本要求（yfinance 无 4h，用 60m 近似）。
    """
    out: Dict[str, Optional[float]] = {"rsi": None, "adx": None, "vwap_pos": None, "vol_ratio": None}
    symbol = _norm_ticker(symbol)
    if not symbol:
        return out
    candles = _fetch_klines(symbol, "60m", 60)
    if not candles or len(candles) < 15:
        return out
    try:
        from market_snapshot import _rsi14, _adx14, _window_vwap, _volume_ratio
    except Exception:
        return out
    # market_snapshot 的算法按 [ts, o, h, l, c, v] 索引取价：补齐 ts 占位
    ms_candles = [[0.0, c[0], c[1], c[2], c[3], c[4]] for c in candles]
    closes = [c[3] for c in candles]
    try:
        out["rsi"] = _rsi14(closes)
        out["adx"] = _adx14(ms_candles)
        vwap = _window_vwap(ms_candles)
        if vwap and closes:
            out["vwap_pos"] = "上" if closes[-1] >= vwap else "下"
        vr = _volume_ratio(ms_candles)
        if vr is not None:
            out["vol_ratio"] = round(float(vr), 2)
    except Exception:
        return out
    for k in ("rsi", "adx"):
        if out[k] is not None:
            out[k] = round(float(out[k]), 1)
    return out


def fetch_stock_mini_snapshot(symbol: str) -> Optional[Dict]:
    """美股迷你快照：现价 + 7d 区间/趋势/ATR（币安永续 1d K 线，10 分钟缓存）。

    返回 {price, high_7d, low_7d, return_7d_pct, atr_pct, trend} 或 None。
    与 market_snapshot._compute_period_stats 同口径（简化：只算 o/h/l/c）。
    """
    symbol = _norm_ticker(symbol)
    if not symbol:
        return None
    cache_key = f"{symbol}:mini"
    with _cache_lock:
        hit = _mini_cache.get(cache_key)
        if hit and time.time() - hit["ts"] < _CACHE_TTL_S:
            return hit["data"]

    data = fetch_stock_price(symbol)
    if not data:
        return None
    price = data["price"]
    candles = _fetch_klines(symbol, "1d", 8)
    if not candles or len(candles) < 2:
        snap = {"price": price, "high_7d": None, "low_7d": None,
                "return_7d_pct": None, "atr_pct": None, "trend": None}
    else:
        closes = [c[3] for c in candles]
        highs = [c[1] for c in candles]
        lows = [c[2] for c in candles]
        prior = closes[0]
        ret = (closes[-1] - prior) / prior * 100.0 if prior > 0 else 0.0
        trs = []
        for i in range(1, len(candles)):
            h, l, pc = highs[i], lows[i], closes[i - 1]
            trs.append(max(h - l, abs(h - pc), abs(l - pc)))
        atr = sum(trs) / len(trs)
        atr_pct = atr / closes[-1] * 100.0 if closes[-1] > 0 else 0.0
        # 趋势：ret>3% Bull / <-3% Bear / 否则 Ranging（与快照阈值一致）
        trend = "Bull" if ret > 3.0 else ("Bear" if ret < -3.0 else "Ranging")
        snap = {
            "price": price,
            "high_7d": round(max(highs), 2),
            "low_7d": round(min(lows), 2),
            "return_7d_pct": round(ret, 2),
            "atr_pct": round(atr_pct, 3),
            "trend": trend,
        }
    with _cache_lock:
        _mini_cache[cache_key] = {"data": snap, "ts": time.time()}
    return snap


_mini_cache: Dict[str, Dict] = {}  # symbol:mini -> {data, ts}（与现价同 10 分钟 TTL）


def detect_stock_symbol(text: str) -> Optional[str]:
    """从新闻文本识别提到的股票 → 真实美股 ticker（如 AAPL）。

    优先级（这个顺序很重要）：
      ① 新闻源自带的 `[美股 XXX]` 标签 —— 由 stock_news 按抓取时的 ticker 生成，最可靠；
      ② 正文里出现的全部候选，按"首次出现位置最靠前"取名次（并列则取出现次数多的）。
    旧实现是按字典顺序遍历、命中即 break，导致"标题写 AAPL 却因 NVDA 在字典里靠前
    而取到 NVDA 行情"——行情与新闻主体错位会直接误导模型判断主品种。
    """
    if not text:
        return None
    text_s = str(text)
    up = text_s.upper()
    # ① 优先采信 [美股 XXX] / [美股·XXX] 标签
    for m in re.finditer(r"\[美股[·\s]*([A-Za-z]{1,6})\]", text_s):
        sym = _STOCK_SYMBOLS.get(m.group(1).upper())
        if sym:
            return sym
    # ② 收集全部候选，按"首次出现位置"取名次（同位置则次数多者优先）
    cands: Dict[str, Dict[str, Any]] = {}
    for key, symbol in _STOCK_SYMBOLS.items():
        if key.isupper() and len(key) <= 5:
            # ticker 全词匹配（前后非字母数字），避免 "MSTRX"/"SMCIO" 误匹配
            m = re.search(rf"(?<![A-Z0-9]){re.escape(key)}(?![A-Z0-9])", up)
            if not m:
                continue
            pos, cnt = m.start(), len(re.findall(rf"(?<![A-Z0-9]){re.escape(key)}(?![A-Z0-9])", up))
        else:
            pos = text_s.find(key)
            if pos < 0:
                pos = up.find(key.upper())
            if pos < 0:
                continue
            cnt = text_s.count(key)
        prev = cands.get(symbol)
        if prev is None or pos < prev["pos"] or (pos == prev["pos"] and cnt > prev["cnt"]):
            cands[symbol] = {"pos": pos, "cnt": cnt}
    if not cands:
        return None
    # 位置最靠前优先；并列时出现次数多者优先；再并列按字母序保证稳定
    best = min(cands.items(), key=lambda kv: (kv[1]["pos"], -kv[1]["cnt"], kv[0]))
    return best[0]


def stock_context_line(text: str) -> str:
    """若新闻提到股票 → 返回行情上下文（现价 + 7d 区间/趋势 + 技术指标）；否则空串。

    技术指标（RSI/ADX/VWAP 位置/量能比）与加密品种同口径 —— 八段式「水位博弈」段
    要求引用这些真实指标且禁止凭空判断，此前股票缺这几项、模型只能判 NONE。
    """
    symbol = detect_stock_symbol(text)
    if not symbol:
        return ""
    snap = fetch_stock_mini_snapshot(symbol)
    if not snap:
        return ""
    ticker = _norm_ticker(symbol)
    parts = [f"[美股行情] {ticker} ${snap['price']:,.2f}"]
    if snap.get("high_7d"):
        parts.append(
            f"7d {snap.get('return_7d_pct'):+.2f}% | 区间 {snap.get('low_7d'):,.2f}–{snap.get('high_7d'):,.2f}"
            f" | 趋势 {snap.get('trend')} | ATR {snap.get('atr_pct'):.2f}%"
        )
    # 技术指标（1H 窗口，与加密快照同口径）；样本不足的项写 N/A，不造数
    try:
        tech = stock_technicals(symbol)
    except Exception:
        tech = {}
    if tech:
        bits = []
        bits.append(f"RSI{tech['rsi']}" if tech.get("rsi") is not None else "RSI N/A")
        bits.append(f"ADX{tech['adx']}" if tech.get("adx") is not None else "ADX N/A")
        bits.append(f"VWAP{tech['vwap_pos']}" if tech.get("vwap_pos") else "VWAP N/A")
        bits.append(f"量{tech['vol_ratio']}x" if tech.get("vol_ratio") is not None else "量 N/A")
        parts.append("技术(1H): " + " ".join(bits))
    parts.append("（来源:Yahoo Finance·10分钟缓存）")
    return " ".join(parts)


# 供模拟盘统一取价（美股资产建仓/结算价，与行情/K线同一口径）
_last_fail_log: Dict[str, float] = {}  # symbol -> 上次打降级日志时间（节流）


def current_price_for_symbol(symbol: str) -> Optional[float]:
    """交易实时价：2s TTL（美股建仓/结算/止损用，像 BTC 的近实时取价）。"""
    symbol = _norm_ticker(symbol)
    if not symbol:
        return None
    with _cache_lock:
        hit = _live_cache.get(symbol)
        if hit and time.time() - hit["ts"] < _LIVE_TTL_S:
            return hit["price"]
    price: Optional[float] = None
    try:
        import yfinance as yf
        p = yf.Ticker(symbol).fast_info.last_price
        if p is not None and float(p) > 0:
            price = float(p)
    except Exception:
        price = None
    if price is None:
        # Yahoo 失败 → Polygon.io 前收价兜底（延时近似价；优先给价而非跳过结算）
        price = _polygon_prev_close(symbol)
    if price is not None:
        with _cache_lock:
            _live_cache[symbol] = {"price": price, "ts": time.time()}
    else:
        # 降级告警（节流 60s）：Yahoo 与 Polygon 均失败 = 建仓/结算被跳过
        now = time.time()
        if now - _last_fail_log.get(symbol, 0.0) > 60.0:
            _last_fail_log[symbol] = now
            print(
                f"  [PRICE] ⚠️ Yahoo+Polygon {symbol} 均取价失败——"
                f"相关建仓/结算将被跳过",
                flush=True,
            )
    return price


def asset_price(asset_or_ticker: str) -> Optional[float]:
    """按资产/股票代码取价：入参可能是 'NVDA'/'NVDAUSDT'/'英伟达'。"""
    s = (asset_or_ticker or "").strip().upper()
    symbol = _STOCK_SYMBOLS.get(s) or _norm_ticker(s)
    if not symbol:
        return None
    return current_price_for_symbol(symbol)
